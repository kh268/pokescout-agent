# Running the application backend

Python 3.12. The application API is implemented in `backend/`, with a conventional
`api:app` entry point. It wraps the existing image, collection, research, decision
and selling modules. The original CLI remains available.

## Start

From the repository root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-api.txt
.\.venv\Scripts\python.exe -m backend --env-file experiments/sc/.env --research-url http://127.0.0.1:18081 --allow-origin http://localhost:5173 --allow-origin http://localhost:3000
```

The already installed experiment interpreter also works:

```powershell
.\experiments\sc\.venv\Scripts\python.exe -X utf8 -m backend --env-file experiments/sc/.env --research-url http://127.0.0.1:18081 --allow-origin http://localhost:5173 --allow-origin http://localhost:3000
```

- Application/OpenAPI docs: `http://localhost:8000/docs`.
- OpenAPI JSON: `http://localhost:8000/openapi.json`.
- The launcher explicitly loads the chosen environment file. It accepts
  `GEMINI_API_KEY`, or aliases `GOOGLE_API_KEY` for the existing image adapter.
- Start the separate research service before researching; see `experiments/sc/RESEARCH.md`.
  `--research-url` enables that connection; provider/service failures remain visible
  on jobs. The capabilities flag means configured, not guaranteed provider health.
- Data defaults to `data/collection.sqlite3`; images go under `data/images/`.
  Use `--data-dir <path>` for an isolated demo or test database.
- Run **one** API process (`--workers 1` if using uvicorn directly). Two local
  workers claim persisted jobs transactionally. Startup retains queued jobs and
  marks interrupted running jobs failed, without blindly repeating provider work.
- This is a loopback-only single-user server, not an authenticated public deployment.
  Use the same hostname for frontend and API (both `localhost`, or both `127.0.0.1`)
  so the SameSite cookie works. Requests from allowed frontend ports need
  `credentials: 'include'`. No credentials belong in frontend code.

## Upload -> recognize -> confirm -> collection

The following browser JavaScript shows the real API flow. All POST/PATCH calls
need a CSRF token and a fresh UUID idempotency key; reuse that key if retrying the
same request. The image is a `File` obtained from the user's file picker.

```javascript
const base = 'http://localhost:8000/api/v1';
const sessionResponse = await fetch(`${base}/session`, { credentials: 'include' });
const { data: session } = await sessionResponse.json();

async function post(path, body, { multipart = false, etag, key = crypto.randomUUID() } = {}) {
  const headers = { 'X-CSRF-Token': session.csrf_token, 'Idempotency-Key': key };
  if (!multipart) headers['Content-Type'] = 'application/json';
  if (etag) headers['If-Match'] = etag;
  const response = await fetch(`${base}${path}`, {
    method: 'POST', credentials: 'include', headers,
    body: multipart ? body : JSON.stringify(body)
  });
  const payload = await response.json();
  if (!response.ok) throw payload.error;
  return payload.data;
}

const form = new FormData();
form.append('files', file);
form.append('manifest', JSON.stringify([{ role: 'front' }]));
const upload = await post('/uploads', form, { multipart: true });
const job = await post('/jobs', { type: 'identify', input: { upload_id: upload.upload_id } });
```

Poll `GET /jobs/{job_id}` until `succeeded`, `failed` or `awaiting_input`.
On success, fetch `job.result.url` from the API origin. The candidate is already
saved in SQLite; it is **not** an owned copy yet. Render all identity fields,
missing fields and uncertainties for the user to review.

After explicit possession confirmation and corrections:

```javascript
const copy = await post('/copies/confirm', {
  candidate_id: candidate.candidate_id,
  confirmed_card: correctedCard,
  confirm_owned: true,
  allow_sale: false,
  cost_jpy: null
});
```

