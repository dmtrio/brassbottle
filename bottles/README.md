# Bottles

A bottle is the YAML manifest that produces one running container.

```bash
cp bottles/TEMPLATE.yml bottles/my-app.yml
./djinn up my-app
```

The filename is the container identity: `bottles/my-app.yml` becomes
`djinn-my-app`.

## Smoke-Test Default

`TEMPLATE.yml` is intentionally runnable before you edit it:

- omitted or empty `repos:` creates `/workspace/repos/scratch`;
- empty identity settings need no secrets;
- `plugins: []` means no optional MCP tools are wired;
- `agents:` may be omitted to enable every agent descriptor, or set to exact
  directory names from `agents/`.

## What Belongs Here

Bottle files are secret-free and committable. They name secret variables, but
not secret values.

Common keys:

- `repos:` clones project repos into `/workspace/repos`.
- `memory:` sets the Docker memory limit.
- `agents:` chooses agent CLIs by `agents/<name>/`.
- `capabilities.egress:` and `egress_cidrs:` open outbound network access.
- `plugins:` enables MCP/plugin directories from `plugins/`.
- `common_secrets:` and `agent_secrets:` bind secret slots to env var names.
- `ssh:` and `remote:` enable remote-host access.

Secret values live in `.djinn/secrets.env` by default. Copy
`secrets.env.example` only when your bottle references secrets.

## Private Bottle Repos

Real bottles often include private repo URLs, LAN ranges, and identity naming.
Keep those out of this public repo.

Use a private directory or repo instead:

```bash
cat > .env <<'EOF'
BOTTLES_PATH="$HOME/git/djinn-bottles"
EOF
```

If `DJINN_HOME` is set and `$DJINN_HOME/bottles` exists, that directory is used
automatically. `CONTAINERS_PATH` and `$DJINN_HOME/containers` are deprecated
aliases that still warn and resolve for compatibility.

When your bottles are their own git repo, `djinn up` fast-forwards that
checkout before reading the bottle, so a merged bottle PR takes effect on
the next run rather than waiting for you to remember `git pull`. It never
touches the bundled `bottles/` (that would pull brassbottle), never fails
the run — offline, no upstream, or a bottle you're mid-edit all just carry
on — and prints which case it took.

## Firewall egress (`capabilities:`)

| Manifest key | Effect |
|---|---|
| `egress: [...]` | extra allowed zones (a zone covers its subdomains) |
| `egress_cidrs: [...]` | IP-range escape hatch (LAN subnets) |

A container without a grant cannot reach the zone — enforced by the
in-container firewall (dnsmasq resolver-driven ipset; rotating CDN DNS
can't outrun it). Plugins fold their own `egress`/`host_port` needs into
the firewall when enabled — see
[`plugins/README.md`](../plugins/README.md).
