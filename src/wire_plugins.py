#!/usr/bin/env python3
"""Agent-config wiring for up.sh — both sides of one docker exec.

up.sh runs this file twice per `up`, so the payload schema lives in exactly
one place and both halves are unit-testable:

  host side:      python3 src/wire_plugins.py --build-payload
                  reads env vars (see build_payload) and prints the JSON
                  payload; booleans use the same strict string comparison the
                  old bash used ([ "$X" = "true" ]), so a manifest value like
                  `gateway: yes` stays OFF instead of leaking into the JSON.
  container side: python3 /usr/local/lib/djinn/wire_plugins.py
                  reads that payload on stdin and wires MCP servers into every
                  installed agent's config files — the work that used to live
                  in up.sh as jq/sed programs inside triple-quoted
                  `docker exec bash -c` strings.

Payload:

    {
      "agents":       [
                        {"binary": ..., "config_path": ..., "format": ...,
                         "dialect": ..., "env_refs": ..., "strategy": ...},
                        ...
                      ],
      "plugin_mcp_entries": [{"<name>": <local or env-scoped-remote spec>}, ...],
      "agent_servers": [
        {"name": "obsidian-annotated", "slot": "OBSIDIAN_ANNOTATED_KEY",
         "spec": {"url": ..., "headers": {...${SLOT}...}},
         "ref":     ["claude", "kimi"],                    # env-ref configs
         "literal": [{"agent": "cursor-agent",
                      "key_envs": {"OBSIDIAN_ANNOTATED_KEY": "IDENTITY_KEY_0"}}],
         "warn":    ["codex"],                             # REMOTE spec only
         "local":   ["cursor-agent", "codex", ...]}        # LOCAL spec only
      ]
    }

- agents is AGENTS_MCP_JSON from manifest.py, ordered by enabled agent
  descriptor directory name. This module validates it strictly (shape and
  allowed dialect/strategy values) before wiring any files. Each entry may
  carry `settings` (the descriptor's config_settings): top-level scalar keys
  stamped into the agent's own config as a second managed block, rendered
  only by the codex_managed_block strategy.
- plugin_mcp_entries carries one object per NON-agent-scoped plugin (host-side
  manifest.py extracts them from plugins/<name>.yml). A spec is LOCAL
  ({command, args} — stdio, wired into every agent) or env-scoped REMOTE
  ({url, headers} — http, wired into Claude's .mcp.json only). Cross-plugin
  duplicate server names hard-fail here as well as host-side (last-wins merge).
- agent_servers carries the AGENT-SCOPED plugins, wired only for the agents
  bound to the slot (its key gates who sees the server). A REMOTE spec (obsidian)
  presents each bound agent's own role derived from AGENTS_MCP_JSON:
  - bool-ref agents (`env_refs: true`) keep ${SLOT} refs (`ref`),
  - named-ref agents (`env_refs: "<field>"`) keep remote URL/header shape but
    swap the bearer header ref for `<field>: "SLOT"` (`ref`),
  - literal agents bake per-agent key env names (`literal`),
  - managed-block agents warn for remote (`warn`),
  and LOCAL specs route through each bound agent's local wiring path (`local`
  for non-ref-style, `ref` for ref-style). Env-only agent-scoped slots (watch
  keys — no server) never reach the payload; up.sh delivers them straight into
  the agent's env file.
- Keys never ride in the payload: each literal[] element names an environment
  variable (set on the docker exec) that holds the key, so the payload is
  secret-free. Ref-style and managed-block agents receive no literal key.
- Version skew between the two halves cannot happen in the up.sh flow: the
  payload is built from the repo checkout and the consumer is baked from the
  same checkout by the `docker compose up --build` that just ran.

Stdlib only — dev-Mac CLT and the image both guarantee python3 but nothing
else, and the TOML we emit (bare validated keys, JSON-escaped strings/arrays,
which are a TOML subset) doesn't need a writer library. Every config write is
atomic (tmp + rename) with the final mode set before the rename, except
~/.claude.json which is written through in place (it predates us and may be a
symlink into someone's dotfiles — the old `cat >` behavior).
"""

import json
import os
import re
import sys
from pathlib import Path

# No server names are reserved any more: as of Plugins v2 Phase 2, EVERY MCP
# server up.sh wires comes from a plugin file (obsidian-annotated is now
# plugins/obsidian-annotated/plugin.yml, an agent-scoped remote plugin). Cross-plugin
# name collisions are caught by the generic duplicate check below and host-side.

# The codex managed block. Detection matches on the PREFIX (like the old sed
# ranges did), so a stale block written by an older up.sh with different
# trailing text is still stripped.
CODEX_OPEN_PREFIX = "# >>> djinn plugin MCP"
CODEX_CLOSE_PREFIX = "# <<< djinn plugin MCP"
CODEX_OPEN_MARKER = "# >>> djinn plugin MCP (managed by up.sh; edits inside are overwritten) >>>"
CODEX_CLOSE_MARKER = "# <<< djinn plugin MCP <<<"

