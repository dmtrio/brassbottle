# Secrets model & per-agent identity

Secret **values** live in one file — `secrets.env` (mode 600, gitignored,
never mounted). Manifests and the Python modules handle only secret
**names**; values are resolved host-side at `up` time. Plugins declare
**secret slots**. Every slot uses one hybrid resolution order:

1. `common_secrets:` provides an explicit default source for every enabled
   agent.
2. `agent_secrets:` may replace that source for one agent.
3. `disabled: true` removes the slot for one agent.

An unset common source warns and provides no value; an unset per-agent
override hard-fails at `up`.

## Per-agent shims deliver them

Each agent CLI is fronted by a shim that, at process start, loads only that
agent's `~/.agent-keys/<agent>.env` — its fully resolved secret set — and
overrides inherited env before exec'ing the real binary. Two consequences:

- `cat <agent>.env` is the full audit of exactly what that agent sees.
- Delegation is safe: when claude spawns `cursor-agent -p`, the child's shim
  loads *its* identity — the invoker's credentials never leak.

GitHub rides the same path: agents act as the machine user (`GH_TOKEN`);
your personal login never enters a container unless you `gh auth login`
there, and agent PRs/comments show as the bot (you review and merge as you).

## Per-org git identity

When one machine user can't reach every repo (it isn't a member of every
org), give the container its own token — and route by repo **owner** — from
the manifest's `git:` block:

```yaml
git:
  name:  "Fry Agent"
  email: "agent+fry@example.com"
  token: GH_TOKEN_fry              # this container's default credential (secrets.env var NAME)
  orgs:                            # optional per-owner overrides (multi-org containers only)
    planetexpress:
      token: GH_TOKEN_planetexpress   # secrets.env var NAME
      name:  "Leela Bot"              # optional — repo-local identity for planetexpress/* repos
      email: "bot@planetexpress.example"
```

`token`/`orgs.*.token` name vars in `secrets.env` (values never enter the
manifest). At `up`, a repo owned by `<owner>` authenticates with
`GH_TOKEN_<owner>` if set, else the container's `git.token`, else the global
`GH_TOKEN` — resolved by the `git-credential-org` helper on every
`github.com` fetch/push. A `git.orgs` owner with a `name`/`email` also gets
that identity stamped repo-locally, so its commits carry the right author.
A `token:` naming a var that isn't in `secrets.env` **hard-fails the
apply** — never a silent fall-back to the wrong identity. This is routing +
attribution, not isolation: every org's token sits in each agent's
`<agent>.env`, so a repo whose token must be unreachable by other work
belongs in a separate container.

### Gitea and other self-hosted forges

The same routing serves a non-github repo. `up` derives every non-github
`https://` origin from `repos:` and the entrypoint installs the helper for
each, so a repo at `https://git.example.test/Emergence/filebrowser.git`
authenticates with `GH_TOKEN_emergence` (the var name keeps the `GH_TOKEN_`
prefix on every forge — one naming rule, one scan, one router):

```yaml
forge: gitea
repos:
  - https://git.example.test/Emergence/filebrowser.git
git:
  orgs:
    Emergence:
      token: GH_TOKEN_emergence      # a gitea access token, in secrets.env
capabilities:
  egress: [git.example.test]         # repo hosts are never auto-allowlisted
```

Two things differ from github.com, both deliberate. The fall-backs are
github-only. A non-github owner with no `GH_TOKEN_<owner>` gets **no**
credential: the helper prints which var is missing and tells git to quit,
so the clone fails immediately instead of prompting for a password, and
neither the github machine user's `GH_TOKEN` nor the human `gh` login is
ever offered to a third-party server. That also means a plain terminal or
editor git client in the bottle has no credential for a non-github host:
in a bottle, git to such a host authenticates only through `git.orgs`
tokens, which reach the agent shims. The token is sent as the HTTP password
with a fixed username; gitea accepts any username alongside an access
token. `http://` repo URLs are rejected outright.

The router reads the owner as the first path segment, so a gitea served
under a sub-path (`ROOT_URL https://host/git/`) derives owner `git` and
finds no token; serve gitea at the host root or on its own hostname.

Owner names are the token key and carry no host: `up` refuses a manifest
where one owner name owns repos on two hosts (they would share
`GH_TOKEN_<owner>`); use separate bottles. The same rule applies across
owner spellings: `a.b` and `a_b` both become `GH_TOKEN_a_b`, so `up` refuses
a manifest where either of two such owners has a `git.orgs` token.

Each `git.orgs` token is bound to one host: the host its owner appears on in
`repos:`, else an explicit `git.orgs.<owner>.host:`, else github.com only in
a bottle with no other git host. The token is never presented to another
host, and a token with no binding (for example one hand-set with
`bin/update-agent-keys.sh` without `GH_HOST_<owner>`) is presented nowhere.
