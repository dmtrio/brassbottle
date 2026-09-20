# src

Internal implementation modules live here. Do not run these files directly
unless a test or maintainer note says to; the public entry points are `djinn`,
`up.sh`, `down.sh`, and `service.sh`.

## Host-Side Flow

- `common.sh` resolves repo paths, `.env`, `DJINN_HOME`, `BOTTLES_PATH`,
  `RULES_PATH`, and the `djinn-` container prefix.
- `pull_manifests.py` fast-forwards an external bottle repo before `up` reads a
  bottle.
- `manifest.py` validates a bottle plus enabled plugin/agent descriptors and
  derives shell variables for `up.sh`.
- `keyfiles.sh` composes per-agent secret env files from `secrets.env` and the
  bottle's secret bindings.
- `git_notices.sh` provides `git_url_split` (the host/path derivation the
  clone loop uses) and prints the up-time notices — a repo host with no
  git.hosts row, or a git host missing from `capabilities.egress` — that
  `up.sh` sources and calls after deriving the manifest.
- `wire_plugins.py` writes generated MCP config for each enabled agent.
- `compose_rules.py` composes global rules, enabled plugin rule fragments, and
  the workspace contract (`/workspace/CONTRACT.md`) into each agent's rules file.
- `code_workspace.py` keeps `dev.code-workspace` in sync with repos and visible
  worktrees.
- `ensure_net.py` creates or verifies the shared Docker network.

## Image / Container Runtime

- `entrypoint.sh` starts container services and prepares mounted paths.
- `init-firewall.sh` applies the default-deny egress firewall.
- `freshness.py` and `freshness-landing.bashrc` print config/image age without
  requiring network access.
- `tmux-*`, `tmux.conf`, and `herdr-config.toml` support remote agent
  sessions. `mosh-server-wrapper.sh` is built into the jump image only
  (`jump/Dockerfile`).
- `git-credential-org.sh` routes git credentials by request host through the
  manifest's git.hosts table (`GIT_HOST_TOKENS`, derived by manifest.py); an
  unlisted host defers to `gh` when gh holds a login for it, else answers
  `quit=1` naming the missing `git.hosts.<host>.token`.

## Testing

The public test entry point is `tests/plugins.test.sh`. It pins the contracts
between these modules, the compose files, and the shell scripts.
