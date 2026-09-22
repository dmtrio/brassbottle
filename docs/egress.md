# Egress approval

Outbound traffic from djinn containers is firewall allowlisted. When a process
hits a destination that is not yet allowed, the egress broker holds the request
until an operator approves or denies it.

## Service

The broker and the admin page run as a docker compose singleton — no terminal
has to stay open:

```
./djinn egress start   # build + start both containers, print the session URL
./djinn egress stop
./djinn egress status
./djinn egress logs [-f]
./djinn egress url     # the admin session URL (one page-load signs you in)
./djinn egress ip      # the broker's static djinn-net address
```

`start` renders `$DJINN_HOME/egress/docker-compose.yml` and drives
`docker compose -p djinn-egress`. Two services from one image
(`egress/Dockerfile`), both `restart: unless-stopped`:

- `broker` — `egress_broker_host.py` on `djinn-net` at a static address
  (`EGRESS_BROKER_HOST`, offset 3 of the bridge subnet; `./djinn egress ip`
  prints it) and published on `127.0.0.1:8816`. It applies allows by
  exec'ing `bin/allow-egress.sh` through the **docker socket mounted at
  `/var/run/docker.sock`**: the host-side `docker`/`yq`/python3 the script
  needs live inside the image, and the socket is what lets it exec into
  bottles and edit manifests without any terminal on the host. `DOCKER_HOST`
  selects the socket (`unix://<path>`), anything else is refused.
- `admin` — `admin_daemon.py` on the compose-private `egress-backend`
  network only (no bottle has a route to it), published on `127.0.0.1:8817`.

Open the admin page from the URL `./djinn egress url` prints
(`http://127.0.0.1:8817/session?key=…`): that one page-load sets the session
cookie. The bare page address serves a pointer page and sets nothing, and a
bottle gets no session over the host gateway either way. The session key and
the operator token live under `$DJINN_HOME/run/egress/`, created host-side
before the containers start.

`EGRESS_ACTIONS_URL` (environment or `secrets.env`) points ntfy action
buttons at an address a phone can reach (the broker's container bind is
`0.0.0.0`, which no phone can dial) — unset means no action buttons.

`EGRESS_BROKER_HOST` reaches bottles through `./djinn up` (derived by
`src/manifest.py` from `DJINN_SUBNET`/`DJINN_EGRESS_IP`; `init-firewall.sh`
opens `EGRESS_BROKER_HOST:8816` the way it opens `HOST_MCP_PORTS`), so a
bottle created before the service existed files at its old address and needs
its next `./djinn up`. `up.sh` prints one warning when the service is down;
bring it up with `./djinn egress start`.

### Subnet drift

If `djinn-net` already exists with a subnet different from `DJINN_SUBNET`
(`ensure_net` only warns and keeps the bridge), both the broker and the
bottles follow the **live bridge**: `egress start` renders the compose file
with the live address, and `up.sh` re-resolves `EGRESS_BROKER_HOST` after
`ensure_net` with the same live-bridge resolver instead of passing the
desired-subnet derivation. Verify after a drift with `./djinn egress ip` and
the `⚠ egress:` line in `./djinn up` output — both should name the same
subnet, and a bottle brought up after that files (and is firewall-granted)
at that live address.

## Operator surface (primary: `djinn admin`)

`./djinn egress start` is the primary decision surface for open egress
requests: it runs the admin page as a service (above). `./djinn admin` still
runs the same UI as a host-side loopback daemon (`http://127.0.0.1:8817` by
default) when you want it outside docker.

The browser session gate on `POST /api/egress/decide` is intentionally narrow:
it defends against hostile web pages (CSRF and DNS-rebinding style requests)
with a strict cookie + header + origin/host policy, but it does **not** defend
against local processes running as the same user. Local processes can already
read the operator token file from disk, so loopback binding is the real trust
boundary.

`./djinn allow --watch` remains available for now and reads the same broker
queue, but it is slated for deprecation in favor of the admin plane.

### Decision action mapping

The admin UI posts one of five actions, mapped to broker `/decide`:

- `allow_live` -> `decision=allow`, `scope=live`
- `allow_manifest` -> `decision=allow`, `scope=manifest`
- `deny` -> `decision=deny`, `scope=once`
- `deny_bottle` -> `decision=deny`, `scope=bottle`
- `deny_global` -> `decision=deny`, `scope=global`

`deny_global` intentionally omits `container`; other actions require it.

### PWA install and alerts

The admin page is a small PWA (manifest + service worker). It supports install
from Chromium/Safari "Add to home screen" flows and can request Notification
permission when the operator clicks "Enable alerts". New unseen requests can
raise a local notification when the page is in the background.

### Decisions, not current state

The queue panel shows **open approval requests and decisions** only. It does
not show currently-permitted hosts; the ipset allowlist remains the runtime
authority.

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

### `POST /decide` response shape

Allow responses always include `decided` and `apply_failures`:

- `decided` — request ids that were fully closed by the allow decision.
- `apply_failures` — objects of `{request_id, reason}` for requests that matched
  the zone but stayed open because no rule was installed.

`reason` is one of:

- `ip_requires_cidr` — request host is an IP literal; add a matching CIDR to
  the bottle manifest (`capabilities.egress_cidrs`) by hand.
- `apply_failed` — `allow-egress.sh` did not install the rule.

When a request appears in `apply_failures`, it remains open and still appears in
`GET /queue` until a later allow succeeds or it is denied.

Deny responses are unchanged: they return `decided` (and `persisted` for
`scope=bottle|global`) and do not include `apply_failures`.

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
