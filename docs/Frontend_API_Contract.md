# Frontend application API contract v1.0

**Status: agreed target for implementation, not an existing HTTP server.**
The application backend owns this contract. AgentField endpoints are internal
services; the browser does not call them or supply provider API keys. Existing
CLI commands remain supported while the HTTP adapter is implemented.

Related contracts: [research](Research_Agent_Contract.md),
[decision/trading](Decision_Trading_Agent_Contract.md),
[storage](Storage_Contract.md), and [image recognition](image-recognition.md).
Live progress uses the shared [central activity/SSE contract](Activity_Stream_Contract.md).
This document replaces the product plan's illustrative frontend endpoint list.
It does not rename the existing agents' Python arguments or wire envelopes.

## Transport and common types

- Base path: `/api/v1`. JSON is UTF-8; field names are snake_case. Every JSON
  response includes `api_version: "1.0"` and `request_id`.
- Success body: `{api_version, request_id, data}`. Errors use the envelope below.
  Unknown request fields are rejected with 422, except explicitly documented
  free-form maps. Optional fields may be omitted; nullable fields accept null.
- IDs are opaque strings, scoped to the current user. Dates are UTC ISO-8601.
  JPY amounts are integers from 0 to 100000000; null means unknown. Never use
  zero to replace missing shipping, fees, cost basis or cash.
- `Card` uses the exact fields and validation in `cards.py`: `name`, `set`,
  `number`, `language`, `variant`, `year`, `brand`, `presentation`, `grader`,
  `grade`, `grade_text`, `cert_number`, `qualifiers`, `defects`, `uncertainties`.
  All scalar fields are string-or-null; the last three are string arrays.
  `presentation` is `raw`, `slabbed` or null. Certificate numbers stay strings.
- Candidate cards may have unknown fields. A confirmed card must pass
  `validate_card(card, confirmed=True)`. Model extraction is never confirmation.
- JSON requests are limited to 1 MiB. Multipart image limits are specified below.
- Lists return `{items: [...], next_cursor: string|null}`. `limit` defaults to 20,
  maximum 100; `cursor` is opaque. Results are ordered by creation time then ID.
  No total-count promise is required for the hackathon.

### Sessions and request ownership

Serve frontend and API on the same origin for the hackathon. Session identity is
server-assigned through an HttpOnly, SameSite=Lax cookie (Secure under HTTPS).
Mutations require `X-CSRF-Token` obtained from `GET /session`; never accept a
client-supplied `user_id`. Production/remote access requires authentication.
A loopback-only single-user demo may use a server-created local session without
an account login; advertise that mode explicitly in capabilities.

Every job, upload, candidate, copy, report, draft, intent and evidence read/write
checks ownership. Return 404 for inaccessible record IDs. A separately hosted
development frontend requires an explicit origin allowlist with credentialed
CORS, never a wildcard. Neither CSRF nor CORS substitutes for ownership checks.

### Idempotency and concurrent edits

All POST endpoints require `Idempotency-Key` (client-generated UUID). Scope by
user, method and route. Persist the canonical request hash and response: the same
key/body returns the same resource/job; a different body returns 409
`IDEMPOTENCY_CONFLICT`. For uploads, hash ordered roles and file-byte hashes, not
the multipart boundary. Persist pending requests before enqueueing provider work.

Keep ordinary keys for at least 24 hours; retain action keys with their intents
for the full lifecycle. After key expiry a new request can be a new operation,
so business invariants still prevent duplicate candidate-to-copy confirmation,
duplicate live listings and resubmission of an unresolved intent.

Mutable jobs/copies/intents expose integer `revision` and `ETag: "<revision>"`.
Answers, cancellation, authorization and execution require `If-Match` for the
resource's current revision. Missing preconditions return 428; stale revisions
return 412. Check revisions and update state atomically. Idempotent replay is
recognized before rejecting an old revision from that same completed request.

## Endpoint inventory

