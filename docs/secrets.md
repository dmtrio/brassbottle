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

GitHub rides the same path: an agent whose github.com row exists acts as
that entry's identity (its token, written as `GH_TOKEN` in its own
`<agent>.env`); an agent named by no github.com entry — with no catch-all —
gets no GitHub credential at all. Your personal login never enters a
container unless you `gh auth login` there, and agent PRs/comments show as
the bot each entry names (you review and merge as you).

The human has a key file too: up.sh writes `user.env` (the `user`
identity's routing) beside the agents' files, and the image's `.bashrc`
sources it in interactive shells (VS Code terminal, ssh login, herdr/tmux
pane) — so interactive `git`/`gh` act as the user identity the manifest
names. Non-interactive `bash -c` sources nothing. `user.env` carries git
routing only, never plugin secrets.

## Per-host git identity

When one machine user can't reach every repo (it isn't a member of every
org, or the repos live on more than one forge), give the container one
plus per-identity tokens from the manifest's `git:` block. Each `git.hosts`
value is either a map — one row serving EVERY identity — or a list of
entries naming the identities each token serves:

```yaml
git:
  name:  "Fry Agent"
  email: "agent+fry@example.com"
  hosts:
    git.example.org:
      token: GITEA_TOKEN_example      # simple form: everyone
    github.com:                       # list form: named identities
      - token: GH_TOKEN_fry_a
        identities: [claude, user]
      - token: GH_TOKEN_fry_b
        identities: [pi, codex]
      - token: GH_TOKEN_fry_c         # catch-all: omits identities:
```

An identity is an enabled agent's binary name (the shim that reads its
`<agent>.env`: `claude`, `pi`, `cursor-agent`, …) or `user`, your
interactive shell. Within one host an identity may appear once; at most one
entry per host may omit `identities:` — that entry is the catch-all for
everyone else, the bootstrap clone included (it runs as no identity); an
empty `identities:` list is an error; so is a list with zero entries.
An identity named by no entry, with no catch-all covering it, gets no row
for that host and no token there.

`hosts.<host>.token` names a variable in `secrets.env` (values never enter
the manifest) and must be a valid variable name; the host is lowercased
and an explicit `:443` stripped. Two keys that normalise to the same host
are rejected, and so is a host key with anything beyond letters, digits,
`.`, `-` and an optional `:port` — the table is the credential router's
only source. A `token:` naming a variable that isn't set in `secrets.env`
**hard-fails the apply** — never a silent fall-back to the wrong identity.
A manifest gets exactly the rows it declares — nothing is implicit. Without
a row for a repo's host (catch-all or per identity), clones of its repos
run anonymously (public repos only) and a push needs
`git.hosts.<host>.token`. The old `git.token`/`git.orgs` spellings feed the
catch-all table and still mean "everyone".

A row for a host other than `github.com` must not name `GH_TOKEN`: `gh`
reads that variable from its environment and sends it to github.com, so a
non-github row naming it would hand a github.com token to another forge.
Name the row's variable specifically (e.g. `GITEA_TOKEN_example`).

At `up`, a repo on host `<host>` authenticates with the token of the entry
that names the running identity — resolved by the `git-credential-org`
helper on every fetch/push through the routing table the running process
itself carries, and presented to no other host. A host with no row in the
caller's own table defers only to a stored `gh` login for exactly that
host (an exact key in gh's hosts file; gh's token environment variables
are stripped before gh runs, so only the stored login can answer);
otherwise the helper names the missing `git.hosts.<host>.token` and tells
git to quit, so a private clone fails immediately instead of prompting.
`http://` repo URLs are rejected outright (no credential over cleartext).
Each identity's table still holds every host's catch-all row, so a repo
whose token must be unreachable by other work belongs in a separate
container.

