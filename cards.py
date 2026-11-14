"""Validated card intake and explicit PSA evidence comparison; no authentication claim."""
from __future__ import annotations
import hashlib
from io import BytesIO
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

DEFAULT_GEMINI_MODEL='gemini-3.5-flash-lite'

IDENTITY_FIELDS=('name','set','number','language','variant')
TEXT_FIELDS=IDENTITY_FIELDS+('year','brand','presentation','grader','grade','grade_text','cert_number')
CARD_SCHEMA={'type':'object','properties':{
    **{k:{'type':['string','null']} for k in TEXT_FIELDS},
    'qualifiers':{'type':'array','items':{'type':'string'}},
    'defects':{'type':'array','items':{'type':'string'}},
    'uncertainties':{'type':'array','items':{'type':'string'}}},
    'required':list(TEXT_FIELDS)+['qualifiers','defects','uncertainties'],
    'additionalProperties':False}

def money(value,name='amount'):
    if type(value) is not int or not 0<=value<=100_000_000:
        raise ValueError(name+' must be nonnegative integer JPY, at most 100,000,000.')
    return value

def validate_card(value,confirmed=False):
    if not isinstance(value,dict):raise ValueError('Card must be an object.')
    card={k:value.get(k) for k in TEXT_FIELDS}
    for k,v in card.items():
        if v is not None and (not isinstance(v,str) or not v.strip() or len(v)>300):
            raise ValueError(k+' must be a nonempty string or null.')
    for key in ('qualifiers','defects','uncertainties'):
        vals=value.get(key,[])
        if not isinstance(vals,list) or len(vals)>30 or any(not isinstance(v,str) or not v.strip() or len(v)>1000 for v in vals):
            raise ValueError(key+' must be a bounded list of strings.')
        card[key]=vals
    if card['presentation'] not in ('raw','slabbed',None):raise ValueError('presentation must be raw, slabbed or null.')
    if card['presentation']=='raw' and any(card[k] is not None for k in ('grader','grade','grade_text','cert_number')):
        raise ValueError('Raw cards cannot have an assigned slab grade or certificate.')
    if card['grade'] is not None:
        try:g=Decimal(card['grade'])
        except InvalidOperation:raise ValueError('Numeric grade must be a decimal string.') from None
        if not g.is_finite() or not 1<=g<=10:raise ValueError('Grade must lie between 1 and 10; use grade_text for nonnumeric outcomes.')
    if confirmed and (any(not card[k] for k in IDENTITY_FIELDS) or card['presentation'] is None or card['uncertainties']):
        raise ValueError('Resolve name, set, number, language, variant, presentation and uncertainties first.')
    if confirmed and card['presentation']=='slabbed' and (not card['grader'] or not (card['grade'] or card['grade_text'])):
        raise ValueError('Confirm grader and grade text/value for a slab.')
    return card

def identity_key(card):
    return json.dumps([str(card.get(k) or '').strip().casefold() for k in IDENTITY_FIELDS],ensure_ascii=False)

def compare_psa(card,registry):
    """Compare a supplied record. Caller must retain its source; no API call here."""
    card=validate_card(card,True)
    if (card.get('grader') or '').upper()!='PSA':raise ValueError('This comparison requires a PSA label.')
    fields=('cert_number','name','set','number','language','variant','grade')
    missing=[k for k in fields if not card.get(k) or not registry.get(k)]
    norm=lambda x:re.sub(r'\s+',' ',str(x).strip()).casefold()
    mismatches=[k for k in fields if card.get(k) and registry.get(k) and norm(card[k])!=norm(registry[k])]
    if 'qualifiers' not in registry:missing.append('qualifiers')
    elif sorted(map(norm,card['qualifiers']))!=sorted(map(norm,registry['qualifiers'])):mismatches.append('qualifiers')
    return {'status':'registry_mismatch' if mismatches else ('incomplete' if missing else 'registry_match'),
            'missing':missing,'mismatches':mismatches,'source':'user_supplied_record; not fetched by PokéScout',
            'physical_authenticity':'not established'}

def identify(images, *, model=None):
    if not os.environ.get('GEMINI_API_KEY'):raise ValueError('GEMINI_API_KEY is not configured.')
    if not isinstance(images,(list,tuple)) or not 1<=len(images)<=4:
        raise ValueError('Provide 1–4 image paths for one card.')
    selected_model=model if model is not None else (os.environ.get('GEMINI_MODEL') or DEFAULT_GEMINI_MODEL)
    if not isinstance(selected_model,str) or not selected_model.strip():
        raise ValueError('Provide a nonempty Gemini model identifier.')
    try:
        from google import genai
        from google.genai import types
        from PIL import Image
    except ImportError:raise ValueError('Install requirements-vision.txt for image intake.') from None
    parts=[]; hashes=[]; total=0
    for file in images:
        if not isinstance(file,(str,Path)):
            raise ValueError('Each image must be a local file path.')
        p=Path(file)
        with p.open('rb') as stream:raw=stream.read(12_000_001-total)
        total+=len(raw)
        if total>12_000_000:raise ValueError('Images exceed 12 MB combined limit.')
        with Image.open(BytesIO(raw)) as image:
            image.verify();mime=Image.MIME.get(image.format)
        if mime not in ('image/jpeg','image/png','image/webp'):raise ValueError('Use JPEG, PNG or WebP.')
        hashes.append(hashlib.sha256(raw).hexdigest())
        parts.append(types.Part.from_bytes(data=raw,mime_type=mime))
    prompt=('Extract visible physical Pokemon card and slab-label fields. Preserve original text. '
            'Use null for unreadable fields and list ambiguities in uncertainties. '
            'presentation is raw or slabbed. Never grade raw cards or claim authenticity. '
            'Grade and year are strings. Separate disclosed/visible defects from uncertainty. '
            'No inferred perfect condition. Identity will be confirmed by a user.')
    with genai.Client(api_key=os.environ['GEMINI_API_KEY'],
            http_options={'timeout':65000,'retry_options':{'attempts':1}}) as client:
        response=client.models.generate_content(model=selected_model,contents=[prompt,*parts],
            config={'response_mime_type':'application/json','response_json_schema':CARD_SCHEMA})
    try:
        parsed=json.loads(response.text)
        if not isinstance(parsed,dict) or set(parsed)!=set(CARD_SCHEMA['required']):
            raise ValueError('Missing or unexpected card fields.')
        card=validate_card(parsed)
    except (ValueError,TypeError) as error:
        raise ValueError('Gemini response failed card schema validation.') from error
    return {'card':card,'image_sha256':hashes,'provider':'Gemini','configured_model':selected_model,
            'returned_model':getattr(response,'model_version',None),'request_id':getattr(response,'response_id',None),
            'status':'candidate; user confirmation required','physical_authenticity':'not established'}