| Method/path (under `/api/v1`) | Response and purpose |
| --- | --- |
| `GET /session` | 200: `{csrf_token, mode}`; mode `local_single_user` or `authenticated`. |
| `GET /capabilities` | 200: feature flags and limits; unsupported operations remain disabled. |
| `POST /uploads` | 201: one saved physical-card image group; no AI call. |
| `GET /uploads/{id}` | 200: saved group metadata and authorized image URLs. |
| `POST /jobs` | 202: create an identification, research or decision job. |
| `GET /jobs/{id}` | 200: durable job state, questions, progress and result references. |
| `GET /activity/stream` | SSE: shared activity timeline for the current session; optional job filter and replay cursor. |
| `GET /activity` | 200: paginated activity history. See Activity_Stream_Contract.md. |
| `POST /research-reports/{id}/listing-confirmations` | 201: immutable explicit facts for one exact observed listing; enables compatible verified evidence to enter planning. |
| `POST /jobs/{id}/answers` | 202: validate answers and resume that job. |
| `POST /jobs/{id}/cancel` | 202: record a cancellation request. |
| `GET /candidates/{id}` | 200: one unconfirmed identity candidate. |
| `POST /copies/confirm` | 201: confirm possession and register a physical copy. |
| `GET /collection` | 200: paginated owned-copy records. |
| `GET /copies/{id}` | 200: a copy with revision, condition evidence and images. |
| `GET /research-reports/{run_id}` | 200: frontend-safe research report. |
| `GET /decisions/{id}` | 200: persisted decision and referenced evidence. |
| `POST /sale-drafts` | 202: job preparing a sale draft, without publication. |
| `GET /sale-drafts/{id}` | 200: immutable draft revision. |
| `POST /action-intents` | 201: prepare an immutable buy/sell action. |
| `GET /action-intents/{id}` | 200: current intent, exact payload and outcome. |
| `POST /action-intents/{id}/authorize` | 200: persist authorization for the exact payload. |
| `POST /action-intents/{id}/execute` | 202: execute through a durable job. |
| `POST /action-intents/{id}/reconcile` | 202: inspect a submitted action through a durable job. |
| `GET /assets/{id}` | 200: authorized image bytes; content type matches saved format. |
| `GET /evidence/{id}` | 200: authorized evidence text and source/hash/time metadata. |

`GET /capabilities` data is `{features, limits}`. Feature keys are `identify`,
`research`, `decision`, `sale_draft`, `fixture_execution`, `live_execution`,
`watch`, `forecast`, `activity_stream`; values are booleans. Keep `activity_stream`
false unless the central SSE/replay implementation is mounted. The application
now mounts it through `backend/activity.py` and advertises `activity_stream=true`.
Detailed agent steps are currently integrated for the research AgentField node;
all application jobs publish lifecycle events.
`limits` contains `max_views_per_upload`,
`max_upload_bytes`, `max_active_jobs`, `job_timeout_seconds`. Disabled mutations
return 409 `FEATURE_UNAVAILABLE`, not a fabricated success. A missing provider
configuration disables the affected feature. No credential values are returned.

## 1. Upload and identify

`POST /uploads` is multipart/form-data. Repeated `files` parts contain JPEG, PNG
or WebP bytes. A `manifest` part is a JSON array matching their order, with one
`{role}` entry per file. Roles: `front`, `back`, `slab_label`, `detail`, `other`.
Accept 1–4 views of **one physical card**, at most 12000000 combined bytes and
25000000 decoded pixels per image. Validate actual bytes, not just extensions.
Do not accept local paths, remote URLs or base64 images in this endpoint.

Response data is `{upload_id, created_at, images}`. Each image contains
`asset_id`, `role`, `sha256`, `mime_type`, `size_bytes`, `url`. Save original bytes
before responding. Three separate cards mean three upload groups, followed by
three identification jobs. Multiple-card photographs are unsupported; request
separate views rather than pretending the model can reliably segment them.

Create an identification job:

```json
{
  "type": "identify",
  "input": {"upload_id": "upload_demo_1"}
}
```

For each group the backend calls `image_agent.recognize_card()` once, subject to
its retry budget, then persists a candidate. The job result references that
candidate even if its identity is incomplete. A successful extraction may finish
the identification job with `requires_confirmation: true`; it does not add an
owned copy. An unreadable or failed extraction is an explicit failed job.

Candidate data is `{candidate_id, upload_id, card, candidate_complete,
missing_fields, requires_confirmation, observed_at, provider, configured_model,
returned_model}`. `requires_confirmation` is always true for model candidates.
Preserve the image agent's uncertainty and provenance; omit secrets and raw
provider exception bodies. Corrections are submitted through copy confirmation
or a research/decision job's typed clarification flow, not by mutating history.

