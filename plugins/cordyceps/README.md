# cordyceps

[brookstalley/cordyceps](https://github.com/brookstalley/cordyceps) (MIT) — a
Grasshopper-first MCP server where the Grasshopper component **is** the server.
7 consolidated tools with 100+ actions: `gh_canvas` (components, values,
groups, baking, variable parameters), `gh_wire` (connections), `gh_document`
(file ops, snapshots, solver control), `gh_script` (script-component
configuration), `gh_inspect` (debugging, data tracing), plus `rhino_scene`
(objects/layers) and `rhino_render` (materials, lighting, viewport/render
capture).

## What it's for

Agent-driven parametric design with the full presentation pipeline: build a
definition on the canvas, bake to Rhino, apply PBR materials, set up lighting,
and capture stills or orbit animations — the README demos that exact
end-to-end flow. Of the smaller community servers it has the best ergonomics
for an LLM: few tools, many actions, so the tool list stays small while the
surface stays broad.

## Benefits

- **Only server with a first-class rendering path** (`rhino_render`) — the
  others stop at geometry; Cordyceps closes the loop to images, which also
  gives an agent visual feedback on what it built.
- **Actively developed and easy to install** — 133 commits since Jan 2026,
  Rhino Package Manager distribution with auto-updates, Windows + macOS
  (Rhino 8.21+/.NET 8), thorough README with troubleshooting.
- **Consolidated tool design** — 7 tools instead of 60 keeps agent tool
  catalogs (and prompt overhead) small.
- **Config-only here** — the .gha serves streamable HTTP directly; nothing is
  baked into the image.

## Negatives

- **No auth on the endpoint** — loopback binding is its entire security model,
  and Docker Desktop's host proxy pierces exactly that. The port-scoped
  firewall grant (`HOST_MCP_PORTS`) is the only gate; a granted container can
  manipulate the user's live Grasshopper document and Rhino scene.
- **Young, single-maintainer, unproven in the wild** — started Jan 2026, 89★,
  and no independent third-party success reports surfaced in research (its
  Discourse/Food4Rhino pages exist but community feedback is thin).
- **Server lives inside the GH document** — the Cordyceps component must be on
  the open canvas; close the definition and the server is gone. Multiple open
  definitions with the component means port juggling.
- **Grasshopper-first** — Rhino-side coverage (`rhino_scene`) is shallower
  than [rhinomcp-official](../rhinomcp-official/) or [rhinomcp](../rhinomcp/); no
  arbitrary-code escape hatch (arguably also a benefit).

## Enable it

```yaml
# manifest
plugins: [cordyceps]
# plugin_ports: {cordyceps: 26930}   # optional; must match the component's Port input
```

On the Mac (once):

1. Install **Cordyceps** from Rhino's Package Manager (or the `.gha` from the
   repo's releases).
2. Open Grasshopper and drop the **Cordyceps** component (Params → Util) onto
   the canvas of the definition you want driven. It starts serving on port
   26929 (change via its Port input; set DebugLevel ≥ 1 to watch traffic in
   the Rhino command line).

Then `./up.sh <container>` — config-only, no image rebuild.
