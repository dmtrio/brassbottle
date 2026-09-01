# Scripts

Every script in this repo, grouped by when you reach for it. All are run from
the repo root on the host (macOS or Linux) unless noted — `djinn` (the
dispatcher) plus the `up.sh` / `down.sh` it wraps live at the root; the other
host commands live in `bin/` (so you invoke them directly as `bin/<name>.sh`,
or via `djinn allow` / `djinn keys`). Names in `<>` are placeholders; `<name>`
is a manifest/container short name (the container itself is `djinn-<name>`).

The one non-script prerequisite: `secrets.env` holds every secret value (chmod
600, never mounted). By default it's the gitignored `./.djinn/secrets.env`;
copy `secrets.env.example` to fill it in. `up.sh` composes per-container
credentials from it according to each manifest.

All the scripts below source `src/common.sh` (not run directly), which resolves the
"djinn home" — where secrets/keys/artifacts live. It defaults to a
gitignored `./.djinn/`; override via `DJINN_HOME` / `RULES_PATH` /
`BOTTLES_PATH` in a gitignored `./.env` at the repo root (keeps your own
setup working). `BOTTLES_PATH`
is where `bottles/<name>.yml` are read from — it defaults to
`$DJINN_HOME/bottles` when that exists, so your real manifests can live
outside this repo.

---

## Before you create a container

Some plugins are backed by a service that runs on your Mac (the container
reaches it over `host.docker.internal`). Start the ones a container's manifest
lists **before** `./djinn up`, and leave them running (tmux or launchd):

```bash
./djinn service <name>   # execs plugins/<name>/run.sh; self-generates its token on first run
./djinn service          # lists the plugins that ship a host service
```

(`./service.sh <name>` still works directly — `djinn service` just dispatches
to it.) `service.sh` is deliberately separate from `up.sh` — `up.sh` recreates
the container (killing a running agent), so starting a host service is its own
root-level command that never touches docker. Which plugins need a service, and
the token each uses, are documented in `plugins/<name>/README.md`.

## Creating / updating a container

`djinn up` (dispatching to `up.sh`) is the one entry point. It's declarative
and idempotent: edit the manifest, rerun, done.

- **`./djinn up <name>`** — create or update `djinn-<name>` from
  `bottles/<name>.yml`. Composes `$DJINN_HOME/keys/<name>/` from `secrets.env`,
  builds the per-container image, waits for the firewall to come up, clones/inits
  the workspace, and generates each agent's MCP config. Re-run any time after
  editing the manifest or rotating a secret.

```bash
./djinn up my-app      # no args → lists available manifests
```

## While a container runs

Operate on a live container without a rebuild or restart.

- **`./djinn allow <container> <domain> [<domain> ...] [--save yml|firewall|none]`**
  (dispatches to `./bin/allow-egress.sh`, still runnable directly) — add domains
  to the running container's egress allowlist immediately. Appends
  `ipset=/<domain>/allowed-domains` zones to its `/etc/dnsmasq.conf` and reloads
  only dnsmasq (the ipset and iptables rules stay up). The live change is
  ephemeral; at the end it asks where to persist:
  `yml` → this manifest's `capabilities.egress` (next `./djinn up`),
  `firewall` → `src/init-firewall.sh` base zones (all containers, next build),
  `none` → live only. Validates every domain first.

  ```bash
  ./djinn allow my-app cdn.playwright.dev api.stripe.com
  ./djinn allow my-app api.stripe.com --save yml   # skip the prompt
  ```

- **`./djinn keys <container> <agent|common> <VAR> [value]`** (dispatches to
  `./bin/update-agent-keys.sh`, still runnable directly) — TEMPORARY
  override of one MCP credential for one agent, picked up the next time that agent
  starts (the shims read `~/.agent-keys` at launch). No arguments beyond the
  container name lists the current composed keys. Note: `$DJINN_HOME/keys/<name>/`
  is derived — the next `./djinn up <name>` wipes and recomposes it, so make durable
  changes in `secrets.env`/the manifest and use this only for quick experiments.

  ```bash
  ./djinn keys my-app pi OBSIDIAN_ANNOTATED_KEY   # prompts for the value
  ./djinn keys my-app                             # list keys
  ```

## Teardown & cleanup

- **`./djinn down <name> [--purge]`** (dispatches to `./down.sh`) — stop and
  remove the container. Default keeps the workspace volume (your code), so
  `./djinn up <name>` restores the container around it. `--purge` also deletes
  the volume, the per-container image, and the derived keys; the manifest,
  `secrets.env`, and `artifacts/<name>/` always survive.

  ```bash
  ./djinn down my-app            # stop, keep the code
  ./djinn down my-app --purge    # full teardown
  ```

## Inside the image (baked from `src/` — automatic, you don't run these)

The host commands above live in `bin/`; `src/` is internal source — the
`common.sh` those commands source, the host-side `manifest.py` / `wire_plugins.py`
`up.sh` calls, and the files below, which get baked into the image by the
`Dockerfile` and run themselves inside the container (listed for completeness).

- **`src/entrypoint.sh`** — the container entrypoint (not PID 1). Docker Compose
  runs tini as PID 1 through `init: true`, which forwards signals and reaps
  orphaned children while the entrypoint runs unchanged as its child (persists
  `~/.claude.json`, runs the firewall (fail-loud), applies git config, guarantees
  `/workspace/repos` and `/workspace/worktrees` exist, then starts sshd when
  `SSH_ENABLED=true` [host-published] or the default jump path is active, or idles
  for attach mode).
- **`src/init-firewall.sh`** — builds the default-deny egress allowlist at boot
  (GitHub IP ranges + dnsmasq-mirrored zones), verifies itself, and exits non-zero
  on failure so the container never runs with open egress. `bin/allow-egress.sh` edits
  the same zone list live; the base `ALLOWED_ZONES` here is the durable default.
- **`src/tmux.conf` / `src/tmux-landing.bashrc`** — remote access:
  mobile-friendly tmux defaults, and the guarded snippet that lands interactive
  SSH/mosh logins in a fresh `login-*` session.
- **`src/djinn_net_addr.py`** — shared djinn-net subnet/static-address rules
  (top-of-subnet derivation, reserved-address checks). Used by both the jump
  and the tunnel so the two cannot drift.
- **`src/tunnel_config.py`** — singleton VPN/tunnel connector: identity, the
  bridge address, image pinning, provider credentials, compose generation.
  All vendor-specific detail is confined to its `Provider` block.
- **`src/tunnel_host.py`** — `./djinn tunnel` operator commands.
- **`src/jump_config.py`** — singleton jump container: identity, host paths,
  bridge address resolution and compose generation (host-side, stdlib).
- **`src/jump_host.py`** — `./djinn jump` operator commands (start/stop/
  status/logs/pubkey); thin docker-compose glue over `jump_config`.
- **`jump/Dockerfile`, `jump/entrypoint.sh`, `jump/ssh_config`** — the jump
  image: sshd + mosh only. The entrypoint generates and persists both the
  host keys and the jump's own client key on the mounted volume, so neither
  changes across a rebuild.
- **`src/mosh-server-wrapper.sh`** — pins client-launched mosh servers to the
  firewalled/published UDP range.
- **`src/tmux-notify.sh`** — agent-blind idle notifier; fired by the tmux
  silence hook when `remote.notify: ntfy` is on, pushes to your ntfy topic
  unless a client is attached.