A `git.hosts` value is either a map (one row serving every identity) or a
list of per-identity entries (see above). Its optional `name`/`email`
fields are per-HOST author attribution and are rejected inside a list-form
entry — a list-form host cannot carry an author; declare the author on a
simple-form host entry. The old spellings feed the catch-all table:
`git.token: X` is the `github.com` row; each `git.orgs` entry is a row for
the host it resolves to (its `host:` field, else the one `https://` host
its owner's repos: URLs name). One host carries one token per identity: a
`git.orgs` token serves every repository on the host it resolves to (a
second owner on that host gets no separate token), and two `git.orgs`
entries resolving to one host with different tokens is an error; `up`
prints a note when a host has another owner. Two `git.hosts` keys that
normalise to the same host are rejected outright; `git.hosts` and either
old spelling never mix in one manifest. Per-owner attribution
(`git.orgs.<owner>.name/email`) still stamps a repo-local
`user.name`/`user.email` at bootstrap clone — it is authorship only, never
routing.

### Per-host author attribution

A `git.hosts` entry in the SIMPLE form may carry an optional
`name`/`email` beside `token:`. At bootstrap clone, a repo whose remote
URL's host has such a record is stamped repo-local
`user.name`/`user.email`, so commits from that host carry that author
instead of the bottle default (`git.name`/`git.email`); repos matching no
record keep the default. Attribution is by the URL's host for `https://`,
`ssh://` and scp-style remotes alike — the host is matched as the URL
spells it (lowercased, an explicit `:443` stripped, like the token rows),
so an `ssh://` URL with an explicit port does not match a host entry (the
entry key carries no ssh port; only an explicit `:443` is stripped).
Either field may be given alone; a tab, newline or carriage return is
rejected in any identity field (records are tab-separated, one per line),
and otherwise the validation is the same as the per-owner spellings: a
scalar each (a map/list is a named error), no charset rule beyond that.
`token:` stays required on every `git.hosts` entry — an author never
replaces the token row. In the LIST form `name`/`email` are rejected
inside an entry: they are per-host attributes, and the error says where
the author goes (the simple form).

### Gitea and other self-hosted forges

The same table serves a non-github repo. The entrypoint installs the
helper for every `GIT_CREDENTIAL_HOSTS` host — the table's hosts, every
`https://` origin in `repos:`, and the CLI host's origin always (row or
no row: with no row the router answers `quit=1` there, never the desktop
credential bridge) — so a repo at
`https://git.example.test/Emergence/filebrowser.git` authenticates with
the variable its row names:

```yaml
forge: gitea
repos:
  - https://git.example.test/Emergence/filebrowser.git
git:
  hosts:
    git.example.test:
      token: GH_TOKEN_emergence      # a gitea access token, in secrets.env
capabilities:
  egress: [git.example.test]         # repo hosts are never auto-allowlisted
```

The helper's only fall-back is the human `gh` login, and only when `gh`
holds a stored login for exactly that host (an exact key in gh's hosts
file; the container's token environment variables never reach gh through
the helper). A host with no token and no stored `gh` login
gets **no** credential: the helper prints which `git.hosts.<host>.token`
is missing and tells git to quit, so the clone fails immediately instead
of prompting for a password. That also means a plain terminal or editor
git client in the bottle has no credential for a host the table doesn't
carry. The token is sent as the HTTP password with a fixed username; gitea
accepts any username alongside an access token.

The router reads the host from the request, not the path, so a gitea
served under a sub-path (`ROOT_URL https://host/git/`) works as long as
the table names the host. Serve gitea at the host root or on its own
hostname so the URL's host matches the table.

Each host carries exactly one token PER IDENTITY: the entry that names the
running identity supplies its token; two rows for one host (from two old
spellings, or two `git.hosts` keys that normalise together) are rejected
unless they name the same variable, and within a host's list form an
identity may appear only once. A non-github row naming `GH_TOKEN` is
rejected (see above). Repo hosts are never auto-allowlisted in the
firewall — put each new host in `capabilities.egress` (or cover it with
`capabilities.egress_cidrs`).
