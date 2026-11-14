# Decision and trading agent contracts v1.0

These are the agreed integration contracts for the decision and trading
workstreams. They supersede the shorter decision/action signatures in
`PokeScout_Product_Plan.md`. They define the intended interface, not a claim that
the existing implementation already supports every operation.

Coordinate changes to `decisions.py` with the decision workstream and changes to
`selling.py`, `collection.py` and execution adapters with their owners. The
research workstream owns `research.py` and `experiments/sc`; consume its report
without editing its implementation in parallel.

## Responsibilities

| Component | Owns |
| --- | --- |
| Research agent | Query planning, retrieval, source inspection, comparable offers and cited findings. |
| Decision agent | User-specific buy, sell or hold recommendations using the goal, collection, cash and research evidence. |
| Trading agent | Sale drafting, action preparation, authorized execution, receipt verification and reconciliation. |
| Python policy/calculation layer | Identity and condition filters, totals, fees, affordability, rankings, reservations and execution limits. |

The main agent orchestrates these interfaces. Research findings are evidence;
they are not permission to trade. A recommendation is not an executed action.

## Decision agent

```python
plan_decision(goal, collection, research_reports) -> Decision
```

### Inputs

- `goal`: goal ID/version, desired action and target cards/quantities, budget,
  reserve, condition or grading requirements, duplicate policy and any explicit
  resale assumptions. Missing material constraints require clarification.
- `collection`: an identified collection snapshot with owned-copy IDs, confirmed
  card identity, condition/defects, ownership state, sale permissions, confirmed
  cash and active reservations. Cash may come from the application's ledger;
  it must not be inferred from the cards' estimated value.
- `research_reports`: one or more `ResearchReport` objects from
  `Research_Agent_Contract.md`, retaining listing evidence, timestamps, coverage,
  uncertainty and failures. A report may be partial.

### Decision output

| Field | Meaning |
| --- | --- |
| `decision_id`, `created_at`, `rule_version` | Identifies this decision and the rules used. |
| `goal_ref`, `collection_ref`, `research_refs` | Identifies the exact input versions or immutable snapshots. |
| `recommendation` | `buy`, `sell`, `sell_then_buy`, `hold`, `insufficient_evidence` or `needs_user_input`. |
| `purchase_candidates` | Selected listing references, requested quantity, observed price and complete acquisition cost where known. |
| `sale_candidates` | Owned-copy IDs, proposed asking prices, evidence or explicit assumptions and required defect disclosures. |
| `calculations` | Currency, confirmed cash, reserve, reservations, available cash, purchase totals and any explicitly labeled resale scenarios. |
| `rejections` | Candidate references and reasons for exclusion. |
| `rationale`, `evidence_refs` | Explanation grounded in supplied sources and explicit assumptions. |
| `requirements` | Missing fields, clarification, freshness checks or completed-sale dependencies needed before action preparation. |

Observation references must remain traceable through the research report. Where
shared IDs are not yet available, retain the report/run reference, source URL
and observation timestamp rather than inventing an ID that cannot be resolved.

### Decision rules

- Compare the same printing, language, variant and raw/graded cohort. Apply the
  user's condition, quantity and ownership constraints.
- Calculate using Python and integer JPY amounts for the hackathon. A currency
  mismatch cannot silently become a currency conversion.
- The research report's item-price ranking is not an affordability decision.
  Include purchase shipping and fees; unknown required costs remain unknown.
- Search snippets and Tavily extraction alone cannot establish current stock.
  Apply an explicit freshness policy and preserve source verification status.
- Partial research may support a recommendation only when its required evidence
  is sufficient; disclose the coverage limits. Otherwise return
  `insufficient_evidence`, rather than presenting missing evidence as `hold`.
- `hold` means the evidence supports taking no action. `needs_user_input` means
  a material goal or preference is unresolved.
- Pending sale proceeds are not spendable cash. `sell_then_buy` records a
  dependency on the sale completing and the funds settling.
- Forecasts are optional inputs with provenance and a horizon. No forecast model
  is required for the hackathon; explicit resale scenarios are not predictions.
- This function returns a decision only. It does not reserve cash, publish a
  listing, contact a seller or submit a purchase.

## Trading agent

