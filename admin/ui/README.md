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
only) while it is up, and the tab does not poll then. While the stream opens the
tab reads `GET /api/egress/queue` once at once (Connecting), so the list is
never empty, and keeps reading every 5 s until the first frame. If the stream is
unavailable, drops, or goes silent for about two of the daemon's `hb`
heartbeats, the tab polls every 5 s until the stream is back (Reconnecting, then
Polling). A hidden tab closes its stream, because a browser allows about six
connections to one origin and each open tab's stream would take one: it reads
the queue every 30 s (Paused) and opens a stream again, with one read, when it
is shown. A failed read while Live polls until one succeeds. The state machine
is `src/lib/stream.ts`, wired in `src/composables/useQueue.ts`.
