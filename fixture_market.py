"""Team-owned loopback test marketplace and real Playwright execution.

All orders are synthetic. This module contains no Mercari buy/listing adapter.
"""
from __future__ import annotations
import hashlib
import html
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from collection import Collection

class TestMarket:
    def __init__(self,listings,path):
        self.listings={r['id']:r for r in listings};self.path=str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS orders(intent TEXT PRIMARY KEY, listing TEXT UNIQUE, total INTEGER, receipt TEXT UNIQUE)')
        market=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reply(self,data,status=200,mime='application/json'):
                raw=(json.dumps(data,ensure_ascii=False) if mime=='application/json' else data).encode()
                self.send_response(status);self.send_header('Content-Type',mime+'; charset=utf-8')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_GET(self):
                path=urlparse(self.path).path
                if path.startswith('/offer/'):
                    row=market.listings.get(path.removeprefix('/offer/'))
                    if not row:return self.reply({'error':'not found'},404)
                    content=html.escape(json.dumps(row,ensure_ascii=False))
                    page='''<!doctype html><html lang="en"><title>PokéScout TEST marketplace</title>
                    <body><h1>TEST MARKETPLACE — no real purchase</h1><pre id="offer">'''+content+'''</pre>
                    <label>Intent ID <input id="intent"></label><label>Maximum total <input id="ceiling" type="number"></label>
                    <button onclick="buy()">Place test order</button><pre id="result"></pre>
                    <script>async function buy(){let o=JSON.parse(document.getElementById('offer').textContent);
                    let r=await fetch('/purchase',{method:'POST',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({listing:o.id,intent:document.getElementById('intent').value,
                    ceiling:Number(document.getElementById('ceiling').value)})});
                    document.getElementById('result').textContent=JSON.stringify(await r.json());}</script></body></html>'''
                    return self.reply(page,mime='text/html')
                if path.startswith('/order/'):
                    value=market.lookup(path.removeprefix('/order/'))
                    return self.reply(value or {'error':'not found'},200 if value else 404)
                return self.reply({'error':'not found'},404)
            def do_POST(self):
                if self.path!='/purchase':return self.reply({'error':'unsupported'},404)
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<4096:raise ValueError('Bad request size.')
                    body=json.loads(self.rfile.read(length));result=market.purchase(body)
                    return self.reply(result)
                except (ValueError,KeyError,sqlite3.IntegrityError) as e:
                    return self.reply({'error':str(e)},409)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def __enter__(self):self.thread.start();return self
    def __exit__(self,*args):self.server.shutdown();self.server.server_close();self.thread.join()
    def lookup(self,intent):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory=sqlite3.Row
            row=db.execute('SELECT * FROM orders WHERE intent=?',(intent,)).fetchone()
            return dict(row) if row else None
    def purchase(self,body):
        intent=body['intent'];lid=body['listing'];ceiling=body['ceiling']
        if not isinstance(intent,str) or not 1<=len(intent)<=100 or type(ceiling) is not int:raise ValueError('Invalid intent/ceiling.')
        row=self.listings[lid]
        if row.get('availability')!='available':raise ValueError('No longer available.')
        values=[row.get(k) for k in ('price_jpy','shipping_jpy','purchase_fee_jpy')]
        if any(type(v) is not int or v<0 for v in values):raise ValueError('Unknown or invalid total.')
        total=sum(values)
        if total>ceiling:raise ValueError('Price exceeds authorized ceiling.')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT listing,total,receipt FROM orders WHERE intent=?',(intent,)).fetchone()
            if old:
                if old[0]!=lid or old[1]>ceiling:raise ValueError('Intent payload conflict.')
                receipt=old[2]
            else:
                receipt='TEST-'+uuid.uuid4().hex[:14]
                db.execute('INSERT INTO orders VALUES(?,?,?,?)',(intent,lid,total,receipt))
        return {'intent':intent,'listing':lid,'total':total,'receipt':receipt}

def run_browser_fixture(goal,listings,selected_ids,out,simulate_lost_response=False):
    from playwright.sync_api import sync_playwright
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    ledger=Collection(out/'fixture-ledger.sqlite3');events=[];results=[]
    by_id={r['id']:r for r in listings}
    try:
        with TestMarket(listings,out/'test-market.sqlite3') as market,sync_playwright() as p:
            options={'headless':True}
            if os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE'):
                options['executable_path']=os.environ['PLAYWRIGHT_CHROMIUM_EXECUTABLE']
            browser=p.chromium.launch(**options)
            try:
                page=browser.new_page();page.set_default_timeout(10000)
                for lid in selected_ids:
                    expected=by_id[lid];total=sum(expected[k] for k in ('price_jpy','shipping_jpy','purchase_fee_jpy'))
                    intent='fixture-'+uuid.uuid4().hex
                    ledger.reserve_fixture(intent,lid,total,goal['budget_jpy']-goal.get('active_reservations_jpy',0),goal.get('reserve_jpy',0))
                    try:
                        page.goto(market.base+'/offer/'+lid)
                        fresh=json.loads(page.locator('#offer').inner_text())
                        if fresh!=expected or fresh['availability']!='available':raise ValueError('Preflight changed; no submission.')
                        events.append({'event':'preflight_passed','listing':lid,'total_jpy':total})
                        page.locator('#intent').fill(intent);page.locator('#ceiling').fill(str(total))
                        page.get_by_role('button',name='Place test order').click()
                        page.wait_for_function("document.getElementById('result').textContent.length > 0")
                        response=json.loads(page.locator('#result').inner_text())
                        if response.get('error'):raise ValueError(response['error'])
                        if simulate_lost_response:
                            ledger.unknown(intent);events.append({'event':'simulated_lost_response','intent':intent})
                        # Verify on a separate GET, not merely the click result.
                        page.goto(market.base+'/order/'+intent)
                        verified=json.loads(page.locator('body').inner_text())
                        if verified.get('listing')!=lid or verified.get('total')!=total or not verified.get('receipt','').startswith('TEST-'):
                            raise ValueError('Order verification failed.')
                        ledger.confirm_fixture(intent,verified['receipt']);results.append(verified)
                        events.append({'event':'test_order_verified','intent':intent,'receipt':verified['receipt']})
                    except Exception:
                        ledger.unknown(intent);raise
            finally:browser.close()
        result={'mode':'synthetic_fixture','browser':'Playwright Chromium','orders':results,
                'events':events,'ledger':ledger.actions(),'live_marketplace_transactions':0}
        (out/'browser-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        return result
    finally:ledger.close()
