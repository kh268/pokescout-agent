"""v0.2 collection, image intake, structured planning and repeatable fixture demo."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from cards import compare_psa
from image_agent import recognize_card
from collection import Collection
from decisions import plan
from selling import draft

COMMANDS={'identify','add','collection','psa-compare','plan','sell','demo','browser-demo'}
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def output(value,path=None):
    text=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    if path:
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
        with p.open('x', encoding='utf-8') as f:f.write(text)
    print(text)
def fresh_directory(path):
    p=Path(path)
    if p.exists() and any(p.iterdir()):raise ValueError('Output directory must be new or empty.')
    p.mkdir(parents=True,exist_ok=True);return p
def main(argv):
    parser=argparse.ArgumentParser(description='PokéScout v0.2 collection and test execution')
    sub=parser.add_subparsers(dest='cmd',required=True)
    p=sub.add_parser('identify');p.add_argument('images',nargs='+');p.add_argument('--out',required=True);p.add_argument('--model',help='Explicit Gemini model; overrides GEMINI_MODEL')
    p=sub.add_parser('add');p.add_argument('--record',required=True);p.add_argument('--confirm-owned',action='store_true',required=True)
    p.add_argument('--allow-sale',action='store_true');p.add_argument('--cost-jpy',type=int)
    p.add_argument('--db',default='data/collection.sqlite3')
    p=sub.add_parser('collection');p.add_argument('--db',default='data/collection.sqlite3')
    p=sub.add_parser('psa-compare');p.add_argument('--record',required=True);p.add_argument('--registry-record',required=True)
    p=sub.add_parser('plan');p.add_argument('--goal',required=True);p.add_argument('--listings',required=True)
    p.add_argument('--db',default='data/collection.sqlite3');p.add_argument('--out')
    p=sub.add_parser('sell');p.add_argument('--copy',required=True);p.add_argument('--price-jpy',type=int,required=True)
    p.add_argument('--db',default='data/collection.sqlite3');p.add_argument('--nosana',action='store_true');p.add_argument('--out')
    p=sub.add_parser('demo');p.add_argument('--out',required=True);p.add_argument('--browser',action='store_true')
    p.add_argument('--simulate-lost-response',action='store_true')
    p=sub.add_parser('browser-demo');p.add_argument('--out',required=True);p.add_argument('--daytona',action='store_true')
    args=parser.parse_args(argv)
    if args.cmd=='identify':
        result=recognize_card(args.images,model=args.model)
        output(result,args.out)
        return 0 if result['ok'] else 2
    if args.cmd=='psa-compare':
        v=load(args.record);output(compare_psa(v.get('card',v),load(args.registry_record)));return 0
    if args.cmd in ('demo','browser-demo'):return demo(args)
    db=Collection(args.db)
    try:
        if args.cmd=='add':
            v=load(args.record);hashes=v.get('image_sha256',[])
            output(db.add(v.get('card',v),hashes[0] if hashes else None,args.cost_jpy,args.allow_sale))
        elif args.cmd=='collection':output(db.list())
        elif args.cmd=='plan':output(plan(load(args.goal),load(args.listings),db.list()),args.out)
        elif args.cmd=='sell':
            model=None
            if args.nosana:
                from providers import Nosana
                model=Nosana(lambda *a,**kw:print(json.dumps({'provider':a[0],'event':a[1],**kw}),file=sys.stderr))
            output(draft(db.get(args.copy),args.price_jpy,model),args.out)
        return 0
    finally:db.close()

def demo(args):
    root=Path(__file__).parent;fixtures=root/'examples'/'v2';out=fresh_directory(args.out)
    goal=load(fixtures/'goal.json');listings=load(fixtures/'listings.json')
    db=Collection(out/'collection.sqlite3')
    try:
        owned=db.add(load(fixtures/'owned-card.json'),cost=1200,allow_sale=True)
        result=plan(goal,listings,db.list());sale=draft(owned,1800)
        report={'version':'0.2.0','mode':'synthetic_fixture','identification':'fixture JSON; Gemini not called',
                'collection':db.list(),'plan':result,'sale_draft':sale,
                'providers':{k:'not called' for k in ('Gemini','Tavily','Nosana','CodeAF','Daytona')},
                'live_marketplace_transactions':0}
        report['status']='complete'
        try:
            if args.cmd=='browser-demo' and args.daytona:
                from daytona_browser import execute_fixture
                report['providers']['Daytona']='attempted; see outcome'
                report['browser']=execute_fixture(goal,listings,result['selected_ids'])
                report['providers']['Daytona']='real cloud response; synthetic marketplace transaction'
            elif args.cmd=='browser-demo' or args.browser:
                from fixture_market import run_browser_fixture
                report['browser']=run_browser_fixture(goal,listings,result['selected_ids'],out/'browser',getattr(args,'simulate_lost_response',False))
        except Exception as error:
            from providers import redact
            report['status']='partial; browser step failed'
            report['browser']={'status':'failed','error':redact(str(error)),
                               'note':'No successful browser execution is claimed. Inspect any persistent fixture action ledger before retry.'}
        output(report,out/'report.json');return 0 if report['status']=='complete' else 2
    finally:db.close()
