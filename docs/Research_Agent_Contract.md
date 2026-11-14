# Research agent contract v1.0

**Reserved owner:** research-agent workstream. Coordinate changes to `research.py`
with this workstream. The implementation remains in `experiments/sc`; do not wire
it into `pokescout.py::full_run()` or replace `providers.py` in parallel.

Approved interface: `research_market(request: ResearchRequest) -> ResearchReport`.
Canonical Python/JSON field names are defined in the root `research.py`.

The research agent owns query planning/refinement, source inspection, comparison
and evidence-backed synthesis. Python owns arithmetic and rankings. The decision
agent retains all budget/collection-based buy/sell decisions.

## Invoke from Python

```python
from research import ResearchRequest, research_market

request: ResearchRequest = {
    "confirmed_target": {"name_ja": "ミュウツーV", "number": "273/S-P"},
    "currency": "JPY",
    "raw_only": True,
    "price_basis": "item_price",
    "source_limits": {
        "allowed_domains": ["jp.mercari.com", "item.fril.jp"],
        "max_searches": 3,
        "max_inspections": 4,
        "results_per_search": 5,
        "timeout_seconds": 120,
    },
}
report = await research_market(request)
```

`RESEARCH_AGENT_URL` defaults to `http://127.0.0.1:18081`. The async adapter uses
only Python's standard library. The main application does not need AgentField
or Gemini packages just to call the service. This commit reserves the contract;
the expanded endpoint is being implemented/live-tested next. Existing
`/reasoners/research` is the earlier, smaller experiment and is not this contract.

## AgentField invocation

Direct: `POST http://localhost:18081/reasoners/research_market`

Control plane: `POST http://localhost:18080/api/v1/execute/pokescout-research.research_market`

Both accept `{"input": {"request": <ResearchRequest>}}`.
Start instructions and verification evidence will be maintained in
`experiments/sc/RESEARCH.md` after the live test.

## Report semantics

- `listings`: observed listings, including rejected/noncomparable observations.
- `comparable_offers`: qualifying observations, sorted by Python using the
  requested price basis. Currency and condition filters remain explicit.
- `lowest_qualifying_observed_price`: the first qualifying observed offer, or
  null. This is bounded by the searched sources, never a global-cheapest claim.
- `findings`: synthesis with source URLs; `uncertainty` and
  `suggested_next_steps` explain what is missing or should be checked next.
- `coverage`: actual queries, source coverage and budget usage.
- `failures`: explicit search, extraction, inspection, model or validation failures.
- Every observation preserves its URL, observation time and source evidence.

`indexed_snippet` means indexed search text. `extracted_page` means Tavily
extraction, which does not prove live stock. `verified_listing` requires a
successful direct source inspection supporting the primary listing's identity,
price/currency and availability at the recorded time. It never means authenticity
verification or guaranteed checkout availability. Unknown fields stay unknown.
Unknown/sold stock, unsupported identity/price, excluded condition or unknown
shipping for a delivered-price comparison must not qualify.

Defaults are Mercari/Rakuma, JPY, raw cards, any condition unless explicitly
filtered, item-price comparison, three searches and four inspections. Runtime
validation must cap caller budgets and enforce marketplace URL allowlists after
search as well as before inspection. Partial results and timeouts must retain
observations and failures rather than manufacture success.
