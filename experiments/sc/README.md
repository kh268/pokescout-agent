# Tavily + LangChain ReAct experiment

For the Gemini + AgentField research agent, see [RESEARCH.md](RESEARCH.md).

An isolated experiment for [Agent Forge](https://luma.com/agentforgesg).
Use **Python 3.12**, selected explicitly because the machine's default is 3.14.
The virtual environment lives in this directory's `.venv`.

Uses `langchain.agents.create_agent`, the current LangChain v1 tool-calling
ReAct loop, and `langchain_tavily.TavilySearch`. Older examples using
`langchain.agents.create_react_agent` or `TavilySearchResults` target other APIs.
See the official [v1 migration guide](https://docs.langchain.com/oss/python/migrate/langchain-v1)
and [Tavily integration](https://docs.langchain.com/oss/python/integrations/tools/tavily_search).

## Setup (PowerShell)

```powershell
cd D:\Users\user\CodeProjects\pokescout-agent\experiments\sc
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` pins the full installed environment. `requirements.in` records
the direct dependency constraints used for initial resolution. Activation is
optional; the commands here always use this experiment's interpreter directly.
To activate interactively, use `.\.venv\Scripts\Activate.ps1`.

If `.env` does not exist, copy `.env.example` to `.env`, then fill in the keys.
`.env` and `.venv` are ignored by Git. Existing environment variables take
precedence over `.env`; no parent directory's `.env` is loaded.

## Run

Check imports and construct the graph without any API calls or keys:

```powershell
.\.venv\Scripts\python.exe agent.py --check
.\.venv\Scripts\python.exe -m unittest -v
```

Test a real Tavily request using **only `TAVILY_API_KEY`**:

```powershell
.\.venv\Scripts\python.exe agent.py --search-only "Tavily Search API official documentation"
```

Run the real ReAct agent using **both `TAVILY_API_KEY` and `GOOGLE_API_KEY`**:

```powershell
.\.venv\Scripts\python.exe agent.py "What is Tavily Search API? Cite the official docs."
```

Gemini is the default provider, using `GEMINI_MODEL=gemini-3.5-flash-lite`.
For an OpenAI-compatible provider, set `LLM_PROVIDER=openai`,
`OPENAI_BASE_URL` if needed, its model ID in `OPENAI_MODEL`, and its key in
`OPENAI_API_KEY`. The OpenAI model defaults to `gpt-4.1-mini`.
Alternative provider compatibility and model access need a live test.
Tavily's key alone does not provide the LLM that chooses and interprets searches.

Search defaults to basic depth with three results and no raw page content.
The agent is prompted to use at most two searches, and its graph has an eight-step
recursion limit. The prompt's search count is advisory, not a billing guarantee.
LLM requests have a 30-second timeout and automatic LLM retries are disabled.
The script requires nonempty Tavily results before reporting live success.
The offline tests use scripted model/search replies and spend no API credits.
