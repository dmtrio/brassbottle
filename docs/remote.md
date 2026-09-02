# Remote hosts & remote agent access

## Remote hosts (Linux server or VPS)

Same system, same files, one addition. On any Linux box with Docker:

1. Install `yq` (static binary) and `python3`, and clone this repo. The
   bundled `rules/` and gitignored `./.djinn/` work as-is; set `DJINN_HOME`
   / `RULES_PATH` in `./.env` only if you want them elsewhere.
2. Put your secrets in `secrets.env` (default `./.djinn/secrets.env`,
   600) — including `SSH_AUTHORIZED_KEY` (your public key).
3. (Optional) To also reach the bottle directly from your Mac via VS Code
   **Remote-SSH**, add an `ssh:` section:

   ```yaml
   ssh:
     port: 2222        # published on the host
     bind: 127.0.0.1   # keep loopback; reach it through your tunnel
   ```
   Omit this section for phone access alone — jump reachability is the default.

4. `./djinn up <name>` — identical to the Mac. Connect with VS Code
   **Remote-SSH** to the host/port if you added the `ssh:` section; everything
   else (firewall, secrets, rules, artifacts) behaves exactly the same.

Never expose sshd publicly: keep the bind on loopback (or a tunnel
interface) and front it with your WireGuard/VPN tunnel. The remote MCP
plugins (`gateway`/`proxyman`/`browser`) are Mac-desktop services — on
headless hosts leave them out of `plugins:` or run the host service on that
host.

## Remote agent access (phone / second device)

Every bottle is reachable from a phone with nothing in its manifest: sshd
listens on the bottle's bridge IP, the firewall accepts :22 only from the
jump container's static address, and `SSH_AUTHORIZED_KEY` plus
`JUMP_AUTHORIZED_KEY` are both authorised (either alone works; with neither,
sshd simply does not start). Start a task on the laptop, walk away, hop
through the jump from the phone — every agent in the image, nothing
vendor-hosted or public-facing. Opt out per bottle:

```yaml
remote:
  jump: false
```

**Landing shell.** Interactive SSH logins land in a fresh `login-*`
session by default. Customize with `remote.shell`:

```yaml
remote:
  shell: tmux     # default; interactive terminals land in tmux picker
  # shell: herdr  # agent-aware terminal workspace
  # shell: bash   # opt out — land in plain bash
```

- **tmux (default)** — SSH and VS Code/Cursor terminals land in a fresh
  `login-*` session. If other sessions already exist, tmux opens the
  session picker automatically; press Esc to keep the fresh landing session.
  You can always reopen the picker with `Ctrl-b s`.
- **herdr** — agent-aware workspace: the sidebar shows every pane with its
  agent state (working / blocked / done / idle). One persistent server per
  bottle, so every login lands in the same workspace — including every
  interactive VS Code/Cursor terminal, which attaches as another client of
  that one workspace (a mirror, not a fresh shell); detach with `ctrl+b q`;
  the prefix is `ctrl+b` like tmux; `herdr --help` and https://herdr.dev/docs
  cover the rest. Two things still need tmux for now: `remote.notify` and
  plugin background jobs (both move to herdr in later phases of
  PLN - herdr adoption).
- **bash** — land in a plain bash shell; no session picker or shared view.
- **notify: ntfy** (optional) — an agent-blind monitor pushes to your ntfy
  topic when the session goes idle at a prompt and nobody is attached. Set
  `NTFY_URL` (+ optional `NTFY_TOPIC`) in `secrets.env` and use
  `remote.notify: ntfy`; the host is auto-allowlisted. Requires
  `remote.shell: tmux` (the idle monitor runs inside the tmux session).

**Reach.** All containers sit on one shared bridge (`djinn-net`,
`172.30.0.0/24` by default, `DJINN_SUBNET` in `./.env` to override; created
automatically by `djinn up`). Point your WireGuard/VPN layer at that CIDR
once; enrolled devices then reach the jump at its static address, and the
jump reaches every bottle. A bottle's own sshd answers only the jump unless
the manifest has an `ssh:` section (then the firewall admits the whole
bridge and direct SSH over the tunnel works — `djinn up` prints the IP).
Nothing listens publicly.

## The tunnel connector (`./djinn tunnel`)

One connector per djinn installation, joined to `djinn-net`. It dials **out**
to your VPN control plane, and devices you enrol there get L3 access to the
bridge — which is what makes the jump container and every bottle reachable
from a phone.

```bash
./djinn tunnel start | stop | status | logs [-f]
```

**Why a container and not a process on the Mac.** Docker Desktop runs
containers in a LinuxKit VM and macOS has no route to `172.30.0.x`. A
connector running natively on the Mac could not reach the bridge any more than
anything else on macOS can, and every bottle would be back to publishing its
own ports. On a Linux host this would not matter.

