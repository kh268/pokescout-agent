"""Evidence handling and independently checkable purchasing math. No model calls."""
from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


def now():
    return datetime.now(timezone.utc).isoformat()


def listing_url(url):
    """Allow product pages only. No private-network URLs or authenticated endpoints."""
    p = urlsplit(url)
    if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Expected an HTTPS marketplace product URL.')
    patterns = {'jp.mercari.com': r'/item/m\d+',
                'item.fril.jp': r'/[a-fA-F0-9]{20,64}'}
    if not re.fullmatch(patterns.get(p.hostname, r'(?!)'), p.path.rstrip('/')):
        raise ValueError('Unsupported marketplace product URL.')
    return urlunsplit(('https', p.hostname, p.path.rstrip('/'), '', ''))


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.blocks, self.jsonld, self.links = [], [], []
        self.hidden, self.script, self.buf, self.href, self.anchor = 0, False, [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.hidden += 1
            self.script = tag == 'script' and attrs.get('type') == 'application/ld+json'
            self.buf = []
        if tag == 'a':
            self.href, self.anchor = attrs.get('href'), []

    def handle_data(self, data):
        if self.script:
            self.buf.append(data)
        if not self.hidden and data.strip():
            self.blocks.append(data.strip())
            if self.href:
                self.anchor.append(data.strip())

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            if self.script:
                try:
                    self.jsonld.append(json.loads(''.join(self.buf)))
                except (ValueError, TypeError):
                    pass
            self.script = False
            self.hidden = max(0, self.hidden - 1)
        if tag == 'a' and self.href:
            self.links.append((self.href, ' '.join(self.anchor)))
            self.href = None


def products(value):
    if isinstance(value, list):
        for item in value:
            yield from products(item)
    elif isinstance(value, dict):
        if value.get('@type') == 'Product':
            yield value
        if '@graph' in value:
            yield from products(value['@graph'])


def number(x):
    try:
        value = float(str(x).replace(',', ''))
        return value if math.isfinite(value) and value >= 0 else None
    except (ValueError, TypeError):
        return None


def parse_html(html, url):
    """Read the primary Product JSON-LD, never prices of recommended items."""
    url = listing_url(url)
    page = Page()
    page.feed(html)
    candidates = list(products(page.jsonld))
    exact = []
    for product in candidates:
        offer = product.get('offers', {})
        if isinstance(offer, dict) and offer.get('url'):
            try:
                if listing_url(offer['url']) == url:
                    exact.append(product)
            except ValueError:
                pass
    if len(exact) != 1:
        raise ValueError('No unambiguous primary Product with this exact offer URL.')
    product = exact[0]
    offer = product['offers']
    text = '\n'.join(page.blocks).split('この商品を見ている人におすすめ')[0]
    condition = re.search(r'商品の状態\s*\n([^\n]+)', text)
    condition_label = condition.group(1).strip() if condition else 'unknown'
    known_conditions = ('新品、未使用','未使用に近い','目立った傷や汚れなし','やや傷や汚れあり','傷や汚れあり','全体的に状態が悪い')
    if condition_label not in known_conditions:
        condition_label = 'unknown'
    shipping = re.search(r'配送料の負担\s*\n([^\n]+)', text)
    availability = str(offer.get('availability', '')).rsplit('/', 1)[-1]
    state = {'InStock': 'available', 'SoldOut': 'sold', 'OutOfStock': 'sold'}.get(availability, 'unknown')
    price = number(offer.get('price'))
    currency = offer.get('priceCurrency')
    shipping_label = shipping.group(1).strip() if shipping else ''
    shipping_cost = 0 if '送料込み' in shipping_label else None
    title = str(product.get('name', ''))
    return {
        'url': url, 'title': title, 'price': price, 'currency': currency,
        'availability': state, 'shipping_jpy': shipping_cost,
        'condition': condition_label,
        'description': str(product.get('description', ''))[:3500],
        'observed_at': now(), 'method': 'direct-public-html-jsonld',
        'source_sha256': hashlib.sha256(html.encode()).hexdigest(),
        'evidence': {'price': str(offer.get('price', '')), 'availability': availability,
                     'condition': condition_label if condition_label != 'unknown' else '',
                     'shipping': shipping_label},
        'related': [{'url': href if href.startswith('https://') else 'https://jp.mercari.com'+href,
                     'title': name} for href, name in page.links
                    if re.match(r'^(https://jp\.mercari\.com)?/item/m\d+$', href)]
    }


def validate_extraction(data, text, url):
    """LLM fields need literal supporting snippets. Unsupported values become unknown."""
    if not isinstance(data, dict):
        raise ValueError('Extraction must be an object.')
    evidence = data.get('evidence', {})
    if not isinstance(evidence, dict):
        evidence = {}
    def supported(field):
        value = evidence.get(field)
        return isinstance(value, str) and 0 < len(value) < 220 and value in text
    price = number(data.get('price')) if supported('price') else None
    if price is not None:
        nums = [number(x) for x in re.findall(r'[0-9][0-9,]*(?:\.[0-9]+)?', evidence['price'])]
        if price not in nums:
            price = None
    state = data.get('availability', 'unknown')
    if not supported('availability'):
        state = 'unknown'
    elif state == 'sold' and not any(x in evidence['availability'].lower() for x in ['売り切れ','sold out','soldout','outofstock']):
        state = 'unknown'
    elif state == 'available' and not any(x in evidence['availability'].lower() for x in ['購入手続きへ','購入に進む','購入する','instock','buy now']):
        state = 'unknown'
    elif state not in ('available', 'sold'):
        state = 'unknown'
    shipping = 0 if supported('shipping') and ('送料込み' in evidence['shipping'] or 'free shipping' in evidence['shipping'].lower()) else None
    title = evidence['title'].strip().lstrip('#').strip() if supported('title') else ''
    condition = str(data.get('condition', 'unknown')) if supported('condition') and str(data.get('condition','')) in evidence['condition'] else 'unknown'
    currency = 'JPY' if supported('price') and any(x in evidence['price'] for x in ['¥','￥','円','JPY']) else None
    return {'url':listing_url(url), 'title':title, 'price':price, 'currency':currency,
            'availability':state,'shipping_jpy':shipping,'condition':condition,
            'description':'','observed_at':now(),'method':'tavily-extract-nosana-structured',
            'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'evidence':{k:v for k,v in evidence.items() if supported(k)}, 'related':[]}


def validate_request(d):
    if not isinstance(d, dict) or d.get('mode') not in ('collector', 'investor'):
        raise ValueError('mode must be collector or investor.')
    if not isinstance(d.get('near_mint_only', True), bool):
        raise ValueError('near_mint_only must be true or false.')
    for key in ('budget_jpy', 'reserve_jpy', 'exit_shipping_jpy'):
        value = d.get(key, 0 if key != 'exit_shipping_jpy' else 250)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000000:
            raise ValueError(key+' must be an integer from 0 to 1000000.')
        d[key] = value
    d['fee_rate'] = float(d.get('fee_rate', .10))
    if not math.isfinite(d['fee_rate']) or not 0 <= d['fee_rate'] < 1:
        raise ValueError('fee_rate must be between 0 and 1, excluding 1.')
    targets = d.get('targets', [])
    if not isinstance(targets, list) or not 1 <= len(targets) <= 10:
        raise ValueError('Provide 1–10 target card printings.')
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or not re.fullmatch(r'\d{1,3}/\d{1,3}',str(target.get('number',''))):
            raise ValueError('Each target needs a printed card number such as 173/165.')
        if not isinstance(target.get('name_ja'), str) or not target['name_ja'] or len(target['name_ja']) > 80:
            raise ValueError('Each target needs its Japanese card name.')
        if target.get('rarity') not in ['AR','SAR','SR','RR','R','U','C','UR','CHR','CSR']:
            raise ValueError('Each target needs a recognized rarity.')
        if not isinstance(target.get('owned', False), bool):
            raise ValueError('owned must be true or false.')
        identity = target['name_ja']+' '+target['number']+' '+target['rarity']
        target['key'] = identity
        if identity in seen:
            raise ValueError('Duplicate target card.')
        seen.add(identity)
        if 'exit_assumption_jpy' in target:
            value = number(target['exit_assumption_jpy'])
            if value is None or value > 10000000:
                raise ValueError('Invalid explicitly supplied exit assumption.')
            target['exit_assumption_jpy'] = value
    return d


def classify(record, request):
    title = record['title']
    target = next((t for t in request['targets'] if re.search(r'(?<!\d)'+re.escape(t['number'])+r'(?!\d)', title)
                   and t['name_ja'].casefold() in title.casefold()
                   and re.search(r'(?<![A-Za-z])'+t['rarity']+r'(?![A-Za-z])', title, re.I)), None)
    reasons = []
    if target is None:
        reasons.append('Exact name / printed number / rarity not established in title')
    if re.search(r'PSA|BGS|CGC|まとめ|セット|(?:[2-9２-９]|[1-9１-９][0-9０-９]+)\s*枚',title,re.I):
        reasons.append('Graded card or bundle: not comparable to one raw card')
    if record['availability'] != 'available':
        reasons.append('Sold' if record['availability']=='sold' else 'Availability unverified')
    if record['currency'] != 'JPY' or record['price'] is None:
        reasons.append('JPY price not established')
    if record['shipping_jpy'] is None:
        reasons.append('Total acquisition cost unknown: shipping not established')
    if request.get('near_mint_only', True) and record['condition'] not in ('新品、未使用','未使用に近い'):
        reasons.append('Seller condition does not meet requested near-mint filter')
    damage_warning = bool(record.get('seller_damage_warning')) or bool(re.search(r'白かけあり|白欠けあり|傷あり|折れあり|凹みあり|ジャンク|プレイ用', title+' '+record.get('description','')))
    damage_warning = damage_warning or 'Seller text contains a damage / play-use warning' in record.get('reasons',[])
    if request.get('near_mint_only', True) and damage_warning:
        reasons.append('Seller text contains a damage / play-use warning')
    if target and target.get('owned'):
        reasons.append('Already owned')
    record = {k:v for k,v in record.items() if k not in ('description','related')}
    record['target'] = target['key'] if target else None
    record['reasons'] = reasons
    record['eligible'] = not reasons
    record['authenticity'] = 'not authenticated; condition is seller-claimed'
    record['seller_damage_warning'] = damage_warning
    record['total_jpy'] = round(record['price']+record['shipping_jpy']) if record['price'] is not None and record['shipping_jpy'] is not None and record['currency']=='JPY' else None
    if record['total_jpy'] is not None:
        record['break_even_jpy'] = math.ceil((record['total_jpy']+request['exit_shipping_jpy'])/(1-request['fee_rate']))
    record['scenario_net_jpy'] = None
    if target and 'exit_assumption_jpy' in target and record['total_jpy'] is not None:
        record['exit_assumption_jpy'] = target['exit_assumption_jpy']
        record['scenario_net_jpy'] = math.floor(target['exit_assumption_jpy']*(1-request['fee_rate']))-request['exit_shipping_jpy']-record['total_jpy']
    return record


def optimize(records, request):
    """One raw card per target; maximize completion or explicit scenario outcome."""
    by_target = {}
    for row in records:
        if row['eligible'] and (row['target'] not in by_target or row['total_jpy'] < by_target[row['target']]['total_jpy']):
            by_target[row['target']] = row
    rows = list(by_target.values())
    if request['mode'] == 'investor':
        rows = [r for r in rows if r['scenario_net_jpy'] is not None and r['scenario_net_jpy'] > 0]
    spendable = max(0,request['budget_jpy']-request['reserve_jpy'])
    best, cost, score = [], 0, 0
    for mask in range(1<<len(rows)):
        chosen = [r for i,r in enumerate(rows) if mask&(1<<i)]
        spend = sum(r['total_jpy'] for r in chosen)
        value = len(chosen) if request['mode']=='collector' else sum(r['scenario_net_jpy'] for r in chosen)
        if spend <= spendable and (value > score or (value == score and spend < cost)):
            best, cost, score = chosen, spend, value
    return {'urls':[r['url'] for r in best], 'cost_jpy':cost, 'score':score,
            'cash_remaining_jpy':request['budget_jpy']-cost,
            'objective':'wishlist count, then lowest cost' if request['mode']=='collector' else 'net outcome under user-supplied exit assumptions',
            'scope':'best among retrieved, eligible listings; not the entire market',
            'forecast':'No trained price forecast or guaranteed resale profit.'}


def summary(report):
    lines=['# PokéScout agent run', '', 'Status: '+report['status'], '',
           'Goal: '+report['goal'], '', 'Run completed: '+report['completed_at'], '',
           'Individual source observation times are retained in report.json; replanning does not refresh them.', '',
           '| Listing | Price JPY | State | Decision |', '|---|---:|---|---|']
    selected=set(report.get('plan',{}).get('urls',[]))
    for r in report.get('listings',[]):
        title = r['title'].replace('|',' ') or 'Unresolved listing'
        decision='Selected' if r['url'] in selected else ('Eligible; not selected' if r['eligible'] else '; '.join(r['reasons']))
        lines.append(f"| [{title}]({r['url']}) | {r.get('total_jpy') if r.get('total_jpy') is not None else 'unknown'} | {r['availability']} | {decision} |")
    p=report.get('plan',{})
    lines += ['', 'Selected cost: ¥'+str(p.get('cost_jpy',0)),
              'Cash remaining: ¥'+str(p.get('cash_remaining_jpy',0)), '',
              'No purchase, message or listing was submitted. Prices are observations, not guarantees. Unknown fields remain unknown. A sold listing is not a verified settlement-price record.', '',
              '## Sponsor execution', '', '| Partner | State |', '|---|---|']
    lines += [f'| {k} | {v} |' for k,v in report['sponsors'].items()]
    if report.get('request',{}).get('mode')=='investor':
        job=report['request']
        lines += ['', '## Investor scenario', '',
                  f"Assumed sale fee: {job['fee_rate']:.1%}; assumed outbound shipping: ¥{job['exit_shipping_jpy']}. Taxes, grading, returns, financing and other costs are excluded.", '',
                  '| Listing | Break-even sale price JPY | Assumed exit JPY | Scenario net JPY |', '|---|---:|---:|---:|']
        for r in report.get('listings',[]):
            lines.append(f"| [{r['title'].replace('|',' ')}]({r['url']}) | {r.get('break_even_jpy','unknown')} | {r.get('exit_assumption_jpy','not supplied')} | {r.get('scenario_net_jpy') if r.get('scenario_net_jpy') is not None else 'not estimated'} |")
        lines += ['', 'These are conditional calculations, not a resale forecast. Ineligible listings remain excluded from the plan.']
    if report.get('errors'):
        lines += ['', '## Incomplete steps', *['- '+x for x in report['errors']]]
    if report.get('analysis'):
        lines += ['', '## Agent explanation', report['analysis']]
    return '\n'.join(lines)+'\n'
