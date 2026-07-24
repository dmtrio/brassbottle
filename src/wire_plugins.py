#!/usr/bin/env python3
"""Agent-config wiring for up.sh — both sides of one docker exec.

up.sh runs this file twice per `up`, so the payload schema lives in exactly
one place and both halves are unit-testable:

  host side:      python3 src/wire_plugins.py --build-payload
                  reads env vars (see build_payload) and prints the JSON
                  payload; booleans use the same strict string comparison the
                  old bash used ([ "$X" = "true" ]), so a manifest value like
                  `gateway: yes` stays OFF instead of leaking into the JSON.
  container side: python3 /usr/local/lib/dev-agent/wire_plugins.py
                  reads that payload on stdin and wires MCP servers into every
                  installed agent's config files — the work that used to live
                  in up.sh as jq/sed programs inside triple-quoted
                  `docker exec bash -c` strings.

Payload:

    {
      "wire":         {"cursor": bool, "gemini": bool, "pi": bool, "codex": bool},
      "plugin_mcp_entries": [{"<name>": <local or env-scoped-remote spec>}, ...],
      "agent_servers": [
        {"name": "obsidian-annotated", "slot": "OBSIDIAN_ANNOTATED_KEY",
         "spec": {"url": ..., "headers": {...${SLOT}...}},
         "claude": bool,                                   # → .mcp.json ref
         "literal": [{"agent": "cursor-agent", "key_env": "IDENTITY_KEY_0"}],
         "warn":    ["codex"],                             # REMOTE spec only
         "local":   ["cursor-agent", "codex", ...]}        # LOCAL spec only
      ]
    }

- plugin_mcp_entries carries one object per NON-agent-scoped plugin (host-side
  manifest.py extracts them from plugins/<name>.yml). A spec is LOCAL
  ({command, args} — stdio, wired into every agent) or env-scoped REMOTE
  ({url, headers} — http, wired into Claude's .mcp.json only). Cross-plugin
  duplicate server names hard-fail here as well as host-side (last-wins merge).
- agent_servers carries the AGENT-SCOPED plugins, wired only for the agents
  bound to the slot (its key gates who sees the server). A REMOTE spec (obsidian)
  presents each bound agent's own key: claude gets the ${SLOT} ref in .mcp.json
  (shim expands it), cursor/gemini/pi get the literal key baked in (`literal`),
  codex gets a warning (`warn`). A LOCAL spec (axiom's mcp-remote stdio bridge)
  wires the same command into every bound agent's config (`local`, codex
  included — its toml supports command servers); the token rides in the agent's
  env, so nothing is baked into the config. Env-only agent-scoped slots (watch
  keys — no server) never reach the payload; up.sh delivers them straight into
  the agent's env file.
- Keys never ride in the payload: each literal[] element names an environment
  variable (set on the docker exec) that holds the key, so the payload is
  secret-free. Only cursor-agent/gemini/pi literal keys are shipped at all —
  claude's rides in its shim env, codex's is pending (warning only).
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
import sys
from pathlib import Path

# No server names are reserved any more: as of Plugins v2 Phase 2, EVERY MCP
# server up.sh wires comes from a plugin file (obsidian-annotated is now
# plugins/obsidian-annotated/plugin.yml, an agent-scoped remote plugin). Cross-plugin
# name collisions are caught by the generic duplicate check below and host-side.

# The codex managed block. Detection matches on the PREFIX (like the old sed
# ranges did), so a stale block written by an older up.sh with different
# trailing text is still stripped.
CODEX_OPEN_PREFIX = "# >>> dev-agent plugin MCP"
CODEX_CLOSE_PREFIX = "# <<< dev-agent plugin MCP"
CODEX_OPEN_MARKER = "# >>> dev-agent plugin MCP (managed by up.sh; edits inside are overwritten) >>>"
CODEX_CLOSE_MARKER = "# <<< dev-agent plugin MCP <<<"


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


def _local_plugins(plugins):
    """The stdio (local) subset. Remote plugins are wired into Claude's
    .mcp.json only (Phase 1): cursor/gemini can't expand ${VAR} refs in remote
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


def _literal_agent_config(agent, spec, keys):
    """Render a required remote server for a literal-key agent. Replace every
    ${SLOT} header reference from the effective per-agent key map, then shape
    per agent (gemini wants httpUrl; pi wants type: http; cursor takes url)."""
    def replace(value):
        if not isinstance(value, str):
            return value
        for slot, key in keys.items():
            value = value.replace("${" + slot + "}", key)
        return value

    headers = {k: replace(v)
               for k, v in (spec.get("headers") or {}).items()}
    if agent == "gemini":
        return {"httpUrl": spec.get("url"), "headers": headers}
    if agent == "pi":
        return {"type": "http", "url": spec.get("url"), "headers": headers}
    return {"url": spec.get("url"), "headers": headers}  # cursor-agent


