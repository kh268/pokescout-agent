# PokéScout Agent

A Python agent for physical Pokémon card collection, marketplace research and budget decisions. This release updates the earlier ZIP to the current architecture: models extract evidence; Python owns purchase calculations; Playwright is isolated behind an explicit test-execution adapter.

The release CLI modules remain at the repository root to preserve their existing imports. New backend-facing providers and a reusable Daytona browser worker are documented in [Provider Setup](docs/Provider_Setup.md). `RELEASE.json` and `MANIFEST.sha256` describe the earlier v0.2 release snapshot.

## Connected workflow

The persisted Python workflow now connects image intake, typed clarification,
bounded research, evidence validation, deterministic purchase decisions and
review-only sale drafting. It stores jobs, candidates, research reports and
decisions under a chosen data directory. Research observations without confirmed
printing, condition and fee facts remain rejected evidence. A labeled fixture
can optionally execute test orders through Playwright; live marketplace buying
and sale publication are not enabled.

```powershell
.\.venv\Scripts\python.exe pokescout.py pipeline --data-dir data/pipeline demo
.\.venv\Scripts\python.exe pokescout.py pipeline --data-dir data/pipeline start --input request.json --idempotency-key my-request-1
.\.venv\Scripts\python.exe pokescout.py pipeline --data-dir data/pipeline status JOB_ID
.\.venv\Scripts\python.exe pokescout.py pipeline --data-dir data/pipeline result JOB_ID
```

`start` accepts a JSON object with `goal` and optional `images`, `reports`,
`facts`, `fixture_listings`, `asking_price_jpy` and `execute_fixture` fields.
When a job returns `awaiting_input`, save answers as a JSON array of
`{question_id, value}` objects, then run `pipeline answer JOB_ID --answers
answers.json --revision N`. An image candidate never confirms a card or its
ownership automatically. `execute_fixture: true` is accepted only for a fixture
buy request and submits synthetic test orders; omit it for a decision-only run.
For that optional local browser test, install `requirements-browser.txt` and run
`python -m playwright install chromium`. The ordinary demo needs neither.

The HTTP backend has its own authenticated, session-scoped job store. Its
research-to-decision path now uses `research_decisions.py` to preserve exclusion
reasons from the shared research contract. The CLI pipeline's data directory is
not exposed as HTTP resources; its `result` command reads its saved artifacts.

**Start with the reproducible local demo. It needs no API keys and performs no real transactions.**

```bash
cd pokescout-agent
python3 pokescout.py demo --out runs/demo-001
```

The demo stores a synthetic owned card, compares six synthetic offers, selects A2 + B1 for **¥8,900**, preserves **¥1,100** of a ¥10,000 allowance including a ¥1,000 reserve, and drafts a sale description preserving a disclosed defect. `runs/demo-001/report.json` contains the complete result. Existing nonempty run directories are refused.

## What's in this release

| Capability | Implemented behavior | Verification boundary |
| --- | --- | --- |
| Raw-card research | Original `inspect`, `run`, `replan` commands retained | Historical Mercari evidence bundled; no new live marketplace retrieval claimed for this release |
| Deterministic research decisions | `run` now uses host Python directly | Local tests and mocked provider contract; no generated-code policy in the default path |
| Image intake | Gemini image adapter plus callable agent, structured failures and confirmation status | 10 clean public images live-tested: 9 responses, 6 strict identity matches; missing fields and aliases documented. See [image-agent contract](docs/image-recognition.md) |
| PSA fields | Label schema and comparison with a supplied registry record | Local validation tested; no authenticated PSA API lookup or physical authentication |
| Collection | SQLite persistence, sell protection, optional cost basis, duplicate-image check | Tested across database reopen |
| Structured planner | Exact identity/cohort, quantities, owned copies, total costs, reserve and explicit reservations | Tested on supplied structured observations and synthetic fixtures |
| Selling | Defect-preserving deterministic draft and optional Nosana suggestion | Template tested; live Nosana selling call unverified; publishing disabled |
| Test marketplace | Loopback HTTP offer and order endpoints, durable test orders, idempotency | Actual local HTTP order/retrieval test passes |
| Playwright | Preflight, test purchase, separate order verification, simulated lost-response recovery | Browser worker implemented; local host prevented Chromium launch, so browser end-to-end is unverified |
| Daytona browser | Same fixture worker uploaded to a private sandbox with cleanup request | API contract implemented; no account credentials available for live verification |
| CodeAF | Legacy adapter and snapshot retained for development/reference | Not invoked by default; no new CodeAF generation claimed |
| Monitoring / forecasts / real trading | Detailed in product specification | Not implemented as production features |

