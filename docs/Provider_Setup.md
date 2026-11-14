# Provider setup for backend integration

The new `pokescout_agents/` package exposes typed provider calls and a deterministic single-listing decision function. It can be integrated into a future backend without changing the existing `pokescout.py` CLI. The CLI's image-recognition flow uses `image_agent.py` and defaults to `gemini-3.5-flash-lite`; the backend-facing client here uses the configurable `gemini-3.8-flash`. The older CLI and `providers.py` remain separate release code.

## Configuration

Install the local dependencies and fill in the ignored `.env` file from `.env.example`:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pokescout_agents.doctor
```

The new package loads `.env` automatically. The existing CLI still expects exported environment variables as documented in the main README. Never commit credentials. Tavily calls require `TAVILY_API_KEY`; the doctor reports whether it is configured without spending search credits.

The doctor checks Nosana's authenticated model list and Gemini's model endpoint. Optional smoke checks use a small amount of the configured provider quota or credits:

```powershell
.\.venv\Scripts\python.exe -m pokescout_agents.doctor --smoke-nosana --smoke-gemini-image
```

The Gemini smoke check sends a generated blank image, not a user's card. It verifies API access and structured output, not card-recognition accuracy or the project's billing status. `gemini-3.8-flash` has a documented Standard free tier, but account-specific quota and billing must be checked in the Google console. There is no provider fallback.

## Backend-facing methods

```python
from pokescout_agents import PokeScoutServices
from pokescout_agents.schemas import ListingObservation, TargetCard

services = PokeScoutServices()
identity = services.identify_card("card-front.jpg")
facts = services.analyze_listing("Card title", "Seller description")
sources = services.research_sources("Japanese card set and number")
summary = services.summarize_research(sources)

target = TargetCard(name="Pikachu", set_code="SVP", card_number="001", max_total_jpy=5000)
listing = ListingObservation(
    marketplace="fixture", listing_id="item-1", url="https://example.com/item/1",
    title="Card title", description="Seller description", extracted=facts,
)
decision = services.evaluate_listing(target, listing, remaining_budget_jpy=5000)
```

`GeminiClient` returns nullable card identity fields. `NosanaClient` extracts listing evidence, summarizes supplied research sources, and drafts Japanese sale text from confirmed facts. `TavilyClient` returns timestamped sources from Search and Extract. `evaluate_listing` checks identity, grade, condition, availability, shipping, budget, quantity, and duplicate listing IDs in Python. It returns a score only when all hard checks pass. These methods do not purchase or publish anything.

`pokescout_agents.providers.daytona.DaytonaBrowserClient` sends browser actions to the already running Daytona worker. The worker code lives in `daytona_worker/` and is uploaded by the root `setup_daytona.py` command. It serializes actions in its single Playwright context. The current sandbox passes a local browser self-test, but outbound access to `example.com` is blocked; inspect Daytona network limits before using a live marketplace. Site-specific adapters, authorization, durable reservations, and reconciliation remain separate implementation work.

## Verification status

- Nosana `qwen/qwen3.8-27b`: authenticated model list and JSON chat smoke call passed.
- Nosana listing extraction: passed on a synthetic Pikachu listing, preserving price, shipping, seller condition, and a disclosed defect.
- Gemini `gemini-3.8-flash`: authenticated model lookup and blank-image structured output passed.
- Tavily: client implemented and mock-tested; no live Search or Extract request has been run in this setup.
- Daytona: reusable browser worker self-test passed; external website navigation is blocked by current sandbox network access.
