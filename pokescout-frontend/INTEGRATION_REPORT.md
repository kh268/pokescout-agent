# Frontend integration report

## Connected to the existing Python backend

- Same-origin Next.js proxy to `/api/v1`, using the backend session cookie, CSRF token and idempotency keys.
- User-supplied offer planning through `POST /api/v1/structured-plans`. Python validates identity, condition, availability, integer JPY costs, ownership, budget and reserve. Plans are saved in the existing owner-scoped SQLite store. A source URL and timezone timestamp are required; stale observations are excluded. URLs and prices remain user-entered evidence, not verified market facts.
- Physical collection through existing upload → identify job → candidate review → confirm-copy routes. Image identification requires configured Gemini. Unknown or uncertain fields must be reviewed before ownership confirmation.
- Existing sale-draft jobs, research/decision jobs, cancellation, activity history and activity SSE. Reconnect uses the durable job ID and stable event IDs. The page displays failures and insufficient evidence.
- A **Try live Tavily** button in Demo market starts a durable backend job: Tavily searches/extracts up to three public listings, Nosana produces evidence-backed fields, and Python evaluates eligibility. URL, timestamp, field snippets and exclusion reasons appear separately from the synthetic plan. Missing keys or blocked marketplaces produce an explicit error; the demo still works.
- No production purchase path is exposed. The live watchlist says unavailable.

## Demo remains separate

The supplied design, artwork, map, colors and desktop layout remain. Its synthetic market, paper portfolio, local alerts and scenario values stay in the Demo market workspace. The direct browser-to-sponsor development route was removed; provider secrets belong in Python/server configuration.

## Validation

- `PYTHONPATH=.:tests python3 -m unittest test_frontend_plan test_live_market_plan -q`: 8 passed. Covers the demo wishlist/budget flow, persisted Python plans, wrong printing, unknown shipping, stale offers, fixture source rejection, the Tavily job API with mocked providers and missing-key behavior.
- Existing FastAPI upload/confirmation and research job tests: 2 passed.
- `node --experimental-strip-types --test tests/engine.test.mjs`: 9 passed.
- TypeScript syntax transpilation of modified TS/TSX files: passed.
- `npm run typecheck`: passed. `npm test`: 14 passed. `npm run build`: passed with Next.js 16.3.4.
- Browser viewport checks remain unrun in this sandbox.
- Both servers reached their listen step, but the sandbox rejected binding `127.0.0.1:8000` and `127.0.0.1:3000` with `Operation not permitted` / `EPERM`. Run both services in a normal local terminal to open the demo at `http://127.0.0.1:3000`.
- Full Python test discovery was attempted: 71 tests ran; 3 socket-dependent tests could not bind loopback in this sandbox and a fixture purchase test ended `unknown` for the same local socket restriction. No provider calls were made.

## Current limits

The backend research service requires `RESEARCH_AGENT_URL`; Gemini requires `GEMINI_API_KEY`. The research report schema does not yet establish complete printing identity or all fees, so a research-driven decision may correctly return `insufficient_evidence`. User-supplied structured observations are a separate explicit input path. Background watching, forecasts, automatic purchases and marketplace listing publication are unavailable.

The Tavily flow requires both `TAVILY_API_KEY` and `NOSANA_API_KEY`. No live provider call was run during this integration because those keys were unavailable. Its extraction leaves purchase fees unknown, so it may correctly show source-backed listings with no eligible purchase recommendation.
