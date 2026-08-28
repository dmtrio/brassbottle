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

## Loopback is never filed

Traffic to `127.0.0.0/8` is local, not egress, and never reaches the operator
queue. Two guards enforce it:

- The nat REDIRECT rule carries `! -d 127.0.0.0/8`, so a local service on
  :80/:443 is not intercepted at all.
- The broker refuses a loopback destination at the socket. A dial to its own
  listen port (`127.0.0.1:3128`) is logged as `self_dial` and closed; any other
  loopback destination is spliced straight through.

The self-dial case means a process has `http_proxy`/`https_proxy` pointed at
`127.0.0.1:3128`. The broker is a *transparent* proxy — it reads the
destination from the kernel via `SO_ORIGINAL_DST` — and takes no forward-proxy
clients. Unset those variables in the bottle; with them set, every request the
process makes arrives as an unanswerable IP-literal approval prompt.

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

## Persistent deny list

`./djinn deny <zone> --bottle NAME|--global` writes an entry to
`$DJINN_HOME/run/egress/denylist.json` that short-circuits future requests for
that zone before an operator is ever prompted; `./djinn undeny` removes one.
`./djinn deny --list` reads the same file; so does `bin/allow-egress.sh`
internally, via `egress_denylist.py --check <bottle> <domain>...` (a bare
bypass with no `./djinn` front-end — it is not a `deny` flag).

### Audit log fields for a denylist-caused denial

A `denied` event in the audit log (`$DJINN_HOME/run/egress/*.jsonl`) can be
caused by a persisted deny-list entry in two ways: an already-denylisted zone
short-circuits the request outright (no hold, no operator prompt), or a
brand-new entry (`./djinn deny` / the watcher's D/G keys / `/decide` with
`scope=bottle|global`) sweeps closed any request it now covers. Both cases
write the same three fields to name the entry that caused the denial:

- `via` — always the literal string `"denylist"`.
- `zone` — the entry's zone (the value that was persisted, not necessarily
  the exact host that triggered the check — a zone covers its subdomains).
- `denylist_scope` — the entry's own scope as written to disk: a bottle name,
  or `"global"`.

`scope` on a `denied` event keeps its plain-deny meaning — the *request's*
own scope (`once`/`bottle`/`global`), i.e. what was asked for, not what the
matched entry is scoped to — and is absent entirely on a short-circuited
denial, since no `/decide` call ever happened for it. `reason` stays free
text throughout: the operator's own explanation (from `./djinn deny --reason
...` or the matched entry's own `reason`, if either was given), never the
literal string `"denylist"` — that literal is confined to the HTTP response
body a still-held container sees, not the audit event.