# The codex managed SETTINGS block (descriptor config_settings). It is a
# separate block from the MCP one and lives at the HEAD of the file, because a
# bare TOML key written after a table would be parsed as a member of that
# table — the MCP block renders [mcp_servers.*] tables and must stay at the
# tail for exactly the mirror-image reason.
CODEX_SETTINGS_OPEN_PREFIX = "# >>> djinn codex settings"
CODEX_SETTINGS_CLOSE_PREFIX = "# <<< djinn codex settings"
CODEX_SETTINGS_OPEN_MARKER = "# >>> djinn codex settings (managed by up.sh; edits inside are overwritten) >>>"
CODEX_SETTINGS_CLOSE_MARKER = "# <<< djinn codex settings <<<"
# Settings keys are emitted as bare TOML keys, so they are constrained to the
# bare-key charset host-side (manifest.py) and re-checked here.
CODEX_SETTING_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")

AGENT_MCP_FIELDS = frozenset({
    "binary", "config_path", "format", "dialect", "env_refs", "strategy",
    "settings",
})
# settings is the one OPTIONAL descriptor field: an agent with no
# config_settings block renders no settings block, and absent must mean the
# same as {} so a payload built before the field existed still wires.
AGENT_MCP_REQUIRED_FIELDS = AGENT_MCP_FIELDS - frozenset({"settings"})
AGENT_MCP_FORMATS = frozenset({"json", "toml"})
AGENT_MCP_STRATEGIES = frozenset({"claude_preapprove", "codex_managed_block"})
LITERAL_DIALECTS = frozenset({"url", "httpUrl", "type-http", "serverUrl"})
AGENT_MCP_DIALECTS = LITERAL_DIALECTS | frozenset({"mcpServers"})

# COSMETIC ONLY: per-agent progress notes. Wiring logic MUST NOT key off these
# names; agents absent from these maps still wire with generic messages.
# _AGENT_NOTES suffixes a ✓ line when a required remote server is WRITTEN for
# the agent (why it got a literal key); _AGENT_SYNC_NOTES prints once after the
# agent's plugin sync regardless (a standing caveat about the agent itself).
_AGENT_NOTES = {
    "cursor-agent": "literal key: env interpolation broken for remote headers",
    "pi": "inert until the pi-mcp-adapter extension is installed",
}
_AGENT_SYNC_NOTES = {
    "pi": "pi: inert until the pi-mcp-adapter extension is installed",
}


class WireError(Exception):
    """Fatal wiring error; main() prints it as 'Error: …' and exits 1."""


def _write_atomic(path, text, mode=None, errors=None):
    """Write text to path via tmp + rename; chmod the tmp BEFORE the rename so
    the final path never exists with looser permissions. mode=None keeps the
    umask default (e.g. repos/.mcp.json, which holds ${VAR} refs, not
    secrets)."""
    tmp = path.parent / (path.name + ".tmp")
    # newline="": no newline translation — what we assembled is what lands.
    with open(tmp, "w", encoding="utf-8", errors=errors, newline="") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


