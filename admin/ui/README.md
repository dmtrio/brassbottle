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
Polling). A failed read while Live polls until one succeeds, or until a stream
frame arrives (a frame is a good read). The state machine is `src/lib/stream.ts`.

One stream per browser, however many admin tabs are open: a browser allows
about six HTTP/1.1 connections to one origin across all its tabs, and a stream
holds one for as long as it is open, so tabs that each held their own froze the
admin at six. The tabs elect a leader with the Web Locks API
(`navigator.locks.request('djinn-admin-stream', …)`, held for the tab's
lifetime). The leader opens the one stream, visible or hidden (a stream is not
throttled in a background tab, a timer is), and relays each snapshot, each
heartbeat and each change of link state over a `BroadcastChannel`
(`djinn-admin-queue`). The other tabs, the followers, open no stream and read
nothing while they hear from the leader; they show its link state and apply its
snapshots, and a tab that opens asks (`hello`) and is answered at once. When the
leader's tab closes (or is reloaded) the lock passes to another tab, which opens
the stream (Connecting, then Live) while the rest follow. A follower that hears
nothing at all for 40 s (the stream's 35 s silence limit and a 5 s margin, so a
leader whose stream died has said so first), or gets no answer to its `hello`
within 3 s, reads the queue every 5 s itself and says Polling until the leader is
heard again. Decides stay per tab: the acting tab posts `/api/egress/decide` and
reads the queue once itself; the leader's next frame reaches everyone else. Every
tab with the bell on still notifies once per new request, from the snapshots it
receives. The logic is `src/lib/shared-link.ts` (pure, unit-tested); the browser's
locks and channel are `src/lib/browser-link.ts`; both are wired in
`src/composables/useQueue.ts`, which logs each leader change and broadcast
(`[queue-link] stage=… sent=… received=…`, console debug).

Where `navigator.locks` (secure contexts only: https or localhost) or
`BroadcastChannel` is missing, a tab is on its own, as it was before: it opens its
own stream, and a hidden tab closes it (a browser allows about six connections to
one origin) and reads the queue every 30 s (Paused), opening the stream again,
with one read, when it is shown.

Notification bell: the top bar's bell raises an OS notification (the Notification API,
while the page is open; no push) for each request new to the tab. It asks for permission
only when clicked. Granted, a click mutes and unmutes (kept per viewer in `localStorage`);
denied, it explains the block is in the browser's site settings. The requests in the first
snapshot a tab loads, and any id it has shown before, never notify. A notification's tag
is the request id; clicking one focuses that request's row. The logic is `src/lib/notify.ts`
(unit-tested) and `src/composables/useNotifications.ts`. If the browser's constructor throws
(Chrome for Android has the API but not the constructor), the bell moves to unavailable for
the session instead of saying on; there are no service-worker notifications. A hidden
tab's notifications arrive over the leader's stream and the channel, which the browser does
not throttle as it does timers, so they are as prompt as a visible tab's (in a browser without
Web Locks a hidden tab reads the queue every 30 s, and timer throttling can stretch that to a
minute).
