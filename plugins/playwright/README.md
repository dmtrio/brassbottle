# playwright

Headless-browser MCP that runs **inside the bottle** —
[`@playwright/mcp`](https://github.com/microsoft/playwright-mcp) plus headless
Chromium, baked into the image and wired into every installed MCP-capable
agent over stdio.

```yaml
plugins: [playwright]
```

## Why it is in the bottle

The host [`gateway`](../gateway/) plugin drives a browser on the Mac, so a page
load leaves from the host and the bottle's egress rules never see it. Here the
browser is a process in the bottle: every page load is the bottle's own
traffic, subject to the bottle's egress rules, and a destination that is not
approved is filed to the egress queue **as that bottle**.

The plugin declares no `egress:` and no `host_port:` on purpose. It must not
widen the allowlist by itself; the bottle's rules decide what a page can reach.

## What it runs

`plugin.yml`'s `mcp:` entry is `python3 mcp_filter.py -- playwright-mcp …`:

| Piece | Why |
|---|---|
| `--headless` | a bottle has no display |
| `--no-sandbox` | Chromium's sandbox needs user namespaces the container does not grant; the bottle is the sandbox |
| `--browser chromium` | the browser we baked; the default is the Chrome channel, which is not installed |
| `--isolated` | profile kept in memory, nothing persisted |
| `--output-dir /tmp/playwright-mcp` | screenshots and snapshots never land in a repo checkout |
| `--idle-timeout 300000` | Chromium is closed after 5 idle minutes and relaunched by the next tool call, so an idle bottle does not hold its memory |

### Tool allowlist — `mcp_filter.py`

`@playwright/mcp` has no allowlist option: every core tool, including
`browser_run_code_unsafe` (its own description: "RCE-equivalent"), is always
exposed. The gateway cut the surface with `--tools`, so this plugin puts a small
stdlib proxy in front of the server that does the same:

- `tools/list` results are stripped to the **22 tools the gateway exposes**
  (`ALLOWED_TOOLS` in `mcp_filter.py`; the tests pin the set). On @playwright/mcp
  0.0.83 the server offers 25 and 22 come out.
- a `tools/call` for anything else is answered with a JSON-RPC error and never
  reaches the server.
- everything else passes through unchanged; an unparseable client line is
  dropped, not forwarded.
- boundary log on stderr: start, each `tools/list` (tools in → out), each refused
  call (tool name only, never arguments), exit status and duration.

## Install and update

The `install:` block pins `@playwright/mcp` (`PW_MCP_VERSION`) and installs
Chromium with the `playwright-core` that package pins, so the browser revision
is the one the server asks for. To update, bump the pin and rebuild the image;
re-check `ALLOWED_TOOLS` against the new version's tool list.

The bake needs full network at build time (npm, Playwright's CDN, apt); at run
time the plugin needs none of its own.

## Cost (opt-in, so measured)

Image-size delta and idle/active memory are recorded in the PR that added the
plugin. Chromium and its shared libraries are the bulk of it. Only full
Chromium is installed (`install --no-shell`): `--browser chromium` launches it
even headless, and the separate headless shell would add ~275 MB nothing runs.