## 2. Confirm possession and collection

`POST /copies/confirm` body:

```json
{
  "candidate_id": "candidate_demo_1",
  "confirmed_card": {
    "name": "Fixture card A",
    "set": "SYNTHETIC DEMO SET",
    "number": "001/099",
    "language": "Japanese",
    "variant": "AR",
    "year": null,
    "brand": null,
    "presentation": "raw",
    "grader": null,
    "grade": null,
    "grade_text": null,
    "cert_number": null,
    "qualifiers": [],
    "defects": [],
    "uncertainties": []
  },
  "confirm_owned": true,
  "allow_sale": false,
  "cost_jpy": null
}
```

`candidate_id`, `confirmed_card`, `confirm_owned` are required; the other fields
default as shown. This fixture is not a real observed card. Validate the full
confirmed card, candidate ownership and duplicate registration. Persist original
candidate plus corrections for provenance. `confirm_owned` must be true; merely
uploading or researching a card cannot establish ownership.

Return a `Copy`: existing collection fields `id`, `card`, `image_hash`, `cost`,
`allow_sale`, `state`, `created`, plus API additions `revision`, `images`,
`condition_record`. Map `cost_jpy` to existing `cost`; do not rename persisted
columns implicitly. `condition_record` follows the storage contract or is null.
Empty defect arrays do not prove near-mint condition. Reject a second confirmation
of the same candidate with 409 `ALREADY_CONFIRMED`; replaying its idempotency key
returns the original copy.

## 3. Research and decision jobs

`POST /jobs` accepts exactly `{type, input}`. Client types are `identify`,
`research`, `decision`; other job types are created by the specific endpoints.
There is no arbitrary tool name or provider/model selection in browser input.

Research input is `{request: ResearchRequest}` using the root `research.py`
schema and runtime-enforced source limits. Example:

```json
{
  "type": "research",
  "input": {
    "request": {
      "confirmed_target": {"name_ja": "ミュウツーV", "number": "273/S-P"},
      "currency": "JPY",
      "raw_only": true,
      "price_basis": "item_price",
      "source_limits": {
        "allowed_domains": ["jp.mercari.com", "item.fril.jp"],
        "max_searches": 2,
        "max_inspections": 4,
        "results_per_search": 5,
        "timeout_seconds": 120
      }
    }
  }
}
```

An explicit submitted target represents the user's chosen research target, not
possession or proof of every printing field. Research may proceed with its
narrower name/number contract; decision matching must not invent missing fields.

Decision input is `{goal, research_run_ids}`. `research_run_ids` is an optional
array, maximum 10, default empty. An empty array asks the orchestrator to perform
bounded research first. Supplied IDs must refer to owned, compatible reports;
their presence cannot bypass freshness or matching checks. `goal` fields:

| Field | Type/default |
| --- | --- |
| `message` | Required user text, 1–4000 characters. |
| `intent` | Required `buy`, `sell` or `assess`. |
| `targets` | Array of `{card: Card, quantity: integer 1..10}`, maximum 10; default empty. |
| `copy_ids` | Owned-copy ID array, maximum 10; default empty. |
| `budget_jpy` | Integer JPY or null; default null. |
| `reserve_jpy` | Integer JPY; default 0. |
| `allowed_conditions` | Array drawn from `near_mint`, `unused`, `played`, `damaged`, or null; default null (ask, do not assume). |
| `avoid_owned` | Boolean; default true. |
| `allow_defects` | Boolean; default false. |

Structured fields take precedence over an inferred interpretation of `message`;
ask if materially contradictory. Missing identity, condition, buy budget or sale
copy permission creates typed questions before a dependent decision. Research
alone need not require a budget. A goal is not trade authorization.

### Research-to-decision adapter: mandatory boundary

The backend resolves the full confirmed printing and selected collection/cash
snapshot, then invokes the decision contract. It must:

