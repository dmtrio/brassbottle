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
- `wire_plugins.py` writes generated MCP config for each enabled agent.
- `compose_rules.py` composes global rules, local rules, repo rules, and enabled
  plugin rule fragments.
- `code_workspace.py` keeps `dev.code-workspace` in sync with repos and visible
  worktrees.
- `ensure_net.py` creates or verifies the shared Docker network.

## Image / Container Runtime

- `entrypoint.sh` starts container services and prepares mounted paths.
- `init-firewall.sh` applies the default-deny egress firewall.
- `freshness.py` and `freshness-landing.bashrc` print config/image age without
  requiring network access.
- `tmux-*`, `tmux.conf`, and `mosh-server-wrapper.sh` support remote agent
  sessions.
- `git-credential-org.sh` routes GitHub credentials by repo owner.

## Testing

The public test entry point is `tests/plugins.test.sh`. It pins the contracts
between these modules, the compose files, and the shell scripts.
