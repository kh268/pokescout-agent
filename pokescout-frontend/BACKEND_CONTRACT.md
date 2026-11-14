# Proposed backend adapter contract

**Design target only. These routes are not implemented by this ZIP.** Inspect the existing Python project before implementing or adapting names and fields.

Use a same-origin authenticated API or a development proxy with narrowly configured CORS. Keep provider credentials in Python/server configuration.

## Endpoint mapping

| Operation | Suggested endpoint | Purpose |
|---|---|---|
| Normalize request | POST `/api/requests/normalize` | Convert user goal to editable constraints |
| Start run | POST `/api/runs` | Return run ID; execute configured orchestration |
| Stream events | GET `/api/runs/{id}/events` | SSE with replay from last sequence |
| Run snapshot | GET `/api/runs/{id}` | Current status, observations and plan |
| Cancel | POST `/api/runs/{id}/cancel` | Stop pending work; preserve completed evidence |
| Replan | POST `/api/plans/{id}/recalculate` | New plan from saved observations and revised limits |
| Refresh | POST `/api/plans/{id}/refresh` | Fetch new observations, then make a new plan |
| Image intake | POST `/api/cards/identify` | Multipart photo(s), extraction candidate, uncertainties |
| Collection | GET/POST `/api/collection` | User-confirmed physical inventory |
| Draft | POST `/api/collection/{copy_id}/selling-draft` | Title/description from verified fields and defects |
| Connections | GET `/api/connections` | Configured/healthy/error; never expose keys |
| Watchlist | GET/POST `/api/watchlist` | Persistent targets and alert thresholds |
| Export | GET `/api/runs/{id}/export` | Mode, constraints, observations, evidence and plan |

A response that has insufficient evidence must return a state such as `needs_review`, `partial` or `unavailable`, not invent missing values.

## Example normalized intent

```json
{
  "mode": "collector",
  "currency": "JPY",
  "budget_jpy": 10000,
  "reserve_jpy": 1000,
  "targets": [{
    "name": "Pikachu",
    "set": "sv2a",
    "number": "173/165",
    "language": "ja",
    "variant": "AR",
    "presentation": "raw",
    "quantity": 1
  }],
  "exclude_owned": true,
  "execution_mode": "research_only"
}
```

## Observation record

Use stable observation IDs and timestamps. Preserve raw/source facts separately from interpretation.

```json
{
  "observation_id": "obs_example",
  "mode": "live",
  "source": "mercari",
  "url": "<actual retrieved listing URL>",
  "observed_at": "<ISO timestamp>",
  "identity": {"set":"sv2a","number":"173/165","language":"ja","variant":"AR","presentation":"raw"},
  "price_jpy": 4200,
  "shipping_jpy": null,
  "currency": "JPY",
  "availability": "unknown",
  "condition_source": "seller_stated",
  "defects": [],
  "missing_fields": ["shipping_jpy"],
  "eligibility": "needs_review",
  "reasons": ["Shipping cost unknown"]
}
```

The example amounts illustrate the schema and are not market facts. Do not send this example as a live observation.

## Event shape

```json
{
  "run_id": "run_example",
  "seq": 12,
  "event_id": "run_example:12",
  "at": "<ISO timestamp>",
  "component": "listing_analysis",
  "tool": "nosana.extract_listing",
  "call_id": "call_example",
  "status": "completed",
  "mode": "live",
  "summary": "Extracted seller-stated condition; shipping still unknown.",
  "observation_ids": ["obs_example"],
  "duration_ms": 820,
  "error": null
}
```

- `mode`: `live`, `recorded`, `synthetic`, or `local`; do not infer from success alone.
- `status`: `queued`, `running`, `completed`, `needs_input`, `failed`, `cancelled`.
- Send sanitized inputs/results suitable for display, not secrets or private model reasoning.
- SSE envelope can use `id: run_example:12`, `event: tool_event`, and a JSON `data:` line.
- Resume via Last-Event-ID or an explicit sequence query. Deduplicate on stable event ID.
- Run status: `ready`, `running`, `needs_input`, `completed`, `partial`, `failed`, `cancelling`, `cancelled`.

## Plan response

Return `plan_id`, `version`, `run_id`, `mode`, `created_at`, the exact constraints used, selected observation IDs, rejected offers with reasons, total cost, spendable limit, reserve, cash remaining, missing targets and evidence timestamps. Only include scenario profit when the resale assumption and all included costs are explicitly attached.

The frontend must distinguish:

- Recalculate: same observations, new constraints.
- Refresh: new retrieval, new observation timestamps.
- Save: immutable plan/rules version, not an implied reservation or purchase.

## Transaction boundary

Real marketplace execution is a separate backend operation. It needs an explicit reviewed intent, permitted account/session handling, price/availability recheck, budget reservation, an idempotency key, a durable result record and reconciliation after ambiguous failures. Do not automatically retry an uncertain purchase.

This export includes no production marketplace transaction endpoint.
