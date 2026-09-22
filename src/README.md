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
  derives shell variables for `up.sh`. Git token routing derives per
  identity: the catch-all/simple-form table (`GIT_HOST_TOKENS` — the rows
  the bootstrap clone, which runs as no identity, uses) plus
  `GIT_IDENTITY_HOST_TOKENS`/`GIT_IDENTITY_TOKEN_SOURCES` records (one per
  identity a list-form git.hosts entry names) that `keyfiles.sh` builds each
  identity's env file from.
- `keyfiles.sh` composes per-agent secret env files from `secrets.env` and
  the bottle's secret bindings — plus `user.env`, the `user` identity's git
  routing (sourced by the image's `.bashrc` in interactive shells), which
  carries git routing only, never plugin secrets. Each agent's file carries
  only the git rows that serve that identity, so one agent's file never
  contains another entry's token.
- `agent_shim.sh` holds `write_agent_shim` — the identity-shim template the
  Dockerfile bakes for every mcp-capable enabled agent and
  tests/bash.test.sh drives through the same function.
- `user-keys-landing.bashrc` is the .bashrc piece that sources
  `~/.agent-keys/user.env` (the `user` identity's git routing) in
  interactive shells only.
- `git_notices.sh` provides `git_url_split` (the host/path derivation the
  clone loop uses) and prints the up-time notices — an https:// repo host
  with no git.hosts row for any identity (clones run anonymously; a push
  needs `git.hosts.<host>.token`), or a git host missing from
  `capabilities.egress` — that `up.sh` sources and calls after deriving the
  manifest.
- `git_identity.sh` stamps per-repo author identity (git.hosts name/email
  records) onto each clone after the bootstrap `git clone`.
- `credential_router_install.sh` holds the idempotent per-origin
  `git config` loop the container entrypoint runs to install
  `git-credential-org` for every `GIT_CREDENTIAL_HOSTS` host (the entrypoint
  sources it from `/usr/local/lib/djinn/`).
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
- `git-credential-org.sh` routes git credentials by request host through
  the routing table the RUNNING PROCESS carries (`GIT_HOST_TOKENS`, derived
  by manifest.py — per identity, so each env file carries its own); an
  unlisted host defers only to a stored gh login for exactly that host, else answers
  `quit=1` naming the missing `git.hosts.<host>.token`.

## Testing

The public test entry point is `tests/plugins.test.sh`. It pins the contracts
between these modules, the compose files, and the shell scripts.
