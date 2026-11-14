"""v0.2 exact-identity, quantity-aware collector planning for structured evidence."""
from __future__ import annotations
import itertools
from cards import validate_card, identity_key, money

def plan(goal,listings,copies=()):
    if not isinstance(goal,dict) or not isinstance(listings,list) or len(listings)>16:
        raise ValueError('Provide a goal object and at most 16 structured offers.')
    budget=money(goal.get('budget_jpy'),'budget_jpy');reserve=money(goal.get('reserve_jpy',0),'reserve_jpy')
    reservations=money(goal.get('active_reservations_jpy',0),'active_reservations_jpy')
    if type(goal.get('avoid_owned',True)) is not bool:raise ValueError('avoid_owned must be boolean.')
    if type(goal.get('allow_defects',False)) is not bool:raise ValueError('allow_defects must be boolean.')
    conditions=goal.get('allowed_conditions',['near_mint','unused'])
    if not isinstance(conditions,list) or not conditions or any(x not in ('near_mint','unused','played','damaged') for x in conditions):
        raise ValueError('Choose explicit allowed_conditions: near_mint, unused, played, damaged.')
    targets=goal.get('targets',[])
    if not isinstance(targets,list) or not 1<=len(targets)<=10:raise ValueError('Provide 1–10 targets.')
    keys={};remaining={}
    for t in targets:
        card=validate_card(t['card'],True);key=identity_key(card)
        if key in keys:raise ValueError('Duplicate target identity.')
        qty=t.get('quantity',1)
        if type(qty) is not int or not 1<=qty<=10:raise ValueError('quantity must be 1–10.')
        keys[key]=card
        owned=sum(1 for c in copies if c['state']=='owned' and identity_key(c['card'])==key
                  and cohort(c['card'])==cohort(card)) if goal.get('avoid_owned',True) else 0
        remaining[key]=max(0,qty-owned)
    checked=[];ids=set();eligible=[]
    for row in listings:
        lid=row.get('id')
        if not isinstance(lid,str) or not lid or lid in ids:raise ValueError('Unique listing IDs required.')
        ids.add(lid);reasons=[]
        card=validate_card(row['card'],True);key=identity_key(card);target=keys.get(key)
        if not target:reasons.append('WRONG_PRINTING_LANGUAGE_OR_VARIANT')
        elif cohort(card)!=cohort(target):reasons.append('WRONG_GRADING_COHORT')
        if card['presentation']=='slabbed' and row.get('registry_status')=='registry_mismatch':reasons.append('REGISTRY_MISMATCH')
        if row.get('availability')!='available':reasons.append('NOT_AVAILABLE')
        if type(row.get('quantity',1)) is not int or row.get('quantity',1)!=1:reasons.append('UNSUPPORTED_BUNDLE')
        if card['presentation']=='raw' and row.get('condition') not in goal.get('allowed_conditions',['near_mint','unused']):
            reasons.append('CONDITION_BELOW_REQUIREMENT_OR_UNKNOWN')
        if card['defects'] and not goal.get('allow_defects',False):reasons.append('DISCLOSED_DEFECT')
        if target and remaining[key]==0:reasons.append('ALREADY_OWNED_OR_QUANTITY_COMPLETE')
        costs=[row.get(k) for k in ('price_jpy','shipping_jpy','purchase_fee_jpy')]
        total=None
        if any(x is None for x in costs):reasons.append('ACQUISITION_TOTAL_UNKNOWN')
        else:total=sum(money(x) for x in costs)
        observed={'id':lid,'identity':key,'total_jpy':total,'reasons':reasons,'eligible':not reasons,
                  'source_url':row.get('source_url'),'observed_at':row.get('observed_at')}
        checked.append(observed)
        if not reasons:eligible.append(observed)
    best=[];best_cost=0;available=max(0,budget-reserve-reservations)
    # Bounded exhaustive search: max 16 offers; objective is quantity, then cost.
    for mask in range(1<<len(eligible)):
        selected=[r for i,r in enumerate(eligible) if mask&(1<<i)]
        counts={k:0 for k in keys}
        for r in selected:counts[r['identity']]+=1
        if any(counts[k]>remaining[k] for k in keys):continue
        cost=sum(r['total_jpy'] for r in selected)
        if cost<=available and (len(selected)>len(best) or (len(selected)==len(best) and cost<best_cost)):
            best,best_cost=selected,cost
    return {'engine':'deterministic-python-v0.2','selected_ids':[r['id'] for r in best],
            'cost_jpy':best_cost,'remaining_allowance_jpy':budget-reservations-best_cost,
            'reserve_jpy':reserve,'checked':checked,'source_mode':goal.get('source_mode','user_supplied_observations'),
            'execution':'none; current availability and total must be rechecked',
            'scope':'best among supplied eligible single-card offers; no forecast'}

def cohort(card):
    return (card['presentation'],card.get('grader'),card.get('grade'),card.get('grade_text'),tuple(sorted(card.get('qualifiers',[]))))
