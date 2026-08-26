# egress

Optional **agent-facing** tools for filing outbound access requests. Core egress
interception (transparent :80/:443 broker, NFLOG for other ports, operator
approval, `./djinn allow`) works in every bottle with
`capabilities.egress_broker` enabled — this plugin is **not** required for that.

Enable it when agents need explicit MCP tools or when you want the slot wired
through `common_secrets` / `agent_secrets`:

```yaml
plugins: [egress]
```

## What it adds

| Surface | Tool / command |
|---|---|
| MCP (`request_egress`, `check_egress`) | Local stdio server — wired into every MCP-capable agent when the plugin is enabled |
| `request-egress` CLI | Universal in-container path for agents without MCP (codex, `cursor-agent`, shell scripts) |

Both call the same host broker endpoint (`POST /egress` on port **8816**).

## `request-egress` CLI

```bash
request-egress docs.stripe.com "fetch API reference"
request-egress host1.example.com host2.example.com "batch pre-flight"
request-egress neon.tech:5432 "db:migrate pre-flight"
request-egress --check api.stripe.com    # ipset probe only, no filing
```

Exit codes: **0** allowed, **1** denied (or broker error), **2** timed out still
queued (`decision: pending`).

**Codex** does not load remote MCP servers (`src/wire_plugins.py`); for codex,
`request-egress` is the supported path. Other agents can use either the CLI or
the MCP tools.

## Broker token

`EGRESS_BROKER_TOKEN` is auto-provisioned per bottle at `./djinn up` and injected
into the container env by core (`up.sh` + `compose/docker-compose.local.yml`).
It is not a plugin secret slot — do not bind it in `common_secrets` /
`agent_secrets` or copy a value from `secrets.env`.

Port **8816** (host egress broker) is granted by core whenever
`capabilities.egress_broker` is enabled; this plugin does not declare
`host_port:`.
