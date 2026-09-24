Admin-UI contract schemas for the egress broker and the browser-facing admin
proxy: the exact JSON shapes of `queue_snapshot()`, the three successful
broker `/decide` bodies (allow, deny once, persistent deny), the 200 body of
the admin proxy's `/api/egress/decide`, the error body every non-200 response
carries, the keyset-paged recent-history page (`GET /recent` on the broker,
`GET /api/egress/recent` on the admin), and the planned queue SSE event.
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