**Nothing is published.** The connector dials out, so there is no inbound port
on this Mac, no router forward and no dynamic DNS. The internet-facing surface
is your VPN's control plane, not this machine.

**It must be a private, L3 route — not a public HTTP one.** A public resource
is fronted by an HTTP reverse proxy, and UDP does not traverse one, so mosh
could not ride it. Point a public hostname at web UIs (backrest, later the
management app) instead; terminals go over the private path.

**Addresses.** The connector takes the second-from-last address in
`DJINN_SUBNET` (`172.30.0.253` by default), one below the jump. Override with
`DJINN_TUNNEL_IP`; the image is pinned and overridable via
`DJINN_TUNNEL_IMAGE`, never `:latest`.

### Current provider

The role is provider-neutral — a Tailscale subnet router, Netbird, headscale
or a plain wireguard-go container would occupy the same slot. Everything
vendor-specific lives in one marked block in `src/tunnel_config.py`; nothing
in `./djinn` or `tunnel.sh` names a product.

Today that provider is **Newt**, the site connector for Pangolin:

1. In the Pangolin admin UI, create a **site**. It generates an ID and a
   secret (the secret is shown once).
2. Put all three in `secrets.env`, quoted:

   ```bash
   PANGOLIN_ENDPOINT="https://pangolin.example.com"
   NEWT_ID="abcd1234"
   NEWT_SECRET="..."
   ```

3. `./djinn tunnel start` — it prints the bridge CIDR to register.
4. Add a **private site resource** targeting that CIDR (`172.30.0.0/24` by
   default), then enrol the phone with Olm and scope its `AllowedIPs` to the
   same CIDR.

Newt is a fully user-space WireGuard client, so the container needs neither
`NET_ADMIN` nor `/dev/net/tun`. Its credentials are written to a `0600`
`env_file` under `$DJINN_HOME/compose/`, never inline in the compose overlay,
and are removed on `./djinn tunnel stop`.

### Sharing sessions across devices

Interactive editor terminals use the same landing gate as SSH logins:
`src/tmux-landing.bashrc` triggers for `TERM_PROGRAM=vscode`, creates a fresh
session, and opens the picker when other sessions exist so you can jump into a
session opened elsewhere.

Non-interactive VS Code terminals (tasks, debug consoles, git operations)
stay out of tmux or herdr because the landing snippet is gated on interactive
shells (`$-` contains `i`), and `terminal.integrated.defaultProfile.linux`
stays plain `bash`.

herdr is also one click away on any bottle, whatever `remote.shell` says:
`dev.code-workspace` carries a `herdr` terminal profile, so the "New
Terminal" dropdown (the `˅` next to `+`) offers **herdr** next to **bash**
and opens the bottle's workspace directly (launch-or-attach; detach with
`ctrl+b q`). The profile sets `REMOTE_SHELL=bash` so herdr's own panes never
re-land in tmux, and the workspace file also drops VS Code's sidebar toggle
from `terminal.integrated.commandsToSkipShell` so `ctrl+b` reaches herdr (and
tmux) instead of the editor. Both are add-if-missing like the other managed
settings: edit them in the workspace file (a `--session` arg, say) and
`djinn up` leaves your version alone.

Landing-session GC runs on login and on tmux detach/session-switch hooks:
an unattached `login-*` session is removed only when it is still an idle bare
shell in one window/one pane, so sessions with real work are left untouched.
`login-` is a **reserved prefix** — name your own durable sessions anything
else, or the GC will treat an idle one as an abandoned landing. The GC logs
its kills (and failures) to `/tmp/djinn-tmux-landing-gc.log` in the bottle.

## The jump container (`./djinn jump`)

A singleton container per djinn installation that terminates your inbound
mosh session and hops onward to bottles over `djinn-net`. One mosh endpoint
for the whole fleet instead of a UDP range baked into every bottle.

```bash
./djinn jump start      # build + start; prints the address and the key to authorise
./djinn jump ip         # print the jump's static bridge address
./djinn jump pubkey     # the key your bottles must trust
./djinn jump status
./djinn jump logs -f
./djinn jump stop
```

**First run, once:**

