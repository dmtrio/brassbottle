# gateway

A headless Playwright MCP gateway (Docker MCP gateway, `coding` profile). An
HTTP server on the Mac host, port **8811**, bridged to **local stdio** by
`mcp-remote`. Nothing is baked by this plugin — `mcp-remote` is a base tool the
image already provides.

```yaml
plugins: [gateway]
common_secrets: [MCP_GATEWAY_TOKEN]   # required — declares the slot; agents wire from this source
```

## Start the host service

```bash
./djinn service gateway        # leave it running (tmux or launchd)
```

First run self-generates `MCP_GATEWAY_TOKEN` into `secrets.env`. Declare it in
`common_secrets` to use it as every agent's default; individual agents can
override or disable that slot.

## Security posture

- Binds localhost only; containers reach it via `host.docker.internal`.
- Bearer token required (401 without it). `mcp-remote` substitutes
  `${MCP_GATEWAY_TOKEN}` into the header from its own process env at connect
  time, so the token is in no agent's config file and not on any command line
  (argv carries the literal `${MCP_GATEWAY_TOKEN}`). It replaced a remote
  (`url:` + `headers:`) shape under which cursor-agent and pi — which cannot
  expand `${VAR}` inside a remote header — had the raw token written to disk.
- Plaintext `http://` to the host is deliberate and sufficient: the listener is
  localhost-only on the Mac, and `mcp-remote` does not require TLS.
- Tool allowlist: Playwright `browser_*` only — no gateway-management tools, no
  `browser_run_code_unsafe`.
