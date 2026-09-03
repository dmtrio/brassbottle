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
  run.sh         optional — a host-side service, started with ./djinn service <name>
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
`~/.codex/AGENTS.md`, `~/.kimi-code/AGENTS.md`); an interactive-shell hook
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
| [`gateway`](gateway/) | remote HTTP | `./djinn service gateway` | [README](gateway/README.md) |
| [`proxyman`](proxyman/) | local (stdio bridge → host :8813, baked) | `./djinn service proxyman` | [README](proxyman/README.md) |
| [`browser`](browser/) | local (stdio bridge → host :8814, baked) | `./djinn service browser <container>` | [README](browser/README.md) |
| [`obsidian-annotated`](obsidian-annotated/) | remote HTTP (real host) | — | [README](obsidian-annotated/README.md) |
| [`axiom`](axiom/) | remote HTTP (real host) | — | [README](axiom/README.md) |
| [`annotated-watch`](annotated-watch/) | env-only (no server) | — | [README](annotated-watch/README.md) |
| [`openrouter`](openrouter/) | env-only (no server) | — | [README](openrouter/README.md) |
| [`ngrok`](ngrok/) | CLI binary + secret (no server) | — | [README](ngrok/README.md) |
| [`rhinomcp-official`](rhinomcp-official/) | remote HTTP (in-Rhino server, official) | Rhino: `MCPStart` | [README](rhinomcp-official/README.md) |
| [`rhinomcp`](rhinomcp/) | local (stdio bridge → host TCP 1999, baked) | Rhino: `mcpstart` | [README](rhinomcp/README.md) |
| [`cordyceps`](cordyceps/) | remote HTTP (in-Grasshopper server) | GH: Cordyceps component | [README](cordyceps/README.md) |
| [`egress`](egress/) | local (stdio, baked) | host broker on 8816 | [README](egress/README.md) |

## `plugin.yml` schema

No `type:` field — the entry shape decides. An `mcp:` entry carries **exactly
one** of `command:` (local) or `url:` (remote).

`url:` declares an **endpoint**, not a transport. How each agent reaches it is
`src/wire_plugins.py`'s decision, made per agent from its descriptor: an agent
that can hold a `${VAR}` inside a remote header (claude, codex, kimi) gets a
native remote entry; one that cannot (cursor-agent, pi, antigravity-cli) gets an
`mcp-remote` stdio shim carrying the same `${VAR}`. Neither writes a key to
disk, and the shim drops away by itself the moment an agent gains native
support. **Do not hand-write the bridge into a plugin** — a `command:
mcp-remote` entry forces every agent onto the shim, including the three that
don't need it.

