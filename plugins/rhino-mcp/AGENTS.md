## Rhino MCP Platform (official)

The `rhino-mcp` server drives a **real Rhino/Grasshopper session on the user's
desktop** — it does not run in this container, and there is no Rhino here to
install or launch.

- **If the connection fails**, Rhino isn't running or its listener isn't
  started: STOP and ask the user to run `MCPStart` in Rhino (port must match
  the plugin's `host_port`, default 10500). Never conclude the tools are
  broken or try to install Rhino.
- **The user can see the Rhino window.** They may edit the model between your
  calls — re-inspect state rather than assuming your last write survived.
- **Mutations are real and mostly irreversible from here** — you have no undo
  tool. Prefer inspecting before writing; batch related changes; say what you
  changed. Ask before destructive operations (deleting objects/layers,
  overwriting files).
- Grasshopper 2 tools (`gh2_*`) need Rhino 9 WIP — if they're absent, the user
  is on Rhino 8; use the Rhino/GH1 tools instead of reporting a failure.
