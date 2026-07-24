# Plugins

A **plugin** is one directory — `plugins/<name>/` — that describes an MCP server
(or just a secret) a container can get. A manifest opts in by name:

```yaml
plugins: [serena, gateway, obsidian-annotated]
```

Unlisted plugins stay dormant in the shared image. The directory name **is** the
plugin name.

```
plugins/<name>/
  plugin.yml     required — what the server is and what it needs
  run.sh         optional — a host-side service, started with ./service.sh <name>
  README.md      optional — human docs for this plugin
  AGENTS.md      optional — agent-facing usage guidance (when/how to use the
                 tools). Merged into each agent's global rules, but ONLY in
                 containers whose manifest enables this plugin.
```

### `AGENTS.md` — agent usage guidance

`plugin.yml` wires the server; `AGENTS.md` tells the agent *when and how to use
it*. It is a heading-scoped markdown fragment (own your `##`/`###`; no top-level
`#` title). At `up`, `src/compose_rules.py` appends the fragments of the
**enabled** plugins to each agent's global rules file (base rules from the
read-only `/agent-rules` mount + fragments → `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`); an interactive-shell hook
recomposes so base edits stay live. No fragment (or the plugin not enabled) ⇒
nothing merged. This complements a server's own MCP instructions — it also
covers env-only plugins, servers you don't control, and container-specific
opinion.

## Shipped plugins

| Plugin | Kind | Host service | Docs |
|---|---|---|---|
| [`serena`](serena/) | local (stdio, baked) | — | [README](serena/README.md) |
| [`archex`](archex/) | local (stdio, baked) | — | [README](archex/README.md) |
| [`gateway`](gateway/) | remote HTTP | `./service.sh gateway` | [README](gateway/README.md) |
| [`proxyman`](proxyman/) | remote HTTP | `./service.sh proxyman` | [README](proxyman/README.md) |
| [`browser`](browser/) | remote HTTP | `./service.sh browser` | [README](browser/README.md) |
| [`obsidian-annotated`](obsidian-annotated/) | remote HTTP (real host) | — | [README](obsidian-annotated/README.md) |
| [`axiom`](axiom/) | local (stdio bridge → mcp.axiom.co, baked) | — | [README](axiom/README.md) |
| [`annotated-watch`](annotated-watch/) | env-only (no server) | — | [README](annotated-watch/README.md) |

## `plugin.yml` schema

No `type:` field — the entry shape decides. An `mcp:` entry carries **exactly
one** of `command:` (local) or `url:` (remote).

| Key | Meaning |
|---|---|
| `mcp: {<server>: {command, args}}` | **Local** stdio server, runs in the container. Requires `install:`. |
| `mcp: {<server>: {url, headers}}` | **Remote** HTTP server, reached on the host or internet. |
| `install: \|` | Bash run at **image build** (full network). Required iff a local `command:` entry exists. |
| `host_port: <int>` | Remote-only; opens the container firewall to `host.docker.internal:<port>`. |
| `secrets: {<SLOT>: {hint: "…"}}` | Secret slots. Every slot resolves through the same common-default / per-agent-override model. `hint` is shown when a declared common source is missing. A plugin may have `secrets:` and **no** `mcp:` (env-only). |
| `requires: [<SLOT>, …]` | Optional MCP-server field. The server is configured only for agents with every required slot; uncredentialed servers omit it. |
| `egress: [host, …]` | Bare hostnames added to this container's firewall allowlist. |

**Local example** (`serena`):

```yaml
install: |
  uv tool install -p 3.13 serena-agent
mcp:
  serena: {command: bash, args: [-c, 'exec serena start-mcp-server --context ide-assistant --project "$PWD"']}
egress: [blob.core.windows.net]
```

**Remote example** (`gateway`):

```yaml
host_port: 8811
secrets:
  MCP_GATEWAY_TOKEN: {hint: "gateway (run ./service.sh gateway once)"}
mcp:
  coding:
    url: http://host.docker.internal:8811/mcp
    headers: {Authorization: "Bearer ${MCP_GATEWAY_TOKEN}"}
    requires: [MCP_GATEWAY_TOKEN]
```

## Binding secrets in a manifest

Slots declare *what* a plugin needs; `common_secrets` supplies an explicit
default and `agent_secrets` overrides or removes it for one agent:

```yaml
plugins: [gateway, obsidian-annotated]
common_secrets: [MCP_GATEWAY_TOKEN]            # default source has the same name
# Or remap: {MCP_GATEWAY_TOKEN: GATEWAY_PROD_TOKEN}
agent_secrets:
  - {agent: claude, slot: OBSIDIAN_ANNOTATED_KEY, secret: OBSIDIAN_KEY_me_claude}
  - {agent: cursor-agent, slot: MCP_GATEWAY_TOKEN, secret: CURSOR_GATEWAY_TOKEN}
  - {agent: pi, slot: MCP_GATEWAY_TOKEN, disabled: true}
```

Resolution is `disabled` → no value, then agent `secret` override, then the
common default. A server with `requires:` is omitted for an agent missing any
required value. Sources resolve by name only — never value — against
`secrets.env`; an unset override is a hard error, while an unset common default
warns and yields no binding.

## How it loads

- **`up.sh`** globs `plugins/*/plugin.yml` → `src/manifest.py --derive` validates
  and derives the wiring → `src/wire_plugins.py` (baked in the image) writes each
  agent's MCP config.
- **`Dockerfile`** bakes every local plugin's `install:` block at build.
- **`service.sh <name>`** runs `plugins/<name>/run.sh` on the host (resolves
  `BASE_PATH` and hands it down); it never touches docker.

## Adding a plugin

1. `mkdir plugins/<name>` and write `plugin.yml` (see schema above).
2. Needs a Mac-side service? Add `run.sh` (reads `BASE_PATH` from the
   environment; started via `./service.sh <name>`).
3. Enable it in a manifest: `plugins: [<name>]` (+ a secret binding if it
   declares `secrets:`).
4. **Local** plugin → rebuild the image so `install:` bakes. **Remote** → just
   rerun `./up.sh <container>`.
5. Add `plugins/<name>/README.md` (human docs) and, if the agent needs guidance
   on *using* the tools, `plugins/<name>/AGENTS.md` (merged into enabled
   containers' rules — see above). The fragment is baked with the image, so a
   change to it needs a rebuild, like `install:`.

No `up.sh` / `Dockerfile` edits — the loader globs the directory.