That axis is independent of `requires:`, which decides whether the server is
*uniform* (same entry for everyone) or *agent-scoped* (wired per agent from that
agent's own key):

| | `requires:` — agent-scoped | no `requires:` — uniform |
|---|---|---|
| **local** (`command:`) | every bound agent | every agent |
| **remote** (`url:`) | every bound agent, native or shimmed | Claude only |

| Key | Meaning |
|---|---|
| `mcp: {<server>: {command, args}}` | **Local** stdio server, runs in the container. Requires `install:` unless `command` is a base tool (below). |
| `mcp: {<server>: {url, headers}}` | **Remote** HTTP endpoint, on the host or internet. With `requires:`, wiring renders it per agent (native or `mcp-remote` shim — see above); without, it reaches Claude only. |
| `install: \|` | Bash run at **image build** (full network). Required iff a local `command:` entry exists whose `command` is not a **base tool** — a binary the image already provides, listed in `BASE_IMAGE_BINS` (`src/manifest.py`) and installed by the `Dockerfile`. Today that is `mcp-remote` only. Checked per **server**: a plugin running `mcp-remote` for one server and its own binary for another still needs `install:` for the second. Interpreters (`bash`, `python3`) are deliberately **not** base tools — an entry means "exec'ing this *is* the whole server", which is false of a wrapper whose real payload sits in `args`. |
| `host_port: <int>` | Opens the container firewall to `host.docker.internal:<port>`. Valid with a remote (`url:`) server **or** a local bridge that dials the host (`rhinomcp`); rejected with no MCP server, and a local-only plugin must reference `${HOST_PORT}` in the bridge's `command`/`args` (proof something dials the grant). `${HOST_PORT}` in a remote `url` or a local `command`/`args` resolves to it. A manifest `plugin_ports:` override replaces this default (see below). |
| `secrets: {<SLOT>: {hint: "…"}}` | Secret slots. Every slot resolves through the same common-default / per-agent-override model. `hint` is shown when a declared common source is missing. A plugin may have `secrets:` and **no** `mcp:` (env-only). |
| `volumes: {<name>: /container/path}` | Per-container named volume(s) for state that must outlive a container recreate — see below. Mounted only in containers that enable the plugin. |
| `services: {<name>: "<command>"}` | In-container process(es) started at `up` — see below. Mounted only for enabled plugins. |
| `setup: "<command>"` | One command run once per `up` inside the container, before `services:` start — see below. One command per plugin; re-runs every `up` and must be idempotent. |
| `requires: [<SLOT>, …]` | Optional MCP-server field making the server **agent-scoped**: configured only for agents holding every required slot, each from its own resolved key. The key stays a `${SLOT}` ref in every config file — a native entry's ref is expanded by the agent, a shim's by `mcp-remote` from the agent's own process env — so it reaches no file and no command line. |
| `egress: [host, …]` | Bare hostnames added to this container's firewall allowlist. |

**Local example** (`serena`):

```yaml
install: |
  uv tool install -p 3.13 serena-agent
mcp:
  serena: {command: bash, args: [-c, 'exec serena start-mcp-server --context ide-assistant --project "$PWD"']}
egress: [blob.core.windows.net]
```

**Remote example** (`cordyceps`) — no secret, so no `requires:`, so the same
entry for everyone. Without a key to resolve per agent there is nothing to
shim, and a uniform remote reaches Claude only:

```yaml
host_port: 26929
mcp:
  cordyceps:
    url: http://host.docker.internal:${HOST_PORT}/mcp
```

**Credentialed remote example** (`gateway`) — same `url:` shape, plus
`requires:`, which makes it agent-scoped: only agents with an effective
`MCP_GATEWAY_TOKEN` get it, each from its own key. Note there is no
`mcp-remote` anywhere in the declaration — wiring adds the shim for the agents
that need one, so this reaches *every* bound agent, not just Claude:

```yaml
host_port: 8811
secrets:
  MCP_GATEWAY_TOKEN: {hint: "gateway (run ./djinn service gateway once)"}
mcp:
  coding:
    url: http://host.docker.internal:${HOST_PORT}/mcp
    headers: {Authorization: "Bearer ${MCP_GATEWAY_TOKEN}"}
    requires: [MCP_GATEWAY_TOKEN]
```

## `volumes` — state that survives a recreate

Anything a plugin writes into the container's filesystem is image layer: it
dies with the container, so every `./djinn up` costs a rebuild of that state (a
code index, a downloaded model, a language-server cache). Declare a volume and
it survives instead:

```yaml
volumes:
  cbm-cache: /home/coder/.cache/codebase-memory-mcp
```

Compose prefixes the name with the project, so `cbm-cache` is really
`djinn-<container>_cbm-cache` — **per container**, exactly like the auth
volumes, and removed by `./djinn down <container> --purge` (never by a plain
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
more `-f`, the same way the ssh overlay works. De-list the plugin and the
next `up` drops the mount (the volume itself waits for a `--purge`).

Ownership is handled centrally, not by each plugin: docker seeds a fresh named
volume from the image directory it covers, *including ownership*, so a
mountpoint the image doesn't contain would arrive root-owned and the coder-run
agent could not write to it. The overlay passes the paths to the entrypoint as
`PLUGIN_VOLUME_PATHS` and it chowns them to coder as root, before any agent
starts — walking up to fix the missing **parents** docker created root-owned
too, stopping at the first directory that is already coder's. So a plugin
declares the volume and does not have to `mkdir` anything, at any depth.

## `services` — in-container processes started at `up`

Anything a plugin needs kept running inside the container — a long-lived MCP
backend, a watcher, a scheduler — declares it here instead of a hand-started
tmux session that dies with the container:

```yaml
services:
  bm-server: "bm mcp --transport streamable-http --host 127.0.0.1 --port 8801"
  capture:   "uv --directory /workspace/repos/collabrain run collabrain capture --watch"
```

At the end of `./djinn up`, for each declared service of an **enabled**
plugin, `src/plugin_services.py` (host-side, like `src/manifest.py`) renders
one idempotent script and `up.sh` pipes it into one `docker exec … bash` per
service:

- Runs detached in a tmux session named `svc-<name>` — `tmux has-session -t
  svc-<name>` gates the whole thing, so a service still running is left
  alone and re-running `./djinn up` only restarts what died.
- The command runs under a small restart loop: on exit it logs a timestamp
  and exit code to `/tmp/djinn-services/<name>.log` **inside the
  container**, then restarts — 5s between attempts, escalating to 30s once
  restarts keep happening faster than a minute apart (a crash loop stays
  loud in the log instead of spinning silently). Nothing caps the retry
  count.
- The log path is deliberately generic and container-local (`/tmp`, not a
  mounted volume) — this mechanism has no idea what a service's own durable
  logging needs are. A service that wants logs to survive a recreate writes
  them itself under a path the plugin's own `volumes:` mounts.

Rules, enforced by `src/manifest.py` at derive time:

| Rule | Why |
|---|---|
| Name: lowercase letters, digits, dash (`[a-z0-9-]+`) | Becomes the tmux session name (`svc-<name>`) and the log/script filename — a boring charset keeps it stable across the docker-exec/tmux/heredoc hops. |
| Command: a non-empty string | It runs verbatim inside the restart loop; an empty command would loop forever doing nothing while still "restarting" every 5s. |
| Unique across every **enabled** plugin | Service names share one tmux/log namespace — two plugins racing for `svc-<name>` would silently restart each other's process. Two containers each running a different plugin that happens to reuse a name are unaffected (only enabled plugins contend). |

## `setup` — one-shot wiring at `up`

Some plugins need a **live container** to finish wiring: `herdr integration
install claude` writes into the `~/.claude` volume (which the image cannot
pre-populate), `gh extension install` mutates a user config on a volume. That
is what `setup:` is for — one command, run inside the container:

```yaml
setup: "herdr integration install claude"
```

At the end of `./djinn up`, for each **enabled** plugin declaring `setup:`,
`src/plugin_setup.py` (host-side, like `src/plugin_services.py`) renders one
script and `up.sh` pipes it into one `docker exec … bash` per plugin — after
the MCP wiring is written, **before** `services:` start:

- Runs as **coder**, in the foreground — `up` waits for it. Re-runs on
  **every `./djinn up`**, so the command MUST be idempotent (`herdr
  integration install claude` is: it reports `current` instead of rewriting).
- The firewall is already up, so `setup:` **wires what `install:` already
  fetched** at image build — it never downloads anything.
- **One command per plugin.** A plugin needing several commands chains them
  (`cmd1 && cmd2`) or ships a script in `install:` and calls it.
- The command's output is appended to `/tmp/djinn-setup/<plugin>.log`
  **inside the container** (start/end timestamps, duration and exit code per
  run), so the `up` console carries one summary line per plugin. The log does
  not survive a recreate; a plugin wanting durable logs writes them itself
  under its own `volumes:` mount.
- A failure prints `! setup <plugin> FAILED code=N — see /tmp/djinn-setup/<plugin>.log`
  and is **non-fatal to `up`** — the same posture as a failed service start.
- The command runs with **stdin from `/dev/null`** and under a **300 s
  `timeout`** (exit 124, reported as a failure): `up` waits for it, so a
  command that prompts or hangs must fail, not stall `up`.

Rules, enforced by `src/manifest.py` at derive time:

| Rule | Why |
|---|---|
| Command: a non-empty string | It runs verbatim at `up`; an empty command is always a typo. |
| No tabs or newlines in the command | The `PLUGIN_SETUP` export is TAB-separated lines — a tab or newline corrupts it. Chain with `&&` or call a script. |
| One command per plugin | The plugin name is the namespace (log file, script file); a map would suggest an ordering the per-plugin exec loop does not define. |
| No uniqueness constraint between plugins | Each command runs in its own exec under its own name — plugins cannot collide the way `services:` names can. |

## `plugin_ports` — per-container host port

Host-service plugins (`host_port:` in `plugin.yml`) listen on a Mac port. Host
ports are exclusive, so two containers running the same plugin need different
values — the same reason `ssh.port` is per-container.

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

- **`djinn up`** (via `up.sh`) globs `plugins/*/plugin.yml` → `src/manifest.py --derive` validates
  and derives the wiring → `src/wire_plugins.py` (baked in the image) writes each
  agent's MCP config.
- **`Dockerfile`** bakes every local plugin's `install:` block at build, plus
  the base tools (`ARG MCP_REMOTE_VERSION`) that plugins are excused from
  installing themselves.
- **Compose** gets a generated overlay when an enabled plugin declares
  `volumes:` (`$BASE_PATH/compose/<container>.plugins.yml`, one more `-f`); the
  entrypoint chowns its mountpoints to coder.
- At the end of `up.sh`, each declared `setup:` command of an enabled plugin
  runs once inside the container (before `services:` start) — see `setup`
  above. Then each declared `services:` entry of an enabled plugin is started
  (idempotently) — see `services` above.
- **`djinn service <name>`** (via `service.sh`) runs `plugins/<name>/run.sh` on
  the host (resolves `BASE_PATH` and hands it down); it never touches docker.
  Not to be confused with `services:` above — `run.sh` is a **host**-side
  process the user starts by hand; `services:` are **in-container** processes
  `up.sh` starts automatically.

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
   environment; started via `./djinn service <name>`).
3. Enable it in a manifest: `plugins: [<name>]` (+ a secret binding if it
   declares `secrets:`).
4. **Local** plugin → rebuild the image so `install:` bakes. **Remote** → just
   rerun `./djinn up <container>`.
5. Writes state worth keeping across a recreate (an index, a cache, a
   downloaded model)? Declare `volumes:` (see above) — no compose edit.
6. Tests, if the plugin has logic worth pinning: `plugins/<name>/test_*.py`,
   discovered automatically (see above).
7. Add `plugins/<name>/README.md` (human docs) and, if the agent needs guidance
   on *using* the tools, `plugins/<name>/AGENTS.md` (merged into enabled
   containers' rules — see above). The fragment is baked with the image, so a
   change to it needs a rebuild, like `install:`.

No `djinn` / `Dockerfile` edits — the loader globs the directory.
