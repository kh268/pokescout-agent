"""Run the actual Playwright fixture worker inside Daytona; credentials required.

Cloud contract is implemented but must be smoke-tested with the user's account.
"""
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from providers import api,need

def execute_fixture(goal,listings,selected_ids):
    key=need('DAYTONA_API_KEY');snapshot=need('DAYTONA_BROWSER_SNAPSHOT')
    sandbox_id=None;events=[]
    try:
        box=api('https://app.daytona.io/api/sandbox',key,{
            'snapshot':snapshot,'public':False,'autoStopInterval':5,'ttlMinutes':10,
            'labels':{'app':'pokescout-agent','purpose':'playwright-fixture'}})
        sandbox_id=box.get('id')
        if not isinstance(sandbox_id,str) or not re.fullmatch(r'[\w-]+',sandbox_id):raise RuntimeError('Invalid Daytona sandbox ID.')
        events.append({'event':'sandbox_created','id':sandbox_id})
        for _ in range(25):
            if box.get('state')=='started':break
            if box.get('state') in ('error','destroyed','destroying'):raise RuntimeError('Sandbox failed to start.')
            time.sleep(.6);box=api('https://app.daytona.io/api/sandbox/'+sandbox_id,key)
        if box.get('state')!='started':raise RuntimeError('Sandbox startup timed out.')
        proxy=box.get('toolboxProxyUrl') or 'https://proxy.app.daytona.io/toolbox'
        url=urlsplit(proxy)
        if url.scheme!='https' or url.username or url.password or url.port not in (None,443) or not (url.hostname or '').endswith('.daytona.io'):
            raise RuntimeError('Unexpected toolbox URL; credentials not forwarded.')
        root=Path(__file__).parent
        files={name:(root/name).read_text() for name in ('cards.py','collection.py','core.py','fixture_market.py')}
        payload={'goal':goal,'listings':listings,'selected':selected_ids}
        code='FILES='+repr(files)+'\nPAYLOAD='+repr(payload)+'''\nimport json,pathlib,sys,tempfile
root=pathlib.Path(tempfile.mkdtemp(prefix='pokescout-fixture-'))
for name,body in FILES.items():(root/name).write_text(body)
sys.path.insert(0,str(root))
from fixture_market import run_browser_fixture
result=run_browser_fixture(PAYLOAD['goal'],PAYLOAD['listings'],PAYLOAD['selected'],root/'out')
print(json.dumps(result))
'''
        result=api(proxy.rstrip('/')+'/'+sandbox_id+'/process/code-run',key,
            {'language':'python','code':code,'timeout':90},timeout=110)
        if result.get('exitCode')!=0:raise RuntimeError('Daytona browser worker failed; check browser snapshot and trace in Daytona.')
        value=json.loads(str(result.get('result','')).strip())
        if value.get('mode')!='synthetic_fixture' or len(value.get('orders',[]))!=len(selected_ids):
            raise RuntimeError('Unexpected browser result.')
        events.append({'event':'browser_worker_verified','sandbox':sandbox_id})
        value['daytona_events']=events
        return value
    finally:
        if sandbox_id:
            try:
                api('https://app.daytona.io/api/sandbox/'+sandbox_id,key,method='DELETE',timeout=20)
                events.append({'event':'sandbox_deletion_requested','id':sandbox_id})
            except Exception:events.append({'event':'sandbox_cleanup_unconfirmed','id':sandbox_id})
