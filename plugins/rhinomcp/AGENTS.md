## rhinomcp (community Rhino/Grasshopper control)

The `rhinomcp` server drives a **real Rhino 8 + Grasshopper session on the
user's desktop** through an unauthenticated TCP bridge — treat every call as
touching the user's live model.

- **If calls fail to connect**, Rhino isn't running or `mcpstart` wasn't run
  in it: STOP and ask the user. Never conclude the plugin is broken, and never
  try to install Rhino here.
- **Prefer the dedicated tools over the execute-code tools.**
  `execute_rhinoscript_python_code` / `execute_rhinocommon_csharp_code` run
  arbitrary code in the user's Rhino with no sandbox — reach for them only
  when no dedicated tool covers the need, keep the code minimal, and say what
  you ran.
- **The user shares the session.** They may move things between your calls —
  re-read object/canvas state before building on earlier reads.
- **Destructive operations** (delete objects/layers, overwrite definitions)
  need explicit user go-ahead.
