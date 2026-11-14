"""Bounded Tavily → evidenced Nosana extraction → deterministic Python plan."""
from __future__ import annotations

from cards import validate_card
from core import now
from decisions import plan
from providers import Nosana, Tavily


def research_and_plan(goal, copies=(), tavily_factory=Tavily, nosana_factory=Nosana):
    targets = goal.get('targets', [])
    if not isinstance(targets, list) or not 1 <= len(targets) <= 3:
        raise ValueError('Live Tavily research supports 1–3 confirmed targets per run.')
    if goal.get('source_mode') != 'tavily_live':
        raise ValueError('source_mode must be tavily_live.')
    for target in targets:
        validate_card(target['card'], True)
    events = []
    def emit(provider, event, **details):
        events.append({'at': now(), 'provider': provider, 'event': event, **details})
    tavily, nosana = tavily_factory(emit), nosana_factory(emit)
    observations, listings, failures = [], [], []
    for target in targets:
        card = target['card']
        query = f"{card['name']} {card['number']} {card['variant']} {card['set']} ポケモンカード"
        urls = tavily.search(query)[:3]
        extracted = tavily.extract(urls)
        for url, text in extracted:
            try:
                record = nosana.extract(text, url)
                title = record.get('title') or ''
                # A listing's full printing is never inferred from a research target.
                identity_confirmed = all(token.casefold() in title.casefold() for token in
                                         (card['name'], card['number'], card['variant'], card['set']))
                offer_card = dict(card) if identity_confirmed else {**card, 'set': 'UNCONFIRMED',
                    'number': 'UNCONFIRMED', 'variant': 'UNCONFIRMED'}
                condition = {'新品、未使用': 'unused'}.get(record.get('condition'), 'unknown')
                price = record.get('price')
                price = int(price) if type(price) in (int, float) and price == int(price) and 0 <= price <= 100_000_000 else None
                observed_at = record.get('observed_at') or now()
                offer_id = 'live-' + str(len(listings) + 1)
                row = {'id': offer_id, 'card': offer_card, 'price_jpy': price,
                       'shipping_jpy': record.get('shipping_jpy'),
                       'purchase_fee_jpy': None, 'condition': condition,
                       'availability': record.get('availability', 'unknown'), 'quantity': 1,
                       'source_url': url, 'observed_at': observed_at}
                listings.append(row)
                observations.append({'id': offer_id, 'source_url': url, 'observed_at': observed_at,
                    'title': title, 'price_jpy': price, 'shipping_jpy': record.get('shipping_jpy'),
                    'purchase_fee_jpy': None, 'availability': row['availability'],
                    'condition_source': record.get('condition'), 'identity_confirmed': identity_confirmed,
                    'source_sha256': record.get('source_sha256'), 'evidence': record.get('evidence', {})})
            except Exception as exc:
                failures.append({'source_url': url, 'error': str(exc)[:300]})
    calculated = plan(goal, listings, copies)
    for checked in calculated['checked']:
        observation = next(x for x in observations if x['id'] == checked['id'])
        if not observation['identity_confirmed']:
            checked['reasons'].append('IDENTITY_NOT_EVIDENCED')
            checked['eligible'] = False
        # The extraction contract does not prove purchase fees; unknown remains
        # unknown, so no automatic purchase recommendation is possible.
    return {'mode': 'tavily_live', 'currency': 'JPY', 'created_at': now(),
            'status': 'partial' if failures or not observations else 'complete',
            'goal': goal, 'observations': observations, 'plan': calculated,
            'failures': failures, 'events': events,
            'disclosure': 'Tavily public-source retrieval and Nosana evidenced extraction. Fees and physical authenticity remain unverified; no transaction.'}