1. Put the public keys you want to log in with — one per line — in
   `$DJINN_HOME/jump/authorized_keys`:

   ```bash
   mkdir -p "$DJINN_HOME/jump"
   cat > "$DJINN_HOME/jump/authorized_keys" <<'KEYS'
   # mac
   ssh-ed25519 AAAA... you@mac
   # phone
   ssh-ed25519 AAAA... moshi
   KEYS
   ```

   Standard `authorized_keys` format: blank lines and `#` comments are fine,
   OpenSSH options work (`restrict`, `from="10.0.0.0/8"`, `command="..."`),
   and the file is left exactly as you wrote it — the labels are how you tell
   later which key belongs to which device. Options are passed through
   untouched and interpreted by sshd, not by djinn. Public keys are not secrets, so
   they live here rather than in `secrets.env`, which is what makes
   **multiple** keys practical with no shell quoting to get wrong.

   Adding or removing a key takes effect on the next `./djinn jump start`:
   the overlay carries a hash of the key set, so compose recreates the
   container instead of leaving the old one running. Reordering the file
   changes nothing, so it will not drop a live session for no reason.

   **Emptying the file authorises nobody, and is an error rather than a
   fallback** — clearing it is how you revoke every device, so it must never
   quietly restore an old key.

   *Upgrading from a single key?* If the file does not exist,
   `SSH_AUTHORIZED_KEY` from `secrets.env` seeds it once. After that the file
   is the only authority and the variable stops mattering for the jump —
   otherwise you could never remove a key `secrets.env` kept re-adding.
2. `./djinn jump start` — creates `djinn-net` if it does not exist yet, then
   generates the jump's own ed25519 keypair (persisted under
   `$DJINN_HOME/jump/ssh/`, so a rebuild keeps it) and prints it.
3. Put that line in `secrets.env` as `JUMP_AUTHORIZED_KEY`, **quoted** — the
   file is sourced by every host script, so an unquoted key's spaces would
   try to run its comment as a command:

   ```bash
   JUMP_AUTHORIZED_KEY="ssh-ed25519 AAAA... djinn-jump"
   ```
4. `./djinn up <bottle>` for each bottle you want reachable. The bottle
   picks up the jump key automatically on its next `./djinn up` — no manifest
   edit needed. The bottle *appends* it to `authorized_keys` — your own
   `SSH_AUTHORIZED_KEY` keeps working, so a bottle stays directly reachable
   even when the jump is down.

**Then, from a phone over your tunnel:**

```
mosh coder@<jump-ip>           # e.g., mosh coder@172.30.0.254
```

Use `./djinn jump ip` to get the jump's static bridge address. The jump shows
a numbered list of running, reachable bottles; selecting one opens
`ssh djinn-<bottle>` while keeping the Mosh connection and picker alive. When
that SSH session exits (or cannot connect), the picker returns so you can choose
another bottle. Press `q` to keep a normal jump shell and make a manual SSH hop;
for example: `ssh djinn-coding-tanks`.

After upgrading, rerun `./djinn up <bottle>` for each existing bottle so it
receives the picker labels, then run `./djinn jump start` (or
`./djinn jump refresh`) to rebuild the list.
The selector is a host-generated, read-only registry — the jump never receives
Docker socket access — and `djinn up` / `djinn down` refresh it as bottles
change.

The downstream hop is **SSH**, not `docker exec`: the bottle's landing logic
applies to sshd logins and interactive editor terminals. `docker exec`
deliberately stays a bare shell.

**Why no published host ports.** The jump is reached at its bridge IP over
the tunnel, so nothing is published to the host at all. Its address is
static so the tunnel target survives a recreate.

**Which address.** The *last* usable address in `DJINN_SUBNET` (`.254` on the
default `/24`), not the first. `djinn-net` is created with `--subnet` and no
`--ip-range`, so docker's dynamic allocator hands out addresses ascending
from `.2` — the low end is exactly where bottles land, and a running fleet
already occupies it. Override with `DJINN_JUMP_IP` (must be an assignable
host address in the subnet, and not the gateway). Both `DJINN_SUBNET` and
`DJINN_JUMP_IP` are read from `./.env`.

**Bottle host keys.** A bottle regenerates its SSH host keys on every
recreate, so the jump uses `StrictHostKeyChecking accept-new` against a
persisted `known_hosts`. After `./djinn up <bottle>`, clear the stale entry:

```bash
docker exec -u coder djinn-jump-<suffix> ssh-keygen -R djinn-<bottle> \
    -f /etc/djinn-jump/ssh/known_hosts
```

`-u coder` matters: the image sets no `USER`, and `ssh-keygen -R` *replaces*
the file rather than editing it. Run as root it leaves a root-owned
`known_hosts`, and `accept-new` then fails to append the recreated bottle's
key on every hop until the container is recreated.

Per-bottle jump users (one Unix user per bottle, each holding only that
bottle's key) are a planned follow-up — see *PLN - Djinn Admin Plane* §D8.
Today the jump holds one key that opens every bottle that authorises it.

The jump is the fleet's only mosh endpoint: bottles run sshd alone, and the
mosh leg from your phone always terminates on the jump.