This transaction writes the existing `copies` table, an ownership-scoped API
record, and a unique candidate-confirmation record. A retry cannot insert a
second copy. Uploaded images and candidate records survive backend restarts.
`GET /collection` returns this session's copies; `GET /copies/{id}` includes
images and revision. Existing CLI inventory is not silently assigned to the
first anonymous web session; it needs an explicit owner/import migration.

## Research -> decision -> clarification

`POST /jobs` with `type: research` accepts the shared research request. Results
are available through `GET /research-reports/{run_id}`; local log paths are not
returned. A job may succeed while the research report is `partial`.

`type: decision` accepts the goal and optional saved `research_run_ids`. Missing
budget, exact card identity or accepted conditions produces `awaiting_input` and
typed questions. Submit every current question exactly once to
`POST /jobs/{id}/answers`, with `If-Match` set to the job's ETag. The same job
resumes; completed research reports are retained.

The existing purchase planner requires full printing identity, condition,
shipping and purchase fees. The current research schema does not establish all
of these. Those observations remain excluded and produce
`insufficient_evidence`; the API does not copy the requested identity onto an
unverified listing or assume zero fees. Fixture mode below supplies complete
synthetic observations and exercises a successful purchase journey.

## Sale journey

A copy is protected by default. To change explicit sale permission:

```text
PATCH /api/v1/copies/{id}
If-Match: "<current revision>"
X-CSRF-Token: <session token>
Idempotency-Key: <UUID>

{"allow_sale": true}
```

Then `POST /sale-drafts` with `{copy_id, asking_price_jpy}` returns a job and a
persisted draft. Disclosed defects remain in the draft. Sale publication and live
trading are disabled; drafting does not claim that a listing was published.

## Fixture purchase journey

Enable the existing synthetic listings explicitly, using a separate data folder:

```powershell
.\experiments\sc\.venv\Scripts\python.exe -X utf8 -m backend --port 8001 --data-dir data/fixture-api --fixtures examples/v2/listings.json
```

1. Submit a decision job using one exact fixture card from `examples/v2/listings.json`,
   quantity 1, budget JPY 5000, and `allowed_conditions: ["near_mint"]`.
2. Read the decision, then prepare a fixture buy intent using its selected
   `listing_id` and the proposed `max_total_jpy`.
3. Review the payload, then authorize its exact `payload_hash` with `If-Match`.
4. Execute with the updated ETag. The worker calls the existing loopback
   `TestMarket` HTTP purchase endpoint, then independently reads the persisted
   order. The outcome has a `TEST-*` receipt and `execution_mode: fixture`.
5. Repeating execution returns the existing job; an uncertain submission is
   reconciled against that intent rather than blindly resubmitted.

Fixture orders do not register real physical ownership or confirmed real cash.
Live purchase/sale execution, automatic watches and forecasting remain disabled
in `/capabilities`.

## SSE integration boundary (owned by the activity workstream)

The API does not implement another event journal or stream. `create_app()` exposes
`app.state.store` and `request.state.owner` after session validation.
`store.on_job_event(db, owner, job, event_type)` is an optional synchronous hook.
It is invoked **after the job is persisted, inside that same SQLite transaction**.
The activity implementation can append its journal using that connection, then
mount its router against the application's session resolver. Do not open a
second writer transaction from inside the callback. `api_sessions` and
`api_records` are the authoritative application session/job records.

Set `app.state.activity_stream_enabled = True` only after mounting/verifying the
activity implementation. Until then capabilities advertise false and polling
works. No activity credentials are exposed through public jobs.

## Verification

```powershell
.\experiments\sc\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_api.py -v
```

These tests use scripted provider replies but real multipart parsing, disk image
storage, SQLite transactions, workers, session isolation, clarification, sale
drafting and actual test-marketplace HTTP orders. They cover restart persistence,
idempotent confirmations/execution, CSRF, stale revisions and malformed uploads.
A separate live check used a public TCGdex card image through real Gemini and
saved the resulting candidate in the application database. Its local record is
`data/api-live-upload-check.json`; it did not assert possession or add that public
sample to the user's collection.
