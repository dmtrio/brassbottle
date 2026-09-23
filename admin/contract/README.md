Admin-UI contract schemas for the egress broker: the exact JSON shapes of
`queue_snapshot()`, the three successful `/decide` bodies (allow, deny once,
persistent deny), the error body every non-200 response carries, the planned
recent-history page, and the planned queue SSE event. The broker
(`src/egress_broker_host.py`) is the source of truth; these files describe it,
never the reverse. `tests/test_admin_contract.py` validates real broker
output against them, so any field change there fails that test until the
schema is updated too.
