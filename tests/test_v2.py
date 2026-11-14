import copy
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cards import validate_card, compare_psa, money, identify
from collection import Collection
from decisions import plan
from selling import draft
from fixture_market import TestMarket
from providers import Nosana
from daytona_browser import execute_fixture

ROOT=Path(__file__).resolve().parents[1]
def fixture(name):return json.loads((ROOT/'examples/v2'/name).read_text(encoding='utf-8'))

class NewWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Collection(Path(self.temp.name)/'c.sqlite3')
        self.goal=fixture('goal.json');self.rows=fixture('listings.json');self.card=fixture('owned-card.json')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def test_exact_fixture_plan(self):
        result=plan(self.goal,self.rows)
        self.assertEqual(result['selected_ids'],['A2','B1']);self.assertEqual(result['cost_jpy'],8900)
        self.assertEqual(result['remaining_allowance_jpy'],1100)
    def test_price_change_replans(self):
        self.rows[1]['price_jpy']=3800
        self.assertEqual(plan(self.goal,self.rows)['selected_ids'],['A1','B1'])
    def test_unknown_shipping_and_sold_rejected(self):
        rows={r['id']:r for r in plan(self.goal,self.rows)['checked']}
        self.assertIn('ACQUISITION_TOTAL_UNKNOWN',rows['B2']['reasons'])
        self.assertIn('NOT_AVAILABLE',rows['B3']['reasons'])
    def test_wrong_variant_rejected(self):
        self.assertFalse(plan(self.goal,self.rows)['checked'][2]['eligible'])
    def test_owned_target_excluded(self):
        self.db.add(self.goal['targets'][0]['card'])
        self.assertEqual(plan(self.goal,self.rows,self.db.list())['selected_ids'],['B1'])
    def test_multiple_quantity_same_printing(self):
        self.goal['targets'][0]['quantity']=2;self.goal['budget_jpy']=15000
        self.assertEqual(plan(self.goal,self.rows)['cost_jpy'],12900)
    def test_reservations_reduce_budget(self):
        self.goal['active_reservations_jpy']=500
        self.assertEqual(plan(self.goal,self.rows)['selected_ids'],['A2'])
    def test_hard_limit_types(self):
        for field,value in [('budget_jpy',True),('reserve_jpy',-1),('allow_defects','false'),('allowed_conditions','near_mint')]:
            goal=copy.deepcopy(self.goal);goal[field]=value
            with self.assertRaises(ValueError):plan(goal,self.rows)
    def test_inventory_survives_restart(self):
        owned=self.db.add(self.card,cost=1200,allow_sale=True)
        second=Collection(Path(self.temp.name)/'c.sqlite3')
        try:self.assertEqual(second.get(owned['id'])['cost'],1200)
        finally:second.close()
    def test_duplicate_image_blocked(self):
        self.db.add(self.card,'a'*64)
        with self.assertRaises(ValueError):self.db.add(self.card,'a'*64)
    def test_raw_grade_blocked(self):
        card=dict(self.card,grade='10')
        with self.assertRaises(ValueError):validate_card(card,True)
    def test_uncertain_identity_not_confirmed(self):
        card=dict(self.card,uncertainties=['number unreadable'])
        with self.assertRaises(ValueError):self.db.add(card)
    def test_psa_preserves_leading_zero_and_match_is_not_authentication(self):
        card=fixture('psa-card.json');self.assertEqual(validate_card(card)['cert_number'],'00000000')
        result=compare_psa(card,fixture('psa-registry-fixture.json'))
        self.assertEqual(result['status'],'registry_match');self.assertEqual(result['physical_authenticity'],'not established')
    def test_psa_mismatch_and_incomplete(self):
        reg=fixture('psa-registry-fixture.json');reg['grade']='9'
        self.assertEqual(compare_psa(fixture('psa-card.json'),reg)['status'],'registry_mismatch')
        del reg['grade']
        self.assertEqual(compare_psa(fixture('psa-card.json'),reg)['status'],'incomplete')
    def test_slab_cohort_never_matches_raw(self):
        self.rows[0]['card']=fixture('psa-card.json')
        self.assertIn('WRONG_GRADING_COHORT',plan(self.goal,self.rows)['checked'][0]['reasons'])
    def test_selling_keeps_defects(self):
        owned=self.db.add(self.card,allow_sale=True)
        result=draft(owned,1800)
        self.assertIn(self.card['defects'][0],result['description']);self.assertFalse(result['publish_ready'])
    def test_protected_copy_cannot_be_sold(self):
        owned=self.db.add(self.card)
        with self.assertRaises(ValueError):draft(owned,1800)
    def test_untrusted_model_suggestion_does_not_replace_facts(self):
        class Model:
            def complete(self,*args):return {'title':'Perfect!','description':'No damage'}
        result=draft(self.db.add(self.card,allow_sale=True),1800,Model())
        self.assertIn(self.card['defects'][0],result['description'])
        self.assertFalse(result['publish_ready']);self.assertNotIn('No damage',result['description'])
    def test_reservation_duplicate_and_budget(self):
        self.db.reserve_fixture('i1','A',4000,10000,1000)
        with self.assertRaises(ValueError):self.db.reserve_fixture('i2','A',4000,10000,1000)
        with self.assertRaises(ValueError):self.db.reserve_fixture('i3','B',6000,10000,1000)
        self.assertEqual(len(self.db.actions()),1)
    def test_unknown_action_stays_reserved(self):
        self.db.reserve_fixture('i1','A',4000,10000,1000);self.db.unknown('i1')
        with self.assertRaises(ValueError):self.db.reserve_fixture('i2','B',6000,10000,1000)
        self.db.confirm_fixture('i1','TEST-1');self.db.confirm_fixture('i1','TEST-1')
        self.assertEqual(len(self.db.actions()),1)
    def test_market_idempotency_and_price_check(self):
        market=TestMarket(self.rows,Path(self.temp.name)/'m.sqlite3')
        try:
            payload={'intent':'i1','listing':'A2','ceiling':3900}
            self.assertEqual(market.purchase(payload),market.purchase(payload))
            self.assertEqual(market.lookup('i1')['total'],3900)
            with self.assertRaises(ValueError):market.purchase({'intent':'i2','listing':'B1','ceiling':4900})
        finally:market.server.server_close()
    def test_gemini_missing_key_fails_before_reading_images(self):
        with patch.dict(os.environ,{},clear=True):
            with self.assertRaisesRegex(ValueError,'GEMINI_API_KEY'):identify(['nonexistent.png'])
    def test_market_real_http_roundtrip(self):
        with TestMarket(self.rows,Path(self.temp.name)/'http.sqlite3') as market:
            body=json.dumps({'intent':'http-1','listing':'A2','ceiling':3900}).encode()
            request=urllib.request.Request(market.base+'/purchase',data=body,headers={'Content-Type':'application/json'})
            with urllib.request.urlopen(request) as response:order=json.load(response)
            with urllib.request.urlopen(market.base+'/order/http-1') as response:verified=json.load(response)
            self.assertEqual(order,verified);self.assertTrue(verified['receipt'].startswith('TEST-'))
    def test_requested_nosana_model_not_silently_replaced(self):
        with patch.dict(os.environ,{'NOSANA_API_KEY':'fixture'},clear=True),patch('providers.api',return_value={'data':[{'id':'other'}]}):
            with self.assertRaises(RuntimeError):Nosana(lambda *a,**k:None).choose_model()
    def test_daytona_cleanup_on_worker_failure(self):
        calls=[]
        def fake(url,key,body=None,method=None,timeout=35):
            calls.append((url,method))
            if method=='DELETE':return {}
            if url.endswith('code-run'):return {'exitCode':1}
            return {'id':'box-1','state':'started','toolboxProxyUrl':'https://proxy.app.daytona.io/toolbox'}
        with patch.dict(os.environ,{'DAYTONA_API_KEY':'fixture','DAYTONA_BROWSER_SNAPSHOT':'fixture'}),patch('daytona_browser.api',fake):
            with self.assertRaises(RuntimeError):execute_fixture(self.goal,self.rows,['A2'])
        self.assertEqual(calls[-1],('https://app.daytona.io/api/sandbox/box-1','DELETE'))

if __name__=='__main__':unittest.main()
