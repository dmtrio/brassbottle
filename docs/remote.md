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
