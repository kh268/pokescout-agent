# Research agent experiment

This is the market research component from the proposed PokeScout architecture.
It accepts an already identified card. Upload normalization, collection storage,
prediction, trading and the main agent belong to later work.

`research_agent.py` uses **AgentField's native `app.ai()` with Gemini via LiteLLM**,
an `@app.reasoner()` research endpoint, and an `@app.skill()` Tavily search tool.
The previous LangChain experiment remains in `agent.py` for comparison.

Flow: Gemini produces one or two queries → Tavily returns up to five results per
query → Gemini extracts structured observations → Python checks source IDs,
exact evidence quotes, card numbers, quoted prices and stock evidence.
This is a bounded research pipeline; it does not use autonomous tool discovery.

## Run immediately

From `D:\Users\user\CodeProjects\pokescout-agent\experiments\sc`:

```powershell
.\.venv\Scripts\python.exe -X utf8 research_agent.py
```

Defaults to Mewtwo V `273/S-P`, JPY. Override with:

```powershell
.\.venv\Scripts\python.exe -X utf8 research_agent.py --card-name "Mewtwo V" --card-number "273/S-P" --currency JPY
```

Uses the existing ignored `.env`: `TAVILY_API_KEY`, `GOOGLE_API_KEY`,
`GEMINI_MODEL=gemini-3.5-flash-lite`. If the primary Gemini key reports quota
exhaustion, it tries `GOOGLE_API_KEY_FALLBACK` once. Credentials are redacted from
local run logs. No automatic provider or model switching occurs.

## AgentField service

```powershell
.\start_research.ps1
```

This starts the pinned Docker control plane, registers the research agent, and
runs its HTTP service. Control plane: `http://localhost:18080`; agent API and
OpenAPI docs: `http://localhost:18081/docs`.

Call through the control plane from another PowerShell window:

```powershell
$body = @{ input = @{ card_name = 'Mewtwo V'; card_number = '273/S-P'; currency = 'JPY' } } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri 'http://localhost:18080/api/v1/execute/pokescout-research.research' -ContentType 'application/json' -Body $body -TimeoutSec 180
```

The direct agent endpoint is `POST http://localhost:18081/reasoners/research`
with the same JSON body. Agent registration exposes the callback address
`http://host.docker.internal:18081` so the container can reach Windows.
The registered service listens on all host interfaces for that callback;
standalone service mode (`research_agent.py --serve`) binds only to loopback.

Stop the foreground agent with Ctrl+C, then `docker compose stop`. Data is kept
in the experiment's Docker volume. No Docker restart policy is installed.

## Logs and output

Every research call prints a `logs/research-*.jsonl` path and saves a matching
`.report.json`. Logs contain inputs, model/schema selection, Gemini plans and
extractions, full Tavily responses/request IDs, timing, validation rejections,
errors and completion. Logs and credentials are ignored by Git.

The report includes source IDs/URLs, exact quotes, prices, conditions and stock
observations. **All offers are `search_snippet_only`; `cheapest_verified` is
always null.** Search indexing can lag shop prices. This experiment does not
verify checkout availability, shipping, or live listing prices. The caller
must not treat search observations as a confirmed cheapest purchasable offer.

## Check / reproduce

Python 3.12 is required for this tested environment; AgentField documents support
for Python 3.10–3.13. Use the pinned environment in `requirements.txt`:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -X utf8 research_agent.py --check
.\.venv\Scripts\python.exe -X utf8 -m unittest -v
.\.venv\Scripts\python.exe -m pip check
```

The control plane image is pinned by digest in `compose.yaml`.
The SDK and running control plane are version 0.1.145.

Verified on 2026-11-14: six offline tests passed and `pip check` found no broken
dependencies. The real CLI research call completed in 21.11 seconds. A separate
call through the control plane returned HTTP 200 (`status: succeeded`) and
completed research in 23.69 seconds. Both used Gemini 3.5 Flash Lite and two
real Tavily searches. Evidence is in `logs/control-plane-smoke.json` and the
matching research JSONL/report files. Key fallback is implemented but has not
been live-tested by deliberately exhausting a key.

## Moving into the main project

Keep `research(card_name, card_number, currency)` as the integration boundary.
The main agent can call `pokescout-research.research` through AgentField. Move the
input/report schemas and evidence validation together. Keep secrets and trace
storage outside the business logic. Add live shop verification before using
these observations to make buying/selling recommendations, and keep condition,
currency and shipping comparisons explicit.

References: [Python SDK](https://agentfield.ai/docs/agentfield/reference/sdks/python),
[AI configuration](https://agentfield.ai/docs/agentfield/reference/sdks/configuration),
[deployment](https://agentfield.ai/docs/agentfield/reference/deploy).