Read `docs/PokeScout_Product_Plan.md` for the full design. It is a roadmap, not a claim that all 20 sections are implemented. This README is the capability reference for this release.

## Backend provider and Daytona setup

The current CLI's image-recognition path uses `image_agent.py` and defaults to `gemini-3.5-flash-lite`. The separate `pokescout_agents/` package provides typed Gemini, Nosana, Tavily and decision interfaces for backend integration; its configured Gemini model is `gemini-3.8-flash`. Both models remain configurable, with no automatic provider fallback. The browser runtime lives in `daytona_worker/` and is uploaded by `setup_daytona.py`.

On PowerShell, fill in the ignored `.env` file using `.env.example`, then run:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pokescout_agents.doctor
.\.venv\Scripts\python.exe setup_daytona.py setup
```

Authenticated model checks and small smoke calls passed for the configured Nosana Qwen and Gemini 3.8 models. Tavily's client is mock-tested; no live Search or Extract call is claimed. The Daytona worker passed its local Chromium self-test, while outbound navigation to `example.com` failed under the current sandbox network policy. See [Provider Setup](docs/Provider_Setup.md) for the backend API, tests and limits. These additions do not submit marketplace transactions.

## Installation

The core CLI, persistence, structured planner, template selling and original HTTP research adapters use the Python standard library. Use Python 3.10 or newer.

Optional image dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-vision.txt
```

Optional browser dependencies:

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
```

Use the browser version installed by Playwright. If deliberately testing an existing local Chromium, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to its absolute path. That is an explicit compatibility choice, not a guaranteed substitute. This build environment has a Chromium process/socket restriction; attempts were recorded as failed rather than reported as successful execution.

`.env.example` documents configuration names. The existing CLI does **not** load `.env` automatically; export variables in your shell. The new `pokescout_agents/` package and `setup_daytona.py` load the ignored `.env` file. Never commit credentials.

## 1. Identify and register an owned card

```bash
export GEMINI_API_KEY='your-key'
export GEMINI_MODEL='gemini-3.5-flash-lite'
python pokescout.py identify ./front.jpg ./back.jpg --out runs/card-candidate.json
```

For Python integration, model selection, structured failures and the recorded live sample, see [the image-recognition agent contract](docs/image-recognition.md). The default model is `gemini-3.5-flash-lite`; an explicit `--model` or `GEMINI_MODEL` overrides it. There is no automatic model fallback.

Review the returned `card` object. Correct uncertain fields using the actual image or verified metadata. Clear the corresponding `uncertainties` only when they have been resolved. Required confirmed fields are name, set, number, language, variant and presentation. Use `raw` or `slabbed`; raw cards cannot have a slab grade or certificate.

```bash
python pokescout.py add --record runs/card-candidate.json \
  --confirm-owned --cost-jpy 1200 --allow-sale
python pokescout.py collection
```

Without `--allow-sale`, the copy is protected. Without a cost, its basis remains unknown rather than zero. Importing the same image hash twice is blocked for manual review. Different physical copies require a deliberate separate record; this prototype does not deduplicate arbitrary photos of the same object.

`identify` proposes identity; `add --confirm-owned` records your confirmation. Neither authenticates a physical card.

## 2. Compare a PSA record

```bash
python pokescout.py psa-compare \
  --record examples/v2/psa-card.json \
  --registry-record examples/v2/psa-registry-fixture.json
