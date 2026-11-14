import copy
import json
import os
import sys
import tempfile
import io
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import parse_html, validate_request, classify, optimize, validate_extraction, listing_url
from providers import verify_policy, execute_policy, redact
from pokescout import full_run, Run

URL='https://jp.mercari.com/item/m86771047650'
def job(mode='collector'):
    return validate_request({'mode':mode,'budget_jpy':10000,'reserve_jpy':1000,'near_mint_only':True,
        'targets':[{'name_ja':'ピカチュウ','number':'173/165','rarity':'AR'}]})

def html(state='SoldOut',price=2222):
    p={'@type':'Product','name':'ピカチュウ AR 173/165','description':'単品です',
       'offers':{'url':URL,'availability':'https://schema.org/'+state,'price':price,'priceCurrency':'JPY'}}
    return '<script type="application/ld+json">'+json.dumps(p)+'</script><h3>商品の状態</h3><p>未使用に近い</p><h3>配送料の負担</h3><p>送料込み(出品者負担)</p><h3>この商品を見ている人におすすめ</h3><p>¥100 別のカード</p>'

def candidate(url,target,cost,net=None):
    return {'url':url,'target':target,'total_jpy':cost,'eligible':True,'scenario_net_jpy':net}

class EvidenceTests(unittest.TestCase):
    def test_sold_is_not_a_buy_recommendation(self):
        r=classify(parse_html(html(),URL),job())
        self.assertEqual(r['price'],2222)
        self.assertEqual(r['availability'],'sold')
        self.assertFalse(r['eligible'])
        self.assertEqual(optimize([r],job())['urls'],[])

    def test_primary_price_shipping_and_exact_match(self):
        r=classify(parse_html(html('InStock'),URL),job())
        self.assertTrue(r['eligible'])
        self.assertEqual(r['total_jpy'],2222)
        self.assertEqual(r['condition'],'未使用に近い')
        self.assertEqual(r['break_even_jpy'],2747)

    def test_wrong_rarity_and_graded_bundle_excluded(self):
        for title in ['ピカチュウ SAR 173/165','ピカチュウ AR 173/165 PSA10','ピカチュウ AR 173/165 2枚','ピカチュウ AR 173/165 10枚']:
            r=parse_html(html('InStock'),URL);r['title']=title
            self.assertFalse(classify(r,job())['eligible'])

    def test_unknown_shipping_not_assumed_free(self):
        r=parse_html(html('InStock').replace('送料込み(出品者負担)','着払い(購入者負担)'),URL)
        self.assertIsNone(r['shipping_jpy'])
        self.assertFalse(classify(r,job())['eligible'])

    def test_llm_values_require_exact_evidence(self):
        d={'title':'invented','price':100,'availability':'available','condition':'未使用に近い','evidence':{}}
        r=validate_extraction(d,'unrelated text',URL)
        self.assertIsNone(r['price']);self.assertEqual(r['availability'],'unknown');self.assertEqual(r['title'],'')

    def test_numeric_quote_cannot_justify_different_price(self):
        d={'price':100,'evidence':{'price':'¥4,000'}}
        self.assertIsNone(validate_extraction(d,'¥4,000',URL)['price'])

    def test_disallowed_urls_and_credentials_rejected(self):
        for url in ['http://127.0.0.1/','https://jp.mercari.com/mypage/','https://user:pass@jp.mercari.com/item/m123']:
            with self.assertRaises(ValueError):listing_url(url)

    def test_no_investment_valuation_is_invented(self):
        r=classify(parse_html(html('InStock'),URL),job('investor'))
        self.assertIsNone(r['scenario_net_jpy'])
        self.assertEqual(optimize([r],job('investor'))['urls'],[])

    def test_collector_completion_and_unique_printings(self):
        rows=[candidate('A','one',8000),candidate('B','two',4000),candidate('C','three',3000),candidate('D','two',4500)]
        p=optimize(rows,job())
        self.assertEqual(p['urls'],['B','C']);self.assertEqual(p['cost_jpy'],7000)

    def test_investor_optimization_beats_greedy_roi(self):
        rows=[candidate('A','one',6000,3000),candidate('B','two',4500,2400),candidate('C','three',4500,2400)]
        p=optimize(rows,job('investor'))
        self.assertEqual(p['urls'],['B','C']);self.assertEqual(p['cost_jpy'],9000)

    def test_generated_policy_cannot_override_budget_or_eligibility(self):
        rows=[candidate('A','one',10001),candidate('B','two',4000)]
        with self.assertRaises(ValueError):verify_policy({'urls':['A']},rows,job())
        with self.assertRaises(ValueError):verify_policy({'urls':['B','B']},rows,job())
        self.assertEqual(verify_policy({'urls':['B']},rows,job())['cost_jpy'],4000)

    def test_secrets_removed_from_results(self):
        with patch.dict(os.environ,{'NOSANA_API_KEY':'TEST-SECRET-NOT-A-KEY'}):
            self.assertEqual(redact({'x':'TEST-SECRET-NOT-A-KEY'}),{'x':'[REDACTED]'})

    def test_sandbox_failure_still_requests_cleanup(self):
        calls=[]
        def mock_api(url,key,body=None,method=None,timeout=35):
            calls.append((url,method))
            if method=='DELETE':return {}
            if url.endswith('/process/code-run'):return {'exitCode':1,'result':''}
            return {'id':'test-sandbox','state':'started','toolboxProxyUrl':'https://proxy.app.daytona.io/toolbox'}
        with patch.dict(os.environ,{'DAYTONA_API_KEY':'test','NOSANA_API_KEY':'test','DAYTONA_CODEAF_SNAPSHOT':'test'}),patch('providers.api',mock_api),tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):execute_policy([],job(),'model',lambda *a,**k:None,Path(d))
        self.assertEqual(calls[-1],('https://app.daytona.io/api/sandbox/test-sandbox','DELETE'))

    def test_ambiguous_booleans_and_card_number_substrings_rejected(self):
        bad=job();bad['near_mint_only']='false'
        with self.assertRaises(ValueError):validate_request(bad)
        r=parse_html(html('InStock'),URL);r['title']='ピカチュウ AR 1173/165'
        self.assertFalse(classify(r,job())['eligible'])

    def test_damage_evidence_survives_replanning(self):
        r=parse_html(html('InStock'),URL);r['description']='プレイ用'
        relaxed=job();relaxed['near_mint_only']=False
        saved=classify(r,relaxed)
        self.assertTrue(saved['eligible'])
        self.assertFalse(classify(saved,job())['eligible'])

    def test_missing_configuration_stops_before_network(self):
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{},clear=True),patch('providers.api') as network,redirect_stdout(io.StringIO()):
            args=Namespace(out=d,goal='',request=None)
            self.assertEqual(full_run(args),2)
            network.assert_not_called()
            report=json.loads((Path(d)/'report.json').read_text(encoding='utf-8'))
            self.assertTrue(report['status'].startswith('blocked'))
            self.assertTrue(all(v=='not called' for v in report['sponsors'].values()))

    def test_cleanup_failure_is_visible_in_report(self):
        with tempfile.TemporaryDirectory() as d,redirect_stdout(io.StringIO()):
            run=Run(d,'test')
            run.emit('Daytona','sandbox.cleanup-unconfirmed',sandbox_id='test-sandbox')
            self.assertIn('test-sandbox',run.errors[0])

    def test_full_provider_contract_with_mock_responses(self):
        # Mocked contract test, not evidence of real sponsor execution.
        page='ピカチュウ AR 173/165\n¥2,222\n購入手続きへ\n未使用に近い\n送料込み'
        extraction={'price':2222,'availability':'available','condition':'未使用に近い',
                    'evidence':{'title':'ピカチュウ AR 173/165','price':'¥2,222','availability':'購入手続きへ','condition':'未使用に近い','shipping':'送料込み'}}
        calls=[]
        def fake_api(url,key,body=None,method=None,timeout=35):
            calls.append((url,method))
            if method=='DELETE':return {}
            if url.endswith('/models'):return {'data':[{'id':'test-tool-model','available':True}]}
            if url.endswith('/search'):return {'results':[{'url':URL}]}
            if url.endswith('/extract'):return {'results':[{'url':URL,'raw_content':page}]}
            if url.endswith('/chat/completions'):
                answer=json.dumps(extraction,ensure_ascii=False) if 'Extract ONE' in body['messages'][0]['content'] else 'One eligible observed listing. Verify it before purchasing.'
                return {'choices':[{'message':{'content':answer}}]}
            if url.endswith('/sandbox'):
                return {'id':'test-sandbox','state':'started','toolboxProxyUrl':'https://proxy.app.daytona.io/toolbox'}
            if url.endswith('/process/code-run'):
                compile(body['code'],'sandbox-task','exec')
                self.assertEqual(body['envs']['CODEAF_BASE_URL'],'https://inference.nosana.com/v1')
                self.assertNotIn('DAYTONA_API_KEY',body['envs'])
                return {'exitCode':0,'result':json.dumps({'answer':{'urls':[URL]},'code':'# mocked generation\n','codeaf':{'ok':True,'stop':'done'}})}
            raise AssertionError('Unexpected endpoint '+url)
        env={k:'mock-only' for k in ('NOSANA_API_KEY','TAVILY_API_KEY','DAYTONA_API_KEY','DAYTONA_CODEAF_SNAPSHOT')}
        env['NOSANA_MODEL']='test-tool-model'
        with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,env,clear=True),patch('providers.api',fake_api),redirect_stdout(io.StringIO()):
            root=Path(d);(root/'request.json').write_text(json.dumps(job()))
            args=Namespace(out=str(root/'run'),goal='',request=str(root/'request.json'),budget=None,mode='collector',rounds=2,max_pages=4)
            self.assertEqual(full_run(args),0)
            report=json.loads((root/'run/report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['plan']['cost_jpy'],2222)
            self.assertEqual(report['plan']['urls'],[URL])
            self.assertEqual(report['sponsors']['Nosana'],'verified response in this run')
            self.assertEqual(report['sponsors']['Tavily'],'verified response in this run')
            self.assertEqual(report['sponsors']['CodeAF'],'not called')
            self.assertEqual(report['sponsors']['Daytona'],'not called')
            self.assertNotIn('mock-only',(root/'run/report.json').read_text(encoding='utf-8'))
            self.assertFalse((root/'run/generated-analysis.py').exists())
            self.assertEqual(report['plan']['engine'],'deterministic-python-v0.2')
        self.assertFalse(any('daytona.io' in u for u,_ in calls))

if __name__=='__main__':unittest.main()
