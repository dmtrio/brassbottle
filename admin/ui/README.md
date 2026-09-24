# Djinn admin UI

Vue 3 + shadcn-vue SPA for the djinn admin plane.

- `npm run dev` — Vite dev server on port 5173.
- `npm run build` — `check:tokens`, `vue-tsc` and `vite build`.
- `npm run lint` — ESLint over `src/` (generated `src/components/ui/` is ignored).
- `npm run test:tokens` — unit tests for the token Gate.
- `npm run test:unit` — vitest unit tests of the SPA's TypeScript (`src/**/*.test.ts`), e.g. the DST-day range maths in `src/lib/history.ts`.

Styling rule: every spacing, size, type and status colour in app code must
come from `src/styles/tokens.css`; see the header comment there. The Gate
(`scripts/check_tokens.py`) enforces it and runs in the image build.

`DJINN_ADMIN_UI=spa` switches `src/admin_daemon.py` from the legacy Preact
page to this app; `DJINN_ADMIN_UI_DIST` overrides the dist directory.
