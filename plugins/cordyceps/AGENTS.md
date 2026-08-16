## Cordyceps (Grasshopper canvas + rendering)

The `cordyceps` server is a Grasshopper component running **inside a live GH
document on the user's desktop** — the canvas you manipulate is one the user
is looking at.

- **If the connection fails**, Grasshopper isn't open or the Cordyceps
  component isn't on the current canvas: STOP and ask the user to drop it in
  (Params → Util → Cordyceps). Don't conclude the tools are gone.
- **Its 7 tools are action-multiplexed** — each takes an action parameter
  covering many operations; check the tool's own description for the action
  list instead of expecting one tool per operation.
- **Use `gh_document` snapshots before big edits** — the user's undo stack is
  theirs, not yours; snapshot, then mutate.
- **`rhino_render` gives you eyes** — after building geometry, capture a
  viewport to verify the result visually rather than trusting the graph.
- **The user shares the canvas** — they may rewire between your calls;
  re-inspect with `gh_inspect` rather than assuming prior state.
