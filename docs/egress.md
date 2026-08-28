# Egress approval

Outbound traffic from djinn containers is firewall allowlisted. When a process
hits a destination that is not yet allowed, the egress broker holds the request
until an operator approves or denies it.

## Watcher and daemon

`./djinn allow --watch` runs an interactive watcher on the host. It starts the
egress broker HTTP server in-process, polls the approval queue, and presents
each open request in the terminal. On macOS it also shows a system dialog in
parallel with the terminal prompt.

The standalone egress broker daemon (`egress_broker_host.py`) provides the same
queue and HTTP API without the interactive UI. Containers file requests via
`POST /egress`; the operator answers through the watcher, CLI, or notification
actions.

## Notifications

### macOS banner / dialog

On macOS, each new request triggers a `display dialog` prompt alongside the
terminal UI. The dialog offers Allow and Deny; the terminal supports live
allow, persist-to-manifest, deny, and skip.

### Push (ntfy)

When `NTFY_URL` is set, every new egress request also publishes one ntfy push.
Use the same values as the tmux idle notifier:

- `NTFY_URL` — bare origin (for example `https://ntfy.example.com`)
- `NTFY_TOPIC` — optional; defaults to `djinn-agents`
- `NTFY_TOKEN` — optional bearer token for authenticated servers

Set these in `$DJINN_HOME/secrets.env`, subscribe to the topic on each device,
then restart `./djinn allow --watch`.

Without `NTFY_URL`, notifications are terminal-only (plus the macOS dialog when
applicable).

### Action buttons

ntfy action buttons appear only when the broker binds an address a device can
dial: not loopback, and not the unspecified `0.0.0.0` / `::`. Use the concrete
host IP the devices reach, for example:

```bash
./djinn allow --watch --host <wireguard-ip>
```

With actions enabled, each push includes HTTP buttons that call the broker
`POST /decide` endpoint using the operator bearer token. That token is embedded
in the notification payload sent to the ntfy server and delivered to every
subscribed device.

**Do not use the public ntfy.sh service for action buttons.** Request metadata
includes container names and destination hosts. Self-host ntfy on your VPN or
private network instead.

Button behavior:

- **Allow** — live allow for the request's host zone
- **Allow + persist** — allow and write the zone into the bottle manifest
- **Deny** — deny with reason `denied from notification`

IP-literal destinations (for example `192.0.2.55:5432`) cannot be allowed via
`/decide` (they require a manifest CIDR grant). Those pushes show a warning tag
and only a **Deny** button.