def write_agent_server(agent, name, spec, keys, *rest):
    """Wire one required remote server into a LITERAL-key agent's config.
    Cursor and Gemini cannot reliably expand env vars in remote headers, so
    their configs carry effective literal keys: home files, mode 600, never
    inside the repo, regenerated from secrets.env on every up (rotation)."""
    # Accept the former (slot, key, home) form during the schema migration so
    # older callers/tests continue to render the same literal remote entry.
    if len(rest) == 1:
        home = rest[0]
    elif len(rest) == 2 and isinstance(keys, str):
        keys = {keys: rest[0]}
        home = rest[1]
    else:
        raise TypeError("write_agent_server expects (keys, home) or (slot, key, home)")
    entry = _literal_agent_config(agent, spec, keys)
    if agent == "cursor-agent":
        _merge_named_entry(home / ".cursor" / "mcp.json", name, entry)
        print(f"  ✓ cursor-agent MCP config for {name} (literal key: env interpolation broken for remote headers)")
    elif agent == "gemini":
        _merge_named_entry(home / ".gemini" / "settings.json", name, entry)
        print(f"  ✓ gemini MCP config for {name} (literal key: header env expansion is an open FR)")
    elif agent == "pi":
        # Merge (not wholesale write) so multiple agent servers coexist; plugin
        # entries are re-merged right after by wire_plugin_servers_json.
        _merge_named_entry(home / ".pi" / "agent" / "mcp.json", name, entry)
        print(f"  ✓ pi MCP config for {name} (NOTE: inert until pi-mcp-adapter extension is installed — pi has no built-in MCP)")


def warn_agent_server(agent, name, slots):
    """codex's remote-MCP config format is still pending, so a required remote
    server is not wired — but its resolved key slots stay in codex's shim env."""
    if agent == "codex":
        if isinstance(slots, str):
            slots = [slots]
        print(f"  ⚠ codex agent-scoped server '{name}' not yet wired into ~/.codex/config.toml "
              "(pending verification of codex's remote-MCP config format). The key(s) "
              f"{', '.join(slots)} are available to codex processes via its shim.")


def wire_plugin_servers_json(path, plugins):
    """Sync the plugin stdio servers into a JSON agent config (cursor, gemini,
    pi). The set of plugin-managed names is tracked in a sidecar
    (<file>.dev-agent-plugins) so stale entries from a plugin removed from the
    manifest are deleted without touching identity or hand-added servers."""
    sidecar = path.parent / (path.name + ".dev-agent-plugins")
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


def wire_codex_toml(path, plugins):
    """Sync the plugin servers into codex's config.toml as a managed marker
    block, stripped and re-appended each run; hand edits outside the markers
    survive. An opening marker without its closer hard-fails rather than
    letting the strip eat the rest of the file."""
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

    kept = []
    in_block = False
    for line in lines:
        if not in_block and line.startswith(CODEX_OPEN_PREFIX):
            in_block = True
            continue
        if in_block:
            if line.startswith(CODEX_CLOSE_PREFIX):
                in_block = False
            continue
        kept.append(line)
    if in_block:
        # Stricter than the old grep guard on purpose: a block still open at
        # EOF is caught even when an EARLIER block closed properly (stray
        # second opener, or a closer that sits above its opener) — the old
        # sed silently deleted from the stray opener to EOF in those cases.
        raise WireError(
            f"{path} has an opening dev-agent plugin marker but no closing one "
            "— repair the markers (the strip would delete everything below them)"
        )
    stripped = "\n".join(kept) + "\n" if kept else ""

    if plugins:
        content = (
            stripped
            + CODEX_OPEN_MARKER + "\n"
            + _codex_block_body(plugins)
            + CODEX_CLOSE_MARKER + "\n"
        )
    else:
        content = stripped
    _write_atomic(path, content, mode=0o600, errors="surrogateescape")
    print(f"  ✓ plugin MCP servers synced into {path} (managed block)")


