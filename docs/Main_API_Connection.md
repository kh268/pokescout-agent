# Main-agent API connection

`POST /api/v1/jobs` with `type: decision` uses the existing durable worker.
After clarification and research, the non-fixture path delegates to
`backend/main_agent.py::decide(...)`, which creates the teammate-owned
`orchestrator.Pipeline`, supplies saved reports and explicit observation facts,
and executes `Pipeline.start()`. The adapter does not edit
or duplicate the teammate implementation. Job results and lifecycle SSE retain
the existing API envelope and persistence.

The API snapshots session-owned copies and active reservations, passes complete
research reports, and supplies session-owned listing confirmations from
`POST /api/v1/research-reports/{run_id}/listing-confirmations`.
Missing printing/fee/condition facts remain
unconfirmed, so insufficient-evidence decisions are expected and correct.
Fixture demonstrations retain their existing deterministic path.

Main-agent artifacts/checkpoints persist under
`data/main-agent/<session-owner-hash>/`. Session-owned copies and reservations
are supplied by the API. Child attempts use the outer job ID, revision and goal
digest as idempotency keys and run directly; no additional polling worker starts.
Child `awaiting_input` questions are translated into the frontend question fields;
the child is cancelled and the outer job waits. Answering resumes the same outer
job and starts a fresh child attempt with the updated constraints.

For a replacement main-agent implementation, inject `Settings(main_agent=fn)`.
`fn(goal, copies, reports, reservations)` is a synchronous callable returning the
Decision dictionary, including `decision_id`; the API runs it in a worker thread.
The API owns research invocation, clarification/resumption, jobs and SSE. Do not
start a second job scheduler or duplicate research inside this callback.

Failed standalone research reports are retained and mark their job failed;
partial research reports remain successful completed jobs with partial results.

Main pipeline stage updates publish as producer `pokescout-main`, operations
`pipeline.identify`, `pipeline.research`, `pipeline.decision`, `pipeline.clarify`
and `pipeline.finished`. Research still runs through the existing AgentField
client before pipeline planning, preserving provider-step SSE and avoiding a
duplicate provider call. The API does not enable Pipeline's optional automatic
fixture execution; authorized fixture action intents retain their existing path.
Frontend consumption remains frontend work. Live trading remains disabled.
