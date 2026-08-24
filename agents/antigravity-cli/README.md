# antigravity-cli

Google's Antigravity CLI. The tool is `antigravity-cli` (the directory name a
manifest opts into); the binary is `agy`. It is the successor to Gemini CLI and
shares that CLI's home directory, so it owns `~/.gemini` here.

## Layout

| Path | Holds |
|---|---|
| `~/.gemini/antigravity-cli/settings.json` | user preferences (sparse — only non-default values) |
| `~/.gemini/config/mcp_config.json` | global MCP servers (wired by this repo) |
| `~/.gemini/GEMINI.md` | global rules (composed target) |
| `~/.gemini/antigravity-cli/log/` | CLI logs |

Workspace-local `.agents/mcp_config.json` is also read; this repo wires the
global file only.

## MCP wiring

Remote servers render as `{serverUrl, headers}` — `url` and `httpUrl` are
rejected by name, which is why the `serverUrl` dialect exists. `agy` does not
expand `${VAR}` refs in its MCP config, so agent-scoped remote keys are baked
literally into the config (mode `0600`, on the agent's own volume), the same
deal cursor and pi get.

## Auth in a container

`agy` tries the OS keyring first and falls back to a browser sign-in; over SSH
it prints an authorization URL and takes a pasted code. Neither is comfortable
in a headless container, so the practical path is a Gemini API key:

```jsonc
// ~/.gemini/antigravity-cli/settings.json
{ "modelProvider": "gemini" }
```

plus `GEMINI_API_KEY` in the environment. The env var alone does nothing —
`modelProvider` has to be set too.

## Install behavior notes

- Installer URL is unpinned and serves the latest release: `https://antigravity.google/cli/install.sh`
- Installs to `~/.local/bin/agy` (already on PATH).
- Dockerfile install loops run extracted scripts with `bash -e -o pipefail`, so
  a failed download fails the build.
- Egress (folded in by `egress:`): `antigravity.google` for the installer and
  the CLI's own endpoints, `googleapis.com` for model traffic,
  `accounts.google.com` for sign-in.

## Sources

- <https://antigravity.google/docs/cli/install>
- <https://antigravity.google/docs/cli/mcp>
- <https://antigravity.google/docs/cli/settings>
- <https://github.com/google-antigravity/antigravity-cli>