def _dump_json(obj):
    # jq-style output: 2-space indent, raw UTF-8, trailing newline.
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def _load_json_file(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise WireError(f"{path} is not valid JSON: {e}")
    return data


def _agent_note_suffix(binary):
    note = _AGENT_NOTES.get(binary)
    return f" ({note})" if note else ""


def _home_config_path(home, binary, config_path):
    rel = Path(config_path)
    if rel.is_absolute():
        raise WireError(f"agent '{binary}' config_path must be home-relative (got '{config_path}')")
    if ".." in rel.parts:
        raise WireError(f"agent '{binary}' config_path must not traverse directories (got '{config_path}')")
    return home / rel


def _normalize_agent_mcp_entry(entry, where):
    if not isinstance(entry, dict):
        raise WireError(f"{where} must be a JSON object")
    extra = sorted(k for k in entry if k not in AGENT_MCP_FIELDS)
    if extra:
        raise WireError(
            f"{where} has unsupported field(s): {', '.join(extra)} "
            "(expected binary, config_path, format, dialect, env_refs, strategy, settings)"
        )
    missing = sorted(k for k in AGENT_MCP_REQUIRED_FIELDS if k not in entry)
    if missing:
        raise WireError(f"{where} is missing required field(s): {', '.join(missing)}")

    binary = entry.get("binary")
    config_path = entry.get("config_path")
    fmt = entry.get("format")
    dialect = entry.get("dialect")
    env_refs = entry.get("env_refs")
    strategy = entry.get("strategy")
    settings = entry.get("settings")
    if settings is None:
        settings = {}

    if not isinstance(binary, str) or not binary:
        raise WireError(f"{where}.binary must be a non-empty string")
    if not isinstance(config_path, str) or not config_path:
        raise WireError(f"{where}.config_path must be a non-empty string")
    if not isinstance(fmt, str) or fmt not in AGENT_MCP_FORMATS:
        raise WireError(f"{where}.format must be one of: {', '.join(sorted(AGENT_MCP_FORMATS))}")
    if not isinstance(dialect, str):
        raise WireError(f"{where}.dialect must be a string")
    if dialect and dialect not in AGENT_MCP_DIALECTS:
        raise WireError(f"{where}.dialect must be one of: {', '.join(sorted(AGENT_MCP_DIALECTS))}")
    if not isinstance(env_refs, (bool, str)):
        raise WireError(f"{where}.env_refs must be a boolean or string")
    if not isinstance(strategy, str):
        raise WireError(f"{where}.strategy must be a string")
    if strategy and strategy not in AGENT_MCP_STRATEGIES:
        raise WireError(f"{where}.strategy must be one of: {', '.join(sorted(AGENT_MCP_STRATEGIES))}")
    if not isinstance(settings, dict):
        raise WireError(f"{where}.settings must be a JSON object")
    for key, value in settings.items():
        if not isinstance(key, str) or not CODEX_SETTING_KEY_RE.match(key):
            raise WireError(
                f"{where}.settings has key {key!r} that is not a bare TOML key "
                "([A-Za-z0-9_-]+)")
        if not isinstance(value, (str, bool, int)):
            raise WireError(
                f"{where}.settings.{key} must be a string, boolean, or integer")
    if settings and strategy != "codex_managed_block":
        raise WireError(
            f"{where}.settings is only rendered by strategy codex_managed_block")

    return {
        "binary": binary,
        "config_path": config_path,
        "format": fmt,
        "dialect": dialect,
        "env_refs": env_refs,
        "strategy": strategy,
        "settings": dict(settings),
    }


def _parse_agents_payload(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise WireError("agents must be a JSON array")
    normalized = []
    seen = set()
    for i, entry in enumerate(value):
        agent = _normalize_agent_mcp_entry(entry, f"agents[{i}]")
        binary = agent["binary"]
        if binary in seen:
            raise WireError(f"agents has duplicate binary '{binary}'")
        seen.add(binary)
        normalized.append(agent)
    return normalized


def _parse_agent_servers_payload(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise WireError("agent_servers must be a JSON array")
    out = []
    for i, entry in enumerate(value):
        where = f"agent_servers[{i}]"
        if not isinstance(entry, dict):
            raise WireError(f"{where} must be a JSON object")
        if not isinstance(entry.get("name"), str) or not entry.get("name"):
            raise WireError(f"{where}.name must be a non-empty string")
        if not isinstance(entry.get("spec"), dict):
            raise WireError(f"{where}.spec must be a JSON object")
        requires = entry.get("requires")
        if not isinstance(requires, list) or not all(isinstance(slot, str) for slot in requires):
            raise WireError(f"{where}.requires must be a list of slots")
        ref = entry.get("ref")
        if not isinstance(ref, list) or not all(isinstance(agent, str) for agent in ref):
            raise WireError(f"{where}.ref must be a list of agent binaries")
        warn = entry.get("warn")
        if not isinstance(warn, list) or not all(isinstance(agent, str) for agent in warn):
            raise WireError(f"{where}.warn must be a list of agent binaries")
        local = entry.get("local")
        if not isinstance(local, list) or not all(isinstance(agent, str) for agent in local):
            raise WireError(f"{where}.local must be a list of agent binaries")
        literal = entry.get("literal")
        if not isinstance(literal, list):
            raise WireError(f"{where}.literal must be a list")
        normalized_literal = []
        for j, lit in enumerate(literal):
            lit_where = f"{where}.literal[{j}]"
            if not isinstance(lit, dict):
                raise WireError(f"{lit_where} must be a JSON object")
            if not isinstance(lit.get("agent"), str) or not lit.get("agent"):
                raise WireError(f"{lit_where}.agent must be a non-empty string")
            key_envs = lit.get("key_envs")
            if not isinstance(key_envs, dict):
                raise WireError(f"{lit_where}.key_envs must be a JSON object")
            if not all(isinstance(slot, str) and isinstance(key_env, str)
                       for slot, key_env in key_envs.items()):
                raise WireError(f"{lit_where}.key_envs must map slot names to env-var names")
            normalized_literal.append({"agent": lit["agent"], "key_envs": dict(key_envs)})
        out.append({
            "name": entry["name"],
            "spec": entry["spec"],
            "requires": list(requires),
            "ref": list(ref),
            "literal": normalized_literal,
            "warn": list(warn),
            "local": list(local),
        })
    return out


def merge_plugin_entries(entries):
    """Merge the per-plugin mcp objects into one dict (insertion order, like
    jq `add`), hard-failing on a server name defined by more than one plugin
    or squatting on a reserved generated name."""
    merged = {}
    dups = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WireError("plugin_mcp_entries must be JSON objects")
        # Each value is a server spec; downstream (_claude_server /
        # _local_plugins) keys local-vs-remote off `"command" in spec`, which
        # would silently misclassify a non-dict (substring/membership match),
        # so reject it here — the one choke point both wiring paths pass through.
        for name, spec in entry.items():
            if not isinstance(spec, dict):
                raise WireError(f"plugin MCP server '{name}': spec must be a JSON object")
        dups.update(n for n in entry if n in merged)
        merged.update(entry)
    if dups:
        raise WireError(
            "multiple enabled plugins define the same MCP server name(s): "
            + ", ".join(sorted(dups))
        )
    return merged


def _claude_server(spec):
    """Render a plugin's mcp spec for Claude's .mcp.json. Local (stdio) servers
    pass through verbatim ({command, args}); remote servers gain the explicit
    `type: http` Claude expects, ahead of the file's {url, headers}."""
    if "command" in spec:
        return spec
    return {"type": "http", **spec}


def _render_named_env_ref_server(spec, requires, env_ref_field, server_name, binary):
    """Render a remote ref-style server for agents whose env_refs is a STRING
    destination field name (e.g. kimi's bearerTokenEnvVar).

    Contract (validated host-side in build_payload and re-checked defensively
    here): one required slot, and a header value containing ${SLOT}. The header
    carrying that bearer ref is removed and replaced with <env_ref_field>: SLOT.
    """
    if len(requires) != 1:
        raise WireError(
            f"agent server '{server_name}' cannot be rendered for agent '{binary}': "
            "string env_refs remote wiring requires exactly one required slot")
    slot = requires[0]
    headers = spec.get("headers")
    if not isinstance(headers, dict):
        raise WireError(
            f"agent server '{server_name}' cannot be rendered for agent '{binary}': "
            f"string env_refs remote wiring requires headers with a ${{{slot}}} bearer reference")

    marker = "${" + slot + "}"
    matches = [
        key for key, value in headers.items()
        if isinstance(value, str) and marker in value
    ]
    if not matches:
        raise WireError(
            f"agent server '{server_name}' cannot be rendered for agent '{binary}': "
            f"string env_refs remote wiring requires a headers entry containing {marker}")

    # Canonical shape uses Authorization; if multiple match, strip that one.
    header_to_strip = "Authorization" if "Authorization" in matches else matches[0]
    rendered = dict(spec)
    rendered_headers = dict(headers)
    del rendered_headers[header_to_strip]
    rendered["headers"] = rendered_headers
    rendered[env_ref_field] = slot
    return rendered


def _local_plugins(plugins):
    """The stdio (local) subset. Remote plugins are wired into Claude's
    .mcp.json only (Phase 1): cursor/pi can't expand ${VAR} refs in remote
    headers, and their env-scoped service tokens were never wired there before
    — so restricting the other agents to local plugins keeps every config
    byte-identical to the pre-plugin capabilities era."""
    return {n: s for n, s in plugins.items() if "command" in s}


def _load_servers(path):
    """Load an agent config as (data, servers-dict), or (None, None) when the
    file is missing or ZERO-BYTE — the caller then takes the create path. The
    zero-byte case is load-bearing: the old `jq` pipeline exited 0 with empty
    output on empty input, which blanked the config; this helper is the single
    home of the fix for both the identity and plugin merge paths."""
    if not (path.is_file() and path.stat().st_size > 0):
        return None, None
    data = _load_json_file(path)  # hand-broken JSON must abort loudly
    if not isinstance(data, dict):
        raise WireError(f"{path}: expected a JSON object at the top level")
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
        data["mcpServers"] = servers
    if not isinstance(servers, dict):
        raise WireError(f"{path}: .mcpServers is not an object")
    return data, servers


def generate_claude_mcp(workspace, claude_servers, plugins):
    """Regenerate <workspace>/repos/.mcp.json from the claude-bound agent servers
    + plugins — unless the workspace ships its own (no marker file), or repos/
    does not exist yet. claude_servers is {name: spec} for agent-scoped servers
    this container binds to claude (obsidian etc.), rendered ahead of the
    ordinary plugins. Claude expands the ${VAR} header refs at launch via the
    shims, so this file carries no literal secrets."""
    repos_dir = workspace / "repos"
    mcp_path = repos_dir / ".mcp.json"
    marker = workspace / ".mcp.generated"

    # Gate on repos/ existing: the canonical file lives in this container-owned
    # dir (not inside a clone target), so the failed-clone concern is gone —
    # clones land in repos/<name>/; a file in repos/ itself cannot break a
    # clone retry.
    if not repos_dir.is_dir():
        print(f"  (skipping .mcp.json — {repos_dir} does not exist yet; rerun up.sh)")
        return
    if mcp_path.is_file() and not marker.is_file():
        print("  (workspace ships its own .mcp.json — leaving it alone; manifest plugins are NOT merged into it)")
        return

    # Agent-scoped servers bound to claude first (ref form), then ordinary
    # plugins. The two sets are disjoint by construction (manifest.py routes
    # agent-scoped plugins away from plugin_mcp_entries).
    servers = dict(claude_servers)
    for name, spec in plugins.items():
        servers[name] = _claude_server(spec)

    _write_atomic(mcp_path, _dump_json({"mcpServers": servers}))
    marker.touch()
    print("  ✓ .mcp.json generated (" + ", ".join(sorted(servers)) + ")")


def link_repo_mcp(workspace):
    """Point each repos/<name>/.mcp.json at the workspace-level canonical file
    via a relative symlink. Claude Code only reads .mcp.json from its start
    directory, so every clone needs one; repos/.mcp.json is the single source
    (generated or hand-authored). A repo that ships its own regular file is
    left alone."""
    repos_dir = workspace / "repos"
    mcp_path = repos_dir / ".mcp.json"
    # Symlinks are correct whether the canonical file is generated or
    # hand-authored; absent entirely → nothing to point at.
    if not mcp_path.is_file():
        return
    for child in sorted(p for p in repos_dir.iterdir() if p.is_dir()):
        if not (child / ".git").is_dir():
            continue
        link = child / ".mcp.json"
        if link.is_symlink():
            if os.readlink(link) != "../.mcp.json":
                link.unlink()
                link.symlink_to("../.mcp.json")
        elif link.is_file():
            print(f"  (repo {child.name} ships its own .mcp.json — leaving it alone)")
        elif not link.exists():
            link.symlink_to("../.mcp.json")


def preapprove_claude(home, workspace):
    """Approval state lives in ~/.claude.json; since .mcp.json came from the
    manifest, its servers are approved by construction. Merge, don't clobber.

    Approval is per project path: repos/ itself (canonical .mcp.json) plus each
    cloned repos/<name>. .mcp.json may be workspace-shipped or per-repo rather
    than generated (supported opt-outs), so a shape we don't understand is a
    skip-with-warning for that dir, not an abort — the other dirs and agents'
    wiring must not die on a file we promised to leave alone. ~/.claude.json
    itself failing to parse IS an abort: merging into a corrupt state file can
    only destroy it."""
    repos_dir = workspace / "repos"
    if not repos_dir.is_dir():
        return

    project_dirs = [repos_dir]
    for child in sorted(p for p in repos_dir.iterdir() if p.is_dir()):
        if (child / ".git").is_dir():
            project_dirs.append(child)

    cj = home / ".claude.json"
    state = None
    projects = None
    approved = []

    for project_dir in project_dirs:
        mcp_path = project_dir / ".mcp.json"
        # Follow the symlink; for repos/ itself this is the canonical file.
        if not mcp_path.is_file():
            continue
        try:
            mcp = _load_json_file(mcp_path)
        except WireError as e:
            print(f"  ⚠ skipping claude pre-approval — {e}")
            continue
        if not isinstance(mcp, dict) or not isinstance(mcp.get("mcpServers"), dict):
            print(f"  ⚠ skipping claude pre-approval — {mcp_path} has no mcpServers object")
            continue
        servers = sorted(mcp["mcpServers"])

        if state is None:
            state = _load_json_file(cj) if cj.is_file() else {}
            if not isinstance(state, dict):
                raise WireError(f"{cj} is not a JSON object")
            projects = state.setdefault("projects", {})
            if not isinstance(projects, dict):
                raise WireError(f"{cj}: .projects is not an object")

        project = projects.setdefault(str(project_dir), {})
        if not isinstance(project, dict):
            raise WireError(f"{cj}: .projects[…] is not an object")
        project["enabledMcpjsonServers"] = servers
        project["hasTrustDialogAccepted"] = True
        approved.extend(servers)

    if state is None:
        return

    # Write THROUGH rather than tmp+rename: the file predates this module and
    # may be a symlink (dotfiles) — a rename would swap in a detached regular
    # file. Same semantics as the old `cat /tmp/cj.json > ~/.claude.json`,
    # inode, mode, and link target all preserved.
    with open(cj, "w", encoding="utf-8") as f:
        f.write(_dump_json(state))
    print("  ✓ MCP servers pre-approved for claude (" + ", ".join(sorted(set(approved))) + ")")


def _merge_named_entry(path, name, entry):
    """Set mcpServers[name] in an existing config (preserving plugin and
    hand-added servers) or create the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data, servers = _load_servers(path)
    if data is None:
        data = {"mcpServers": {name: entry}}
    else:
        servers[name] = entry
    _write_atomic(path, _dump_json(data), mode=0o600)


def _literal_agent_config(dialect, spec, keys):
    """Render a required remote server for a literal-key dialect. Replace every
    ${SLOT} header reference from the effective per-agent key map, then shape
    by descriptor dialect (url/httpUrl/type-http/serverUrl)."""
    def replace(value):
        if not isinstance(value, str):
            return value
        for slot, key in keys.items():
            value = value.replace("${" + slot + "}", key)
        return value

    headers = {k: replace(v)
               for k, v in (spec.get("headers") or {}).items()}
    if dialect == "httpUrl":
        return {"httpUrl": spec.get("url"), "headers": headers}
    if dialect == "type-http":
        return {"type": "http", "url": spec.get("url"), "headers": headers}
    if dialect == "url":
        return {"url": spec.get("url"), "headers": headers}
    if dialect == "serverUrl":
        return {"serverUrl": spec.get("url"), "headers": headers}
    raise WireError(f"literal MCP wiring requires dialect one of: {', '.join(sorted(LITERAL_DIALECTS))}")


def write_agent_server(binary, config_path, name, entry, home):
    """Wire one required agent-scoped server into this agent's JSON config."""
    path = _home_config_path(home, binary, config_path)
    _merge_named_entry(path, name, entry)
    print(f"  ✓ {binary} MCP config for {name}{_agent_note_suffix(binary)}")


def warn_agent_server(binary, config_path, name, slots):
    """A managed-block agent with remote-MCP pending support warns only."""
    if isinstance(slots, str):
        slots = [slots]
    print(f"  ⚠ {binary} agent-scoped server '{name}' not yet wired into ~/{config_path} "
          "(pending verification of this agent's remote-MCP config format). The key(s) "
          f"{', '.join(slots)} are available to {binary} processes via its shim.")


def _delete_named_servers(path, names):
    """Remove mcpServers[name] for each name from an existing config, leaving
    every other (hand-added, plugin, still-required) entry untouched."""
    data, servers = _load_servers(path)
    if data is None:
        return
    removed = [n for n in names if servers.pop(n, None) is not None]
    if removed:
        _write_atomic(path, _dump_json(data), mode=0o600)


def reconcile_agent_servers(binary, config_path, required, home):
    """Wire this agent's required agent-scoped servers AND delete any a prior
    run wired that this run no longer requires. The exact set this module
    manages is tracked in a <config>.djinn-servers sidecar so hand-added and
    plugin-managed entries are never touched. `required` maps server name ->
    final rendered entry for this agent."""
    # Known behavior: this path may rewrite one config up to K+2 times per run
    # (stale delete + one write per required server + plugin sync). If K grows,
    # a single-write merge pass is the follow-up optimization.
    path = _home_config_path(home, binary, config_path)
    sidecar = path.parent / (path.name + ".djinn-servers")
    old = []
    if sidecar.is_file() and sidecar.stat().st_size > 0:
        old = _load_json_file(sidecar)
        if not isinstance(old, list):
            raise WireError(f"{sidecar}: expected a JSON array of names")
    stale = [n for n in old if n not in required]
    if not required and not stale:
        return  # nothing managed now or before — don't create empty files
    if stale:
        _delete_named_servers(path, stale)
        print(f"  ✓ {binary}: removed {len(stale)} stale MCP server(s) no longer required "
              f"({', '.join(sorted(stale))})")
    for name, entry in required.items():
        write_agent_server(binary, config_path, name, entry, home)
    _write_atomic(sidecar, json.dumps(sorted(required), separators=(",", ":")) + "\n", mode=0o600)


def wire_plugin_servers_json(path, plugins):
    """Sync the plugin stdio servers into a JSON agent config (cursor,
    pi). The set of plugin-managed names is tracked in a sidecar
    (<file>.djinn-plugins) so stale entries from a plugin removed from the
    manifest are deleted without touching identity or hand-added servers."""
    sidecar = path.parent / (path.name + ".djinn-plugins")
    old = []
    if sidecar.is_file() and sidecar.stat().st_size > 0:
        old = _load_json_file(sidecar)
        if not isinstance(old, list):
            raise WireError(f"{sidecar}: expected a JSON array of names")

    data, servers = _load_servers(path)
    if data is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"mcpServers": dict(plugins)}
    else:
        merged = {k: v for k, v in servers.items() if k not in old}
        merged.update(plugins)
        data["mcpServers"] = merged

    _write_atomic(path, _dump_json(data), mode=0o600)
    _write_atomic(sidecar, json.dumps(sorted(plugins), separators=(",", ":")) + "\n", mode=0o600)
    print(f"  ✓ plugin MCP servers synced into {path}")


def _codex_block_body(plugins):
    """Render the [mcp_servers.*] tables. Server names were validated to
    [A-Za-z0-9_-] host-side (safe as bare TOML keys); command/args are emitted
    as JSON, whose string and array escapes are a TOML subset."""
    tables = []
    for name, spec in plugins.items():
        command = json.dumps(spec.get("command"), ensure_ascii=False)
        args = json.dumps(spec.get("args") or [], ensure_ascii=False, separators=(",", ":"))
        tables.append(f"[mcp_servers.{name}]\ncommand = {command}\nargs = {args}")
    return "\n\n".join(tables) + "\n"


def _codex_settings_body(settings):
    """Render config_settings as bare top-level TOML keys, sorted for
    determinism. Strings go through json.dumps (whose escapes are a TOML
    subset); booleans are lowercased; ints render bare."""
    lines = []
    for key in sorted(settings):
        value = settings[key]
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = json.dumps(value, ensure_ascii=False)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"


def _strip_marked_block(path, lines, open_prefix, close_prefix):
    """Drop one prefix-marked managed block from `lines`, returning the rest.

    An opener with no closer hard-fails rather than letting the strip eat the
    remainder of the file (the old sed silently deleted to EOF)."""
    kept = []
    in_block = False
    for line in lines:
        if not in_block and line.startswith(open_prefix):
            in_block = True
            continue
        if in_block:
            if line.startswith(close_prefix):
                in_block = False
            continue
        kept.append(line)
    if in_block:
        # Stricter than the old grep guard on purpose: a block still open at
        # EOF is caught even when an EARLIER block closed properly (stray
        # second opener, or a closer that sits above its opener).
        raise WireError(
            f"{path} has an opening '{open_prefix}' marker but no closing one "
            "— repair the markers (the strip would delete everything below them)"
        )
    return kept


def wire_codex_toml(path, plugins, settings=None):
    """Sync codex's config.toml: descriptor `settings` into a managed block at
    the HEAD of the file, plugin servers into a managed block at the TAIL.
    Both are stripped and re-rendered each run; hand edits outside the markers
    (and between the blocks) survive. An opening marker without its closer
    hard-fails rather than letting the strip eat the rest of the file.

    The two ends are not stylistic: bare top-level TOML keys are only
    top-level while no table has opened above them, and [mcp_servers.*] tables
    would claim any bare key written below them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    # newline="" disables universal-newline translation on read, and the
    # split is on \n ONLY (like the old sed) — str.splitlines() would also
    # split on \r/\f/U+2028…, silently rewriting a CRLF or exotic-char
    # config. A trailing newline yields one empty tail element — drop it so
    # the rejoin below is the single place that decides newline termination.
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        raw = f.read()
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    kept = _strip_marked_block(path, lines, CODEX_OPEN_PREFIX, CODEX_CLOSE_PREFIX)
    kept = _strip_marked_block(
        path, kept, CODEX_SETTINGS_OPEN_PREFIX, CODEX_SETTINGS_CLOSE_PREFIX)
    stripped = "\n".join(kept) + "\n" if kept else ""

    head = ""
    if settings:
        head = (
            CODEX_SETTINGS_OPEN_MARKER + "\n"
            + _codex_settings_body(settings)
            + CODEX_SETTINGS_CLOSE_MARKER + "\n"
        )
    tail = ""
    if plugins:
        tail = (
            CODEX_OPEN_MARKER + "\n"
            + _codex_block_body(plugins)
            + CODEX_CLOSE_MARKER + "\n"
        )
    content = head + stripped + tail
    _write_atomic(path, content, mode=0o600, errors="surrogateescape")
    if settings:
        print(f"  ✓ agent settings + plugin MCP servers synced into {path} (managed blocks)")
    else:
        print(f"  ✓ plugin MCP servers synced into {path} (managed block)")


def run(payload, home, workspace, env):
    if not isinstance(payload, dict):
        raise WireError("payload must be a JSON object")
    agents = _parse_agents_payload(payload.get("agents"))
    plugins = merge_plugin_entries(payload.get("plugin_mcp_entries") or [])
    agent_servers = _parse_agent_servers_payload(payload.get("agent_servers"))

    agents_by_binary = {agent["binary"]: agent for agent in agents}

    # Resolve agent-scoped servers to per-agent final entries up front so each
    # strategy loop can stay descriptor-driven and side-effect free.
    ref_required_by_agent = {}      # binary -> {name: rendered entry}
    literal_required_by_agent = {}  # binary -> {name: rendered entry}
    for s in agent_servers:
        for binary in s["ref"]:
            agent = agents_by_binary.get(binary)
            if agent is None:
                continue
            env_refs = agent["env_refs"]
            if isinstance(env_refs, str) and "command" not in s["spec"]:
                rendered = _render_named_env_ref_server(
                    s["spec"], s["requires"], env_refs, s["name"], binary
                )
            else:
                rendered = _claude_server(s["spec"])
            ref_required_by_agent.setdefault(binary, {})[s["name"]] = rendered
        for lit in s["literal"]:
            binary = lit["agent"]
            agent = agents_by_binary.get(binary)
            if agent is None:
                continue
            keys = {slot: env.get(key_env, "")
                    for slot, key_env in lit["key_envs"].items()}
            literal_required_by_agent.setdefault(binary, {})[s["name"]] = _literal_agent_config(
                agent["dialect"], s["spec"], keys
            )

    # Runs for every installed agent even with no plugins enabled, so entries
    # from a plugin removed from the manifest are cleaned up, not orphaned
    # (Claude gets this for free from wholesale .mcp.json regeneration). Uniform
    # LOCAL plugins go to every agent; an agent-scoped LOCAL server (e.g.
    # mcp-remote) is added only for the agents bound to it (its token gates who
    # sees it) — the token is delivered separately into each bound agent's env.
    local = _local_plugins(plugins)

    def local_for(binary):
        d = dict(local)
        for s in agent_servers:
            if binary in s["local"]:
                d[s["name"]] = s["spec"]
        return d

    for agent in agents:
        binary = agent["binary"]
        config_path = agent["config_path"]
        strategy = agent["strategy"]
        fmt = agent["format"]
        dialect = agent["dialect"]
        env_refs = agent["env_refs"]

        if strategy == "claude_preapprove":
            if config_path != ".mcp.json":
                raise WireError(
                    f"agent '{binary}' strategy claude_preapprove requires config_path '.mcp.json'")
            generate_claude_mcp(workspace, ref_required_by_agent.get(binary, {}), plugins)
            link_repo_mcp(workspace)
            preapprove_claude(home, workspace)
            continue

        path = _home_config_path(home, binary, config_path)
        if strategy == "codex_managed_block":
            for s in agent_servers:
                if binary in s["warn"]:
                    warn_agent_server(binary, config_path, s["name"], s["requires"])
            wire_codex_toml(path, local_for(binary), agent["settings"])
            continue

        if fmt != "json":
            raise WireError(
                f"agent '{binary}' has unsupported non-strategy MCP format '{fmt}'")

        literal_dialect = (not env_refs and dialect in LITERAL_DIALECTS)
        if not literal_dialect and dialect != "mcpServers":
            raise WireError(
                f"agent '{binary}' has unsupported MCP descriptor combination "
                f"(format={fmt!r}, dialect={dialect!r}, env_refs={env_refs!r}, strategy={strategy!r})")

        required = (literal_required_by_agent.get(binary, {})
                    if literal_dialect else
                    ref_required_by_agent.get(binary, {}))
        reconcile_agent_servers(binary, config_path, required, home)
        wire_plugin_servers_json(path, local_for(binary))
        if binary in _AGENT_SYNC_NOTES:
            print(f"    ({_AGENT_SYNC_NOTES[binary]})")
        continue


def build_payload(env):
    """Host side: assemble the payload from env vars set by up.sh.

    AGENTS_MCP_JSON is the manifest-derived descriptor list for ENABLED
    mcp-capable agents. The order of this list is the authoritative role order
    for agent-scoped servers.

    PLUGIN_MCP_ENTRIES is the newline-separated one-line-JSON-per-plugin
    accumulation from manifest.py (local + env-scoped remote plugins only).

    Required servers are assembled from three inputs:
      AGENT_SERVERS_JSON — {name: {"spec": ..., "requires": [SLOT, ...]}}
      AGENT_SECRETS      — resolved "agent<TAB>slot<TAB>source" records
      IDENTITY_SECRETS   — "agent:key_env:slot" records for literal-key agents;
                           the docker-exec environment supplies those values.
    A server is present only where all of its required slots resolve. Role
    routing is descriptor-driven: bool-ref env_refs, named-ref env_refs,
    managed-block strategy, literal dialects, and local command servers.
    """
    try:
        raw_agents = json.loads(env.get("AGENTS_MCP_JSON") or "[]")
    except ValueError as e:
        raise WireError(f"AGENTS_MCP_JSON is not valid JSON ({e})")
    agents = _parse_agents_payload(raw_agents)

    entries = []
    for line in (env.get("PLUGIN_MCP_ENTRIES") or "").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as e:
            raise WireError(f"plugin mcp extraction produced invalid JSON ({e}): {line}")
        if not isinstance(entry, dict):
            raise WireError(f"plugin mcp extraction line is not a JSON object: {line}")
        entries.append(entry)

    try:
        servers_by_slot = json.loads(env.get("AGENT_SERVERS_JSON") or "{}")
    except ValueError as e:
        raise WireError(f"AGENT_SERVERS_JSON is not valid JSON ({e})")

    if not isinstance(servers_by_slot, dict):
        raise WireError("AGENT_SERVERS_JSON must be a JSON object")

    effective = set()
    for line in (env.get("AGENT_SECRETS") or "").splitlines():
        agent, sep, rest = line.partition("\t")
        slot, sep2, source = rest.partition("\t")
        if not (agent and sep and slot and sep2 and source):
            raise WireError(f"AGENT_SECRETS has an invalid record: {line!r}")
        effective.add((agent, slot))

    key_envs = {}
    for triple in (env.get("IDENTITY_SECRETS") or "").split():
        parts = triple.split(":")
        agent = parts[0]
        key_env = parts[1] if len(parts) > 1 else ""
        slot = parts[2] if len(parts) > 2 else ""
        if not (agent and key_env and slot):
            raise WireError(f"IDENTITY_SECRETS has an invalid record: {triple!r}")
        key_envs[(agent, slot)] = key_env

    agents_by_binary = {agent["binary"]: agent for agent in agents}
    agent_order = [agent["binary"] for agent in agents]

    agent_servers = []
    for name, sd in servers_by_slot.items():
        if not isinstance(sd, dict) or not isinstance(sd.get("spec"), dict):
            raise WireError(f"agent server '{name}' must define an object spec")
        requires = sd.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(slot, str) for slot in requires):
            raise WireError(f"agent server '{name}' requires must be a list of slots")
        e = {"name": name, "spec": sd["spec"], "requires": requires,
             "ref": [], "literal": [], "warn": [], "local": []}
        is_local = "command" in sd["spec"]
        for binary in agent_order:
            if not all((binary, slot) in effective for slot in requires):
                continue
            agent = agents_by_binary[binary]
            if agent["env_refs"]:
                if isinstance(agent["env_refs"], str) and not is_local:
                    # Closed contract: a named env_refs field (e.g.
                    # bearerTokenEnvVar) is only legal for the single-slot
                    # bearer-header remote shape. Validate HOST-side so
                    # unsupported descriptors fail before container wiring.
                    _render_named_env_ref_server(
                        sd["spec"], requires, agent["env_refs"], name, binary
                    )
                e["ref"].append(binary)
                continue
            if is_local:
                e["local"].append(binary)
                continue
            if agent["strategy"] == "codex_managed_block":
                e["warn"].append(binary)
                continue
            if agent["dialect"] in LITERAL_DIALECTS:
                slots = {slot: key_envs.get((binary, slot), "") for slot in requires}
                missing = [slot for slot, key_env in slots.items() if not key_env]
                if missing:
                    raise WireError(
                        f"agent server '{name}' is missing literal key env for {binary}: {', '.join(missing)}")
                e["literal"].append({"agent": binary, "key_envs": slots})
                continue
            raise WireError(
                f"agent server '{name}' cannot be rendered for agent '{binary}' "
                "(unsupported remote MCP dialect/strategy combination)")
        if e["ref"] or e["literal"] or e["warn"] or e["local"]:
            agent_servers.append(e)

    return {
        "agents": agents,
        "plugin_mcp_entries": entries,
        "agent_servers": agent_servers,
    }


def _home():
    """The wiring targets the exec user's REAL home (passwd), like the old
    hardcoded /home/coder paths — not $HOME, which a future `ENV HOME=…` in
    the image would leak into `docker exec -u coder`."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path(os.environ.get("HOME", "/home/coder"))


def main(argv):
    if "--build-payload" in argv:
        try:
            print(json.dumps(build_payload(os.environ)))
        except WireError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        return 0

    try:
        payload = json.load(sys.stdin)
    except ValueError as e:
        print(f"Error: invalid JSON payload on stdin: {e}")
        return 1
    try:
        run(payload, _home(), Path("/workspace"), os.environ)
    except WireError as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
