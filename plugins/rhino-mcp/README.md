# rhino-mcp

[McNeel's official **Rhino MCP Platform**](https://mcneel.github.io/RhinoMCP/)
([mcneel/RhinoMCP](https://github.com/mcneel/RhinoMCP), MIT) — the first-party
MCP server built into a Rhino plugin by the maker of Rhino and Grasshopper.
Agents get tools to inspect Rhino/Grasshopper files, create geometry, run Rhino
commands, write and edit scripts, and build Grasshopper definitions in a live
Rhino 8/9 session on the Mac.

## What it's for

Driving a real, running Rhino/Grasshopper from an agent: parametric modeling,
definition building, file inspection, scripted geometry. This is the
"vendor-blessed" route — the same platform McNeel wires into Claude Desktop,
Claude Code, Copilot, Codex, Gemini CLI, and LM Studio.

## Benefits

- **Official and actively maintained** — vendor-owned (416 commits and moving,
  vs. single-maintainer community projects), free, MIT. Tool surface tracks new
  Rhino releases; Grasshopper 2 tools (`gh2_*`) already exist for Rhino 9 WIP.
- **The only Rhino/GH MCP with independently documented real-world success** —
  a McNeel Discourse case study of an agent designing a complete parametric
  pavilion, and a published account of building 100 Grasshopper programs
  through RhinoMCP + Claude Code.
- **No container-side install** — the Rhino plugin itself serves streamable
  HTTP; this plugin is config-only (a `url:` + a firewall grant), nothing baked.

## Negatives

- **No auth on the endpoint.** The in-Rhino Kestrel server is loopback-bound
  with no token/TLS — its whole security model is "unreachable except from
  localhost". Docker Desktop's host proxy pierces exactly that, so inside this
  setup the port-scoped firewall grant is the only gate. Any container granted
  port 10500 can run arbitrary Rhino commands and scripts on the Mac.
- **Grasshopper canvas depth is unproven vs. specialists.** Whether the
  official tool set matches the per-component canvas manipulation of
  [jingcheng-chen/rhinomcp](../rhinomcp/) or [cordyceps](../cordyceps/) is an
  open question — it may lag the specialized projects in GH-specific depth
  despite being better maintained.
- **Port pinning is manual.** The listener defaults to 10500 but
  auto-increments for each additional Rhino document with a listener — if the
  user opens several, the one this plugin dials may not be the one they meant.
  `MCPStart` prompts for a port; it must match `host_port`/`plugin_ports`.
- **Apple Silicon only on Mac** (Intel Macs unsupported); Rhino 8.21+/9.

## Architecture note — why not McNeel's router

McNeel's documented client path is `rhino-mcp-router`, a stdio proxy that
spawns/adopts Rhino on the same machine (filesystem-based discovery, hardcoded
`http://localhost:<port>` dialing, binaries for win-x64/osx-arm64 only — no
Linux). None of that survives a container boundary. But the Rhino-side plugin
itself serves streamable HTTP MCP at `http://localhost:<port>/`, and Docker
Desktop's `host.docker.internal` proxy connects onto the Mac's loopback — so
this plugin dials the in-Rhino server directly and the router is simply not
needed. The cost: none of the router's conveniences (launching Rhino for you,
multi-instance slot management) — Rhino must already be running with a
listener started.

## Enable it

```yaml
# manifest
plugins: [rhino-mcp]
# plugin_ports: {rhino-mcp: 10501}   # optional override; must match MCPStart's port
```

On the Mac (once):

1. Install **Rhino-MCP-Platform** from Rhino's Package Manager
   (`Tools → Package Manager`, search "Rhino-MCP-Platform").
2. In the Rhino session you want driven: run **`MCPStart`** and set the port to
   **10500** (or your `plugin_ports` override).

Then `./up.sh <container>` — config-only, no image rebuild. `MCPStop` kills the
listener when done.
