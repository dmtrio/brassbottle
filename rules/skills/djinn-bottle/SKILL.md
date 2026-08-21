---
name: djinn-bottle
description: Create a new bottle (djinn container manifest), or amend an existing one, by interview — then validate it against the fleet and ship it (PR when the bottles dir is a git repo, plain file otherwise). Use for "new container", "new bottle", "spin up a container for X", "make me a bottle for X", "add the browser plugin to <name>", "give <name> more memory", "change <name>'s egress".
argument-hint: [what the container is for, or the change you want]
---

You are producing a bottle for **djinn** (the `brassbottle` tool): one
`<name>.yml` = one dev container. `$ARGUMENTS` is what the user wants, in
their words — possibly empty, in which case the interview starts from scratch.

## 1. Locate the two directories

**A brassbottle checkout** — `/workspace/repos/brassbottle` (inside a
container), else the repo this skill ships in (`git rev-parse --show-toplevel`
from the skill's own directory usually finds it), else `~/git/brassbottle`.
Without it you lose both real validation and every implicit plugin port —
get one; don't skip it.

**The bottles directory.** Where the user's manifests live:

- On the host, `src/common.sh` resolves it — `cd <brassbottle> && ./up.sh`
  with no argument prints the manifests it can actually see. It is the
  bundled `bottles/` by default, or `$DJINN_HOME/bottles` / a `BOTTLES_PATH`
  override when the user keeps bottles in their own private repo
  (see `bottles/README.md`).
- Inside a container, the private bottles repo is often cloned at
  `/workspace/repos/<name>` — look for a sibling repo whose files are
  `*.yml` manifests. If none is present and the user's bottles live outside
  the container, clone their bottles repo to `/tmp` or ask where it is.

Do **not** create a branch yet — it is named after the bottle, and you do not
know that until §3.

## 2. Read before you ask

Never interview from the template alone — read the live state:

```bash
ls <bottles-dir>/*.yml                       # the existing fleet and its naming
cat <bottles-dir>/<an-existing>.yml          # local conventions, if any exist
cat <brassbottle>/bottles/TEMPLATE.yml       # every key, fully commented
ls <brassbottle>/plugins/                    # the real plugin set
cat <brassbottle>/plugins/<name>/README.md   # what a plugin does + its secret slots
ls <brassbottle>/agents/                     # the installable agent CLIs
```

The plugin and agent lists change; read them, never recite them from memory.

## 3. Interview — core first, drill only where the answers lead

Ask the core in one batch (use AskUserQuestion with real options drawn from
what you read, not free text):

- **Name** — the filename stem, and therefore the container name
  (`djinn-<name>`). Kebab-case. **Ask for it separately; never derive it from
  a repo.** A bottle can hold several repos or none, so the two are
  independent. Offer a `coding-*` style name when repos are involved and a
  bare noun otherwise, but let the user overrule.
- **`task:`** — an informational label per TEMPLATE.yml. Default it to the
  name, accept anything.
- **Repos** — zero or more full clone URLs under `repos:`. Ask which repos,
  plural, as its own question: sibling repos developed against each other
  belong in one bottle. **No repo is a first-class answer**, not a fallback —
  omitting the key gives a `git init` scratch repo at
  `/workspace/repos/scratch`.
- **Memory** — TEMPLATE.yml documents the default; existing bottles show the
  local range. Lighter for research/appliance bottles, more for coding.
- **Agents** — offer the `agents/` directory names; omitting `agents:`
  enables every descriptor.
- **Plugins** — offer the live `plugins/` list with one-line descriptions
  from their READMEs.

Then drill **only** when something implies it:

| Trigger | Drill into |
|---|---|
| "phone access", "from my laptop", "long-running" | `ssh:` + `remote: {tmux, mosh, notify}` — and allocate ports (§4) |
| repos owned by an org that isn't the default identity | `git.orgs.<owner>: {token, name, email}` — see `docs/secrets.md` |
| a plugin whose README names a secret slot | `common_secrets:` / `agent_secrets:` (§5) |
| an API/service the container must reach | `capabilities.egress:` — domains only, no scheme, no path |
| talking to something on the LAN | `capabilities.egress_cidrs:` |

Don't ask what you can default. Write plugins under `plugins:`, never as
`capabilities: {browser: true}` — that sugar is deprecated and prints a
warning. Use `agents:` (the `tools:` key was renamed and is rejected by name).

## 4. Allocate ports yourself — never ask the user to pick

Host ports are exclusive among **running** containers, and the clashes that
bite are implicit: a plugin's `host_port` default in
`plugins/<name>/plugin.yml` that no manifest ever mentions. Run the checker
(§6) and take the next free value:

- `ssh.port` — `2222`, `2223`, … Keep `bind: 127.0.0.1`; only front it with a
  WireGuard/VPN tunnel, never the open internet.
- `remote.mosh_ports` — 11-port UDP blocks: `60000:60010`, `60011:60021`, …
- `plugin_ports.<plugin>` — only when the default is taken by a container
  that will run at the same time. A browser bridge must stay **below 9222**:
  its Chrome debug port is derived as bridge+408.

The checker reports a sibling clash as a *warning*, not an error — it cannot
know what runs concurrently. For a new bottle an unused port is usually free,
so take one; if sharing is intentional, say so and move on.

## 5. Secrets — names here, values on the host

A plugin whose slot nothing binds is **inert** — wired for no agent, silently
doing nothing. The checker surfaces that as a warning; treat it as a real
finding. When a plugin's README names secret slots, bind them under
`agent_secrets:` for every enabled agent, copying the exact source-variable
names from an existing bottle when one uses the same plugin — a typo'd source
is a hard `up.sh` failure, by design. Use `agent_secrets:`, not the
deprecated `identities:` form.

Secret **values** live only in the host's `secrets.env`, which no container
can read (see `docs/secrets.md`). Any secret the manifest names that the user
has not set yet is a step for them, not for you: collect those names and list
them in §8.

## 6. Validate before shipping

```bash
python3 <skill-dir>/check_manifest.py <bottles-dir>/<name>.yml \
    --brassbottle <brassbottle>
```

Two passes. The draft goes through brassbottle's own `src/manifest.py` — the
exact code `up.sh` runs, with every named secret declared present — then
every sibling manifest is scanned for host-port clashes, implicit plugin
defaults and deprecated `capabilities:` sugar included.

- **Exit 1 (errors)** — the manifest cannot work: a validation failure, a
  port the manifest claims twice, a browser bridge at/above 9222. Fix and
  rerun.
- **Exit 0 with warnings** — judgement calls: a shared port, a deprecated
  key, an inert plugin, a `task:` that differs from the filename. Read each
  one and say what you decided. Never silently "fix" a deliberate one.

Pre-existing problems in *other* manifests are not yours to fix here — say
what you saw and move on.

## 7. Ship it

**Bottles dir is a git repo with a remote** (the recommended private-repo
setup): never push its default branch — branch and PR, even if nothing
server-side stops you.

```bash
BRANCH="bottle/<name>"                # or bottle/<name>-<change> when amending
git -C <bottles-dir> checkout -b "$BRANCH"    # or a worktree, per your workspace rules
git add <name>.yml && git commit -m "<summary>"
git push -u origin "$BRANCH"
gh pr create --base <default-branch> --title "…" --body "…"
```

The PR body: what the container is for, the ports claimed, the plugins
wired, and any `secrets.env` variables that must exist. A merged PR deploys
itself: `djinn up` fast-forwards an external bottles checkout before reading
the bottle.

**Plain directory** (bundled `bottles/` or an un-versioned `BOTTLES_PATH`):
just write the file; there is nothing to PR. Suggest making the directory its
own private repo (`bottles/README.md` explains why).

## 8. Tell the user what only they can do

1. Merge the PR (when there is one).
2. Add any missing variables to the host's `secrets.env` — name them
   explicitly.
3. `./djinn up <name>` from the brassbottle checkout.
4. For a plugin with a host-side service (browser, proxyman, gateway):
   also `./djinn service <plugin>`.

## Amending an existing bottle

Same path, narrower: read the current file, ask only about what changes, and
keep the diff minimal — no reordering, no reflowing untouched blocks, no
"while I'm here" cleanups. Re-run the checker afterwards: adding a plugin is
the single most likely way to introduce a port clash.

## Failure modes worth naming

- **Editing a bottle inside a container's clone** and thinking it took
  effect. The host reads its own checkout; a change deploys via merged PR
  (auto-pulled at `up`) or by editing the host's copy directly.
- **Naming a secret that doesn't exist on the host.** `up.sh` hard-fails
  rather than falling back to the wrong identity. The checker cannot catch
  this — it declares every named secret present — so list the names for the
  user.
- **Copying a header comment.** A copied manifest often opens with a stale
  filename or purpose line from its source. Write the real one.
- **Re-upping a container the user is currently working in.** `./djinn up`
  recreates it and kills every session inside. Warn before recommending it
  for a container that is running right now.