1. Preserve run/listing/time/evidence references. Map a listing into the current
   planner's `{id, card, price_jpy, shipping_jpy, purchase_fee_jpy, condition,
   availability, quantity, source_url, observed_at}` only when supported.
2. Establish `name`, `set`, `number`, `language`, `variant` and raw/slabbed cohort.
   A research target's name/number is not evidence for the other fields.
3. Accept only valid integral JPY values; unknown shipping/fees remain null.
   Normalize shop conditions only under the storage contract's explicit rules.
4. Retain unverified or mismatching observations as excluded evidence. Do not
   convert research `qualifies: true` into purchase eligibility automatically.
5. Keep report completeness, affordability and execution readiness distinct.
   Missing evidence yields `insufficient_evidence` or clarification, not invented
   values. Python owns filtering, total-cost calculation and rankings.

## 4. Durable job protocol

All job-creating endpoints return 202, `Location: /api/v1/jobs/<id>`,
`Retry-After: 2`, and the complete `Job` in the success envelope. Subscribe to
`GET /activity/stream` for live progress using Activity_Stream_Contract.md.
The central SSE service is the target transport; its implementation status must
be advertised accurately. If unavailable, poll `GET /jobs/{id}` every two seconds
while active; stop polling on a terminal state. Job snapshots remain authoritative.

`Job` always contains:

| Field | Type/meaning |
| --- | --- |
| `job_id`, `type` | ID; type `identify`, `research`, `decision`, `sale_draft`, `execute` or `reconcile`. |
| `state` | State from the list below. |
| `revision` | Integer, increments on persisted state/question/result changes. |
| `created_at`, `updated_at` | UTC timestamps. |
| `stage` | `queued`, `identify`, `clarify`, `research`, `decision`, `draft`, `preflight`, `execute`, `reconcile` or `finished`. |
| `progress` | `{message: string, completed_steps: integer, total_steps: integer|null}`; no invented percentage. |
| `questions` | Pending `Question` array, otherwise empty. |
| `result` | A typed result reference or null. |
| `error` | Error object or null. |

States are `queued`, `running`, `awaiting_input`, `cancel_requested`, `succeeded`,
`failed`, `cancelled`. The last three are terminal. Transitions:

```text
queued -> running -> succeeded | failed
running -> awaiting_input -> queued
queued | running | awaiting_input -> cancel_requested -> cancelled
```

If work completes before cancellation takes effect, its real terminal outcome
wins. Persist the job before returning 202. Disconnecting or refreshing the
browser does not cancel it. Limit to two active jobs per session. A configured
worker recovers queued jobs after restart. A running job that cannot safely
resume becomes failed with `JOB_INTERRUPTED`, retaining partial artifacts and
references; it is not silently rerun. Submitted actions require reconciliation.

Default active-work deadline is 180 seconds; queue/input waiting does not consume
it. Nested source/provider budgets stay within that deadline. Input waiting
expires after 24 hours with `INPUT_EXPIRED`. Bounds include attempts, not just
successful provider calls. These deadlines are implementation requirements, not
claims about the experimental SDK's current bookkeeping latency.

Result is exactly `{kind, id, url}`. Kinds: `candidate`, `research_report`,
`decision`, `sale_draft`, `action_intent`. `url` is an owned application API path,
not a local filesystem path or raw AgentField URL. Failed/cancelled jobs may
retain a partial result reference. A research job can be `succeeded` while its
report status is `partial` or `failed`: the report was produced successfully,
but evidence collection was incomplete. Transport success is not a buy signal.

### Clarification and resume

`Question` is `{question_id, prompt, field, value_type, choices, required}`.
`field` is a backend-generated allowed input path, `value_type` is `string`,
`integer`, `boolean`, `card` or `condition_list`; `choices` is a string array or
null. `question_id` is unique to the current pending question set.

`POST /jobs/{id}/answers` body is
`{answers: [{question_id, value}]}`. The backend validates values against the
persisted question types/choices, applies them to the goal/candidate snapshot,
and resumes from the dependent step. The frontend cannot submit arbitrary JSON
patch paths or tool instructions. Missing required answers return 422; answers
to a stale question set or a nonwaiting job return 409. Answers may create a new
question set if ambiguity remains; they never confirm possession or authorize a
trade implicitly. Resume retains completed evidence and does not redo external
actions. `POST /jobs/{id}/cancel` takes `{}` and never reverses a submitted trade;
return 409 `ACTION_ALREADY_SUBMITTED` and the intent reference when applicable.

## 5. Drafts, authorization and execution

`POST /sale-drafts` takes `{copy_id, asking_price_jpy, decision_id?}`. Validate
ownership and `allow_sale`; return a job. The resulting `SaleDraft` follows the
decision/trading contract with immutable ID/revision, required defects and
unresolved fields. Editing a draft means creating a new draft request/revision;
it does not alter an authorized intent.

`POST /action-intents` takes:

```json
{
  "action": "buy",
  "execution_mode": "fixture",
  "decision_id": "decision_demo_1",
  "listing_id": "listing_demo_1",
  "draft_id": null,
  "max_total_jpy": 4500
}
```

For buy, `decision_id`, selected purchase `listing_id`, and `max_total_jpy`
are required; `draft_id` is null. `listing_id` is scoped to the persisted
decision's research observation, not an image-recognition candidate. For sell, `draft_id`
is required and `max_total_jpy` is null; `decision_id` may be null for a direct
user-requested draft and `listing_id` is null. Always require `action` and
`execution_mode` (`fixture` or `live`). For v1, live execution is unavailable
unless capabilities explicitly enable a verified adapter.

The server resolves all item/copy/quantity/marketplace/payload fields from the
owned persisted decision or draft; the browser cannot replace its listing URL,
condition or description in the prepare call. An application policy supplies
fees/shipping limits. Unknown required costs or incomplete decisions block
preparation. Return `ActionIntent` with the fields in the trading contract plus
`revision`, `payload_hash`, `expires_at`, `authorization_state` and `outcome`.
Authorization state is `required`, `authorized`, `expired` or `revoked`.

`POST /action-intents/{id}/authorize` takes
`{payload_hash: string, confirm: true}`. Show the exact action, item, quantity,
price/total ceiling, mode and disclosures before asking for this submission.
Only explicit user interaction or a separately implemented standing policy may
authorize. Bind authorization to the persisted payload hash, limits and expiry.
Default intent/authorization lifetime is five minutes from preparation; record
the actual expiry. Stale evidence can expire sooner. Changes require a new
intent and authorization. `allow_sale` alone is not this authorization.

`POST /action-intents/{id}/execute` and `/reconcile` take `{}`. Execution requires
unexpired authorization, matching revision, current reservations and fresh
preflight checks. Repeated requests for the same intent return its existing job
or outcome; an unknown submitted outcome routes to reconciliation, never a new
submission. Reconciliation can continue after authorization/intent expiry because
it inspects an already submitted action, rather than authorizing another one.

The intent's `outcome` is null before an outcome exists, otherwise
`{status, observed_at, receipt_ref, actual_total_jpy, evidence_refs, error,
next_step}`. `status` is `confirmed`, `failed`, `unknown` or `needs_user_action`.
A job finishing does not imply that its action was confirmed. A lost submission
response must persist `unknown` and return a reconcile path. Fixture evidence
must remain labeled fixture; no live receipt or settled cash may be fabricated.

## Frontend result presentation

- `GET /research-reports/{run_id}` returns the research contract with internal
  `log_path` omitted. Each evidence item gains an authorized `evidence_url` and
  retains source URL/time/hash/content and verification status. Keep provider
  failures safe and preserve research `status`, coverage and uncertainty.
- Decisions and drafts use their respective contracts, with application URLs
  for referenced records. Collection records preserve protected/sale-permitted
  state and raw condition wording. Never display `unknown` as near mint.
- Execution views must distinguish order confirmation, physical receipt, sale
  publication and settled proceeds. See the storage lifecycle; these are not
  interchangeable events.
- Never return API keys, session/browser credentials, absolute local paths,
  stack traces or unrestricted raw provider logs in frontend responses.

## Errors

```json
{
  "api_version": "1.0",
  "request_id": "request_demo_1",
  "error": {
    "code": "IDENTITY_INCOMPLETE",
    "message": "Confirm the set and variant before making a purchase decision.",
    "retryable": false,
    "field_errors": [
      {"field": "input.goal.targets.0.card.set", "message": "Required for exact printing matching."}
    ],
    "resource_url": null
  }
}
```

`error` always contains the five fields shown. `field_errors` may be empty;
`resource_url` may link to the relevant job/intent or be null. Job/outcome errors
use the same error object. No auto-retry of a mutation without its original
idempotency key. A provider error after enqueueing appears on the job, not as a
retroactive change to the original 202 response.

| HTTP status | Codes / meaning |
| --- | --- |
| 400 | `INVALID_JSON` or malformed multipart manifest. |
| 401 / 403 | `AUTH_REQUIRED` / `CSRF_INVALID`. |
| 404 | `NOT_FOUND`, including records not owned by the session. |
| 409 | `IDEMPOTENCY_CONFLICT`, `ALREADY_CONFIRMED`, `INVALID_STATE`, `FEATURE_UNAVAILABLE`, `ACTION_ALREADY_SUBMITTED`, `AUTHORIZATION_REQUIRED`, `INTENT_EXPIRED`, `EVIDENCE_STALE`. |
| 412 / 428 | `REVISION_CONFLICT` / `PRECONDITION_REQUIRED`. |
| 413 / 415 | `UPLOAD_TOO_LARGE` or `BODY_TOO_LARGE` / `UNSUPPORTED_MEDIA_TYPE`. |
| 422 | `INVALID_INPUT`, `IDENTITY_INCOMPLETE`, `CONDITION_UNRESOLVED`, `COST_UNKNOWN`; precise field errors. |
| 429 | `JOB_LIMIT_REACHED`; return Retry-After. |
| 503 | `SERVICE_UNAVAILABLE` before job acceptance. |
| 500 | `INTERNAL_ERROR`; safe message and request ID for diagnosis. |

Additional job-only codes: `PROVIDER_UNAVAILABLE`, `PROVIDER_RATE_LIMITED`,
`JOB_TIMEOUT`, `INPUT_EXPIRED`, `JOB_INTERRUPTED`, `IDENTIFICATION_FAILED`.
Preserve image-agent failure codes in a safe nested cause if needed; do not
expose its raw provider body. A retryable error still requires the orchestrator's
attempt/time budget. Cancellation or resumption is not permission to duplicate
an external trade.

## Minimum implementation acceptance checks

1. Upload two groups with front/back views; identify each independently and
   confirm only one as owned. Refreshing the browser retains jobs/candidates.
2. Replaying copy confirmation returns the same copy; new keys cannot register
   that candidate twice. Ownership/condition are never inferred from upload.
3. Start research, poll to its report, then adapt it into a decision. Unknown
   identity/cost/condition stays excluded or creates clarification.
4. Answer a clarification and resume the same job. Reject stale revisions;
   retain completed evidence and do not repeat submitted actions.
5. A partial research report is displayed as partial even if the job succeeded.
6. Prepare, authorize and execute one fixture intent; verify its receipt.
   Replaying execute creates no second order. A lost response remains unknown
   until reconciliation. Expiry prevents new submission, not reconciliation.
7. Another session cannot read IDs, images or evidence. Provider keys and absolute
   filesystem paths do not appear in any frontend response.

No routes or storage migrations are implemented by this document. The first
backend slice is session/capabilities, uploads, durable identify/research/decision
jobs, copy confirmation and collection reads. Add draft/fixture-action endpoints
next. Keep `live_execution`, `watch` and `forecast` disabled until implemented.


## Listing observation confirmation

POST `/api/v1/research-reports/{run_id}/listing-confirmations` uses the normal
session, CSRF and UUID idempotency headers. Body:

```json
{
  "listing_id": "s1",
  "observed_at": "<exact timestamp from the report observation>",
  "confirmed": true,
  "facts": {
    "card": "<complete confirmed Card object, not a string>",
    "quantity": 1,
    "purchase_fee_jpy": 0,
    "condition_record": null,
    "registry_status": "not_checked"
  }
}
```

The response contains the immutable fact record. Changed facts for the same
observation return 409; another session's report returns 404. If normalizing
condition wording, supply `{condition_raw, condition_normalized,
condition_basis: "user_confirmed"}`; retain the report's exact raw wording.
A confirmation does not change source verification, price, availability or
observation time. Snippets, stale evidence and unknown acquisition costs remain
ineligible. Create a new decision job with `research_run_ids: [run_id]` after
confirmation. Purchase execution remains disabled for live marketplaces.
