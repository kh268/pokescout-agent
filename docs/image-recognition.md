# Image-recognition agent

The image workstream exposes a Python function and the existing `identify` CLI.
It identifies one card from 1–4 local views, returns a schema-validated candidate,
and leaves confirmation and ownership changes to the orchestrator. It does not
call TCGdex, research prices, write collection records, or authenticate a card.

## Call from the orchestrator

```python
from image_agent import recognize_card

result = recognize_card(["front.png", "back.png"])
if not result["ok"]:
    # Apply the job's retry budget only when retryable is true.
    handle_failure(result["error"])
elif not result["candidate_complete"]:
    ask_about(result["missing_fields"], result["card"]["uncertainties"])
else:
    request_user_confirmation(result["card"])
```

Set `GEMINI_API_KEY` securely in the calling process. Never pass a key in CLI
arguments or commit one. Install `requirements-vision.txt` in an isolated virtual
environment. The app does not load `.env` files automatically.

Model precedence: `recognize_card(..., model="...")` / CLI `--model`, then
`GEMINI_MODEL`, then `gemini-3.5-flash-lite`. The team selected Flash-Lite after
3.8 Flash returned three 503s and a replacement-key attempt timed out. No provider
or model is silently substituted. An existing environment override still wins.
The lower-level `cards.identify(images, model=...)` remains available and raises
errors instead of returning an error envelope.

## CLI

```bash
python pokescout.py identify front.png back.png \
  --model gemini-3.5-flash-lite --out runs/card-candidate.json
```

Exit 0 means a candidate was extracted, including incomplete candidates. Exit 2
means identification failed. Both outcomes write JSON to `--out`; existing output
files are refused. CLI argument and output-file errors remain ordinary CLI errors.

Successful results preserve `card`, `image_sha256`, `provider`,
`configured_model`, `returned_model`, `request_id`, `status`, and
`physical_authenticity`. These remain compatible with `add --record`.

The agent adds:

| Field | Meaning |
| --- | --- |
| `agent` | `image_recognition` |
| `ok` | Extraction succeeded, not identity confirmation |
| `candidate_complete` | Required identity and presentation fields are present, with no reported uncertainties; slabs also need grader and grade text/value |
| `missing_fields` | Fields needing correction or another view |
| `requires_confirmation` | Always true, including complete candidates |
| `observed_at` | UTC request start time, not a marketplace quote time |
| `elapsed_ms` | Local duration including decoding and the provider call |

Empty `defects` or `uncertainties` arrays are model output, not proof of flawless
condition or correct identity. Images are evidence references via hashes; this
adapter does not yet return independently verified field-level visual spans.

Failures have `ok: false` and an `error` object with `code`, `retryable`, and a
safe `message`. Codes include `MISSING_API_KEY`, `DEPENDENCIES_UNAVAILABLE`,
`INVALID_INPUT`, `INVALID_RESPONSE`, `AUTHENTICATION_FAILED`, `RATE_LIMITED`,
`MODEL_UNAVAILABLE`, `PROVIDER_REJECTED_REQUEST`, `PROVIDER_UNAVAILABLE`,
`PROVIDER_TIMEOUT`, and `IDENTIFICATION_FAILED`. No card is returned on failure.
Provider exception bodies are omitted to avoid exposing credentials or image data.

The adapter uses a 65-second HTTP timeout and one SDK attempt. The orchestrator
owns bounded retries; a retryable error is not permission to spend without limits.
Free-tier eligibility and remaining account quota must be checked separately.

## Input and output validation

- Accept JPEG, PNG or WebP local files, at most 12 MB combined.
- Supply views of one physical card per call; multiple separate cards are not a
  batch input. This API cannot reliably detect accidental multi-card images.
- Unreadable fields stay null and ambiguities stay in `uncertainties`.
- Require the full expected response shape before using model output.
- Raw cards cannot acquire a slab grade or certification number.
- Keep certification numbers as strings, including leading zeros.
- Require user correction and `add --confirm-owned` before recording ownership.

## Verification and evidence

[Recorded live result](../evidence/image-recognition/gemini-flash-lite-public-sample.json):

- Baseline commit: `cd1e6c9`, existing `pokescout.py identify -> cards.identify`.
- Model requested and returned: `gemini-3.5-flash-lite`.
- Public input: https://assets.tcgdex.net/ja/SV/SV2a/201/high.png
- Expected catalog: https://api.tcgdex.net/v2/ja/cards/SV2a-201
- Result: Japanese `リザードンex`, `sv2a`, `201/165`, `SAR`, `raw`; grading fields null.
- Approximate CLI duration: 4.26 seconds. The prompt did not contain the expected identity.

That single run predates this wrapper. It is not a live test of this branch,
real-photo accuracy, blurred images, multiple cards, PSA slabs, or authenticity.
The branch's tests use provider mocks and do not call paid or free cloud services.

Run the regression suite:

```bash
python -m unittest discover -s tests -v
```

A new live acceptance run requires a configured free-tier key and a permitted
sample. Start with the same clean image, then test one real photo and one image
with an unreadable number. Preserve nulls and confirmation requirements; do not
claim an accuracy percentage from these few examples.

## Team ownership

Branch `feat/image-recognition-agent` owns `cards.py`, `image_agent.py`, the
`workflow.py` identify entry, image tests, this contract, and its evidence. The
main orchestrator owner integrates it; no main merge, deployment, or public push
is implied by the local workstream commit.
