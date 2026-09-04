# Workspace Contract — Agent Dev Container

This file governs every session in this container. `djinn up` installs it at
`/workspace/AGENTS.md`, and `compose_rules.py` appends it to every agent's
global rules file, so every harness reads the same contract.

## Layout (v2)

```
/workspace/
  repos/<name>/        # every repo the manifest declares — equal siblings
  repos/.mcp.json      # canonical MCP config (generated; repos symlink to it)
  worktrees/<repo>/<branch>/  # one directory per active worktree (yours to manage)
  dev.code-workspace   # multi-root VS Code workspace — keep in sync (see below)
```

No repo is privileged. The default session sees them all: start Claude from
`/workspace/repos` (per-repo sessions from inside a repo also work — each repo
carries a `.mcp.json` symlink to the canonical file).

## Worktree rules (mandatory)

Worktrees are the first-class way to do feature work here. The user watches
them live in VS Code/Cursor, so visibility rules are not optional.

- **Create** a worktree for each task/feature, namespaced by repo:
  `git -C /workspace/repos/<repo> worktree add ../../worktrees/<repo>/<branch> -b <branch>`
  then link the MCP config into it (Claude Code reads `.mcp.json` only from
  its start directory — without this, MCP tools silently vanish in the
  worktree): `ln -s ../../../repos/.mcp.json /workspace/worktrees/<repo>/<branch>/.mcp.json`
- **Remove** it after the branch is merged or abandoned:
  `git -C /workspace/repos/<repo> worktree remove ../../worktrees/<repo>/<branch> && git -C /workspace/repos/<repo> worktree prune`
- **After every create or remove**, update `folders` in
  `/workspace/dev.code-workspace`: the repo entries first
  (`{"path": "repos/<name>", "name": "<name>"}`, sorted by name — `up.sh`
  manages these, leave them intact), then one entry per worktree
  (`{"path": "worktrees/<repo>/<branch>", "name": "<repo>/<branch>"}`) matching
  `git -C /workspace/repos/<repo> worktree list` across repos, sorted by name —
  then a fixed trailing `{"path": "/artifacts", "name": "artifacts"}` entry.
  Valid JSON, no trailing commas.
- Do **not** use Claude Code's built-in session worktrees for feature work —
  they live in hidden paths and defeat the visibility requirement.

## Git rules

- Never delete or break anything under `/workspace/repos/`.
- Never commit to a repo's default branch. All work goes through a feature
  branch in a worktree, pushed with a PR (`gh pr create` or `tea pr create`).
- Treat every `repos/<name>` checkout as read-mostly: pull/fetch there,
  develop in worktrees.

## Cross-repo development (npm link)

Sibling repos exist to be developed concurrently. When one repo consumes
another as a package:

- Link by **path**, never by global registry: `npm link ../<lib>` from a repo,
  or the equivalent relative path from a worktree (worktree→worktree linking
  is the normal shape for feature-branch-against-feature-branch work). Bare
  `npm link` in the lib registers ONE global slot per package name — a second
  checkout or another agent relinking silently retargets the first.
- Never commit `file:` paths into `package.json` — that encodes this
  container's layout into the repo.
- Peer-dependency pitfall: a linked lib resolves deps from its own
  `node_modules`, so peers (React, canonically) get duplicated — "invalid
  hook call"-style failures. Link the peer back into the lib or alias it in
  the bundler.

## Artifacts outbox — `/artifacts`

`/artifacts` is bind-mounted to the user's Mac and survives container and
volume destruction. Put agent **outputs that aren't code** there: screenshots,
test reports, exports, diagrams, and progress notes (`/artifacts/progress.md`)
you want to outlive this container. The user can open these in Finder
directly. Code never goes there — code exits via git PR only.

## Browser MCP (research containers)

The `browser` MCP server does not run a browser in this container — it
remote-controls a real browser window on the user's desktop (so they can
watch you work). Consequences:
- Never conclude "no browser is installed" and never try to install one.
- If a browser tool fails to connect, the desktop browser or its bridge is
  down: STOP and tell the user to run `./djinn service browser` on the host.
- The user can see every page you open. They may interact with the window;
  re-snapshot rather than assuming state.

## Environment notes

- Egress is firewalled to an allowlist of zones (GitHub, npm, PyPI, agent
  APIs, apt, plus per-container extra zones and LAN CIDR grants from the
  manifest). A refused outbound connection is the firewall, not a network
  flake — say so instead of retrying, and ask the user to add the zone or
  CIDR to the container's manifest if it's genuinely needed.
- The host is unreachable except for ports listed in `HOST_MCP_PORTS`.
- **Run long tasks and helper agents in herdr.** herdr shows each pane's
  state in the sidebar, and a dropped editor does not orphan it. When
  `HERDR_ENV` is not `1`, run them in tmux instead. tmux also remains the
  supervisor for plugin `services:`.
  - For one helper, split the caller's pane with
    `herdr pane split --current --direction <dir> --cwd "$PWD" --no-focus`.
    Read the caller's rect from its own `panes[]` entry in
    `herdr pane layout --pane "$HERDR_PANE_ID"`, not from `area`. Use
    `right` when the width is at least twice the height. Use `down`
    otherwise. Use a tab instead when the new pane would have fewer than 80
    columns or 24 rows.
  - For many helpers, create one tab per helper with
    `herdr tab create --no-focus --cwd "$PWD" --label <name>`.
  - The pane id is `.result.pane.pane_id` from a split and
    `.result.root_pane.pane_id` from a tab. The tab id is
    `.result.tab.tab_id`.
  - For a long command rather than an agent, split or create a pane the same
    way, then `herdr pane run <pane-id> <command>` and
    `herdr pane wait-output <pane-id> --match <text> --timeout <ms>`.
  - Start a helper agent with
    `herdr agent start <name> --kind <claude|pi|cursor|codex> --pane <pane-id> -- <model flag>`.
    Never name a model in a rule or prompt. Pass the harness's model for the
    task key from `/agent-rules/models.yml` (`coding`, `review`, `research`,
    `summarize`, `automation`, `plan`, `senior-review`). A key the harness
    omits resolves to its `default`.
  - Drive it with `herdr agent prompt <name> <text> --wait --timeout <ms>`,
    `herdr agent wait <name> --timeout <ms>`, and `herdr agent read <name>`.
    Always pass `--timeout`, because the default wait is indefinite.
  - `herdr agent read` returns only what is still on the helper's screen.
    For the full result, read the helper's own transcript instead of asking
    it to write files: `herdr agent get <name>` reports `agent_session`.
    Claude: `~/.claude/projects/<cwd-slug>/<id>.jsonl`, last `assistant`
    entry. pi: the reported path. Cursor:
    `~/.cursor/projects/<cwd-slug>/agent-transcripts/<id>/<id>.jsonl`.
  - Close the tab or pane when the helper is done.
  - The installed binary is the authority for syntax. Run `herdr --skill`
    when a command here differs from `herdr --help`.
