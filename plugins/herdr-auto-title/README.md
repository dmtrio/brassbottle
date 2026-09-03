# herdr-auto-title

A **herdr plugin, not an MCP server.** [herdr-auto-title](https://github.com/kryptamine/herdr-auto-title)
watches Claude Code transcripts and renames each herdr tab to the session's
actual topic, so a phone-screen landing shows "Fix egress broker race" instead
of a cwd path. Unlike everything else in this directory it is not wired through
`mcp:` at all — herdr supervises the plugin itself.

- **`install:`** clones the upstream repo at a pinned commit (`4d4554f` =
  v0.3.3), builds the Go binary with Ubuntu 24.04's apt `golang-1.24-go`, then
  registers it with `herdr plugin link`. The link works at image build because
  it needs no running server and no Go — it only writes
  `~/.config/herdr/plugins.json`, an image layer. herdr launches the plugin
  when the server restores a session, so there is no `services:` entry.
- **`setup:`** runs once per `./djinn up` in the live container and installs
  herdr's Claude Code hook into `~/.claude` — a volume the image build cannot
  pre-populate. Without it, only sessions started from the `herdr` landing get
  titled.
- **`volumes:`** keeps `manual-names.json` (tabs you renamed by hand) and an
  optional `config.env` across container recreates.

Because it carries an `install:` block, the plugin is **baked into the shared
image**: enabling it in a new manifest needs an **image rebuild**, not just
`./djinn up`.

## Enable it

```yaml
plugins: [herdr-auto-title]
```

Then rebuild the image and `./djinn up <container>`.

## What happens, and when

- **At image build** (`install:`): the plugin is built and linked into the
  herdr registry — every rebuild re-registers it, so the registry never needs
  to be a volume.
- **At `./djinn up`** (`setup:`): the Claude Code hook is installed into the
  `~/.claude` volume. Idempotent — re-running `up` reports `current` and
  changes nothing.
- **At the first `herdr` landing**: the herdr server restores a session,
  launches the plugin binary it finds in the registry, and tabs start renaming
  from live transcript topics.

## Verify it

```bash
herdr plugin list            # shows herdr.auto-title
herdr integration status     # claude: current (after the setup: step ran)
```

Then `cd` somewhere in a Claude session and watch the tab rename within about
a second.

## Configuration

Tuning (which transcripts to watch, rename cadence, …) goes in
`~/.config/herdr-auto-title/config.env` — persisted by the plugin's volume, so
it survives a recreate. The herdr server reads the plugin's config at startup,
so after editing it run `herdr server stop` and land again (or re-run
`./djinn up`).

## Known caveats

- `herdr plugin disable herdr.auto-title` is the **runtime off switch**, but
  the declared way to remove the plugin is to de-list it from the manifest and
  rebuild — the bake loop then skips it and the registry entry goes away with
  the image layer.
- Only **Claude Code** transcripts are read for topics. Other agents' sessions
  keep their default titles.
