## collabrain (memory faculty)

- **Hydrate at session start.** Before the first substantive action, run
  `collabrain seed` from `/workspace/repos/collabrain` (`uv run collabrain
  seed`) — it loads relevant vault context into this session. Read the
  staleness banner if one appears at the top of the output: it means a
  scheduled job (distill/consolidate) has quietly stopped running, and the
  brain you just hydrated from is stale.
- **Call `check_tripwires`** (the `collabrain` MCP tool) after touching a
  file you have not touched before this session, and before any decision
  you would have to defend in a PR. It is invocation-driven by design —
  nothing fires it for you, so call it deliberately before risky or novel
  actions, not after something has already gone wrong.
- **Never hand-edit `/workspace/repos/.mcp.json`.** This plugin owns both
  `basic-memory` MCP entries; a hand edit is silently overwritten on the
  next `./djinn up` and, worse, hides the real config from every other
  agent in the meantime.
- **Never write into the vault's `atoms/` directory.** The consolidator is
  the sole atom writer. Agents propose durable knowledge (via capture /
  session notes); they do not write atoms directly.
- **Supersede, never delete.** If something you wrote turns out wrong, link
  a `superseded_by` record rather than removing the old one — history is
  evidence.

Full discipline (records/atoms lifecycle, checkpoint-before-death, what "not
to do" covers beyond the two rules above): `docs/PLAYBOOK.md` in the
collabrain repo (`/workspace/repos/collabrain/docs/PLAYBOOK.md`).
