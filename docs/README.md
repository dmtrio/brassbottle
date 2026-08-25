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
- `remote.md` explains SSH, tmux, mosh, and ntfy-driven remote access.
- `backup.md` documents the singleton artifact backup service (restic lifecycle,
  paths, retention, restore, and disaster-recovery checks).
- `workspace.CLAUDE.md` is copied into containers as `/workspace/CLAUDE.md` and
  defines the workspace/worktree contract for agents.

## Assets

- `demo.svg` is the animated README terminal demo for `./djinn up my-app`.