```

The bundled records are **synthetic**, including certificate `00000000`; they are not a genuine certificate lookup. Replace them with an actual label record and a record obtained through authorized PSA access for a real comparison.

The command reports match, mismatch or incomplete, retains leading zeros, and always says physical authenticity is not established. Missing lookup data does not mean an invalid certificate. This command does not fetch PSA's database.

## 3. Plan from structured observations and collection

```bash
python pokescout.py plan \
  --goal examples/v2/goal.json \
  --listings examples/v2/listings.json \
  --out runs/structured-plan.json
```

Schema examples live in `examples/v2/`. Each target contains a confirmed card object and quantity. Each offer contains its own identity, grading cohort, item price, shipping, purchase fee, availability, condition and quantity. Missing shipping is null; zero means explicitly included/free. Unknown costs cannot enter an executable cost plan.

The planner maximizes requested units and then minimizes total cost. It is bounded to 16 supplied offers and 10 targets for auditable exhaustive search. It considers owned copies from the specified `--db`; `active_reservations_jpy` is an explicit goal input. A production cash ledger is not connected to real accounts.

Changing A2's price from ¥3,600 to ¥3,800 in a copy of the fixture makes A1 + B1 the cheaper complete plan, totaling ¥9,000.

**Input-path distinction:** this newer structured planner includes exact set/language/variant and quantity fields. The original live research path below retains its narrower name/number/rarity schema and one raw card per target. They are not yet one automatically normalized pipeline. Confirm and map evidence before importing observations; do not invent missing identity fields to force a match.

## 4. Original live research path, updated decision engine

First export real sponsor keys and verify the configured Nosana model is served:

```bash
export TAVILY_API_KEY='your-key'
export NOSANA_API_KEY='your-key'
export NOSANA_MODEL='qwen/qwen3.8-27b'
python pokescout.py doctor
python pokescout.py run --request examples/collector.json --max-pages 4 --rounds 1
```

`doctor` checks presence only. Nosana's authenticated model list is checked during a run. The requested model is not silently replaced if unavailable. Tavily discovers/extracts pages, Qwen extracts evidence, and **Python calculates the plan**. No Daytona or CodeAF credential is required for this path in v0.2.

Direct inspection, where platform access is permitted:

```bash
python pokescout.py inspect --request examples/collector.json \
  --follow-related --max-pages 4 https://jp.mercari.com/item/m86771047650
```

Recorded-evidence replanning, without new network access:

```bash
python pokescout.py replan --request examples/collector-played.json \
  --report evidence/mercari-live/report.json
