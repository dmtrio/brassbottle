## Egress request tools

Outbound traffic is firewall allowlisted. Blocked destinations are intercepted
and filed for operator approval — you do not need to parse `EHOSTUNREACH` or TLS
errors to discover the hostname.

### When to use

- **Before a batch of fetches** to a new host, call `check_egress` (or
  `request-egress --check`) on every host you plan to hit, then `request_egress`
  once if any are still blocked.
- **Non-TLS destinations** (Postgres :5432, SIP, raw TCP) are invisible to the
  transparent :80/:443 proxy. Name the **hostname zone** explicitly before the
  command runs — e.g. `request_egress(["neon.tech"], "db:migrate")` or
  `request-egress neon.tech:5432 "db:migrate"` — not the IP and not `*.zone`
  wildcards (bare zones already cover subdomains).
- **Codex** has no remote MCP servers in this image; use the `request-egress`
  shell command instead of these MCP tools.

### Tools

- `request_egress(hosts[], reason?)` — file and block until allow / deny /
  timeout. Pass every host you are about to use in one call when pre-flighting a
  batch.
- `check_egress(hosts[])` — read-only ipset probe; does not notify the operator.

### Operator action

Approval is always host-side: `./djinn allow <bottle> <zone>` or
`./djinn allow --watch` on the Mac. The operator may also answer from an ntfy
push notification when `NTFY_URL` is configured. Retries after approval should go direct
without filing again.
