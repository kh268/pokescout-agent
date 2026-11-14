# Changelog

## 0.2.0 — collection and deterministic decisions

- Changed the default `run` workflow to deterministic Python selection. It no longer requires Daytona/CodeAF or executes generated purchase-policy code.
- Added Gemini image-intake adapter with structured card schema and user-confirmation boundary.
- Added raw/slab validation, certification strings, and PSA comparison against a user-supplied registry record. No automatic PSA API lookup or physical-authenticity claim.
- Added persistent SQLite collection with protected copies, optional cost basis and duplicate-image checks.
- Added structured exact-identity planning with set/language/variant/grading cohort, quantities, owned-copy exclusion and budget reservations.
- Added deterministic selling drafts preserving defects, plus optional Nosana wording suggestions that remain review-only.
- Added a self-contained synthetic demo, loopback test marketplace, durable fixture-order ledger and Playwright worker.
- Added a Daytona adapter for running that same synthetic browser worker in a prepared snapshot.
- Added handling for browser failure that saves a truthful partial report.
- Added a strict preferred Nosana model configuration; no silent fallback to an arbitrary served model.
- Bundled the expanded product specification and current implementation status.

The new interfaces supplement the original raw-card research commands. They do not establish real marketplace trading, continuous monitoring or forecast accuracy. See README.md for the supported paths and verification limits.
