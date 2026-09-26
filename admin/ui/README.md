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

Service worker: `npm run build` also emits `dist/sw.js`, its workbox chunk,
`dist/sw-cleanup.js` and `dist/manifest.webmanifest` (vite-plugin-pwa, configured
in `pwa.ts`). The worker precaches the hashed files under `/assets/` and nothing
else: no navigation fallback and no runtime caching, so `/` (the app, or the pointer
page without a session) and `/api/*` always come from the network. On activation it
deletes every cache it does not own, the legacy page's `djinn-admin-shell-v3`
included. `src/main.ts` registers it in the production build only; the daemon serves
these files from `dist/` through its ordinary asset allowlist (`sw.js` is `no-cache`).
`tests/test_admin_sw_build.py` checks the generated worker.

The queue arrives over `GET /api/egress/stream` (server-sent events; spa mode
only) while it is up, and the tab does not poll then. If the stream is
unavailable or drops, the tab polls `GET /api/egress/queue` every 5 s until the
stream is back; the top bar shows which (Live, Reconnecting, Polling). The
state machine is `src/lib/stream.ts`, wired in `src/composables/useQueue.ts`.
