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
- `install.sh` is a two-host affair: it is *served* from `antigravity.google`,
  but the release manifest (`$DOWNLOAD_BASE_URL/manifests/<platform>.json`) and
  the auto-updater both hit
  `antigravity-cli-auto-updater-974169037036.us-central1.run.app`. The manifest
  lists the tarball under `storage.googleapis.com`.
- `fetch_manifest` swallows every error (`2>/dev/null || true`) and reports one
  generic `Fatal: Could not connect to the release server` for a firewall block,
  a 403, a 404 and a transient blip alike — the message names none of them.
  This has been seen to fail a build and then succeed on an unchanged retry, so
  **retry once before diagnosing**. If it repeats, get the real status with
  `curl -o /dev/null -w '%{http_code}' "$DOWNLOAD_BASE_URL/manifests/<platform>.json"`
  rather than reading the wording as a firewall verdict.
- Egress (folded in by `egress:`): `antigravity.google` for the installer and
  the CLI's own endpoints, the Cloud Run host above for the release manifest
  and updater, `googleapis.com` for model traffic and release tarballs,
  `accounts.google.com` for sign-in.

## Sources

- <https://antigravity.google/docs/cli/install>
- <https://antigravity.google/docs/cli/mcp>
- <https://antigravity.google/docs/cli/settings>
- <https://github.com/google-antigravity/antigravity-cli>
