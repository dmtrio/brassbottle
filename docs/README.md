# docs

Longer guides and generated assets live here. The root README should get a new
user to a working container; this directory holds the extra context.

## Guides

- `script.md` explains the shell scripts by lifecycle: create/update, stop,
  host services, key updates, egress changes, and runtime helpers.
- `TIPS.md` collects operational notes: resource tuning, shell aliases, `.env`
  overrides, and persistence.
- `secrets.md` explains secret values, per-agent shim env files, and per-org
  Git identity routing.
- `remote.md` explains the mosh jump, jump-reachable containers, tmux/herdr
  landing, and ntfy notifications.
- `disk.md` explains where Docker disk goes (images vs. volumes), how to
  audit a workspace volume for unpushed work before deleting it, and how to
  reclaim space safely.
- `egress.md` documents the egress approval watcher/daemon and ntfy push notifications.
- `backup.md` documents the singleton artifact backup service (restic lifecycle,
  Backrest browse-only UI, paths, retention, restore, and disaster-recovery checks).
- `workspace.CLAUDE.md` is copied into containers as `/workspace/CLAUDE.md` and
  defines the workspace/worktree contract for agents.

## Assets

- `demo.svg` is the animated README terminal demo for `./djinn up my-app`.

