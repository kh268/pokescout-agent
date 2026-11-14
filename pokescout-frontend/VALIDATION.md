# Export validation

Validated on 2026-11-14 in the build workspace:

- `npm run typecheck`: passed.
- `npm test`: 14 tests passed, 0 failed.
- `npm run build`: standard Next.js 16.3.4 webpack production build passed.
- Routes emitted: static `/`, static `/_not-found`, dynamic `/api/sponsors`.
- `npm install --package-lock-only --ignore-scripts --offline --no-audit --no-fund`: passed; npm lockfile included.
- Final stylesheet and bundled SVGs match the approved ivory/gold/coral design.
- Export has no Cloudflare runtime import, Sites project configuration, API keys, repository credentials, node_modules or generated build output.

Dependencies used for build/typecheck/tests were reused from the existing installed workspace. A completely fresh `npm ci` in a new machine was not run here. The generated lockfile is included for reproducible installation.

The matching website design was visually inspected in the browser. A separate full browser interaction suite was not rerun against this portable Next.js export. Provider tasks were not tested with live credentials. Existing automated provider checks use mocked responses.

This is the frontend integration pack, not a completed connection to the user's local Python repository. The Python repository on the user's machine has not been inspected or modified by this export.
