# Main agent

`main_agent.MainAgent` implements the main orchestration role in the product
diagram as a local, single-user Python application. It uses the existing image,
research, deterministic decision, sale-draft and test-marketplace modules.
Python 3.11 or newer is required. The separate `backend/` application owns the
team's HTTP API; this CLI does not register additional HTTP routes.

Run the reproducible synthetic workflow in a new directory:

```bash
python pokescout.py agent --data-dir runs/main-agent-demo demo
```

This imports explicitly synthetic research, selects A2 + B1 for ¥8,900, prepares
a defect-preserving sale draft, and authorizes and independently verifies one
synthetic HTTP test order. It calls no cloud providers and makes no live trades.
The order remains pending physical receipt; it does not add an owned copy.
The existing Playwright/Daytona commands remain separate browser demos.

Check capabilities or inspect durable work:

```bash
python pokescout.py agent capabilities
python pokescout.py agent jobs
python pokescout.py agent show job JOB_ID
python pokescout.py agent --help
```

`agent submit --request FILE --key KEY [--run]` accepts `{type, input}` for
`identify`, `research` and `decision` jobs. Inputs follow the corresponding
frontend contract shapes. `agent work` processes the persisted queue. Jobs
missing material goal fields wait for typed answers; `agent answers` requires
the current revision and the pending question IDs. `recover` marks interrupted
running jobs failed and retains their artifacts; run it only after stopping
the previous worker. Queued jobs can be processed after restarting.

For identification, first use `agent upload IMAGES --key KEY`, then submit
`{type: "identify", input: {upload_id: "..."}}` as valid JSON. Retained original
images are validated and hashed. Identification creates a candidate; the
separate `confirm-copy` command requires explicit possession confirmation and a
fully confirmed card. Replaying confirmation returns the same physical copy.

Research uses the existing `research.research_market()` client. Set
`RESEARCH_AGENT_URL` explicitly and start the contracted research service before
requesting fresh research. Missing configuration or failures do not trigger a
synthetic fallback. `import-research FILE --mode replay` imports an existing
report without rewriting historical evidence; fixture imports require the
explicit fixture mode. Reports, source text, hashes and original timestamps
are retained under the application data directory.

Research v1 lacks full printing identity, quantity, purchase fees and disclosed
defects. `confirm-listing --file FILE` accepts `{run_id, listing_id, observed_at,
facts, confirmed: true}`. `facts` contains `card`, `quantity`, nullable
`purchase_fee_jpy`, and optional `condition_record` and `registry_status`. This
explicit confirmation is bound to one observation, never inferred from the
search target. Shop letter conditions require an explicit user-confirmed
normalization. Missing facts, unknown costs, stale/unverified evidence, sold
offers and incompatible cohorts remain excluded. Real evidence uses a
15-minute freshness limit by default. Recommendations never authorize trades.

`draft` prepares a protected-copy-aware, review-only sale draft. Sale valuation,
forecasting, continuous watches and live execution remain unavailable. To use
fixture actions outside the demo, pass `--fixture-listings FILE` consistently
when preparing and processing actions. `prepare`, `authorize` and `execute`
are separate operations. Authorization binds the exact payload hash; revisions
and idempotency keys protect updates. Unknown submission outcomes retain their
reservation and require `reconcile`; they cannot be blindly resubmitted.

The default data directory is repository-relative `data/`, or
`POKESCOUT_DATA_DIR`/`--data-dir`. Storage adds `main_*` tables to the existing
`collection.sqlite3`; original inventory columns and APIs are preserved.
Jobs, decisions and fixture reservations here are separate from `backend/`'s
session-owned API job records. HTTP/CLI job unification and activity-stream
integration are follow-up work. Do not run this local Python boundary as an
unauthenticated remote service.

Verification: 82 original and main-agent tests passed before the team's API and
activity modules were integrated, including real loopback HTTP fixture orders,
lost-response reconciliation, duplicate-action prevention, restart persistence,
typed clarification and evidence exclusions. Cloud-provider availability and
browser execution were not live-tested by this implementation.
