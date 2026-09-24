Admin-UI contract schemas for the egress broker and the browser-facing admin
proxy: the exact JSON shapes of `queue_snapshot()`, the three successful
broker `/decide` bodies (allow, deny once, persistent deny), the 200 body of
the admin proxy's `/api/egress/decide`, the error body every non-200 response
carries, the planned recent-history page, and the planned queue SSE event.
The broker (`src/egress_broker_host.py`) and the admin daemon
(`src/admin_daemon.py`) are the source of truth; these files describe them,
never the reverse. `tests/test_admin_contract.py` validates real daemon and
broker output against them, so any field change there fails that test until
the schema is updated too.
