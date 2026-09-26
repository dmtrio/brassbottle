Admin-UI contract schemas for the egress broker and the browser-facing admin
proxy: the exact JSON shapes of `queue_snapshot()`, the three successful
broker `/decide` bodies (allow, deny once, persistent deny), the 200 body of
the admin proxy's `/api/egress/decide`, the error body every non-200 response
carries, the keyset-paged recent-history page (`GET /recent` on the broker,
`GET /api/egress/recent` on the admin), and the queue SSE event.
The broker (`src/egress_broker_host.py`) and the admin daemon
(`src/admin_daemon.py`) are the source of truth; these files describe them,
never the reverse. `tests/test_admin_contract.py` validates real daemon and
broker output against them, so any field change there fails that test until
the schema is updated too.

## Recent page

`recent_page.schema.json` describes `GET /api/egress/recent?before=&limit=&container=&since=&until=`
(the admin passes exactly those five parameters to the broker's `/recent` and
drops any other). Rows are ordered `decided_at DESC, request_id DESC` and have
the same shape as `queue_snapshot().recent`; `next` is the last row's
`decided_at` and `request_id` joined by a comma, to send back as `before`, and
is `null` when fewer than `limit` rows remained (default 50, clamped to
1..200). A page that ends exactly on the last row still carries a cursor, and
the page after it is empty with `next: null`. A malformed `before`, `since`,
`until` or `limit` is a 400 with an `error_response` body on both the broker
and the admin; a missing session is a 403 from the admin.

## Queue stream

`sse_event.schema.json` describes one event of `GET /api/egress/stream` on the
admin (spa mode only; session cookie required, refused 403 like the other
`/api/*` routes). On the wire an event is `event: <name>` plus one `data:` line
of compact JSON; the schema is that event as an object, `{event, data}`, and is
one of two. `queue` holds the whole `queue_snapshot()`: a stream gets the
current one the moment it opens and another whenever the queue changes; a
change ignores `generated_at` and each open row's `age_seconds`, which move on
every poll. `hb` is the heartbeat every 15 s on an idle stream, with `{}` as its
data (a `data:` line must not be empty or the browser dispatches nothing). It
is an event rather than a `: hb` comment because an `EventSource` never shows a
comment to script: the app treats about two missed heartbeats as a dead stream
and reconnects. At most 8 streams are open; a ninth, or an open while the
broker is unreachable, is a 503 with an `error_response` body. There is no
error event: when the broker stays unreachable the admin ends its streams, and
the app falls back to polling `GET /api/egress/queue`.
