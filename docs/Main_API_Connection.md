# Main-agent API connection

`POST /api/v1/jobs` with `type: decision` uses the existing durable worker.
After clarification and research, the non-fixture path delegates to
`backend/main_agent.py::decide(...)`, which creates the teammate-owned
`MainAgent`, imports reports, creates a decision job and awaits `run_job()`.
The adapter does not edit
or duplicate the teammate implementation. Job results and lifecycle SSE retain
the existing API envelope and persistence.

The API snapshots session-owned copies and active reservations, passes complete
research reports, and supplies an empty facts sidecar until explicit listing
confirmation is implemented. Missing printing/fee/condition facts remain
unconfirmed, so insufficient-evidence decisions are expected and correct.
Fixture demonstrations retain their existing deterministic path.

Main-agent artifacts/checkpoints persist under
`data/main-agent/<session-owner-hash>/`. Session-owned copies and reservations
are supplied by the API. The child job uses the outer job ID as its idempotency
key and runs directly; no additional polling worker is started.

For a replacement main-agent implementation, inject `Settings(main_agent=fn)`.
`fn(goal, copies, reports, reservations)` is a synchronous callable returning the
Decision dictionary, including `decision_id`; the API runs it in a worker thread.
The API owns research invocation, clarification/resumption, jobs and SSE. Do not
start a second job scheduler or duplicate research inside this callback.

Failed standalone research reports are retained and mark their job failed;
partial research reports remain successful completed jobs with partial results.

Remaining integration: an authorized listing-facts confirmation API, detailed
main-agent step activity, and frontend consumption. Live trading remains disabled.
