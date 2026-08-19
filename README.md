# brassbottle

Isolated, firewalled Docker environments where AI coding agents work with
full permissions — locally on your Mac (or any Docker host). One container
per project, declared by a manifest. The assembly is a config.

brassbottle — the vessel; `djinn` — the word that calls them.
*(“Genie” traces back to Latin* genius loci *— a spirit bound to a place.)*

```
containers/<name>.yml  ──./djinn up <name>──►  djinn-<name>
        │                                      ├── agents: claude, codex, pi,
.djinn/secrets.env                             │   gemini, cursor-agent, aider
  (all secret values, gitignored;              ├── egress firewall (zone allowlist)
   move via DJINN_HOME)                        ├── /workspace (volume): repos/<name> + worktrees/
                                               ├── /agent-rules (ro): global rules + skills
rules/  (bundled default;                       ├── /artifacts → Mac-visible outbox
  override via RULES_PATH)                      └── per-agent identity via shims
```

(`./up.sh <name>` still works directly — `djinn` is a dispatcher in front of
it and the other scripts below.)

The repo is self-contained: a fresh clone runs with no external setup.
Runtime state (`secrets.env`, keys, artifacts) defaults to a gitignored
`./.djinn/`, and rules come from the bundled `rules/`. A gitignored
`./.env` overrides both — see [Prerequisites](#prerequisites).

## The two files you author

1. **`containers/<name>.yml`** — one manifest = one container: repos, memory,
   tools, capability grants, per-agent identities. Secret-free, committable.
   Copy `containers/TEMPLATE.yml` and edit.
2. **`secrets.env`** — every secret value, one file, mode 600, never mounted
   (default `./.djinn/secrets.env`, gitignored). Copy `secrets.env.example`
   and fill in what your manifests reference.

Everything else is derived: `./djinn up <name>` (idempotent) composes
credentials, applies the firewall, clones the repos, lays out worktrees, and
generates MCP configs. `./djinn down <name>` stops (code survives);
`--purge` forgets the container entirely (artifacts still survive).

## Prerequisites

- Docker Desktop (macOS) or Docker Engine (Linux)
- `yq` (`brew install yq`)
- `python3` (any 3.9+, stdlib only — present via Xcode CLT on macOS; on a
  minimal Linux box, `apt install python3`). `up.sh` (what `djinn up` calls)
  uses it for manifest validation and the wiring payload, preferring
  `/usr/bin/python3` over version-manager shims (`PYTHON3=/path` overrides).

That's it — the repo is self-contained. `djinn` keeps its runtime state
(secrets, keys, artifacts) in a gitignored `./.djinn/` and uses the
bundled `rules/`. To point at your own locations instead, drop a gitignored
`./.env` at the repo root:

```bash
DJINN_HOME="$HOME/djinn"                   # move the runtime home (secrets/keys/artifacts)
RULES_PATH="$HOME/git/agent-conf/rules"    # use your own rules repo instead of bundled rules/
CONTAINERS_PATH="$HOME/djinn/containers"   # read manifests from your own (private) dir
BRAVE_APP="$HOME/Applications/Brave Browser.app"   # browser plugin: app locations
CHROME_APP="/Applications/Chromium.app"            # (defaults are /Applications/…)
```

(When `DJINN_HOME` is set and `$DJINN_HOME/rules` exists, it's used as
the rules dir automatically — no need to set `RULES_PATH` too. The same applies
to manifests: if `$DJINN_HOME/containers` exists it's used automatically, so
you don't need to set `CONTAINERS_PATH` either.)

When your manifests are their own git repo, `djinn up` fast-forwards that
checkout before reading the manifest, so a merged manifest PR takes effect on
the next run rather than waiting for you to remember `git pull`. It never
touches the bundled `containers/` (that would pull brassbottle), never fails
the run — offline, no upstream, or a manifest you're mid-edit all just carry
on — and prints which case it took.

**Keep your manifests out of this repo.** Your real `containers/*.yml` carry
semi-private data (private repo URLs, LAN subnets, identity naming), so this
repo ships only `containers/TEMPLATE.yml`. Point manifests at a directory of
your own — e.g. `~/djinn/containers` (auto-detected) — and make *that* its
own private git repo. The tool stays public; your configs stay private and
versioned, with no second copy of the project to maintain.

## Quick start

```bash
cp containers/TEMPLATE.yml containers/my-app.yml
./djinn up my-app
```

`TEMPLATE.yml` runs unedited (omit/empty `repos:` → git-inits
`/workspace/repos/scratch`, no identities → needs no secrets), so a copy is a
working smoke test. Then edit it — repos, memory, capabilities, identities —
and rerun (idempotent). Add any secrets it references to `secrets.env`:

```bash
mkdir -p .djinn && cp secrets.env.example .djinn/secrets.env   # djinn up also creates it empty
```

Then attach: VS Code / Cursor → **"Dev Containers: Attach to Running
Container"** → `djinn-my-app` (lands as `coder` in `/workspace` — open
`dev.code-workspace` for the multi-root view). Terminal:
`docker exec -it -u coder djinn-my-app bash`. Recommended start:
`cd /workspace/repos && claude` (sees every repo); starting inside one repo
also works.

**First session per container** (persists across rebuilds in per-container
auth volumes): `claude` login; `codex` / `gemini` if used. Agents already
carry the GitHub machine-user token from `secrets.env`.

## Shell aliases (optional)

`djinn` (and the `up.sh` / `down.sh` / `service.sh` it dispatches to) resolves
its own location, so it runs from any directory. Drop these in `~/.bashrc` to
invoke it from anywhere (each subcommand lists its options when run with no
argument):

```bash
export DJINN_REPO="$HOME/git/brassbottle"      # adjust to your clone
alias djup="$DJINN_REPO/djinn up"              # djup <name>          (no arg → lists manifests)
alias djdown="$DJINN_REPO/djinn down"          # djdown <name> [--purge]
alias djsvc="$DJINN_REPO/djinn service"        # djsvc <name> [args]  (no arg → lists host services)
alias djallow="$DJINN_REPO/djinn allow"        # djallow <container> <domain>…
alias cddj="cd \$DJINN_REPO"
```

Tab-completion for container names (`djup`/`djdown`) and host services (`djsvc`):

```bash
_dj_ctr_dir() {   # delegates to common.sh (the single source of truth for
                   # CONTAINERS_PATH resolution) instead of duplicating its
                   # override/compat logic here; stderr note suppressed.
  bash -c '. "$DJINN_REPO/src/common.sh" 2>/dev/null; echo "$CONTAINERS_PATH"'
}
_dj_names() {
  local d f names=""; d="$(_dj_ctr_dir)"
  for f in "$d"/*.yml; do f=${f##*/}; [ "$f" = TEMPLATE.yml ] && continue; names="$names ${f%.yml}"; done
  COMPREPLY=($(compgen -W "$names" -- "${COMP_WORDS[COMP_CWORD]}"))
}
complete -F _dj_names djup djdown
_dj_services() {
  local p names=""
  for p in "$DJINN_REPO"/plugins/*/run.sh; do [ -e "$p" ] && names="$names $(basename "$(dirname "$p")")"; done
  COMPREPLY=($(compgen -W "$names" -- "${COMP_WORDS[COMP_CWORD]}"))
}
complete -F _dj_services djsvc
```

macOS defaults to zsh. The aliases work in `~/.zshrc` unchanged. For completion,
use the native zsh version below — `(N)` makes the globs no-match-safe. Needs
`compinit` to have run (frameworks like oh-my-zsh already do it):

```zsh
_dj_names_zsh() {   # container short-names; delegates to common.sh (single
                     # source of truth) instead of duplicating its
                     # override/compat logic here; stderr note suppressed.
  local dir
  dir=$(bash -c '. "$DJINN_REPO/src/common.sh" 2>/dev/null; echo "$CONTAINERS_PATH"')
  local -a names=(${dir}/*.yml(N:t:r)); names=(${names:#TEMPLATE})
  compadd -a names
}
compdef _dj_names_zsh djup djdown

_dj_services_zsh() {   # plugins that ship a run.sh (:h dir, :t tail = plugin name)
  local -a names=(${DJINN_REPO}/plugins/*/run.sh(N:h:t))
  compadd -a names
}
compdef _dj_services_zsh djsvc
```

## Firewall egress (`capabilities:`)

| Manifest key | Effect |
|---|---|
| `egress: [...]` | extra allowed zones (a zone covers its subdomains) |
| `egress_cidrs: [...]` | IP-range escape hatch (LAN subnets) |

A container without a grant cannot reach the zone — enforced by the
in-container firewall (dnsmasq resolver-driven ipset; rotating CDN DNS can't
outrun it).

> The old `gateway`/`proxyman`/`browser` capability flags are **plugins** now
> (see below). `capabilities: {gateway: true}` still works for one release but
> prints a deprecation warning — prefer `plugins: [gateway]`.

## Plugins (every MCP server is a file)

A **plugin** is a directory — `plugins/<name>/` — describing an MCP server (or
just a secret) a container can get. A manifest opts in by name; unlisted plugins
stay dormant in the shared image:

```yaml
plugins: [serena, gateway, obsidian-annotated]
```

Two shapes, decided by the entry (no `type:` field):

- **Local** — a stdio server baked into the image (`command:` + `install:`),
  wired into every installed agent. A local bridge may also dial a Mac-host
  service (`host_port:` + `${HOST_PORT}` in its args, like `rhinomcp`).
- **Remote** — an HTTP server (`url:`), on the Mac host (`host_port:`, started
  with `./djinn service <name>`) or a real internet host.

A plugin may also be **env-only** — a `secrets:` slot with no server. Slots are
`env`-scoped (one value shared by all agents) or `agent`-scoped (per-agent,
bound under the manifest's `agent_secrets:`). `djinn up` derives the wiring and
folds each plugin's `egress`/`host_port` into the firewall; de-listing one
removes its wiring on the next up.

**→ [`plugins/README.md`](plugins/README.md)** — the schema, how wiring works,
and how to add a plugin. Each **`plugins/<name>/README.md`** documents that
plugin.

## Secrets model

Secret **values** live in one file — `secrets.env` (mode 600, gitignored, never
mounted). Manifests and the Python modules handle only secret **names**; values
are resolved host-side at `up` time. Plugins declare **secret slots**. Every
slot uses one hybrid resolution order:

1. `common_secrets:` provides an explicit default source for every enabled
   agent.
2. `agent_secrets:` may replace that source for one agent.
3. `disabled: true` removes the slot for one agent.

An unset common source warns and provides no value; an unset per-agent override
hard-fails at `up`.

**Per-agent shims deliver them.** Each agent CLI is fronted by a shim that, at
process start, loads only that agent's `~/.agent-keys/<agent>.env` — its fully
resolved secret set — and overrides inherited env before exec'ing the real
binary. Two consequences:

- `cat <agent>.env` is the full audit of exactly what that agent sees.
- Delegation is safe: when claude spawns `cursor-agent -p`, the child's shim
  loads *its* identity — the invoker's credentials never leak.

GitHub rides the same path: agents act as the machine user (`GH_TOKEN`); your
personal login never enters a container unless you `gh auth login` there, and
agent PRs/comments show as the bot (you review and merge as you).

**Per-org git identity.** When one machine user can't reach every repo (it isn't
a member of every org), give the container its own token — and route by repo
**owner** — from the manifest's `git:` block:

```yaml
git:
  name:  "Fry Agent"
  email: "agent+fry@example.com"
  token: GH_TOKEN_fry              # this container's default credential (secrets.env var NAME)
  orgs:                            # optional per-owner overrides (multi-org containers only)
    planetexpress:
      token: GH_TOKEN_planetexpress   # secrets.env var NAME
      name:  "Leela Bot"              # optional — repo-local identity for planetexpress/* repos
      email: "bot@planetexpress.example"
```

`token`/`orgs.*.token` name vars in `secrets.env` (values never enter the
manifest). At `up`, a repo owned by `<owner>` authenticates with `GH_TOKEN_<owner>`
if set, else the container's `git.token`, else the global `GH_TOKEN` — resolved by
the `git-credential-org` helper on every `github.com` fetch/push. A `git.orgs`
owner with a `name`/`email` also gets that identity stamped repo-locally, so its
commits carry the right author. A `token:` naming a var that isn't in `secrets.env`
**hard-fails the apply** — never a silent fall-back to the wrong identity. This is
routing + attribution, not isolation: every org's token sits in each agent's
`<agent>.env`, so a repo whose token must be unreachable by other work belongs in
a separate container.

## Rules & skills (shared knowledge, never shared identity)

The rules dir mounts read-only at `/agent-rules` in every container:
`AGENTS.md` fans out as every agent's global rules file, `skills/` as
Claude's skills. By default this is the repo's bundled `rules/`; set
`RULES_PATH` (in `./.env`) to your own rules repo — e.g. `~/git/agent-conf/rules`
— to override. Rule layers: global → `/workspace/rules.local.md`
(container-local, uncommitted) → the project repo's own CLAUDE.md.
Agents propose rule changes via PR; for an external rules repo, `djinn up`
`git pull`s it each run so merged changes land in every container.

## Persistence map

| State | Lives in | Survives recreate | Survives `--purge` |
|---|---|---|---|
| Code | workspace volume (+ git) | ✓ | ✗ (git: forever) |
| Agent logins, MCP approvals | per-container auth volumes | ✓ | ✗ |
| Identity keys | `secrets.env` (composed at up) | ✓ | ✓ |
| Rules & skills | bundled `rules/` (or your `RULES_PATH` repo) | ✓ | ✓ |
| Non-code outputs | `$DJINN_HOME/artifacts/<name>/` (`/artifacts`) | ✓ | ✓ |

## Repo map

- `djinn` — the dispatcher you run from the repo root: `djinn up|down|service|allow|keys|migrate <args>`
- `up.sh` / `down.sh` / `service.sh` — the container-lifecycle scripts `djinn`
  dispatches to (still runnable directly): `up.sh` / `down.sh` are container
  lifecycle from manifests; `service.sh <name>` starts a plugin's Mac-side
  host service (see `plugins/`)
- `containers/` — manifests (`TEMPLATE.yml` to copy; your own are gitignored)
- `plugins/` — drop-in MCP tools, one directory each (`<name>/plugin.yml` +
  optional host-only `<name>/run.sh`, started via `./djinn service <name>`). See
  [`plugins/README.md`](plugins/README.md) for the schema and how to add one
- `rules/` — bundled default agent rules & skills (override via `RULES_PATH`)
- `bin/` — host commands `djinn allow` / `djinn keys` dispatch to (still
  runnable directly as `bin/<name>.sh`):
  - `allow-egress.sh` — add egress domains to a running container (no restart)
  - `update-agent-keys.sh` — temporary per-agent key override; durable changes
    go in secrets.env
- `src/` — internal source, never run directly:
  - `common.sh` — shared path resolution (sourced by the scripts)
  - `manifest.py` — host-side manifest validation; `wire_plugins.py` — the
    agent-config writer `up.sh` execs after boot; `keyfiles.sh` — host-side
    key-file composition `up.sh` sources
  - `entrypoint.sh`, `init-firewall.sh`, `tmux*`, `mosh-server-wrapper.sh` —
    baked into the image
  - `freshness.py` + `freshness-landing.bashrc` — the no-network landing
    readout of container config age (last `up` + image build date); `up.sh`
    stamps the two timestamps into `/etc/environment` after boot
- `compose/` — `docker-compose.local.yml` (base) plus the `ssh.yml` / `mosh.yml`
  overlays `up.sh` applies for a manifest's `ssh:` / `remote.mosh` settings
- `docs/` — `script.md` (every script, grouped by lifecycle), `TIPS.md`,
  `workspace.CLAUDE.md` (copied into each container as `/workspace/CLAUDE.md`)
- `tests/` — host-runnable checks. `plugins.test.sh` is the entry point (yq +
  jq + python3); it runs the Python unit tests (`test_manifest.py` /
  `test_wire_plugins.py` — manifest validation + wiring logic) and the
  host-side bash unit tests (`bash.test.sh` — `keyfiles.sh`, `common.sh`,
  `allow-egress.sh`, `update-agent-keys.sh`, `service.sh`, the
  `plugins/*/run.sh` token generation)
- `Dockerfile` — the shared image and its contracts
- `secrets.env.example` — template for your `secrets.env`
- `.env` (gitignored) — optional `DJINN_HOME` / `RULES_PATH` /
  `DJINN_SUBNET` overrides

## Remote hosts (Linux server or VPS)

Same system, same files, one addition. On any Linux box with Docker:

1. Install `yq` (static binary) and `python3`, and clone this repo. The bundled `rules/`
   and gitignored `./.djinn/` work as-is; set `DJINN_HOME` /
   `RULES_PATH` in `./.env` only if you want them elsewhere.
2. Put your secrets in `secrets.env` (default `./.djinn/secrets.env`,
   600) — including `SSH_AUTHORIZED_KEY` (your public key).
3. Add an `ssh:` section to the container's manifest:

   ```yaml
   ssh:
     port: 2222        # published on the host
     bind: 127.0.0.1   # keep loopback; reach it through your tunnel
   ```

4. `./djinn up <name>` — identical to the Mac. Connect with VS Code
   **Remote-SSH** to the host/port; everything else (firewall, secrets,
   rules, artifacts) behaves exactly the same.

Never expose sshd publicly: keep the bind on loopback (or a tunnel
interface) and front it with your WireGuard/VPN tunnel. The remote MCP
plugins (`gateway`/`proxyman`/`browser`) are Mac-desktop services — on
headless hosts leave them out of `plugins:` or run the host service on that
host.

## Remote agent access (phone / second device)

The `remote:` manifest block (requires `ssh:`) turns an SSH-reachable
container into something you can drive from a phone — start a task, walk
away, get pinged when the agent needs you, answer from anywhere. Works for
every agent in the image; nothing is vendor-hosted or public-facing.

```yaml
ssh:     { port: 2222, bind: 127.0.0.1 }
remote:  { tmux: true, mosh: true, notify: ntfy }
```

- **tmux** — interactive SSH/mosh logins land attached to one durable
  session (`agent`). Phone and laptop share the same view; agents survive
  disconnects. `docker exec` and editor terminals are exempt.
- **mosh** — a per-manifest UDP range (`remote.mosh_ports`, default
  60000:60010; disjoint per container, like `ssh.port`), published next to
  sshd with the same bind rules and pinned server-side. Survives phone
  sleep and WiFi↔cellular switches; use a mosh-capable client (e.g. Moshi
  or Blink on iOS).
- **notify: ntfy** — an agent-blind monitor pushes to your ntfy topic when
  the session goes idle at a prompt and nobody is attached. Set `NTFY_URL`
  (+ optional `NTFY_TOPIC`) in `secrets.env`; the host is auto-allowlisted.

**Reach.** All containers sit on one shared bridge (`djinn-net`,
`172.30.0.0/24` by default, `DJINN_SUBNET` in `./.env` to override; created
automatically by `djinn up`).
Point your WireGuard/VPN layer at that CIDR once and every container is
reachable at its bridge IP from any enrolled device — `djinn up` prints the
IP in its summary. sshd and the mosh range stay loopback/tunnel-only; nothing
listens publicly.

**Manifest filename = container identity.** A `containers/<name>.yml` under
your own (untracked) manifests directory names the container it produces, so
renaming the file is the same as renaming the container.

## License

[MIT](LICENSE) © Demetrio Urquidi. Use it, modify it, ship it — just keep
the copyright and license notice on copies and derivatives.