```python
draft_sale(owned_copy, confirmed_facts, asking_price) -> SaleDraft
prepare_action(decision_or_draft, policy) -> ActionIntent
execute_action(intent_id) -> ExecutionOutcome
reconcile_action(intent_id) -> ExecutionOutcome
```

### SaleDraft

Contains a draft ID/version, owned-copy ID, currency/asking price, title,
description, required defect disclosures, evidence references and unresolved
fields. The copy must be owned and permitted for sale. Drafting does not publish
anything. Unconfirmed shipping, condition, ownership history or authenticity must
not be invented.

### ActionIntent

| Field | Meaning |
| --- | --- |
| `intent_id`, `idempotency_key` | Stable identifiers for one logical action and its retries/reconciliation. |
| `action`, `execution_mode`, `marketplace` | `buy` or `sell`; explicit `fixture` or `live` mode; the supported adapter. |
| `decision_ref`, `draft_ref`, `evidence_refs` | The recommendation or reviewed draft and evidence being acted on. |
| `listing_ref` or `copy_id` | Exact purchase listing or owned copy to sell, including quantity. |
| `payload`, `payload_hash` | Immutable action details; for a sale, the exact title, description and disclosures. |
| `limits` | Currency, purchase total ceiling or sale price, allowed fees/shipping and other applicable constraints. |
| `created_at`, `expires_at`, `policy_version` | Validity period and governing policy. |
| `authorization_ref`, `reservation_refs`, `state` | Persisted authorization, cash/copy reservations and lifecycle state. |

Preparation validates requirements and persists the intent. It may atomically
reserve the required cash or copy, but makes no external trade. Insufficient
evidence, unknown required costs or unresolved decision requirements prevent a
ready-to-execute intent.

Authorization must come from a trusted user action or an explicit standing
policy, bound to the exact payload and limits. It is never inferred from model
text. Persist authorization before execution. Changing the payload invalidates
its authorization and requires a new or explicitly reauthorized intent.

### ExecutionOutcome

Contains `intent_id`, action/mode/marketplace, `status`, observation time,
actual price and fees where known, receipt/order/listing reference, verification
evidence, error details and any required next step.

| Status | Meaning |
| --- | --- |
| `confirmed` | Independent receipt or resulting listing/order evidence establishes the submitted action. It does not imply a sale has settled. |
| `failed` | The action is known not to have completed; record the reason. |
| `unknown` | Submission may have succeeded; reconcile before any further submission. |
| `needs_user_action` | Login, authorization or another required user step is missing; explain how to proceed. |

### Execution rules

- Load the persisted intent by ID. Recheck authorization, expiry, reservations,
  exact identity, availability and price/fees immediately before submission.
- Enforce purchase ceilings and sale terms in Python. A material change requires
  a new decision/intent or renewed authorization.
- Repeated execution of a confirmed intent returns its recorded outcome. An
  unresolved submitted intent must be reconciled, never blindly resubmitted.
- Persist submission state before losing control of the request. A timeout after
  submission is `unknown`, not proof of failure.
- Reconciliation inspects the existing action; it does not create a new trade.
  Retain reservations while the outcome is unresolved. Release them only when
  safe, or convert them to the appropriate confirmed ledger event.
- A confirmed purchase can update ownership. Publishing a sell listing does not
  remove ownership or create settled cash; record those later lifecycle events
  separately.
- Always distinguish fixture receipts from real marketplace outcomes. Unsupported
  live adapters must report that limitation rather than simulate live success.

## Current implementation and hackathon integration

- `decisions.py::plan(goal, listings, copies)` is the existing deterministic
  collector purchase planner. Preserve it; adapt compatible research observations
  to its structured listing schema before adding other recommendation types.
- `selling.py::draft(copy, price, model=None)` creates a sale draft; it does not
  publish a listing. The interface above is the target contract, not its current
  Python signature.
- `fixture_market.py` and the collection ledger implement test-marketplace
  purchases and reconciliation behavior. They do not establish a live trading
  integration.
- First connect one research report to a purchase recommendation and the existing
  fixture execution. Keep sell/hold recommendations, live execution and forecasting
  explicitly limited to what has actually been implemented and tested.
- This document does not register AgentField endpoints. Their names, payload
  envelopes and runnable implementations must be agreed with the owning
  workstreams before the main agent invokes them.
