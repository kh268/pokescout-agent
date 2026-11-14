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

## Ten-card live evaluation — 2026-11-14

[Full recorded results](../evidence/image-recognition/public-catalog-10-cards.json)
were produced on commit `cba2dbb` using the new agent and exactly 10 live calls.
All 9 successful responses reported `gemini-3.5-flash-lite`. No expected card
identity was supplied in the prompt, and no failed sample was retried. Requests
were started at least 15 seconds apart. The temporary credential file was deleted.

| Sample | Result | Seconds |
| --- | --- | ---: |
| Charizard ex, SV2a 201/165 SAR | Strict match | 3.43 |
| Charmander, SV2a 168/165 AR | Provider unavailable; no identity output | 11.56 |
| Pikachu, SV2a 173/165 AR | Set null; candidate incomplete | 9.43 |
| Bulbasaur, SV2a 166/165 AR | Strict match | 19.60 |
| Squirtle, SV2a 170/165 AR | Strict match | 14.82 |
| Venusaur ex, SV2a 200/165 SAR | English set alias plus inferred-set uncertainty; candidate incomplete | 7.34 |
| Erika's Invitation, SV2a 206/165 SAR | Strict match | 10.74 |
| Charizard ex, SV3 134/108 SAR | Translated set and expanded rarity label; canonicalization needed | 36.55 |
| Charizard ex, SV4a 349/190 SAR | Strict match | 12.40 |
| Charizard ex, SV4a 331/190 SSR | Strict match | 18.02 |

Summary: 9/10 calls returned validated JSON. All 9 returned the correct name,
collector number, language, raw presentation, and null grading fields. Six met
all strict comparison rules. Two used translated/expanded aliases and one
omitted the set. Median successful call duration was 12.40 seconds, with a
3.43–36.55 second range. These are agent-call durations, unlike the earlier
single-sample measurement which included CLI startup.

The strict evaluator accepts the catalog set code or original Japanese set name
and the printed rarity abbreviation. It applies NFKC, whitespace removal and
case-folding; it does not automatically translate aliases. For example,
`Ruler of the Black Flame` and `Special Illustration Rare (SAR)` fail that exact
contract even though manual review identifies translation/format differences,
not a different card. Preserve this distinction rather than reporting 6/10 as a
general model accuracy score. `candidate_complete` checks presence and reported
uncertainty; it does not prove a canonical catalog match.

The TCGdex records for the three SV3/SV4a samples did not include rarity. Their
printed SAR/SSR labels were inspected directly before model calls, and the
report records this separate ground-truth source. Catalog names, numbers, set
codes, source URLs, image hashes and raw returned fields remain inspectable.

All samples are clean Japanese catalog images across three sets (four AR, five
SAR, one SSR), including four Charizard printings and one Trainer card. This is
a selected, small smoke set, not representative real-photo evaluation. No blur,
glare, slab, rotated-image or authenticity result is claimed. The next needed
integration step is explicit catalog canonicalization while preserving original
model text; an unknown set must remain unresolved until checked or confirmed.

## Team ownership

Branch `feat/image-recognition-agent` owns `cards.py`, `image_agent.py`, the
`workflow.py` identify entry, image tests, this contract, and its evidence. The
main orchestrator owner integrates it; no main merge, deployment, or public push
is implied by the local workstream commit.
