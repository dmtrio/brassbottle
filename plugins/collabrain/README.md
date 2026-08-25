# collabrain

Durable wiring for the Agent Collab Hub memory faculty — basic-memory (the
vault index + search) and the collabrain CLI (capture, scheduling, review,
tripwires). Ships the **two MCP servers**, the **four long-running
processes** that keep them current, the **two volumes** that survive a
container recreate, and the **egress** the embedding model needs — as one
plugin, because all of it shares a single lifecycle (see
[PLN - Phase 1 Hardening §2](/artifacts/brain/records/PLN%20-%20Phase%201%20Hardening.md)).

```yaml
plugins: [collabrain]
```

## What it wires

| | |
|---|---|
| **MCP servers** | `basic-memory` (remote, `http://127.0.0.1:8801/mcp`, Claude only — see below), `basic-memory-stdio` (local, `bm mcp`, every other agent vendor), `collabrain` (local, `collabrain tripwires-mcp`, every agent) |
| **Services** (in-container, `services:`) | `bm-server`, `capture`, `scheduler`, `review` — see below |
| **Volumes** | `bm-state` → `/home/coder/.basic-memory`, `bm-hf-cache` → `/home/coder/.cache/huggingface` |
| **Egress** | `huggingface.co`, `hf.co` (basic-memory's embedding model download) |

### Services

Four processes, started idempotently at the end of `./djinn up` (`services:`
in `plugin.yml`, landing in brassbottle PR `[1/3]` on branch
`plugin-services`; this plugin is written to that schema — see **Dependency
on PR [1/3]** below). Each runs under a logging restart wrapper in its own
tmux session (`svc-<name>`); a repeat `./djinn up` restarts only whatever
died since the last run.

- **`bm-server`** — `bm mcp --transport streamable-http --host 127.0.0.1 --port 8801`.
  One long-lived process that is simultaneously the vault file watcher, the
  MCP endpoint the `basic-memory` entry reaches, and the **sole SQLite
  writer** for the index — replacing the old arrangement of an idle stdio
  `bm mcp` kept alive in tmux only for the watcher its lifespan happened to
  start.
- **`capture`** — `uv --directory /workspace/repos/collabrain run collabrain capture --watch`.
  Watches each harness's local transcript store and turns finished sessions
  into episodic notes.
- **`scheduler`** — `uv --directory /workspace/repos/collabrain run collabrain schedule`.
  The stdlib scheduler process (distill every 10 min, nightly consolidate,
  weekly compact/reindex — PLN §1). Writes its own run log and the
  hydration-seed staleness banner; this plugin does not touch that logic,
  only keeps the process alive.
- **`review`** — `uv --directory /workspace/repos/collabrain run collabrain review --port 8830`.
  The weekly review UI.

### Port claims

- **8801** (`bm-server`) binds `127.0.0.1` **inside the container only** —
  no `host_port:`, no firewall grant, no host exposure. Every MCP client
  reaching it (Claude via the `basic-memory` url entry, or any in-container
  process) is already inside the container's network namespace.
- **8830** (`review`) is likewise container-local. Reach it from the Mac via
  VS Code's own port forwarding (`Ports` panel → forward `8830`), the same
  pattern the parent PLN's weekly-sitting workflow assumes — not via this
  plugin's `host_port:`/`plugin_ports:` mechanism, which is for a Mac-side
  service dialing *in*, not a person forwarding a container port *out*.

### The collabrain repo is a dependency, not something this plugin provides

Every `collabrain` invocation above runs via `uv --directory
/workspace/repos/collabrain run collabrain …` — this plugin does **not**
clone, install, or bake the collabrain repo. The container's own manifest
must declare it under `repos:`:

```yaml
repos: [https://github.com/dmtrio/collabrain]
plugins: [collabrain]
```

Without that repo checkout present at `/workspace/repos/collabrain`, every
one of `capture`/`scheduler`/`review`/the `collabrain` MCP entry fails at
process start (visible in its `svc-<name>` tmux session / MCP client error,
not silently).

## The Claude-only caveat on the `basic-memory` MCP entry

brassbottle wires a plugin's **url-form** MCP entry into Claude's
`.mcp.json` only — `src/wire_plugins.py`'s own module docstring: a
non-agent-scoped plugin's spec is LOCAL (`{command, args}` — stdio, wired
into *every* installed agent) or env-scoped REMOTE (`{url, headers}` — http,
wired into *Claude's* `.mcp.json` only). Multi-vendor url wiring is a known
pending expansion, not yet built.

Because `basic-memory` is the arrangement we actually want — one process,
one SQLite writer, one file watcher, reached over a network interface even
though everything is local (PLN §2, citing parent §9's access-pattern rule)
— this plugin ships it as the url entry, accepting that non-Claude agents
don't get it that way. Instead they get **`basic-memory-stdio`**: a second,
distinct MCP server name (an MCP entry cannot be url-form for one agent and
stdio-form for another under a single name — the shape is per-entry, not
per-agent, so this plugin ships two entries rather than one) wired as plain
`command: bm, args: [mcp]` local stdio, reaching every other agent vendor.

**Trade-off, stated plainly:** a stdio `bm mcp` spawned by a non-Claude
agent is a second SQLite writer for as long as that agent's session holds it
open. That is the same multi-writer exposure the parent PLN's entry gate
0(d) already characterises as tolerable (vault is git-versioned; a bad
interleave is recoverable). It shrinks to zero once brassbottle's url wiring
goes multi-vendor — at which point `basic-memory-stdio` can retire.

## Dependency on PR [1/3] (`services:` schema)

`services:` does not exist in `src/manifest.py` on `main` yet. It is added
by a separate PR, `[1/3]` of this workstream, on branch `plugin-services`
(`src/manifest.py` validation + `up.sh` startup + the new
`src/plugin_services.py` renderer). This plugin (`[2/3]`) is written to that
schema and depends on `[1/3]` landing first — see the repo's PR ordering
convention (`[N/3]` titles, dependencies noted in the body, never a stacked
base).

Until `[1/3]` merges, `src/manifest.py` has no `services:` key at all — but
it also has **no top-level-key allowlist for `plugin.yml`** (unlike agent
descriptors), so an unrecognized `services:` map is silently ignored rather
than rejected. This plugin's `mcp:`/`volumes:`/`egress:` entries validate
and wire correctly against `main` today; only the four processes stay
unstarted until `[1/3]` is merged and this container's image is rebuilt.

## Notes

- **Local `install:`** (`uv tool install basic-memory==0.22.1`) bakes into
  the shared image — enabling this plugin on a new container requires an
  image rebuild, same as any other local plugin (`serena`, `codebase-memory`).
- **Volumes are per-container**, like every plugin volume — see
  [`plugins/README.md`](../README.md#volumes--state-that-survives-a-recreate).
  `./djinn down <container> --purge` removes them (a full re-embed of the
  vault on the next `up`); a plain `down` keeps them.
- **No `host_port:`.** Nothing in this plugin dials the host, and nothing on
  the host dials into it — both servers are container-internal, reached over
  `127.0.0.1` from inside, or via an operator's own VS Code port forward for
  `review`.