def run(payload, home, workspace, env):
    if not isinstance(payload, dict):
        raise WireError("payload must be a JSON object")
    plugins = merge_plugin_entries(payload.get("plugin_mcp_entries") or [])
    agent_servers = payload.get("agent_servers") or []
    wire = payload.get("wire") or {}

    # Claude's .mcp.json: agent-scoped servers bound to claude (ref form, key
    # expands from claude's shim env) ahead of the ordinary plugins.
    claude_servers = {s["name"]: _claude_server(s["spec"])
                      for s in agent_servers if s.get("claude")}
    generate_claude_mcp(workspace, claude_servers, plugins)
    link_repo_mcp(workspace)
    preapprove_claude(home, workspace)

    # Per-agent wiring of required servers (literal keys for cursor/gemini/pi,
    # a warning for codex). Runs BEFORE the plugin merge so
    # wire_plugin_servers_json preserves these non-sidecar entries.
    for s in agent_servers:
        for lit in s.get("literal") or []:
            if "key_envs" in lit:
                keys = {slot: env.get(key_env, "")
                        for slot, key_env in (lit.get("key_envs") or {}).items()}
            else:
                # Compatibility with payloads emitted before multi-slot
                # requirements existed.
                keys = {s.get("slot", ""): env.get(lit.get("key_env") or "", "")}
            write_agent_server(lit.get("agent") or "", s["name"], s["spec"], keys, home)
        for agent in s.get("warn") or []:
            warn_agent_server(agent, s["name"], s.get("requires") or [])

    # Runs for every installed agent even with no plugins enabled, so entries
    # from a plugin removed from the manifest are cleaned up, not orphaned
    # (Claude gets this for free from wholesale .mcp.json regeneration). Uniform
    # LOCAL plugins go to every agent; an agent-scoped LOCAL server (axiom's
    # mcp-remote) is added only for the agents bound to it (its token gates who
    # sees it) — the token is delivered separately into each bound agent's env.
    # Env-scoped remote plugins still live in Claude's .mcp.json alone
    # (cursor/gemini can't expand ${VAR} in remote headers).
    local = _local_plugins(plugins)

    def local_for(agent_name):
        d = dict(local)
        for s in agent_servers:
            if agent_name in (s.get("local") or []):
                d[s["name"]] = s["spec"]
        return d

    if wire.get("cursor"):
        wire_plugin_servers_json(home / ".cursor" / "mcp.json", local_for("cursor-agent"))
    if wire.get("gemini"):
        wire_plugin_servers_json(home / ".gemini" / "settings.json", local_for("gemini"))
    if wire.get("pi"):
        wire_plugin_servers_json(home / ".pi" / "agent" / "mcp.json", local_for("pi"))
        print("    (pi: inert until the pi-mcp-adapter extension is installed)")
    if wire.get("codex"):
        wire_codex_toml(home / ".codex" / "config.toml", local_for("codex"))


def build_payload(env):
    """Host side: assemble the payload from env vars set by up.sh.

    Booleans arrive as the raw yq scalars (WIRE_CURSOR/…) and only the literal
    string "true" turns a flag on — the exact semantics of the old
    `[ "$X" = "true" ]` checks. PLUGIN_MCP_ENTRIES is the newline-separated
    one-line-JSON-per-plugin accumulation from manifest.py (local + env-scoped
    remote plugins only).

    Required servers are assembled from three inputs:
      AGENT_SERVERS_JSON — {name: {"spec": ..., "requires": [SLOT, ...]}}
      AGENT_SECRETS      — resolved "agent<TAB>slot<TAB>source" records
      IDENTITY_SECRETS   — "agent:key_env:slot" records for literal-key agents;
                           the docker-exec environment supplies those values.
    A server is present only where all of its required slots resolve. Claude
    keeps variable references, local servers use normal agent configs, remote
    cursor/gemini/pi entries get literal substitutions, and codex warns.
    """
    def flag(name):
        return env.get(name) == "true"

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

    agent_servers = []
    agent_order = ("claude", "codex", "pi", "gemini", "cursor-agent")
    for name, sd in servers_by_slot.items():
        if not isinstance(sd, dict) or not isinstance(sd.get("spec"), dict):
            raise WireError(f"agent server '{name}' must define an object spec")
        requires = sd.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(slot, str) for slot in requires):
            raise WireError(f"agent server '{name}' requires must be a list of slots")
        e = {"name": name, "spec": sd["spec"], "requires": requires,
             "claude": False, "literal": [], "warn": [], "local": []}
        is_local = "command" in sd["spec"]
        for agent in agent_order:
            if not all((agent, slot) in effective for slot in requires):
                continue
            if agent == "claude":
                e["claude"] = True
            elif is_local:
                e["local"].append(agent)
            elif agent == "codex":
                e["warn"].append(agent)
            else:
                slots = {slot: key_envs.get((agent, slot), "") for slot in requires}
                missing = [slot for slot, key_env in slots.items() if not key_env]
                if missing:
                    raise WireError(
                        f"agent server '{name}' is missing literal key env for {agent}: {', '.join(missing)}")
                e["literal"].append({"agent": agent, "key_envs": slots})
        if e["claude"] or e["literal"] or e["warn"] or e["local"]:
            agent_servers.append(e)

    return {
        "wire": {name: flag("WIRE_" + name.upper())
                 for name in ("cursor", "gemini", "pi", "codex")},
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
