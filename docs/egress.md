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

### Broker endpoints

- `GET /health` — no auth; returns `{"status":"ok"}`.
- `POST /egress` — bottle bearer token; files or coalesces one request.
- `POST /decide` — operator bearer token; applies allow/deny decisions.
- `GET /queue` — operator bearer token; returns the daemon's current queue
  snapshot for UI clients:

```json
{
  "open": [
    {
      "request_id": "deadbeef",
      "container": "coding-brassbottle",
      "host": "192.0.2.55",
      "port": 5432,
      "host_is_ip": true,
      "opened_at": "2026-08-31T23:00:00Z",
      "age_seconds": 12,
      "hit_count": 3,
      "uid": 1000,
      "comm": "curl",
      "reason": "npm install"
    }
  ],
  "count": 1,
  "generated_at": "2026-09-01T00:05:00Z"
}
```

`GET /queue` reports open decision requests only. It must never be interpreted
as "currently allowed hosts" state; ipset `allowed-domains` is the sole
authority for active permit checks.

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
clients, so such a request cannot succeed: it is logged as `self_dial`, answered
with a local `502` (or a TLS alert on :443), and closed without filing
anything. Unset those variables in the bottle — until they are, every request
the process makes fails this way, and the only record is the `self_dial` log
line. Before this was refused at the socket the same request was *filed*, and
surfaced as an IP-literal approval prompt no operator answer could clear.

### Type-ahead is discarded

The input queue is flushed each time a prompt is rendered, so only keystrokes
made **after** a question is on screen can answer it. Without that, anything
typed while the watcher was polling is returned the instant the next prompt
appears — silently answering a request the operator never read, and with
`D`/`G` writing a persistent deny-list entry.

If you type ahead deliberately, the keystrokes are dropped rather than queued;
answer each request as it appears.

## Notifications

### macOS banner / dialog

On macOS, each new request triggers a `display dialog` prompt alongside the
terminal UI. The dialog offers Allow and Deny; the terminal supports live
allow, persist-to-manifest, deny, and skip.

The Notification Center **banner** is fired by the poll loop as soon as a
request appears, once per request, for every open request — not by the prompt.
The watcher prompts one request at a time, so a banner welded to the prompt
could not fire for anything queued behind an unanswered request. ntfy push is
unaffected: it is dispatched daemon-side when the request is filed.

### Skipping and IP-literal requests

`[s]` defers a request for the rest of the session; the deferral is released
when the queue drains. `[a]` on an **IP-literal** destination cannot install a
rule (IP grants come from the manifest's `capabilities.egress_cidrs`), so the
watcher prints what to edit and defers the request rather than re-prompting.

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
