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
  test_*.py      optional — unit tests, discovered automatically (below)
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
| [`codebase-memory`](codebase-memory/) | local (stdio, baked) | — | [README](codebase-memory/README.md) |
| [`gateway`](gateway/) | remote HTTP | `./service.sh gateway` | [README](gateway/README.md) |
| [`proxyman`](proxyman/) | remote HTTP | `./service.sh proxyman` | [README](proxyman/README.md) |
| [`browser`](browser/) | remote HTTP | `./service.sh browser <container>` | [README](browser/README.md) |
| [`obsidian-annotated`](obsidian-annotated/) | remote HTTP (real host) | — | [README](obsidian-annotated/README.md) |
| [`axiom`](axiom/) | local (stdio bridge → mcp.axiom.co, baked) | — | [README](axiom/README.md) |
| [`annotated-watch`](annotated-watch/) | env-only (no server) | — | [README](annotated-watch/README.md) |
| [`ngrok`](ngrok/) | CLI binary + secret (no server) | — | [README](ngrok/README.md) |
| [`rhino-mcp`](rhino-mcp/) | remote HTTP (in-Rhino server, official) | Rhino: `MCPStart` | [README](rhino-mcp/README.md) |
| [`rhinomcp`](rhinomcp/) | local (stdio bridge → host TCP 1999, baked) | Rhino: `mcpstart` | [README](rhinomcp/README.md) |
| [`cordyceps`](cordyceps/) | remote HTTP (in-Grasshopper server) | GH: Cordyceps component | [README](cordyceps/README.md) |

## `plugin.yml` schema

No `type:` field — the entry shape decides. An `mcp:` entry carries **exactly
one** of `command:` (local) or `url:` (remote).

| Key | Meaning |
|---|---|
| `mcp: {<server>: {command, args}}` | **Local** stdio server, runs in the container. Requires `install:`. |
| `mcp: {<server>: {url, headers}}` | **Remote** HTTP server, reached on the host or internet. |
| `install: \|` | Bash run at **image build** (full network). Required iff a local `command:` entry exists. |
| `host_port: <int>` | Opens the container firewall to `host.docker.internal:<port>`. Valid with a remote (`url:`) server **or** a local bridge that dials the host (`rhinomcp`); rejected on a plugin with no MCP server. `${HOST_PORT}` in a remote `url` or a local server's `args` resolves to it. A manifest `plugin_ports:` override replaces this default (see below). |
| `secrets: {<SLOT>: {hint: "…"}}` | Secret slots. Every slot resolves through the same common-default / per-agent-override model. `hint` is shown when a declared common source is missing. A plugin may have `secrets:` and **no** `mcp:` (env-only). |
| `volumes: {<name>: /container/path}` | Per-container named volume(s) for state that must outlive a container recreate — see below. Mounted only in containers that enable the plugin. |
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

## `volumes` — state that survives a recreate

Anything a plugin writes into the container's filesystem is image layer: it
dies with the container, so every `./up.sh` costs a rebuild of that state (a
code index, a downloaded model, a language-server cache). Declare a volume and
it survives instead:

```yaml
volumes:
  cbm-cache: /home/coder/.cache/codebase-memory-mcp
```

Compose prefixes the name with the project, so `cbm-cache` is really
`dev-agent-<container>_cbm-cache` — **per container**, exactly like the auth
volumes, and removed by `./down.sh <container> --purge` (never by a plain
`down`). Two containers running the same plugin get separate volumes.

The rules, all enforced by `src/manifest.py` at derive time:

| Rule | Why |
|---|---|
| Name: ≥2 chars, opens on a letter or digit, `[A-Za-z0-9_-]` | Compose reads a 1-character source as a Windows drive letter — the mount silently loses its `source`, `compose config` still exits 0, and only `docker up` fails, naming a path nobody wrote. |
| Path: under `/home/coder/`, and not `/home/coder` itself | Everything runs as coder, so that is where plugin state belongs. It also makes `/usr/local/bin`, `/etc`, and the workspace tree unreachable — a volume there freezes or hides real image content. |
| Path charset: letters, digits, `. _ - + @` | Compose interpolates `$VAR` in **every** `-f` file, so a `$` lets the real mount target differ from the declared one; the entrypoint's word-split loop also globs, so a `*` would chown a different directory than the one mounted. |
| No overlap with a compose mount (`workspace`, the auth volumes, `/artifacts`, …) — as the same path, a **parent**, or a **child** | Compose merges by key. A volume at a parent freezes that whole tree (a rebuilt image never reaches the container again); at a child it hides live content. Both are silent, surfacing only as lost auth or missing repos. |
| No overlap with another enabled plugin's mount, same three ways | Same failure, between plugins. |

