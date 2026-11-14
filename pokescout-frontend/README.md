# PokéScout frontend — VS Code / Codex integration pack

> Integrated into the Python repository. See [INTEGRATION_REPORT.md](INTEGRATION_REPORT.md)
> and the root README for the current run commands. The sections below describe
> the original standalone export; its sponsor route and browser key entry were
> removed during integration. The Python workspace is now available through the
> **Demo market · open Python** control in the header.

This is the latest approved **ivory, gold and coral** desktop-first website design, exported as a portable Next.js application. It is source code, not a screenshot or a hosted-site shortcut.

## Start here

1. Extract this folder beside your existing project or into a new `frontend/` directory. Do not overwrite existing project files blindly.
2. Open the existing project in VS Code.
3. Ask Codex to read `CODEX_INTEGRATION.md` in this folder and integrate it with your project.

Suggested prompt:

> Read the supplied pokescout-frontend/CODEX_INTEGRATION.md. Integrate this exact approved frontend design into my existing project. Inspect the repository and its AGENTS.md first. Preserve my Python orchestration and existing functionality. Keep the ivory/gold/coral palette, radar-ball logo, map background, larger typography and desktop layout. Reuse my current frontend framework if practical. Replace the demo functions with actual backend endpoints where they exist; implement missing adapters only after inspecting the backend. Do not invent successful tool calls, live market data, model availability or purchases. Complete and test the integration, documenting anything not yet connected.

## Run the standalone frontend

Requires Node.js 22.13+ and npm.

```bash
npm install
npm run dev
```

Open `http://localhost:3000` (or the port Next.js prints if occupied).

```bash
npm run typecheck
npm test
npm run build
npm start
```

The standalone frontend works without provider keys. Card pictures, map illustration and logo are included locally. Google Fonts is requested by CSS when available; font fallbacks are included.

## Included functionality

- Collector and Reseller modes.
- Wishlist, card search, type filters and sorting.
- Edition-mismatch and unavailable-offer exclusion in the demo engine.
- Budget, reserve, scenario and position-size controls.
- Buying-plan recalculation, saved rules, and evidence JSON export.
- Agent activity, component map and evidence panel.
- Paper collection, paper sales, manual owned-card entry and editable selling drafts.
- Image preview on manual intake; PSA fields can be entered manually.
- Watchlist and local price alerts checked on demo refresh.
- Lot analysis on synthetic prices.
- Optional development-only sponsor adapters.

## What is demo versus connected

| Feature | Current behavior |
|---|---|
| Card identity/artwork | Bundled catalog identities/artwork; optional TCGdex live identity lookup |
| Market listings, prices, availability | Synthetic fixtures in `lib/market.ts` |
| Planner | Real deterministic TypeScript calculation on demo data |
| Run Scout | Local validation/calculation workflow with local event records |
| Collection and saved plans | Browser localStorage, not server accounts |
| Uploaded photo | Browser preview only; not sent to Gemini or persisted |
| Selling description | Editable deterministic draft from stored fields; not published |
| PSA number/grade | Owner-entered metadata, no registry or authenticity verification |
| Price forecast | None; reseller outputs are explicitly assumed scenarios |
| Alerts | Run when the demo is refreshed; no unattended polling |
| Purchase/sale | Paper bookkeeping; no marketplace orders |
| Tavily/Nosana/Daytona | Optional development adapters; disabled by default |
| CodeAF | Optional development task adapter, not the purchase-policy engine |
| Python backend integration | Not wired in this export; integration instructions supplied |

The website is NOT a completed live trading agent. Connecting it to your existing Python project is the next implementation task for Codex.

## File map

```text
app/page.tsx                  Main React UI and demo action handlers
app/globals.css               Approved responsive styling and console palette
app/layout.tsx                Page metadata and stylesheet import
app/api/sponsors/route.ts      Node adapter, local development only
lib/engine.ts                 Deterministic demo planner and paper accounting
lib/market.ts                 Synthetic offer fixtures
lib/card-catalog.json         Card identities and artwork provenance
lib/sponsors.ts               Optional sponsor-task implementation
public/scout-logo.svg          Original radar-ball logo
public/favicon.svg            Matching favicon
public/scout-map.svg           Decorative field-map background
public/cards/*.webp            Ten bundled Japanese card images
tests/*.test.mjs               Planner/accounting/provider validation tests
CODEX_INTEGRATION.md           Exact task and acceptance checks for Codex
BACKEND_CONTRACT.md            Proposed API/event contract; not an existing API
SOURCE.json                   Design/source provenance
VALIDATION.md                 Actual export validation results
```

## Integrating into an existing frontend

Prefer moving the main React component and assets into the existing frontend rather than replacing its framework. The page uses React hooks, TypeScript and lucide-react. It does not depend on Next navigation or server actions. Global CSS contains Tailwind's reset/import; preserve an equivalent reset if porting to Vite.

If your app already has a layout, merge styles carefully. Namespace `.topbar`, `.panel`, `.modal`, `button`, `body` and other global selectors under a PokéScout root if the frontend is not the entire app. The public asset paths assume the website root; adapt them if serving under a path prefix.

No Cloudflare, Sites authentication, project IDs, Git credentials or deployment configuration are required by this portable export. Existing sponsor code is retained as a development example; the exported route uses Node environment variables instead of Cloudflare bindings.

## Optional local sponsor tasks

Copy `.env.example` to `.env.local` and configure keys only on your development machine. Set `ENABLE_LOCAL_SPONSOR_TASKS=true` to enable the development route. It rejects all production POST requests regardless of that flag. Use your authenticated Python service for production provider calls.

The UI still displays synthetic prices after an API response. These adapters do not implement the final Python orchestration. Exact model IDs, account availability, Daytona images and provider permissions must be checked in your own environment. No credentials are included.

The helper's pre-existing Nosana adapter selects an available model from the returned model list; it does not guarantee the model named in your architecture. When integrating, route through your own explicit model configuration and report unavailable models instead of silently substituting them.

Keep card artwork provenance in source documentation. The visual assets are provided for this fan project; this package makes no claim to ownership of Pokémon card artwork.
