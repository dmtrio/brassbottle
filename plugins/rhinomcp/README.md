# rhinomcp

[jingcheng-chen/rhinomcp](https://github.com/jingcheng-chen/rhinomcp) (MIT) —
the most-adopted community MCP server for Rhino/Grasshopper (985★, the highest
of any Rhino MCP project, official included). ~38 Rhino tools (object
create/modify/delete, layers, viewport capture, RhinoScript-Python and
RhinoCommon-C# code execution) plus ~25 dedicated Grasshopper tools (component
search, add/connect/wire, parameter get/set, solve, batch graph
build/mutate).

## What it's for

Deep, per-component control of a live Rhino 8 + Grasshopper session: an agent
placing and wiring actual components on the canvas, setting parameters, and
solving — rather than generating scripts. Its Grasshopper tool surface is the
richest surveyed anywhere, which is why it earns a slot alongside the official
[rhino-mcp](../rhino-mcp/) platform.

## Benefits

- **Richest Grasshopper canvas tooling** — batch graph building/mutation and
  component-level wiring that the official platform hasn't demonstrably
  matched yet.
- **Most community traction of any Rhino MCP** — 985★/89 forks, Rhino Package
  Manager distribution, tutorials, and active pushes as of mid-2026.
- **Escape hatches** — arbitrary RhinoScript-Python / RhinoCommon-C# execution
  covers whatever a dedicated tool doesn't.

## Negatives

- **Unauthenticated arbitrary code execution over the wire.** The Rhino-side
  TCP protocol has no auth, and the tool set includes execute-code tools. The
  bridge itself refuses non-loopback targets unless `RHINO_MCP_ALLOW_REMOTE=1`
  — this plugin sets that flag knowingly, and the port-scoped firewall grant
  (`HOST_MCP_PORTS`) is the only gate between containers and the Mac's Rhino.
  Only enable this plugin in containers you'd trust with a shell on the Mac.
- **Single maintainer, unofficial** — no vendor guarantee it tracks new Rhino
  releases; Rhino 8 only (no Rhino 9/WIP support documented).
- **No independent success reports found** (unlike the official platform) —
  adoption is high but documented outcomes are thin.
- **Needs an image rebuild to enable** (local bridge baked via `install:`),
  unlike the config-only remote plugins.

## Architecture note

The Rhino plugin (`yak install rhinomcp`, then `mcpstart` in Rhino) listens on
raw TCP `127.0.0.1:1999` speaking a custom JSON protocol — not MCP. MCP enters
via the PyPI `rhinomcp` stdio bridge, which this plugin bakes into the image
and runs in-container, dialing `host.docker.internal:1999` (Docker Desktop
proxies that onto the Mac's loopback). This is the same shape as `axiom` (a
local bridge dialing out) — except the dial target is the host, so the plugin
declares `host_port:` for the firewall grant, with `${HOST_PORT}` in the args
kept in sync with any `plugin_ports:` override.

## Enable it

```yaml
# manifest
plugins: [rhinomcp]
```

On the Mac (once):

1. Install **rhinomcp** from Rhino's Package Manager
   (`Tools → Package Manager`, search "rhinomcp").
2. In the Rhino session: run **`mcpstart`**.

Container side: rebuild the image once so the bridge bakes (`install:`), then
`./up.sh <container>`.
