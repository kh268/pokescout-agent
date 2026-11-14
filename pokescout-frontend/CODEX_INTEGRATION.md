# Codex handoff: integrate the approved PokéScout frontend

## Task

Integrate the frontend supplied in this folder into the user's existing PokéScout repository. This is a real implementation request. Inspect the existing repository, then modify and verify the integration rather than merely proposing a plan.

## Read first

1. Existing repository `AGENTS.md` and setup instructions.
2. Existing Python orchestrator, schemas, environment configuration, collection store and public API.
3. This package's `README.md`, `app/page.tsx`, `lib/engine.ts` and `BACKEND_CONTRACT.md`.
4. Determine the current frontend framework and entrypoint. Preserve unrelated files and user changes.

Do not assume endpoint names in BACKEND_CONTRACT.md already exist. Prefer adapting to actual existing routes.

## Approved visual design — preserve

- Desktop website first, responsive as a secondary requirement.
- Warm ivory background, illustrated map, Pokémon card artwork.
- Coral red primary actions, gold accents, charcoal text.
- **Agent console is ivory with pale gold borders and coral active tabs. No green or purple console.**
- Radar-ball logo in `public/scout-logo.svg`.
- Large legible typography; avoid adding explanatory subtitles under every heading.
- Horizontal navigation: Scout, Collection, Watchlist, Agent Lab.
- Main buying route next to the always-visible agent console on desktop.
- No `POWERED WHEN CONNECTED` sponsor strip on the main page.
- No footer tagline or fan-made artwork line in the UI. Source provenance remains in documentation.
- Keep the sponsor settings accessible from Agent Lab and the settings button.

Do not redesign the page during backend integration. Component extraction is fine if the visual result stays equivalent.

## Backend ownership

The user's final backend architecture uses **Python orchestration**:

- Image identification: configured Gemini model returns structured card fields.
- Listing/evidence extraction and selling-copy assistance: configured Qwen model via Nosana.
- Tavily: search/extract with source URLs and timestamps.
- Python: authoritative identity matching, pricing arithmetic, budget/reserve, quantity, duplicate controls.
- Daytona + Playwright: permitted marketplace navigation and verification.
- CodeAF: development harness, not a runtime policy generator or spending authority.

Do not assume the exact proposed model names exist in the connected accounts. Keep them configurable and make unavailability explicit. Preserve existing validated provider code instead of replacing it with the legacy development examples in this export.

## Integration seams in app/page.tsx

| Current symbol/handler | Replace with |
|---|---|
| `seed`, `cards`, `initialMarket()` | Backend market observations, with explicitly separate demo fixtures |
| `plan` / `optimize()` | Python-authoritative plan response; local calculation only as labeled preview |
| `run()` | Create real run and subscribe to backend events; no timed imitation of provider work |
| `log()` / `events` | Backend event stream with stable run ID and sequence; local UI events can remain labeled local |
| `refresh()` / `marketEvent()` | Real explicit refresh; move synthetic events into demo-only controls |
| `save()` / `saved` | Persistent backend plan versions |
| `buyPlan()` / `paperSell()` | Keep paper behavior in demo; connect permitted real actions only through backend intent/verification workflow |
| `addOwned()` | Image intake, extracted-field review, user confirmation, then persistence |
| `createDraft()` | Verified identity/defect payload to backend draft generator; editable result |
| `sponsor()` | Existing authenticated backend integration status/tasks |
| `catalogLookup()` | Existing catalog resolver if available |
| `alerts` | Actual backend watcher when implemented; do not claim background watching until it runs |
| localStorage persistence | Backend collection, watchlist and plans; preserve import/migration choice |
| `exportAll()` | Versioned server evidence bundle, with mode and timestamps |

## Required implementation details

- Separate demo and live modes in state, URLs/requests and visual labels. Never label fixture observations as live.
- Normalize exact identity: set, number, language, variant, raw/graded cohort; PSA grade must not be compared with raw pricing.
- Make structured user intent editable before a run.
- Include shipping, marketplace fees and reserve in totals; unknown costs must remain unknown and can make offers ineligible.
- Enforce limits on the server; do not trust a client-calculated total.
- Use integer yen and explicit currency.
- Render actual tool events, evidence URLs, observation timestamps, rejection reasons and retry/error states.
- Support cancellation and reconnecting an event stream without duplicate events or duplicate actions.
- Stop counting duplicate owned cards toward an unowned wishlist goal.
- Separate paper holdings from physical ownership and live transactions.
- Preserve user-entered defects in all sale drafts; never claim physical authenticity from a PSA label or number alone.
- Never automatically execute a purchase just because an analysis recommends it. Use the existing authorization and idempotent execution workflow.
- Keep secrets server-side. Add authentication before exposing provider tasks publicly.
- Do not copy the exported development API route into production as the live execution boundary.

## Suggested organization

If compatible with the current app, extract:

- `components/ScoutHeader.tsx`
- `components/MissionControls.tsx`
- `components/BuyingRoute.tsx`
- `components/AgentConsole.tsx`
- `components/CollectionView.tsx`
- `components/CardDrawer.tsx`
- `components/ConnectionSettings.tsx`
- `lib/api.ts` for typed backend calls
- `hooks/useRunEvents.ts` for stream lifecycle

These names are suggestions, not reasons to replace existing structures.

## Acceptance checks

1. Existing backend checks still pass.
2. Frontend builds and typechecks in the target framework.
3. Desktop layout matches the supplied approved design at 1366px and 1440px.
4. No horizontal overflow at 390px; forms, buttons and dialogs remain usable.
5. Collector and Reseller mode switching works.
6. Budget/reserve changes produce a new authoritative plan or explicit pending/error state.
7. A cheaper wrong-edition offer is excluded with a source-backed explanation.
8. Real run events appear once, in order; failures/cancellation/reconnection are represented honestly.
9. Image upload produces either actual extraction + review or an explicit unavailable state, never a fake success.
10. Collection survives refresh without losing ownership/defect fields.
11. Selling draft can be reviewed and edited; it is not published implicitly.
12. Unavailable, stale and shipping-unknown offers cannot silently become recommended purchases.
13. Demo remains usable without credentials and clearly says it is a demo.
14. Integration report states what is live, what remains demo, which commands passed, and any untested provider calls.

Deliver working code and a short validation report. Do not claim complete live integration if credentials or backend capabilities are missing.
