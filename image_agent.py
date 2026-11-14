"""Image identification boundary for the Python orchestrator; never confirms ownership."""
from __future__ import annotations

import time
from datetime import datetime, timezone

from cards import IDENTITY_FIELDS, identify


def recognize_card(images, *, model=None):
    """Return an identification candidate or a safe, structured failure.

    ``images`` are 1–4 views of ONE physical card, not a batch of cards.
    The caller owns retries and any user confirmation or collection writes.
    """
    started = time.monotonic()
    result = {
        'agent': 'image_recognition',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'ok': False,
        'requires_confirmation': True,
    }
    try:
        candidate = identify(images, model=model)
        card = candidate['card']
        missing = [field for field in (*IDENTITY_FIELDS, 'presentation') if not card[field]]
        if card['presentation'] == 'slabbed':
            if not card['grader']:
                missing.append('grader')
            if not (card['grade'] or card['grade_text']):
                missing.append('grade_or_grade_text')
        result.update(candidate, ok=True, missing_fields=missing,
                      candidate_complete=not missing and not card['uncertainties'])
    except Exception as error:
        # Never echo provider exception bodies: they may contain request data or keys.
        code = getattr(error, 'code', None)
        if code is None:
            code = getattr(error, 'status_code', None)
        if code == 429:
            failure = ('RATE_LIMITED', True, 'Provider rate limit reached.')
        elif isinstance(code, int) and 500 <= code <= 599:
            failure = ('PROVIDER_UNAVAILABLE', True, 'Provider is temporarily unavailable.')
        elif code == 404:
            failure = ('MODEL_UNAVAILABLE', False, 'Requested model is unavailable; choose a model explicitly.')
        elif code == 400:
            failure = ('PROVIDER_REJECTED_REQUEST', False, 'Provider rejected the request; check model and schema support.')
        elif code in (401, 403):
            failure = ('AUTHENTICATION_FAILED', False, 'Check API access and credentials.')
        elif isinstance(error, (TimeoutError, ConnectionError)) or 'timeout' in type(error).__name__.lower():
            failure = ('PROVIDER_TIMEOUT', True, 'Provider request timed out.')
        elif isinstance(error, ValueError):
            message = str(error)
            if message == 'GEMINI_API_KEY is not configured.':
                failure = ('MISSING_API_KEY', False, message)
            elif message.startswith('Install requirements-vision.txt'):
                failure = ('DEPENDENCIES_UNAVAILABLE', False, 'Install requirements-vision.txt.')
            elif message.startswith('Gemini response'):
                failure = ('INVALID_RESPONSE', False, 'Provider response failed validation.')
            else:
                failure = ('INVALID_INPUT', False, 'Check image count, type, size and readability.')
        elif isinstance(error, OSError):
            failure = ('INVALID_INPUT', False, 'Image could not be read or decoded.')
        else:
            failure = ('IDENTIFICATION_FAILED', False, 'Identification failed; inspect private runtime diagnostics.')
        result['error'] = dict(zip(('code', 'retryable', 'message'), failure))
    result['elapsed_ms'] = round((time.monotonic() - started) * 1000)
    return result