Overlap is compared component-wise, so `/home/coder/.curse` is not treated as
containing `/home/coder/.cursor`.

Nothing outside `plugins/<name>/` names the plugin. At `up`, `src/manifest.py`
renders the volumes of the **enabled** plugins into a generated compose overlay
under `$BASE_PATH/compose/<container>.plugins.yml` and `up.sh` adds it as one
more `-f`, the same way the ssh/mosh overlays work. De-list the plugin and the
next `up` drops the mount (the volume itself waits for a `--purge`).

Ownership is handled centrally, not by each plugin: docker seeds a fresh named
volume from the image directory it covers, *including ownership*, so a
mountpoint the image doesn't contain would arrive root-owned and the coder-run
agent could not write to it. The overlay passes the paths to the entrypoint as
`PLUGIN_VOLUME_PATHS` and it chowns them to coder as root, before any agent
starts — walking up to fix the missing **parents** docker created root-owned
too, stopping at the first directory that is already coder's. So a plugin
declares the volume and does not have to `mkdir` anything, at any depth.

## `plugin_ports` — per-container host port

Host-service plugins (`host_port:` in `plugin.yml`) listen on a Mac port. Host
ports are exclusive, so two containers running the same plugin need different
values — the same reason `ssh.port` and `remote.mosh_ports` are per-container.

Set `plugin_ports:` in the **manifest** (not in `plugin.yml`):

```yaml
plugins: [browser]
plugin_ports:
  browser: 8815    # overrides plugins/browser/plugin.yml host_port: 8814
```

The override re-points **both** the firewall grant (`HOST_MCP_PORTS`) and any
`${HOST_PORT}` placeholder in the plugin's remote `url` — or in a local
bridge's `args` (`rhinomcp`) — so the port stays a single source of truth. A
plugin may use `${HOST_PORT}` there instead of a literal port to stay in sync
with `host_port` / `plugin_ports`.

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
- **Compose** gets a generated overlay when an enabled plugin declares
  `volumes:` (`$BASE_PATH/compose/<container>.plugins.yml`, one more `-f`); the
  entrypoint chowns its mountpoints to coder.
- **`service.sh <name>`** runs `plugins/<name>/run.sh` on the host (resolves
  `BASE_PATH` and hands it down); it never touches docker.

### `test_*.py` — unit tests

Put tests beside the code they cover. Any `plugins/<name>/test_*.py` is picked
up automatically by `tests/test_plugin_suites.py` — there is nothing to
register:

```bash
cd plugins/<name> && python3 -m unittest discover   # while hacking on it
python3 -m unittest discover -s tests               # everything
```

Each plugin directory is put on `sys.path` before its tests run, so a test can
`import <module_under_test>` directly, and modules are namespaced per plugin so
two plugins can both ship a `test_watch.py`. Test files are kept out of the
image by `plugins/*/test_*.py` in `.dockerignore`.

The loader also asserts that discovery found files and that every test file
yields at least one test case — a glob or import that quietly breaks would
otherwise leave the suite reporting OK while covering nothing.

## Adding a plugin

1. `mkdir plugins/<name>` and write `plugin.yml` (see schema above).
2. Needs a Mac-side service? Add `run.sh` (reads `BASE_PATH` from the
   environment; started via `./service.sh <name>`).
3. Enable it in a manifest: `plugins: [<name>]` (+ a secret binding if it
   declares `secrets:`).
4. **Local** plugin → rebuild the image so `install:` bakes. **Remote** → just
   rerun `./up.sh <container>`.
5. Writes state worth keeping across a recreate (an index, a cache, a
   downloaded model)? Declare `volumes:` (see above) — no compose edit.
6. Tests, if the plugin has logic worth pinning: `plugins/<name>/test_*.py`,
   discovered automatically (see above).
7. Add `plugins/<name>/README.md` (human docs) and, if the agent needs guidance
   on *using* the tools, `plugins/<name>/AGENTS.md` (merged into enabled
   containers' rules — see above). The fragment is baked with the image, so a
   change to it needs a rebuild, like `install:`.

No `up.sh` / `Dockerfile` edits — the loader globs the directory.