```

Historical listings may be sold, removed or changed. Replanning is labeled not refreshed. Direct inspection does not call the sponsors. Investor mode supports explicitly supplied resale-price scenarios and break-even arithmetic; it does not train or invent a future-price model.

## 5. Draft a sale

Use a copy ID returned by `collection`:

```bash
python pokescout.py sell --copy copy-YOUR-ID --price-jpy 1800 --out runs/sale.json
```

For an additional Qwen wording suggestion:

```bash
python pokescout.py sell --copy copy-YOUR-ID --price-jpy 1800 --nosana --out runs/sale-ai.json
```

The authoritative facts and disclosed defects are always rendered deterministically. The optional model wording is returned separately as `nosana_suggestion_for_review`; it never silently replaces the factual draft. `publish_ready` is false because a human must resolve shipping, photos and unsupported claims. No listing is published.

## 6. Real browser mechanics against synthetic transactions

After installing the browser dependencies:

```bash
python pokescout.py demo --out runs/browser-001 --browser
python pokescout.py demo --out runs/recovery-001 --browser --simulate-lost-response
```

This starts a team-owned loopback test marketplace. Playwright inspects the offer, checks the exact expected fields, places a **test** order and verifies it through a separate order page. A local SQLite ledger tracks reservations and verified results. The lost-response option marks the submitted action unknown and reconciles the persisted result; it is an explicit simulation, not a measured real network outage.

All receipts start with `TEST-`; all cards/prices in this demo are synthetic. If a browser step fails, the command exits 2 and still writes a partial `report.json`. Inspect any fixture action ledger before retrying. No Mercari purchase/listing browser adapter is provided.

For Daytona, build a private snapshot using `Dockerfile.browser` through your supported Daytona image workflow, then set its actual name:

```bash
export DAYTONA_API_KEY='your-key'
export DAYTONA_BROWSER_SNAPSHOT='your-prepared-browser-snapshot'
python pokescout.py browser-demo --out runs/daytona-001 --daytona
```

This consumes your Daytona resources when run. The adapter uploads only the fixture worker and synthetic data, requests an isolated sandbox, runs Playwright there and requests deletion afterward. A cleanup request is not independent confirmation that deletion completed; inspect `daytona_events` and your account if cleanup is unconfirmed.

## Sponsor roles and current evidence

| Partner | Code | Role |
| --- | --- | --- |
| Tavily | `providers.py` | Live search/extraction adapter; used by `run` with credentials |
| Nosana | `providers.py`, `selling.py` | Listing extraction, research explanations, optional selling suggestion |
| Daytona | `daytona_browser.py`, `Dockerfile.browser` | Isolated Playwright test-worker execution |
| CodeAF | Legacy `providers.execute_policy`, `prepare_snapshot.py`, `Dockerfile` | Preserved development/reference integration, not called by default |

The legacy CodeAF generator is retained so earlier tests/evidence remain understandable. Do not present it as a newly verified runtime sponsor integration. The revised architecture uses reviewed Python for financial decisions.

## Tests and evidence

```bash
python -m unittest discover -s tests -v
```

`evidence/release-v0.2/` contains the current local test output, synthetic demo report and browser-blocked report. The older `evidence/mercari-live`, `available-offer`, `played-replan` and `investor-break-even` files are preserved historical captures. Provider mocks test contracts and errors, not cloud availability.

New tests cover exact identity and cohort rejection, unknown shipping, quantities, budget reserve, collection persistence, PSA comparison, defect preservation, duplicate reservations, uncertain outcomes, HTTP test orders and failed Daytona cleanup handling.

## Limits to keep visible

- No automatic live purchases, bids, messages, seller contact or marketplace listing publication.
- No automatic PSA database lookup, physical-authenticity classifier or raw-card grading.
- No continuously running watch service or validated future-price predictor.
- The original release did not live-test providers. The image agent was later live-tested on 10 clean public images using Gemini 3.5 Flash-Lite: 9 responses, 1 provider failure, and 6 strict identity matches. This does not validate real-photo accuracy, PSA recognition or other providers. See [10-card evidence](evidence/image-recognition/public-catalog-10-cards.json).
- The local browser end-to-end path was blocked by this host's Chromium runtime restrictions; the HTTP fixture and ledger are tested separately.
- The new structured planner and original research extractor still have distinct schemas; connecting them without losing evidence is remaining work.
- The full product plan includes features beyond this release. Do not pitch them as completed.

Marketplace readability is not permission for production automation. Establish appropriate access before deploying an adapter. Source pages are untrusted data; the code must not follow instructions found in listing descriptions. PokéScout is an independent prototype, not an official Pokémon, PSA or marketplace product.

## Reference documentation

- [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/genai.html)
- [Nosana inference](https://learn.nosana.com/llm/)
- [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search) and [Extract](https://docs.tavily.com/documentation/api-reference/endpoint/extract)
- [Playwright Python](https://playwright.dev/python/docs/intro)
- [Daytona process execution](https://www.daytona.io/docs/en/process-code-execution/)
- [CodeAF](https://github.com/Agent-Field/CodeAF)
- [PSA certificate verification](https://www.psacard.com/cert)
- [Mercari prohibited-conduct guidance](https://help.jp.mercari.com/guide/articles/900/)
