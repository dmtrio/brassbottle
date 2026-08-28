# bin

Host-side helper commands live here. Most users call them through `djinn`:

```bash
./djinn allow <container> <domain...>
./djinn keys <container> ...
```

The scripts remain runnable directly for debugging.

## `allow-egress.sh`

Adds domains to a running container's egress allowlist without rebuilding or
restarting the container.

```bash
./djinn allow my-app cdn.playwright.dev playwright.download.prss.microsoft.com
```

It accepts either the short bottle name (`my-app`) or full container name
(`djinn-my-app`). Domains must be bare hostnames: no scheme, path, or port.

The live change is ephemeral. At the end, choose where to persist it:

- `yml` writes to the bottle's `capabilities.egress` for the next `djinn up`.
- `firewall` adds it to the base firewall for future image builds.
- `none` leaves it live-only.

Pass `--save yml|firewall|none` to skip the prompt.

## `update-agent-keys.sh`

Temporarily edits one container's generated per-agent key files.

```bash
./djinn keys my-app                      # list available vars
./djinn keys my-app claude SOME_TOKEN    # prompt for one agent's value
./djinn keys my-app common SOME_TOKEN    # set for every shim agent
```

The change applies the next time that agent starts. It does not require a
container restart.

This is for quick experiments only. The generated key files are recomposed on
the next `djinn up`; durable changes belong in `.djinn/secrets.env` and the
bottle's `common_secrets:` / `agent_secrets:`.

## `request-egress`

In-container CLI for filing egress approval when an agent has no MCP tools
(codex, `cursor-agent`, shell scripts). Wraps the same host broker long-poll as
the transparent broker and the optional `egress` plugin's MCP tools.

```bash
request-egress docs.stripe.com "fetch API reference"
request-egress host1.example.com host2.example.com "batch pre-flight"
request-egress neon.tech:5432 "db:migrate pre-flight"
request-egress --check api.stripe.com
```

Exit codes: **0** allowed, **1** denied (or broker error), **2** timed out still
queued. Operator approval remains host-side (`./djinn allow` / `./djinn allow
--watch`).

On macOS, `./djinn allow --watch` posts a Notification Center banner (it
appears as coming from **Script Editor**; allow it once in System Settings →
Notifications) and brings the approval dialog to the front.
