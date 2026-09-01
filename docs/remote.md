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

**Landing shell.** Interactive SSH/mosh logins land in a fresh `login-*`
session by default. Customize with `remote.shell`:

```yaml
remote:
  shell: tmux     # default; interactive terminals land in tmux picker
  # shell: bash   # opt out — land in plain bash
```

- **tmux (default)** — SSH, mosh, and VS Code/Cursor terminals land in a
  fresh `login-*` session. If other sessions already exist, tmux opens the
  session picker automatically; press Esc to keep the fresh landing session.
  You can always reopen the picker with `Ctrl-b s`.
- **bash** — land in a plain bash shell; no session picker or shared view.
- **mosh** (optional, needs `ssh:`) — `remote.mosh: true` publishes a
  per-manifest UDP range (`remote.mosh_ports`, default `60000:60010`;
  disjoint per bottle, like `ssh.port`) next to the published sshd, pinned
  server-side. Most bottles never need it: the jump carries mosh for the
  whole fleet, and mosh's reconnect-through-sleep survives the hop.
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
bridge and a direct `ssh`/`mosh coder@<bridge ip>` over the tunnel works
too — `djinn up` prints the IP). Nothing listens publicly.

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

Interactive editor terminals now use the same landing gate as SSH/mosh:
`src/tmux-landing.bashrc` triggers for `TERM_PROGRAM=vscode`, creates a fresh
session, and opens the picker when other sessions exist so you can jump into a
session opened elsewhere.

Non-interactive VS Code terminals (tasks, debug consoles, git operations)
stay out of tmux because the landing snippet is gated on interactive shells
(`$-` contains `i`), and `terminal.integrated.defaultProfile.linux` stays
plain `bash`.

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
ssh djinn-coding-tanks         # hop onward; fresh tmux landing + picker
```

Use `./djinn jump ip` to get the jump's static bridge address. The hop must
be **SSH**, not `docker exec` — `src/tmux-landing.bashrc` only applies to
sshd/mosh-server and interactive editor terminals. `docker exec` deliberately
stays a bare shell.

**Why no published host ports.** The jump is reached at its bridge IP over
the tunnel, so nothing is published to the host at all. That also removes the
host-port exclusivity that forced a disjoint `remote.mosh_ports` range per
bottle: one in-container range now serves every session. Its address is
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

Per-bottle `remote.mosh` still works and is unchanged; the two paths coexist
so the jump can be proven before anything is removed.
