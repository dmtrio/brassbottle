# kimi

`kimi` is Moonshot's Kimi Code CLI. In this repo it is wired as a ref-style
agent via `~/.kimi-code/mcp.json` (`mcpServers` + `bearerTokenEnvVar`), so MCP
config stays secret-free and keys arrive through the per-agent shim env.

## Install behavior notes (pinned as of v1.49, July 2026)

- Kimi Code CLI is still young; installer behavior and home-dir layout may
  change between releases.
- Current installer URL: `https://code.kimi.com/kimi-code/install.sh`
- Current home-dir layout includes `~/.kimi-code/mcp.json`,
  `~/.kimi-code/config.toml`, and a global `~/.kimi-code/AGENTS.md`.

## Sources

- <https://moonshotai.github.io/kimi-code/en/customization/mcp>
- <https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/config-files.html>

## Headless status

Headless/print-mode behavior has not been validated yet. Current docs emphasize
the TUI flow (for example `/mcp` and `/mcp-config`), so delegate-to-CLI
guidance remains claude/cursor-only until non-interactive behavior is confirmed.
