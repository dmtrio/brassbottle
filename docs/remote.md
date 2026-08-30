# Remote hosts & remote agent access

## Remote hosts (Linux server or VPS)

Same system, same files, one addition. On any Linux box with Docker:

1. Install `yq` (static binary) and `python3`, and clone this repo. The
   bundled `rules/` and gitignored `./.djinn/` work as-is; set `DJINN_HOME`
   / `RULES_PATH` in `./.env` only if you want them elsewhere.
2. Put your secrets in `secrets.env` (default `./.djinn/secrets.env`,
   600) — including `SSH_AUTHORIZED_KEY` (your public key).
3. Add an `ssh:` section to the container's manifest:

   ```yaml
   ssh:
     port: 2222        # published on the host
     bind: 127.0.0.1   # keep loopback; reach it through your tunnel
   ```

4. `./djinn up <name>` — identical to the Mac. Connect with VS Code
   **Remote-SSH** to the host/port; everything else (firewall, secrets,
   rules, artifacts) behaves exactly the same.

Never expose sshd publicly: keep the bind on loopback (or a tunnel
interface) and front it with your WireGuard/VPN tunnel. The remote MCP
plugins (`gateway`/`proxyman`/`browser`) are Mac-desktop services — on
headless hosts leave them out of `plugins:` or run the host service on that
host.

## Remote agent access (phone / second device)

The `remote:` manifest block (requires `ssh:`) turns an SSH-reachable
container into something you can drive from a phone — start a task, walk
away, get pinged when the agent needs you, answer from anywhere. Works for
every agent in the image; nothing is vendor-hosted or public-facing.

```yaml
ssh:     { port: 2222, bind: 127.0.0.1 }
remote:  { tmux: true, mosh: true, notify: ntfy }
```

- **tmux** — interactive SSH/mosh logins land attached to one durable
  session (`agent`). Phone and laptop share the same view; agents survive
  disconnects. `docker exec` and editor terminals are exempt.
- **mosh** — a per-manifest UDP range (`remote.mosh_ports`, default
  60000:60010; disjoint per container, like `ssh.port`), published next to
  sshd with the same bind rules and pinned server-side. Survives phone
  sleep and WiFi↔cellular switches; use a mosh-capable client (e.g. Moshi
  or Blink on iOS).
- **notify: ntfy** — an agent-blind monitor pushes to your ntfy topic when
  the session goes idle at a prompt and nobody is attached. Set `NTFY_URL`
  (+ optional `NTFY_TOPIC`) in `secrets.env`; the host is auto-allowlisted.

**Reach.** All containers sit on one shared bridge (`djinn-net`,
`172.30.0.0/24` by default, `DJINN_SUBNET` in `./.env` to override; created
automatically by `djinn up`).
Point your WireGuard/VPN layer at that CIDR once and every container is
reachable at its bridge IP from any enrolled device — `djinn up` prints the
IP in its summary. sshd and the mosh range stay loopback/tunnel-only;
nothing listens publicly.

## The jump container (`./djinn jump`)

A singleton container per djinn installation that terminates your inbound
mosh session and hops onward to bottles over `djinn-net`. One mosh endpoint
for the whole fleet instead of a UDP range baked into every bottle.

```bash
./djinn jump start      # build + start; prints the address and the key to authorise
./djinn jump pubkey     # the key your bottles must trust
./djinn jump status
./djinn jump logs -f
./djinn jump stop
```

**First run, once:**

1. `./djinn jump start` — creates `djinn-net` if it does not exist yet, then
   generates the jump's own ed25519 keypair (persisted under
   `$DJINN_HOME/jump/ssh/`, so a rebuild keeps it) and prints it.
2. Put that line in `secrets.env` as `JUMP_AUTHORIZED_KEY`, **quoted** — the
   file is sourced by every host script, so an unquoted key's spaces would
   try to run its comment as a command:

   ```bash
   JUMP_AUTHORIZED_KEY="ssh-ed25519 AAAA... djinn-jump"
   ```
3. `./djinn up <bottle>` for each bottle you want reachable. The bottle
   *appends* it to `authorized_keys` — your own `SSH_AUTHORIZED_KEY` keeps
   working, so a bottle stays directly reachable even when the jump is down.

**Then, from a phone over your tunnel:**

```
mosh coder@172.30.0.254        # the jump's static bridge address
ssh djinn-coding-tanks         # hop onward; lands in the bottle's tmux session
```

The hop must be **SSH**, not `docker exec` — `src/tmux-landing.bashrc` only
attaches the durable `agent` session for an `sshd`/`mosh-server` parent, so
`docker exec` deliberately drops you in a bare shell with nothing persistent.

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
docker exec djinn-jump-<suffix> ssh-keygen -R djinn-<bottle> \
    -f /etc/djinn-jump/ssh/known_hosts
```

Per-bottle jump users (one Unix user per bottle, each holding only that
bottle's key) are a planned follow-up — see *PLN - Djinn Admin Plane* §D8.
Today the jump holds one key that opens every bottle that authorises it.

Per-bottle `remote.mosh` still works and is unchanged; the two paths coexist
so the jump can be proven before anything is removed.
