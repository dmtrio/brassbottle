# herdr-auto-title

A **herdr plugin, not an MCP server.** [herdr-auto-title](https://github.com/kryptamine/herdr-auto-title)
polls the herdr session twice a second and names every tab after what is
happening in it — directory and branch, the program running, or an agent's
own topic (`dashboard › claude › Implement OAuth scopes`) — so a phone-screen
landing shows the work, not a cwd path. Tabs you rename by hand are left alone. Unlike everything else in this directory it is not wired through
`mcp:` at all — herdr supervises the plugin itself.

- **`install:`** clones the upstream repo at a pinned commit (`4d4554f` =
  v0.3.3), builds the Go binary with Ubuntu 24.04's apt `golang-1.24-go`, then
  registers it with `herdr plugin link`. The link works at image build because
  it needs no running server and no Go — it only writes
  `~/.config/herdr/plugins.json`, an image layer. herdr launches the plugin
  when the server restores a session, so there is no `services:` entry.
- **`setup:`** runs once per `./djinn up` in the live container and installs
  herdr's agent-state hook for **every agent the bottle enables** (Claude
  Code, Codex, cursor, kimi, pi, antigravity-cli — whichever the manifest
  lists) into their state dirs, volumes the image build cannot pre-populate.
  The hook is how herdr learns which session a pane holds and whether the
  agent is working or blocked; without it, tabs fall back to directory and
  branch. Agents herdr has no hook for (aider) are skipped.
- **`volumes:`** keeps `manual-names.json` (tabs you renamed by hand) and an
  optional `config.env` across container recreates.

Because it carries an `install:` block, the plugin is **baked into the
bottle's image**: enabling it in a manifest needs an **image rebuild**, not
just `./djinn up`.

## Enable it

```yaml
plugins: [herdr-auto-title]
```

Then rebuild the image and `./djinn up <container>`.

## What happens, and when

- **At image build** (`install:`): the plugin is built and linked into the
  herdr registry — every rebuild re-registers it, so the registry never needs
  to be a volume.
- **At `./djinn up`** (`setup:`): herdr's hook is installed for each enabled
  agent (`herdr_integrations.py`, logged per agent to
  `/tmp/djinn-setup/herdr-auto-title.log`). Idempotent — re-running `up`
  re-installs in place.
- **At the first `herdr` landing**: the herdr server restores a session,
  launches the plugin binary it finds in the registry, and tabs start renaming
  from live transcript topics.

## Verify it

```bash
herdr plugin list            # shows herdr.auto-title
herdr integration status     # every enabled agent: current (after setup: ran)
```

Then `cd` somewhere in a Claude session and watch the tab rename within about
a second.

## Configuration

Tuning goes in `~/.config/herdr-auto-title/config.env` — persisted by the
plugin's volume, so it survives a recreate. Upstream's
[`config.env.example`](https://github.com/kryptamine/herdr-auto-title/blob/main/config.env.example)
lists every knob: poll interval (`HERDR_AUTO_TITLE_POLL_MS`, 500), title
length (`_MAX_LENGTH`, 50), branch width (`_BRANCH_MAX`, 12, `0` hides
branches), the leading tab number (`_POSITION`), and whether agent transcripts
are read at all (`_TRANSCRIPT`). The plugin reads the file once at startup, so
after editing it run `herdr server stop` and land again — reopening a terminal
or re-running `./djinn up` does not restart the plugin.

## Known caveats

- `herdr plugin disable herdr.auto-title` turns it off **only until the
  container is recreated**: the registry it edits is an image layer, not a
  volume, so the next `./djinn up` silently re-enables it. The declared off
  switch is to de-list the plugin from the manifest and rebuild — the bake
  loop then skips it and the registry entry goes away with the image layer.
- Every enabled agent gets herdr's hook, but Auto Title reads only **Claude
  Code** transcripts for a topic. Other agents' tabs still benefit from the
  hook (herdr knows their state and session) and are named from terminal
  title, directory and branch.
