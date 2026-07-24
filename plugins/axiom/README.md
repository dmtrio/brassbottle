# axiom

[Axiom](https://axiom.co)'s **official MCP server** (`mcp.axiom.co`), bridged to
**local stdio** with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) so
**every agent** (claude, cursor, gemini, codex) reaches it — not just Claude.
(pi is wired too, but pi MCP is inert until the `pi-mcp-adapter` extension is
installed, like every other plugin.) It gives agents tools to query your
observability data with APL: run queries, list datasets, inspect schemas, list
saved queries, and read monitors.

## Why the mcp-remote bridge

Axiom is a **remote** HTTP MCP on a real internet host. A raw remote server
(`url:` + `headers:`) only wires cleanly into Claude — cursor/gemini can't expand
`${VAR}` in remote headers. So instead this plugin runs Axiom's documented
bridge, `mcp-remote https://mcp.axiom.co/mcp` (a stdio↔HTTP proxy), as a **local
command server**. Local servers wire into every agent identically, so all agents
get Axiom through the ordinary local-plugin path. `mcp-remote` is baked into the
image at build time (the `install:` block) and the server execs that baked
binary directly (not `npx`), so startup never reaches for the npm registry — it
runs fully offline behind the egress firewall.

## Secret model — per-agent key with a global fallback

The Axiom token is `scope: agent, global: true`, and it **gates which agents get
Axiom**:

- **One global token** — put `AXIOM_TOKEN` in `secrets.env` and every agent
  shares it. This is the common case.
- **Per-agent tokens** — set `AXIOM_KEY_<agent>` and bind it under
  `agent_secrets`; only the agents that hold a key get Axiom (an agent with no
  token never sees the server). A per-agent key overrides the global one.

The token is delivered into each bound agent's shim env, and **`mcp-remote`
itself substitutes `${AXIOM_TOKEN}`** into the `Authorization` header at connect
time. So the secret is in **no MCP config file** and **never on the process
command line** (argv carries the literal `${AXIOM_TOKEN}`, not the value) — only
in the process environment, like every other agent credential here.

## Enable it

Global token for every agent — just set it in `secrets.env`:

```yaml
plugins: [axiom]
```

```
# secrets.env
AXIOM_TOKEN=xaat-xxxxxxxx…
```

Per-agent (only the listed agents get Axiom, each with its own token):

```yaml
plugins: [axiom]
agent_secrets:
  - {agent: cursor-agent, slot: AXIOM_TOKEN, secret: AXIOM_KEY_cursor}
  - {agent: claude,       slot: AXIOM_TOKEN, secret: AXIOM_KEY_claude}
```

```
# secrets.env
AXIOM_KEY_cursor=xaat-aaaa…
AXIOM_KEY_claude=xaat-bbbb…
```

You can combine them: a global `AXIOM_TOKEN` for everyone plus an
`agent_secrets` override for one agent. Because `mcp-remote` is baked in, adding
or changing tokens is a config-only `./up.sh <container>` — no image rebuild.

## Token

Create one in the Axiom console under **Settings → API tokens**.

- A **scoped API token** (`xaat-…`) is org-bound and needs no org id — simplest
  for the `Authorization: Bearer …` header this bridge sends.
- A **personal token** (`xapt-…`) also works but is tied to a user and may need
  an org id (`mcp-remote … --header "x-axiom-org-id: <id>"`); prefer a scoped
  token for an agent.
- `mcp.axiom.co` also supports interactive **OAuth**, but that needs a browser
  callback — impractical for a headless container, which is why this plugin
  sends a static Bearer token via `--header`.

## Tools

`queryApl`, `listDatasets`, `getDatasetSchema`, `getSavedQueries`,
`getMonitors`, `getMonitorsHistory`.
