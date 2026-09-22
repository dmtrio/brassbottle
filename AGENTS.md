# AGENTS.md — brassbottle

Firewalled Docker workspaces for AI coding agents: `./djinn up` turns a bottle
manifest into one project container with cloned repos, agent CLIs, MCP config,
secret shims, artifacts, and an egress allowlist. This file records what agents
entering this repo need: where things live, what to run, how to verify. The
global container rules (worktrees, PRs, review discipline) apply separately and
are not repeated here.

## Stack

- Host-side **bash** orchestration (`djinn`, `up.sh`, `down.sh`, `service.sh`)
  plus **Python 3.9+ stdlib** modules under `src/` for everything with logic.
  No third-party Python dependencies — new Python stays stdlib unless the
  project adopts one deliberately.
- **yq + jq** for YAML/JSON in shell; **Docker / Docker Compose** for the
  runtime; generated compose files are written by the Python modules, not
  hand-edited templates.
- Public entry points are `djinn`, `up.sh`, `down.sh`, `service.sh`. Do not run
  `src/*.py` or `src/*.sh` files directly unless a test does; `src/README.md`
  is the map of which module the entry points source or call.

## Layout

- `bottles/*.yml` — one manifest per project container ("bottle").
  `bottles/TEMPLATE.yml` is the smoke-test manifest and is *derived*, not
  hand-maintained (see tests below).
- `agents/<name>/` — one directory per agent CLI (descriptor `agent.yml`,
  tests, docs). Ownership rule from `agents/README.md`: everything that knows
  about one agent lives in that directory, and nothing in `tests/` names a
  specific agent.
- `plugins/<name>/` — MCP capabilities (`plugin.yml` + optional tests);
  agents use plugins, agents and plugins never cross-reference.
- `rules/` — bundled rule fragments composed into agent rules files by
  `src/compose_rules.py`.
- `src/` — implementation modules; `manifest.py` is the single validator for
  bottle and plugin descriptors (`--derive` on stdin) — do not write mirrored
  copies of its rules elsewhere.
- `compose/`, `bin/`, `jump/`, `backup/`, `ci-staged/` — compose overlays,
  host-side `djinn` subcommands, singleton jump/backup images, and the staged
  CI workflow (see Conventions).
- `docs/` — deep guides (`script.md` explains every shell script by
  lifecycle); `docs/workspace.CONTRACT.md` is the workspace contract copied
  into containers.

## Commands

- `./djinn up <name>` — create/update a container from `bottles/<name>.yml`
  (needs Docker, `yq`, `python3`).
- `tests/plugins.test.sh` — the main aggregate suite and CI entry point:
  validates every shipped `plugins/*/plugin.yml` through the real
  `src/manifest.py`, derives `bottles/TEMPLATE.yml`, runs
  `python3 -m unittest discover -s tests`, and runs `tests/bash.test.sh`.
- `python3 -m unittest tests.test_<module>` — focused Python run (e.g.
  `tests.test_manifest`, `tests.test_compose_rules`).
- `bash tests/remote.test.sh` — static remote-access/jump contract checks.
- `docker build -t djinn-ci-smoke .` — image build check when the Dockerfile
  or baked assets changed (`backup/Dockerfile`, `jump/Dockerfile` likewise).
- `bash -n <script>` — shell syntax check before committing bash changes.
- `python3 src/manifest.py --derive` — validate a bottle the way `up.sh` does:
  stdin is a yq-JSON manifest, then one `"<name>\t<json>"` line per enabled
  plugin, a `---agents---` sentinel, then the agent lines (format documented
  in the `manifest.py` docstring; the real chain is what
  `tests/plugins.test.sh` drives).

## Tests — what to expect

- Suites **skip explicitly** (printing `SKIP`) when `yq`, `jq`, or Docker are
  unavailable; a skip is never a pass. `tests/egress.smoke.sh` is
  operator-run on a Mac host with a live bottle — it cannot run in CI or in a
  container.
- **Known baseline failure:** `tests.test_jump_host` (`IpTests`) fails on
  `main` as well as on feature branches (observed 2026-09). Compare against a
  `main` baseline before blaming your change, and say so in the PR when your
  diff is not the cause.
- Adjacent suites (`agents/*/test_*.py`, `plugins/*/test_*.py`) are loaded by
  the shared discovery tests — put an agent's tests beside its descriptor.

## Conventions

- Commit messages: `<area>: <imperative summary>` (e.g.
  `manifest: reject reserved plugin names`).
- **CI workflow files are never edited directly** — the agent token lacks
  `workflow` scope. Stage the full file as a copy under top-level `ci-staged/`
  for a human to move into `.github/workflows/`.
- `ci-staged/ci.yml` mirrors `.github/workflows/ci.yml`; when you change one,
  change both in the same commit.
- Manifest/plugin/agent schema questions are answered by `src/manifest.py`,
  `agents/README.md`, and `plugins/README.md` — the schema has no summary doc
  that is safe to trust over the validator.
- Secrets never go in the repo: `secrets.env.example` shows the shape, real
  values live in `.djinn/secrets.env` (gitignored).
