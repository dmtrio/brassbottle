# rules

This directory is the bundled default rules source. It is mounted read-only into
containers at `/agent-rules`.

## Bundled Defaults

The public repo ships `AGENTS.md`. It is composed into each enabled agent's
rules file by `src/compose_rules.py`.

Claude skills are optional. If your custom `RULES_PATH` contains a `skills/`
directory, `up.sh` exposes it for Claude. The bundled default does not ship
skills.

## Override Location

Use a gitignored `.env` at the repo root:

```bash
RULES_PATH="$HOME/git/agent-conf/rules"
```

If `DJINN_HOME` is set and `$DJINN_HOME/rules` exists, that directory is used
automatically.

## Layering

Rules are applied weakest to strongest:

1. global rules from `/agent-rules/AGENTS.md`, plus enabled plugin fragments
   baked under `/opt/plugins`;
2. `/workspace/rules.local.md` inside the container;
3. the project repo's own `CLAUDE.md` / `AGENTS.md`.

Agents should propose rule changes through PRs. For an external rules repo,
`djinn up` pulls it each run so merged changes land in every container.
