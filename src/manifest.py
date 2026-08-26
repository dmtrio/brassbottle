#!/usr/bin/env python3
"""Host-side manifest reading + validation for up.sh (Phase 2 of the Python
extraction; wire_plugins.py was Phase 1).

up.sh feeds this file yq-converted JSON on stdin and evals the derived shell
assignments it prints:

    input (stdin):  line 1: the manifest as JSON (yq -o=json -I=0)
                    then one line per plugins/*/plugin.yml: "<name>\t<json>" — or
                    "<name>\t!" when yq could not parse that file (an error
                    only if the manifest actually lists the plugin; an
                    unlisted broken file must not block unrelated containers)
                    then a literal sentinel line: ---agents---
                    then one line per agents/*/agent.yml:
                    "<name>\t<json>" (agent docs are always required and
                    unreadable docs are always a hard error)
    input (env):    PRESENT_SECRET_VARS — space-separated names of non-empty
                                          secret sources (names only — secret
                                          VALUES never reach this process beyond
                                          NTFY_URL/NTFY_TOPIC below)
                    GIT_NAME_DEFAULT / GIT_EMAIL_DEFAULT — host git config
                                       fallbacks for manifests without git:
                    NTFY_URL / NTFY_TOPIC — from secrets.env, only consumed
                                       when the manifest asks for ntfy
                    SECRETS_FILE     — path, used verbatim in error messages
    output (stdout): one VAR=value line per derived variable, every value
                    shell-quoted (shlex.quote); up.sh does DERIVED=$(…) and
                    eval "$DERIVED". Errors go to stderr as "Error: …" with
                    exit 1 — the command substitution assignment then aborts
                    up.sh under set -e.

Behavioral fidelity notes (each is pinned by tests/test_manifest.py):
- yq/jq `//` treats false AND null as empty: `plugins: false` means "no
  plugins", `repos: false` means no repos, `agents: false` gets the default
  set. A manifest with `tools:` is rejected by name so old manifests fail
  loud instead of silently enabling defaults. The old scalar `repo:` key is
  rejected outright (layout v2).
- agents: matching is exact string equality (agents: [claude-code] no longer
  enables claude).
- null entries inside word-split lists (plugins, identity refs) vanish, the
  way the old `join(" ")` + word splitting dropped them — so a trailing
  flow-style comma (`plugins: [serena,]`) keeps working. Comma-joined lists
  (egress, egress_cidrs) keep their empty slots byte-for-byte.
- agent_for_ref suffix matching is descriptor-derived per mcp-capable agent,
  longest suffix first so _cursor_agent beats _claude.
- Error ordering matches the old top-to-bottom flow: forge → plugins list →
  ssh/remote → mosh ports → identity refs (aggregated) → per-plugin egress +
  mcp entries (fail-fast) → ntfy. Messages are byte-identical to the bash.
- Deliberate departures from the old bash, all loud-instead-of-silent: a
  section written as the wrong YAML type (capabilities:/identities:/… as a
  list) is a named error where yq used to emit a cryptic 'cannot index'
  abort — or, worse, where a sequence-root plugin file validated as empty;
  and a non-scalar leaf (memory: [2g]) is a named error instead of leaked
  YAML/repr garbage.
- Known cosmetic deviation: numeric scalars ride through yq's JSON encoder,
  so `memory: 2.50` derives as "2.5" (the old `yq -r` printed the original
  spelling). Quote the value in YAML if the exact spelling matters.
"""

import json
import os
import re
import shlex
import sys

# wire_plugins.py used to export the reserved-server-name set; as of Plugins v2
# Phase 2 every MCP server comes from a plugin file (obsidian-annotated included),
# so there are no reserved names and nothing is imported from it. Host ports and
# server defs are all DATA (plugins/*/plugin.yml), not tables in code.

# \Z, not $: Python's $ also matches just before a trailing newline, which
# would wave "evil.com\n" through into dnsmasq config (the old bash never saw
# trailing newlines — word splitting ate them).
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")
REF_RE = re.compile(r"^[A-Za-z0-9_]+\Z")
# Placeholder a plugin uses for its own host port — in a remote url, or in a
# local bridge's command/args when the bridge dials the host (rhinomcp) — so
# the port lives once in plugin.yml (host_port:) and a manifest plugin_ports:
# override re-points the dial target and the firewall grant together. Expanded
# host-side at derive time — cursor/pi can't expand ${VAR} refs in remote
# specs.
HOST_PORT_REF = "${HOST_PORT}"


def _host_port_ref_fields(spec):
    """The string fields of an mcp server spec that may carry ${HOST_PORT}:
    a remote url, or a local bridge's command/args."""
    if not isinstance(spec, dict):
        return []
    args = spec.get("args") if isinstance(spec.get("args"), list) else []
    return [v for v in (spec.get("url"), spec.get("command"), *args)
            if isinstance(v, str)]
# ── Plugin-declared volumes (plugins/<name>/volumes:) ────────────────────────
# The static compose file's own named volumes and mount targets. A plugin
# reusing either name would be MERGED into the static definition by compose
# (last -f wins per key) and silently remount a real directory — the failure
# would look like lost auth or an empty workspace, not a config error. Both
# sets are pinned against compose/docker-compose.local.yml by
# tests/plugins.test.sh, so adding a volume there without updating these
# fails loudly instead of re-opening the hole.
STATIC_COMPOSE_VOLUME_NAMES = {
    "workspace", "gh-auth",
}
STATIC_COMPOSE_MOUNT_PATHS = {
    "/workspace", "/artifacts", "/artifacts/browser", "/agent-rules",
    "/home/coder/.agent-keys",
    "/home/coder/.config/gh",
}
# A container mountpoint, restricted to a deliberately boring charset. Not
# paranoia about a hostile plugin.yml (install: already runs arbitrary code at
# build) — the point is that this value survives three hops that each read it
# differently, and a permissive charset makes the validator advertise
# guarantees the pipeline does not keep:
#   • compose interpolates $VAR/${VAR} in EVERY -f file before parsing, so a
#     '$' lets the final mount target differ from the declared one (and can
#     pull in a value from the secrets.env up.sh has sourced);
#   • the entrypoint's word-split loop also glob-expands, so '*' '?' '['
#     chown a different path than the one that was mounted;
#   • ':' is compose's own mount-spec separator and whitespace breaks the
#     word split.
# Alphanumerics plus . _ - + @ covers every real cache/state directory.
VOLUME_PATH_SEGMENT = r"[A-Za-z0-9._@+-]+"
VOLUME_PATH_RE = re.compile(r"^(/%s)+\Z" % VOLUME_PATH_SEGMENT)
# Volume mountpoints live under the coder home. Everything in the container
# runs as coder, so that is where plugin state belongs — and confining it there
# rules out mounting over /usr/local/bin, /etc, or the workspace tree, none of
# which a volume can shadow without freezing or hiding real content.
VOLUME_ROOT = "/home/coder"
# Compose reads a 1-character source as a Windows drive letter, silently
# yielding a mount with a target of "v:/path" and NO source — `docker compose
# config` accepts it and only `up` fails, with a daemon error naming a path
# nobody wrote. Require two characters, opening on an alphanumeric.
VOLUME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]+\Z")
# Directory name under /workspace/repos/<name> — no slash, no leading dot/dash.
REPO_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
# services: keys (plugins/<name>/plugin.yml). Kebab-case only: a service name
# becomes a tmux session name (svc-<name>) and a log/script filename under
# /tmp/djinn-services/ inside the container (src/plugin_services.py) — a
# boring charset keeps it stable across the docker-exec/tmux/heredoc hops.
SERVICE_NAME_RE = re.compile(r"^[a-z0-9-]+\Z")
# Forge org/user name (git.orgs key). GitHub's own rule: alphanumerics and
# single hyphens, no leading/trailing hyphen. Only '-' is non-alphanumeric, so
# the GH_TOKEN_<owner> sanitization (below) is a bijection over valid owners —
# two distinct owners can never collide on one token var.
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\Z")
DOMAIN_RE = re.compile(
    r"^([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z][A-Za-z0-9-]{0,61}[A-Za-z0-9]\Z"
)
IPV4_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\Z")
MOSH_PORTS_RE = re.compile(r"^[0-9]{1,5}:[0-9]{1,5}\Z")

AGENT_TOP_LEVEL_KEYS = frozenset({
    "binary", "install", "state_dirs", "rules_file", "egress", "mcp",
    "config_settings",
})
# Scalar types renderable as TOML top-level keys by the managed-settings block.
AGENT_CONFIG_SETTING_TYPES = (str, bool, int)
AGENT_CONFIG_SETTING_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")
AGENT_MCP_KEYS = frozenset({
    "config_path", "format", "dialect", "env_refs", "strategy",
})
AGENT_MCP_FORMATS = frozenset({"json", "toml"})
# Literal-key dialects differ only in the key that carries the remote URL —
# each agent CLI accepts exactly one (agy rejects url/httpUrl by name, gemini
# rejected url). Kept in lockstep with wire_plugins.LITERAL_DIALECTS, which
# owns the rendering.
AGENT_MCP_LITERAL_DIALECTS = frozenset({"url", "httpUrl", "type-http", "serverUrl"})
AGENT_MCP_DIALECTS = AGENT_MCP_LITERAL_DIALECTS | frozenset({"mcpServers"})
AGENT_MCP_STRATEGIES = frozenset({"claude_preapprove", "codex_managed_block"})
# codex_managed_block's closed env_refs set. False renders no agent-scoped
# remote at all (warns instead); "bearer_token_env_var" is the ONE field name
# _codex_block_body actually renders (codex's own native remote-MCP shape —
# kept in lockstep with wire_plugins.CODEX_MANAGED_BLOCK_ENV_REFS). Any other
# string (a typo, or a field codex doesn't have) would render a broken config —
# either an overwritten field (env_refs: url) or a silently missing credential
# (env_refs: bearer_token_env, _codex_block_body never sees it) — so it is
# rejected here rather than discovered at wire time. True is rejected too: the
# managed TOML block has no ${VAR} header expansion, unlike claude's shim.
CODEX_MANAGED_BLOCK_ENV_REFS = frozenset({False, "bearer_token_env_var"})

# Marker for a plugins/*/plugin.yml that yq could not parse (see module docstring).
UNREADABLE = object()


class ManifestError(Exception):
    """Fatal validation error; main() prints 'Error: …' to stderr, exit 1."""


def _falsy(v):
    # jq/yq `//` alternative operator fires on null and false ONLY.
    return v is None or v is False


def _scalar(v, field, default=""):
    """Render like `yq -r '.x // ""'`: falsy → default, bool → true/false.
    A map/list leaf is a named error — the old yq spat multi-line garbage
    into the variable; refusing loudly is the whole point of this module."""
    if _falsy(v):
        return default
    if v is True:
        return "true"
    if isinstance(v, (dict, list)):
        raise ManifestError(f"manifest {field} must be a single value, not a map/list")
    return str(v)


def _raw_flag(v, field):
    """Render like `yq '.x // false'` (no -r): the raw scalar as yq prints
    it. Downstream only ever compares against the literal string 'true'."""
    if _falsy(v):
        return "false"
    if v is True:
        return "true"
    if isinstance(v, (dict, list)):
        raise ManifestError(f"manifest {field} must be a single value, not a map/list")
    return str(v)


def _section(manifest, key):
    """A top-level map section: absent/null → {}; any other non-map type is a
    named error (the old yq aborted with a cryptic 'cannot index' here — and
    silently-empty would be worse: a list-typo'd identities: must not bring
    the container up unauthenticated)."""
    v = manifest.get(key)
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ManifestError(f"manifest {key}: must be a map (got a {_yaml_type(v)})")
    return v


def _word_list(v, field):
    """A list the old bash consumed via join(" ") + word splitting: falsy →
    [], null entries vanish (they joined as empty words), scalars render like
    yq -r. Non-list values are named errors."""
    if _falsy(v):
        return []
    if not isinstance(v, list):
        raise ManifestError(f"manifest {field} must be a list")
    rendered = (_scalar(x, f"{field} entry") for x in v)
    return [r for r in rendered if r != ""]


def _comma_list(v, field):
    """A list the old bash consumed via join(",") with NO word splitting:
    empty slots from null entries survive byte-for-byte (they always have —
    downstream tolerates them, and inventing a cleanup here would change the
    emitted EGRESS string)."""
    if _falsy(v):
        return []
    if not isinstance(v, list):
        raise ManifestError(f"manifest {field} must be a list")
    return [_scalar(x, f"{field} entry") for x in v]


def _yaml_type(v):
    return {list: "list", str: "string", int: "number", float: "number",
            bool: "boolean"}.get(type(v), type(v).__name__)


def agent_for_ref(ref, agent_suffixes):
    for suffix, agent in agent_suffixes:
        if ref.endswith(suffix):
            return agent
    return ""


def _path_overlaps(a, b):
    """True when two absolute paths are the same or one contains the other.

    Component-wise, never str.startswith: '/home/coder/.cursor' is not inside
    '/home/coder/.curse', but a prefix test would say it is — and the reverse
    mistake (missing a real nesting) is what lets a volume shadow a live tree.
    """
    pa, pb = a.split("/"), b.split("/")
    return pa[:len(pb)] == pb or pb[:len(pa)] == pa


def _compose_overlay(volumes, comment_lines, env_lines=()):
    """Shared renderer for the generated per-container compose overlays, or ""
    when `volumes` is empty (up.sh then adds no -f at all).

    `volumes` is {volume name: container path}. Emitted sorted by name so the
    file is a function of WHICH plugins/agents are enabled, not of their order
    in the manifest — a reordered list must not rewrite the overlay. One
    renderer for both overlays so a format fix (escaping, indentation, header)
    cannot silently diverge between them.
    """
    if not volumes:
        return ""
    names = sorted(volumes)
    lines = ["# GENERATED by src/manifest.py — do not edit; ./up.sh rewrites it."]
    lines += list(comment_lines)
    lines += ["services:", "  djinn:"]
    if env_lines:
        lines.append("    environment:")
        lines += list(env_lines)
    lines.append("    volumes:")
    lines += [f"      - {n}:{volumes[n]}" for n in names]
    lines.append("volumes:")
    lines += [f"  {n}:" for n in names]
    return "\n".join(lines)


def plugin_compose_overlay(volumes):
    """The overlay for plugin-declared volumes (plugins/<name>/volumes:)."""
    paths = " ".join(volumes[n] for n in sorted(volumes))
    return _compose_overlay(
        volumes,
        comment_lines=[
            "# Named volumes declared by the plugins THIS container enables",
            "# (plugins/<name>/volumes:). Compose prefixes each name with the project,",
            "# so `foo` is really `djinn-<container>_foo` — per container, like the",
            "# auth volumes, and removed by `./djinn down <container> --purge`.",
        ],
        env_lines=[
            "      # Mountpoints the entrypoint chowns to coder before any agent runs.",
            "      # Docker seeds a fresh named volume from the image directory it covers,",
            "      # ownership included, so a path that does not exist in the image mounts",
            "      # root-owned and the coder-run agent cannot write to it.",
            f"      - PLUGIN_VOLUME_PATHS={paths}",
        ],
    )


def agent_compose_overlay(volumes):
    """The overlay for enabled agents' auth/state dirs (agents/<n>/state_dirs:).
    No PLUGIN_VOLUME_PATHS-style chown env: agent state dirs are pre-created in
    the image, so their volumes seed with coder ownership on first mount."""
    return _compose_overlay(
        volumes,
        comment_lines=["# Named volumes for enabled agents' auth/state directories"],
    )


def _tool_installed(tools, name):
    return any(isinstance(t, str) and t == name for t in tools)


def _agent_string(agent, field, value):
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"agent '{agent}' {field}: must be a non-empty string")
    return value


# One path component of a descriptor's home-relative path. Deliberately boring:
# beyond blocking traversal, this keeps tabs/newlines/spaces out of fields that
# get flattened into the build-time agents-index.tsv (a permissive charset here
# would let one descriptor corrupt the whole runtime index).
AGENT_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
# An agent binary is a bare command name (shim filename + `type -aP` lookup):
# no separators, no whitespace, nothing the shim's printf could mangle.
AGENT_BINARY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _agent_home_path(agent, field, path):
    path = _agent_string(agent, field, path)
    if path.startswith("/"):
        raise ManifestError(
            f"agent '{agent}' {field}: path '{path}' must be home-relative (no leading /)")
    for part in path.split("/"):
        if part in (".", "..") or not AGENT_PATH_COMPONENT_RE.match(part):
            raise ManifestError(
                f"agent '{agent}' {field}: path '{path}' has an illegal component "
                f"'{part}' (no traversal; letters, digits, . _ - only)")
    return path


def _normalize_agent_docs(agent_files):
    if not isinstance(agent_files, dict):
        raise ManifestError("agent descriptors must be a map of <name> -> YAML object")
    if not agent_files:
        raise ManifestError("agents section is empty — expected at least one agent descriptor")

    normalized = {}
    volume_owner = {}
    mount_owner = {}        # container mount path -> agent, for overlap messages
    mcp_binary_owner = {}   # binary -> agent, mcp-capable only (payload key)
    mcp_config_owner = {}   # mcp config_path -> agent (sidecar clobber guard)
    for agent in sorted(agent_files):
        doc = agent_files[agent]
        if not NAME_RE.match(agent):
            raise ManifestError(
                f"agent '{agent}': illegal directory name (allowed: letters, digits, underscore, dash)")
        if not isinstance(doc, dict):
            raise ManifestError(
                f"agent '{agent}': agents/{agent}/agent.yml must be a YAML map (got a {_yaml_type(doc)})")
        extra = ",".join(k for k in doc if k not in AGENT_TOP_LEVEL_KEYS)
        if extra:
            raise ManifestError(
                f"agent '{agent}': unsupported field(s): {extra} "
                "(only binary, install, state_dirs, rules_file, egress, mcp)")

        if "binary" not in doc:
            raise ManifestError(f"agent '{agent}': binary is required")
        binary = _agent_string(agent, "binary", doc.get("binary"))
        if not AGENT_BINARY_RE.match(binary):
            raise ManifestError(
                f"agent '{agent}' binary: '{binary}' is not a bare command name "
                "(no path separators or whitespace — it names the shim file and the "
                "type -aP lookup)")
        if "install" not in doc:
            raise ManifestError(f"agent '{agent}': install is required")
        install = _agent_string(agent, "install", doc.get("install"))

        state_dirs = doc.get("state_dirs")
        state_dirs = [] if _falsy(state_dirs) else state_dirs
        if not isinstance(state_dirs, list):
            raise ManifestError(f"agent '{agent}' state_dirs must be a list of {{path, volume}} maps")
        parsed_state_dirs = []
        for i, entry in enumerate(state_dirs):
            if not isinstance(entry, dict):
                raise ManifestError(
                    f"agent '{agent}' state_dirs entry {i}: must be a map with path and volume")
            entry_extra = ",".join(k for k in entry if k not in ("path", "volume"))
            if entry_extra:
                raise ManifestError(
                    f"agent '{agent}' state_dirs entry {i}: unsupported field(s): {entry_extra} "
                    "(only path, volume)")
            path = _agent_home_path(agent, f"state_dirs entry {i} path", entry.get("path"))
            volume = _agent_string(agent, f"state_dirs entry {i} volume", entry.get("volume"))
            if not VOLUME_NAME_RE.match(volume):
                raise ManifestError(
                    f"agent '{agent}' state_dirs volume '{volume}': illegal volume name "
                    "(start with letter/digit; letters, digits, _ - thereafter)")
            owner = volume_owner.get(volume)
            if owner is not None and owner != agent:
                raise ManifestError(
                    f"agent '{agent}' state_dirs volume '{volume}': already declared by agent '{owner}'")
            volume_owner[volume] = agent
            # Two descriptors (or two entries of one) mounting the same or a
            # nested path would make compose reject the merged file — or worse,
            # silently mask one volume with the other. Component-wise, like the
            # plugin checks (_path_overlaps).
            mount = f"{VOLUME_ROOT}/{path}"
            clash = next((q for q in sorted(mount_owner) if _path_overlaps(mount, q)), None)
            if clash is not None:
                raise ManifestError(
                    f"agent '{agent}' state_dirs path '{path}': mount '{mount}' overlaps "
                    f"'{clash}' declared by agent '{mount_owner[clash]}'")
            mount_owner[mount] = agent
            parsed_state_dirs.append({"path": path, "volume": volume})

        rules_file = ""
        if "rules_file" in doc and not _falsy(doc.get("rules_file")):
            rules_file = _agent_home_path(agent, "rules_file", doc.get("rules_file"))

        egress = _word_list(doc.get("egress"), f"agent '{agent}' egress")
        for d in egress:
            if not DOMAIN_RE.match(d):
                raise ManifestError(
                    f"agent '{agent}' egress entry '{d}' is not a bare hostname "
                    "(no scheme, path, port, or wildcard — a domain already covers its subdomains)")

        mcp = doc.get("mcp")
        if _falsy(mcp):
            parsed_mcp = None
        else:
            if not isinstance(mcp, dict):
                raise ManifestError(f"agent '{agent}' mcp must be a map")
            mcp_extra = ",".join(k for k in mcp if k not in AGENT_MCP_KEYS)
            if mcp_extra:
                raise ManifestError(
                    f"agent '{agent}' mcp: unsupported field(s): {mcp_extra} "
                    "(only config_path, format, dialect, env_refs, strategy)")
            if "config_path" not in mcp:
                raise ManifestError(f"agent '{agent}' mcp.config_path is required")
            config_path = _agent_home_path(agent, "mcp.config_path", mcp.get("config_path"))
            if "format" not in mcp:
                raise ManifestError(f"agent '{agent}' mcp.format is required")
            fmt = _agent_string(agent, "mcp.format", mcp.get("format"))
            if fmt not in AGENT_MCP_FORMATS:
                raise ManifestError(
                    f"agent '{agent}' mcp.format must be one of {', '.join(sorted(AGENT_MCP_FORMATS))}")
            dialect = ""
            if "dialect" in mcp and not _falsy(mcp.get("dialect")):
                dialect = _agent_string(agent, "mcp.dialect", mcp.get("dialect"))
                if dialect not in AGENT_MCP_DIALECTS:
                    raise ManifestError(
                        f"agent '{agent}' mcp.dialect must be one of {', '.join(sorted(AGENT_MCP_DIALECTS))}")
            env_refs = mcp.get("env_refs")
            if not isinstance(env_refs, (bool, str)):
                raise ManifestError(
                    f"agent '{agent}' mcp.env_refs must be a boolean or string")
            if isinstance(env_refs, str) and env_refs == "":
                raise ManifestError(
                    f"agent '{agent}' mcp.env_refs must be a non-empty string when set as text")
            strategy = ""
            if "strategy" in mcp and not _falsy(mcp.get("strategy")):
                strategy = _agent_string(agent, "mcp.strategy", mcp.get("strategy"))
                if strategy not in AGENT_MCP_STRATEGIES:
                    raise ManifestError(
                        f"agent '{agent}' mcp.strategy must be one of {', '.join(sorted(AGENT_MCP_STRATEGIES))}")
            # The descriptor contract is CLOSED against run()'s dispatch in
            # wire_plugins.py: any combination accepted here must actually wire
            # there. A combo this block does not admit would pass derive, then
            # brick up.sh only AFTER the container is up — the worst place to
            # learn a descriptor is wrong. Keep the two in lockstep, and update
            # agents/README.md ("Closed MCP combo rules") alongside this block.
            if strategy == "claude_preapprove":
                if config_path != ".mcp.json":
                    raise ManifestError(
                        f"agent '{agent}' mcp: strategy claude_preapprove requires "
                        "config_path '.mcp.json' (the workspace-level file the strategy owns)")
                if fmt != "json" or not env_refs:
                    raise ManifestError(
                        f"agent '{agent}' mcp: strategy claude_preapprove requires "
                        "format json and truthy env_refs (it writes ${SLOT} refs)")
            elif strategy == "codex_managed_block":
                if fmt != "toml":
                    raise ManifestError(
                        f"agent '{agent}' mcp: strategy codex_managed_block requires format toml")
                if env_refs not in CODEX_MANAGED_BLOCK_ENV_REFS:
                    raise ManifestError(
                        f"agent '{agent}' mcp: strategy codex_managed_block only accepts "
                        "env_refs: false or env_refs: bearer_token_env_var — env_refs: true "
                        "can't work (the managed TOML block has no ${VAR} header expansion, "
                        "unlike claude's shim), and no other string is safe: "
                        "_codex_block_body only ever renders the bearer_token_env_var field, "
                        "so a typo or a different field name would either overwrite an "
                        "unrelated key (env_refs: url) or silently drop the credential "
                        "(env_refs: bearer_token_env)")
            else:
                if fmt != "json":
                    raise ManifestError(
                        f"agent '{agent}' mcp: non-strategy wiring requires format json "
                        "(a toml agent needs a named strategy)")
                if dialect in AGENT_MCP_LITERAL_DIALECTS:
                    if env_refs:
                        raise ManifestError(
                            f"agent '{agent}' mcp: dialect '{dialect}' is literal-key "
                            "rendering — env_refs must be false (a ref-capable agent "
                            "uses dialect mcpServers)")
                elif dialect == "mcpServers":
                    if not env_refs:
                        raise ManifestError(
                            f"agent '{agent}' mcp: non-strategy mcpServers is generic-ref "
                            "wiring — env_refs must be truthy (a generic-config agent is "
                            "by definition ref-style)")
                elif dialect != "mcpServers":
                    raise ManifestError(
                        f"agent '{agent}' mcp: non-strategy wiring requires a dialect — "
                        f"{'/'.join(sorted(AGENT_MCP_LITERAL_DIALECTS))} (literal keys) "
                        "or mcpServers (generic)")
            parsed_mcp = {
                "config_path": config_path,
                "format": fmt,
                "dialect": dialect,
                "env_refs": env_refs,
                "strategy": strategy,
            }
            owner = mcp_binary_owner.get(binary)
            if owner is not None:
                raise ManifestError(
                    f"agent '{agent}' binary '{binary}': already used by mcp-capable "
                    f"agent '{owner}' (binaries key the wiring payload and must be unique)")
            mcp_binary_owner[binary] = agent
            owner = mcp_config_owner.get(config_path)
            if owner is not None:
                raise ManifestError(
                    f"agent '{agent}' mcp.config_path '{config_path}': already used by "
                    f"agent '{owner}' (two agents sharing a config would clobber each "
                    "other's sidecar-reconciled servers)")
            mcp_config_owner[config_path] = agent

        # config_settings: top-level scalar keys stamped into the agent's own
        # config file as a second managed block. Only the codex_managed_block
        # strategy renders one today (TOML top-level keys must precede every
        # table, so the block has to sit at the head of the file — a shape no
        # other wiring role has), and the check keeps that in lockstep with
        # wire_plugins.py's dispatch the same way the MCP combo rules are.
        raw_settings = doc.get("config_settings")
        config_settings = {}
        if not _falsy(raw_settings):
            if not isinstance(raw_settings, dict):
                raise ManifestError(f"agent '{agent}' config_settings must be a map")
            if parsed_mcp is None or parsed_mcp["strategy"] != "codex_managed_block":
                raise ManifestError(
                    f"agent '{agent}' config_settings requires mcp.strategy "
                    "codex_managed_block (no other wiring role renders a settings block)")
            for key, value in raw_settings.items():
                if not isinstance(key, str) or not AGENT_CONFIG_SETTING_KEY_RE.match(key):
                    raise ManifestError(
                        f"agent '{agent}' config_settings key {key!r} must match [A-Za-z0-9_-]+ "
                        "(it is emitted as a bare TOML key)")
                if not isinstance(value, AGENT_CONFIG_SETTING_TYPES):
                    raise ManifestError(
                        f"agent '{agent}' config_settings.{key} must be a string, "
                        "boolean, or integer")
                config_settings[key] = value

        normalized[agent] = {
            "binary": binary,
            "install": install,
            "state_dirs": parsed_state_dirs,
            "rules_file": rules_file,
            "egress": egress,
            "mcp": parsed_mcp,
            "config_settings": config_settings,
        }
    return normalized


def _parse_secret(val, plugin, slot):
    """Parse one hybrid secret-slot declaration and return its optional hint.

    Every slot uses the same resolution model: common_secrets supplies an
    optional default, agent_secrets can override it or disable it for one
    agent. Scope is intentionally not part of the plugin schema any more."""
    if _falsy(val):
        hint = ""
    elif isinstance(val, dict):
        extra = ",".join(k for k in val if k != "hint")
        if extra:
            raise ManifestError(
                f"plugin '{plugin}' secret '{slot}': unsupported field(s): {extra} (only hint)")
        hint = val.get("hint", "")
        if not isinstance(hint, str):
            raise ManifestError(f"plugin '{plugin}' secret '{slot}': hint must be a string")
    else:
        raise ManifestError(
            f"plugin '{plugin}' secret '{slot}': must be empty or a map with an optional hint: key")
    if "\t" in hint or "\n" in hint:
        raise ManifestError(f"plugin '{plugin}' secret '{slot}': hint must be a single line (no tab/newline)")
    return hint


def _canonical_token_var(owner):
    """The in-container env var a per-org token lands in — keyfiles.sh writes it,
    git-credential-org.sh reads it. This derivation MUST match the shell one in
    that helper byte-for-byte: lowercase the owner (github owners are
    case-insensitive, and the router derives the owner from the clone URL, whose
    case we don't control), then GH_TOKEN_ + every non-alphanumeric replaced by
    _. Case-folding means two owners differing only in case would collide on one
    var, so _git_identity rejects case-insensitive duplicate owners upstream."""
    return "GH_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", owner.lower())


def _git_identity(git, env, secrets_file):
    """Derive git credential routing from the git: section — NAMES only, per the
    module contract (up.sh resolves the secret VALUES). git.token names the
    default credential's secrets.env var; git.orgs.<owner>.{token,name,email}
    override per forge owner. A token: that isn't a currently-set secrets.env var
    (GH_TOKEN_VARS lists the ones up.sh scanned) is a hard error — never a silent
    fall-back to the wrong identity, which is the whole reason this exists.

    Emits (owner is lowercased — github owners are case-insensitive and the
    router/attribution match against the clone URL's owner, whose case we don't
    control, so both sides fold to lowercase):
      GIT_TOKEN_SOURCE     default token's source var name ("" = keep global GH_TOKEN)
      GIT_ORG_TOKENS       owner<TAB>canonical_var<TAB>source_var per line
      GIT_ORG_IDENTITIES   owner<TAB>name<TAB>email per line
    """
    token_vars = set((env.get("GH_TOKEN_VARS") or "").split())
    errors = []

    def source(val, field, required):
        src = _scalar(val, field)
        if not src:
            if required:
                errors.append(f"  {field}: needs token: (a secrets.env var name)")
            return ""
        if not REF_RE.match(src):
            errors.append(f"  {field}: '{src}' is not a valid env var name")
            return ""
        if src not in token_vars:
            errors.append(f"  {field}: {src} not found in {secrets_file}")
            return ""
        return src

    default_source = source(git.get("token"), "git.token", required=False)

    orgs = git.get("orgs")
    records = []  # (owner_lc, canonical_var, source_var, name, email)
    seen_owners = {}  # lowercased owner → the manifest key that claimed it
    if not _falsy(orgs):
        if not isinstance(orgs, dict):
            errors.append("  git.orgs: must be a map of <owner>: {token, name, email}")
            orgs = {}
        for owner, spec in orgs.items():
            field = f"git.orgs.{owner}"
            if not isinstance(owner, str) or not OWNER_RE.match(owner):
                errors.append(f"  git.orgs: illegal owner '{owner}' (a forge org/user name)")
                continue
            # Owners are case-insensitive (routing folds to lowercase), so two
            # keys differing only in case would map to one token var — an
            # ambiguity, not a valid config. Reject it instead of silently
            # letting the last one win.
            owner_lc = owner.lower()
            if owner_lc in seen_owners:
                errors.append(f"  git.orgs: duplicate owner '{owner}' "
                              f"(case-insensitive clash with '{seen_owners[owner_lc]}')")
                continue
            seen_owners[owner_lc] = owner
            if _falsy(spec):
                spec = {}
            if not isinstance(spec, dict):
                errors.append(f"  {field}: must be a map of {{token, name, email}}")
                continue
            extra = ",".join(k for k in spec if k not in ("token", "name", "email"))
            if extra:
                errors.append(f"  {field}: unsupported field(s): {extra} (only token, name, email)")
                continue
            src = source(spec.get("token"), f"{field}.token", required=True)
            if not src:
                continue
            records.append((owner_lc, _canonical_token_var(owner), src,
                            _scalar(spec.get("name"), f"{field}.name"),
                            _scalar(spec.get("email"), f"{field}.email")))

    if errors:
        raise ManifestError("manifest git identity failed validation:\n" + "\n".join(errors))

    return {
        "GIT_TOKEN_SOURCE": default_source,
        "GIT_ORG_TOKENS": "".join(f"{o}\t{c}\t{s}\n" for o, c, s, _, _ in records),
        "GIT_ORG_IDENTITIES": "".join(f"{o}\t{n}\t{e}\n" for o, _, _, n, e in records),
    }


class Derived(dict):
    """Ordered VAR → value string map with shell-quoted rendering."""

    def render(self):
        return "".join(f"{k}={shlex.quote(v)}\n" for k, v in self.items())


def derive(manifest, plugin_files, agent_files, env):
    """The whole old 'Read manifest' section of up.sh as one function.
    manifest: parsed manifest JSON; plugin_files: {name: parsed plugin JSON,
    or UNREADABLE for a file yq couldn't parse} for every file shipped under
    plugins/; agent_files: parsed agent JSON for every file shipped under
    agents/; env: os.environ-like mapping."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a YAML mapping")
    out = Derived()
    secrets_file = env.get("SECRETS_FILE", "secrets.env")
    agents = _normalize_agent_docs(agent_files)
    agent_dir_names = sorted(agents)
    default_tools = list(agent_dir_names)
    mcp_agent_dir_names = [name for name in agent_dir_names if agents[name]["mcp"] is not None]
    agent_suffixes = tuple(
        sorted(
            [("_" + agents[name]["binary"].replace("-", "_"), agents[name]["binary"])
             for name in mcp_agent_dir_names],
            key=lambda item: (-len(item[0]), item[0]),
        )
    )
    known_agent_suffixes = "/".join(s for s, _ in agent_suffixes)
    agent_names = frozenset(agents[name]["binary"] for name in mcp_agent_dir_names)
    compose_volume_names = set(STATIC_COMPOSE_VOLUME_NAMES)
    compose_mount_paths = set(STATIC_COMPOSE_MOUNT_PATHS)
    for name in agent_dir_names:
        for state in agents[name]["state_dirs"]:
            compose_volume_names.add(state["volume"])
            compose_mount_paths.add(f"{VOLUME_ROOT}/{state['path']}")

    # ── Scalars (old Y() reads + defaults) ──────────────────────────────
    # layout v2: repo: (scalar) is gone; repos: is a list of URLs / {name, url}.
    if "repo" in manifest:
        raise ManifestError(
            "manifest repo: is gone — declare repos: [<url>, ...] instead "
            "(layout v2: each repo clones to /workspace/repos/<name>)")
    repos_val = manifest.get("repos")
    if _falsy(repos_val):
        repos_val = []
    elif not isinstance(repos_val, list):
        raise ManifestError("manifest repos: must be a list of URLs or {name, url} maps")
    repo_errors = []
    parsed_repos = []
    seen_repo_names = set()
    for entry in repos_val:
        if isinstance(entry, str):
            url, explicit_name = entry, None
        elif isinstance(entry, dict):
            # Unknown keys are errors, not ignored — a typo'd `nmae:` would
            # otherwise silently fall back to the URL basename.
            extra = ",".join(k for k in entry if k not in ("name", "url"))
            if extra:
                repo_errors.append(
                    f"  repos entry: unsupported field(s): {extra} (only name and url)")
                continue
            url = entry.get("url")
            # yq `//` semantics everywhere else: a falsy name reads as absent.
            explicit_name = None if _falsy(entry.get("name")) else entry.get("name")
        else:
            repo_errors.append(
                f"  repos entry: must be a URL string or {{name, url}} map "
                f"(got a {_yaml_type(entry)})")
            continue
        if not isinstance(url, str) or url == "":
            repo_errors.append("  repos entry: url must be a non-empty string")
            continue
        if any(c in url for c in (" ", "\t", "\n")):
            repo_errors.append(f"  repos entry: URL '{url}' contains whitespace")
            continue
        if explicit_name is not None:
            if not isinstance(explicit_name, str):
                repo_errors.append("  repos entry: name must be a string")
                continue
            name = explicit_name
        else:
            base = url.rstrip("/")
            cut = max(base.rfind("/"), base.rfind(":"))
            name = base[cut + 1:] if cut >= 0 else base
            if name.endswith(".git"):
                name = name[:-4]
            if not name:
                repo_errors.append(
                    f"  repos entry: cannot derive a name from URL '{url}'")
                continue
        if not REPO_DIR_RE.match(name):
            repo_errors.append(
                f"  repos entry: illegal name '{name}' "
                "(must start with letter/digit/underscore; only letters, digits, . _ - thereafter — "
                "it becomes a directory under /workspace/repos)")
            continue
        if name in seen_repo_names:
            repo_errors.append(f"  repos entry: duplicate name '{name}'")
            continue
        seen_repo_names.add(name)
        parsed_repos.append((name, url))
    if repo_errors:
        raise ManifestError(
            "manifest repos failed validation:\n" + "\n".join(repo_errors))
    out["REPOS"] = "".join(f"{name}\t{url}\n" for name, url in parsed_repos)
    forge = _scalar(manifest.get("forge"), "forge") or "github"
    if forge not in ("github", "gitea"):
        raise ManifestError("forge must be github or gitea")
    out["FORGE"] = forge
    git = _section(manifest, "git")
    out["GIT_USER_NAME"] = _scalar(git.get("name"), "git.name") or env.get("GIT_NAME_DEFAULT", "")
    out["GIT_USER_EMAIL"] = _scalar(git.get("email"), "git.email") or env.get("GIT_EMAIL_DEFAULT", "")
    out.update(_git_identity(git, env, secrets_file))
    out["MEM_LIMIT"] = _scalar(manifest.get("memory"), "memory") or "2g"

    # ── Agents (the tools: key was renamed; reject it BY NAME) ──────────
    if "tools" in manifest:
        raise ManifestError(
            "manifest tools: was renamed to agents: — update the manifest (same values)")
    tools = manifest.get("agents")
    if _falsy(tools):
        tools = default_tools
    if not isinstance(tools, list):
        raise ManifestError("manifest agents: must be a list")
    enabled_agent_dirs = sorted(name for name in agent_dir_names if _tool_installed(tools, name))
    # A name with no agents/<name>/ directory is dropped, not fatal — a manifest
    # may outlive a retired agent, and failing every container over a stale list
    # would be worse. Say so, or the CLI just goes missing with no explanation.
    for name in tools:
        if isinstance(name, str) and name not in agent_dir_names:
            print(f"  ⚠ agents: '{name}' has no agents/{name}/ directory — not installed "
                  f"(known: {', '.join(sorted(agent_dir_names))})", file=sys.stderr)
    out["AGENTS_ENABLED"] = " ".join(enabled_agent_dirs)
    out["SHIM_AGENTS"] = " ".join(
        sorted(agents[name]["binary"] for name in enabled_agent_dirs
               if agents[name]["mcp"] is not None)
    )
    # The agents whose remote agent-scoped keys must travel as one-shot docker
    # exec env (IDENTITY_KEY_n) because their configs bake literal values.
    # Ref-style and managed-block agents get keys via their shim env instead —
    # routing by descriptor role here keeps up.sh from ever shipping a
    # ref-capable agent's secret on the exec environment unnecessarily.
    out["LITERAL_KEY_AGENTS"] = " ".join(sorted(
        agents[name]["binary"] for name in enabled_agent_dirs
        if agents[name]["mcp"] is not None
        and not agents[name]["mcp"]["env_refs"]
        and agents[name]["mcp"]["dialect"] in AGENT_MCP_LITERAL_DIALECTS
    ))
    out["AGENTS_MCP_JSON"] = json.dumps(
        [
            {
                "binary": agents[name]["binary"],
                "config_path": agents[name]["mcp"]["config_path"],
                "format": agents[name]["mcp"]["format"],
                "dialect": agents[name]["mcp"]["dialect"],
                "env_refs": agents[name]["mcp"]["env_refs"],
                "strategy": agents[name]["mcp"]["strategy"],
                "settings": agents[name]["config_settings"],
            }
            for name in enabled_agent_dirs
            if agents[name]["mcp"] is not None
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )

    # ── Capabilities: egress firewall keys stay; gateway/proxyman/browser are
    #    deprecated sugar for the equivalent plugins (PLN - Plugins v2). ──────
    caps = _section(manifest, "capabilities")
    egress_items = _comma_list(caps.get("egress"), "capabilities.egress")
    cidr_items = _comma_list(caps.get("egress_cidrs"), "capabilities.egress_cidrs")
    if "egress_broker" not in caps or caps.get("egress_broker") is None:
        enable_egress_broker = "true"
    else:
        enable_egress_broker = _raw_flag(caps.get("egress_broker"), "capabilities.egress_broker")
        if enable_egress_broker not in ("true", "false"):
            raise ManifestError(
                f"capabilities.egress_broker must be true or false (got {enable_egress_broker!r})")
    out["ENABLE_EGRESS_BROKER"] = enable_egress_broker
    sugar_plugins = []
    for cap in ("gateway", "proxyman", "browser"):
        if _raw_flag(caps.get(cap), f"capabilities.{cap}") == "true":
            sugar_plugins.append(cap)
            print(f"  ⚠ capabilities.{cap}: true is deprecated — use plugins: [{cap}] "
                  "instead (the capabilities: flag is sugar and will be removed)",
                  file=sys.stderr)

    # ── identities: deprecated sugar for the obsidian/watch plugins + their
    #    agent_secrets bindings (PLN - Plugins v2 Phase 2). Reading the refs
    #    here lets the sugar auto-enable the plugin files so they validate like
    #    any other; the ref→binding conversion + validation happens below. ────
    ids = _section(manifest, "identities")
    obs_refs = _word_list(ids.get("obsidian"), "identities.obsidian")
    watch_refs = _word_list(ids.get("watch"), "identities.watch")
    if obs_refs:
        sugar_plugins.append("obsidian-annotated")
    if watch_refs:
        sugar_plugins.append("annotated-watch")
    if obs_refs or watch_refs:
        print("  ⚠ identities: is deprecated — bind agent-scoped secrets under "
              "agent_secrets: (see plugins/obsidian-annotated/plugin.yml); identities: is "
              "sugar and will be removed", file=sys.stderr)

    # ── Plugins list (aggregated errors, old order) ─────────────────────
    plugins_val = manifest.get("plugins")
    if not _falsy(plugins_val) and not isinstance(plugins_val, list):
        raise ManifestError("manifest plugins: must be a list, e.g. plugins: [serena]")
    plugins = _word_list(plugins_val, "plugins")
    # capabilities: sugar appends the equivalent plugin names (dedup, explicit
    # list first) so gateway/proxyman/browser flow through the one pipeline.
    for cap in sugar_plugins:
        if cap not in plugins:
            plugins.append(cap)
    plugin_errors = []
    for p in plugins:
        if not NAME_RE.match(p):
            plugin_errors.append(
                f"  plugin '{p}': illegal characters (allowed: letters, digits, underscore, dash)")
            continue
        if p not in plugin_files:
            plugin_errors.append(f"  plugin '{p}': no plugin file at plugins/{p}/plugin.yml")
        elif plugin_files[p] is UNREADABLE:
            plugin_errors.append(f"  plugin '{p}': plugins/{p}/plugin.yml is not valid YAML (yq could not parse it)")
    if plugin_errors:
        raise ManifestError(
            "manifest plugins failed validation:\n" + "\n".join(plugin_errors))
    out["PLUGINS"] = " ".join(plugins)

    # ── plugin_ports: per-container host port for a host-service plugin ──
    # Host ports are exclusive, so two containers running the same host-service
    # plugin (two browsers, say) need different ports — the same reason ssh.port
    # and remote.mosh_ports are per-container. An override re-points BOTH the
    # firewall grant (HOST_MCP_PORTS) and the ${HOST_PORT} placeholder in the
    # plugin's url, so the port stays a single value with one source of truth.
    plugin_ports_val = manifest.get("plugin_ports")
    plugin_ports = {}
    if not _falsy(plugin_ports_val):
        if not isinstance(plugin_ports_val, dict):
            raise ManifestError(
                "manifest plugin_ports: must be a map of plugin: port, e.g. plugin_ports: {browser: 8815}")
        for name, val in plugin_ports_val.items():
            if name not in plugins:
                raise ManifestError(
                    f"plugin_ports '{name}': not an enabled plugin (add it to plugins: first)")
            if isinstance(val, bool) or not isinstance(val, int):
                raise ManifestError(f"plugin_ports '{name}': must be an integer port number")
            if not 1 <= val <= 65535:
                raise ManifestError(f"plugin_ports '{name}': port {val} out of range (1-65535)")
            plugin_ports[name] = val

    # ── ssh / remote (RFC 04) ───────────────────────────────────────────
    ssh = _section(manifest, "ssh")
    ssh_port = _scalar(ssh.get("port"), "ssh.port")
    out["SSH_PORT"] = ssh_port
    out["SSH_BIND"] = _scalar(ssh.get("bind"), "ssh.bind") or "127.0.0.1"

    remote = _section(manifest, "remote")
    remote_tmux = _raw_flag(remote.get("tmux"), "remote.tmux")
    remote_mosh = _raw_flag(remote.get("mosh"), "remote.mosh")
    remote_notify = _scalar(remote.get("notify"), "remote.notify")
    if (remote_tmux == "true" or remote_mosh == "true" or remote_notify) and not ssh_port:
        raise ManifestError(
            "manifest has remote: but no ssh: section — remote access rides the SSH login path (add ssh.port)")
    if remote_notify not in ("", "ntfy"):
        raise ManifestError(f"remote.notify must be 'ntfy' (got '{remote_notify}')")
    if remote_notify and remote_tmux != "true":
        raise ManifestError(
            "remote.notify requires remote.tmux: true (the idle monitor runs inside the tmux session)")
    out["REMOTE_TMUX"] = remote_tmux
    out["REMOTE_MOSH"] = remote_mosh
    out["REMOTE_NOTIFY"] = remote_notify

    mosh_ports = ""
    mosh_ports_dash = ""
    if remote_mosh == "true":
        mosh_ports = _scalar(remote.get("mosh_ports"), "remote.mosh_ports") or "60000:60010"
        if not MOSH_PORTS_RE.match(mosh_ports):
            raise ManifestError(f"remote.mosh_ports must be START:END (got '{mosh_ports}')")
        lo, hi = (int(x) for x in mosh_ports.split(":"))
        if lo > hi or hi > 65535 or lo < 1024:
            raise ManifestError(
                f"remote.mosh_ports '{mosh_ports}' out of range (need 1024 <= START <= END <= 65535)")
        mosh_ports_dash = f"{lo}-{hi}"
    out["MOSH_PORTS"] = mosh_ports
    out["MOSH_PORTS_DASH"] = mosh_ports_dash

    # ── identities: sugar → agent_secrets bindings (aggregated errors) ──────
    # The old ref-suffix form still validates byte-for-byte, then converts to
    # (agent, slot, source) records. The slot's plugin (obsidian-annotated /
    # annotated-watch) was auto-enabled above, so slot existence is guaranteed;
    # only the ref charset / suffix / source existence are checked here.
    present_vars = set((env.get("PRESENT_SECRET_VARS") or env.get("SECRET_KEY_VARS") or "").split())
    identity_errors = []
    sugar_bindings = []  # (agent, slot, source) from identities:

    def check_ref(kind, slot, prefix, ref):
        if not REF_RE.match(ref):
            identity_errors.append(
                f"  {kind} ref '{ref}': illegal characters (allowed: letters, digits, underscore)")
            return
        agent = agent_for_ref(ref, agent_suffixes)
        if not agent:
            identity_errors.append(
                f"  {kind} ref '{ref}': suffix is not a known agent ({known_agent_suffixes})")
            return
        var = f"{prefix}_{ref}"
        if var not in present_vars:
            identity_errors.append(f"  {kind} ref '{ref}': {var} not found in {secrets_file}")
            return
        sugar_bindings.append((agent, slot, var))

    for ref in obs_refs:
        check_ref("obsidian", "OBSIDIAN_ANNOTATED_KEY", "OBSIDIAN_KEY", ref)
    for ref in watch_refs:
        check_ref("watch", "ANNOTATED_WATCH_KEY", "OBSIDIAN_WATCH_KEY", ref)
    if identity_errors:
        raise ManifestError(
            "manifest identity references failed validation:\n" + "\n".join(identity_errors))

    # Plugins fold their own egress (obsidian-annotated ships mcp-obsidian.dmetr.io).
    def add_egress_domain(domain):
        if domain not in egress_items:
            egress_items.append(domain)

    # ── Per-plugin egress + mcp entry validation (fail-fast, old order) ─
    # Each mcp server is local (command:) OR remote (url:) — the shape, not a
    # type: field, decides. `requires:` names the plugin's secret slots needed
    # by that server. A required server is wired only for agents whose resolved
    # hybrid credentials include every named slot; a server with no requirements
    # is a uniform plugin entry.
    plugin_mcp_entries = []
    seen_server_names = set()
    host_ports = []
    secret_slots = {}      # SLOT -> (plugin, hint)
    servers_by_name = {}   # name -> {"spec": {...}, "requires": [SLOT, ...]}
    server_slots = []      # required slot names, first-seen order
    remote_server_slots = []  # subset required by a REMOTE (no-command) server
    plugin_volumes = {}    # volume name -> container path (enabled plugins only)
    volume_owner = {}      # volume name -> plugin, for collision messages
    path_owner = {}        # container path -> plugin
    plugin_services = {}   # service name -> {"command": ..., "plugin": ...} (enabled plugins only)
    service_owner = {}     # service name -> plugin, for collision messages
    for p in plugins:
        doc = plugin_files[p]
        if doc is None:
            doc = {}  # empty yaml file → null → a valid no-op plugin
        if not isinstance(doc, dict):
            raise ManifestError(
                f"plugin '{p}': plugins/{p}/plugin.yml must be a YAML map (got a {_yaml_type(doc)})")
        for d in _comma_list(doc.get("egress"), f"plugin '{p}' egress"):
            if not DOMAIN_RE.match(d):
                raise ManifestError(
                    f"plugin '{p}' egress entry '{d}' is not a bare hostname (no scheme, path, port, or wildcard — a domain already covers its subdomains)")
            add_egress_domain(d)

        # Secret slots no longer choose a delivery scope. They are all resolved
        # through common defaults, per-agent overrides, and explicit disables.
        secrets = doc.get("secrets")
        secrets = {} if _falsy(secrets) else secrets
        if not isinstance(secrets, dict):
            raise ManifestError(f"plugin '{p}' secrets must be a map of SLOT: {{hint: ...}}")
        plugin_slots = set()
        for slot, val in secrets.items():
            if not REF_RE.match(slot):
                raise ManifestError(
                    f"plugin '{p}' secret slot '{slot}': illegal characters (must be a shell env var name)")
            if slot in secret_slots:
                raise ManifestError(f"secret slot '{slot}' is declared by more than one enabled plugin")
            secret_slots[slot] = (p, _parse_secret(val, p, slot))
            plugin_slots.add(slot)

        mcp = doc.get("mcp")
        mcp = {} if _falsy(mcp) else mcp
        if not isinstance(mcp, dict):
            raise ManifestError(f"plugin '{p}' mcp must be a map of MCP servers")
        has_local = False
        has_remote = False
        for n, spec in mcp.items():
            if not NAME_RE.match(n):
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': illegal characters in name (allowed: letters, digits, underscore, dash — it becomes a TOML/JSON key)")
            spec = spec if isinstance(spec, dict) else {}
            is_local = "command" in spec
            is_remote = "url" in spec
            if is_local and is_remote:
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': set exactly one of command: (local stdio) or url: (remote http), not both")
            if not is_local and not is_remote:
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': needs command: (local stdio) or url: (remote http)")
            requires = spec.get("requires", [])
            if _falsy(requires):
                requires = []
            if not isinstance(requires, list) or not all(isinstance(s, str) for s in requires):
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': requires must be a list of this plugin's secret slots")
            if len(set(requires)) != len(requires):
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': requires must not repeat a secret slot")
            unknown = [slot for slot in requires if slot not in plugin_slots]
            if unknown:
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': requires unknown secret slot(s): {', '.join(unknown)}")
            if is_local:
                has_local = True
                if not isinstance(spec.get("command"), str):
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': command must be a string (local stdio server)")
                extra = ",".join(k for k in spec if k not in ("command", "args", "requires"))
                if extra:
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': unsupported field(s) for a local server: {extra} (only command, args, and requires)")
            else:
                has_remote = True
                if not isinstance(spec.get("url"), str):
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': url must be a string (remote http server)")
                headers = spec.get("headers", {})
                if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': headers must be a map of string values")
                extra = ",".join(k for k in spec if k not in ("url", "headers", "requires"))
                if extra:
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': unsupported field(s) for a remote server: {extra} (only url, headers, and requires)")
            if n in seen_server_names:
                raise ManifestError(
                    f"multiple enabled plugins define the same MCP server name: {n}")
            seen_server_names.add(n)

        install = doc.get("install")
        if has_local and (_falsy(install) or not isinstance(install, str) or not install.strip()):
            raise ManifestError(
                f"plugin '{p}': a local (command:) server needs an install: block (baked into the image)")

        # ── volumes: state that must outlive a container recreate ────────────
        # A per-container named volume, mounted at a path the plugin names.
        # Declared here rather than in compose/ so nothing outside plugins/<p>/
        # knows this plugin exists; up.sh feeds the generated overlay to compose
        # only for the containers whose manifest enables it.
        vols = doc.get("volumes")
        vols = {} if _falsy(vols) else vols
        if not isinstance(vols, dict):
            raise ManifestError(
                f"plugin '{p}' volumes must be a map of NAME: /container/path")
        for vname, vpath in vols.items():
            if not VOLUME_NAME_RE.match(vname):
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': name must be at least two characters, start with a letter or digit, and use only letters, digits, underscore, dash (it becomes a compose volume key)")
            if vname in compose_volume_names:
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': that name is already a compose volume (compose would merge into it and remount a real directory)")
            if vname in volume_owner:
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': already declared by plugin '{volume_owner[vname]}' (two plugins cannot share one volume)")
            vpath = _scalar(vpath, f"plugin '{p}' volumes.{vname}")
            if not VOLUME_PATH_RE.match(vpath) or ".." in vpath.split("/"):
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': path '{vpath}' is not an absolute container path (letters, digits, and . _ - + @ only — no spaces, ':', '$', globs, '..', or trailing slash)")
            if not _path_overlaps(vpath, VOLUME_ROOT) or vpath == VOLUME_ROOT:
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': path '{vpath}' must be under {VOLUME_ROOT}/ (a volume elsewhere would shadow image content or the workspace)")
            # Not just an exact hit: a volume at a PARENT of a compose mount
            # freezes that whole tree in a volume (a rebuilt image never reaches
            # the container again), and one at a CHILD hides live content —
            # both silent, both only visible as lost auth or missing repos.
            clash = next((m for m in sorted(compose_mount_paths) if _path_overlaps(vpath, m)), None)
            if clash:
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': path '{vpath}' collides with the compose mount '{clash}'")
            clash = next((q for q in sorted(path_owner) if _path_overlaps(vpath, q)), None)
            if clash:
                raise ManifestError(
                    f"plugin '{p}' volume '{vname}': path '{vpath}' collides with '{clash}', mounted by plugin '{path_owner[clash]}'")
            plugin_volumes[vname] = vpath
            volume_owner[vname] = p
            path_owner[vpath] = p

        # ── services: in-container processes started (idempotently, under a
        # restart wrapper) at the end of `./djinn up` — see src/plugin_services.py
        # and up.sh's "Plugin services" section. Optional and absent from every
        # plugin today, purely additive (Phase 1 Hardening PLN §2). A service
        # name is validated like a volume name but shares ONE namespace across
        # every ENABLED plugin (not just within one plugin's own map), because
        # it becomes a tmux session name — two plugins racing for svc-<name>
        # would silently restart each other's process.
        services = doc.get("services")
        services = {} if _falsy(services) else services
        if not isinstance(services, dict):
            raise ManifestError(
                f"plugin '{p}' services must be a map of NAME: command")
        for sname, scmd in services.items():
            if not isinstance(sname, str) or not SERVICE_NAME_RE.match(sname):
                raise ManifestError(
                    f"plugin '{p}' service '{sname}': illegal characters (allowed: "
                    "lowercase letters, digits, dash — it becomes a tmux session name)")
            if not isinstance(scmd, str) or not scmd.strip():
                raise ManifestError(
                    f"plugin '{p}' service '{sname}': command must be a non-empty string")
            if any(ch in scmd for ch in "\t\n\r"):
                raise ManifestError(
                    f"plugin '{p}' service '{sname}': command must not contain tabs or "
                    "newlines (the PLUGIN_SERVICES export is TAB-separated lines; use "
                    "a one-line command, e.g. `bash -lc '...'`)")
            owner = service_owner.get(sname)
            if owner is not None:
                raise ManifestError(
                    f"plugin '{p}' service '{sname}': already declared by plugin '{owner}' "
                    "(two plugins cannot share one service name)")
            service_owner[sname] = p
            plugin_services[sname] = {"command": scmd, "plugin": p}

        hp = doc.get("host_port")
        if not _falsy(hp):
            # A host grant needs a server that actually dials the host: a
            # remote (url:) server, or a local bridge whose command dials the
            # host over TCP (rhinomcp). Anything else would open a firewall
            # hole nothing uses.
            if not mcp:
                raise ManifestError(
                    f"plugin '{p}': host_port needs an mcp server to use it — a remote "
                    "(url:) server, or a local bridge that dials the host")
            if not has_remote and not any(
                    HOST_PORT_REF in v
                    for spec in mcp.values() for v in _host_port_ref_fields(spec)):
                # Only local servers justify the grant, so one of them must
                # demonstrably take the port — otherwise the firewall opens for
                # a port nothing dials, and a plugin_ports: override would move
                # the grant while the bridge kept dialing the old port.
                raise ManifestError(
                    f"plugin '{p}': host_port with only local servers needs a "
                    f"{HOST_PORT_REF} reference in the bridge's command/args")
            if isinstance(hp, bool) or not isinstance(hp, int):
                raise ManifestError(f"plugin '{p}': host_port must be an integer port number")
            if not 1 <= hp <= 65535:
                raise ManifestError(f"plugin '{p}': host_port {hp} out of range (1-65535)")
        elif p in plugin_ports:
            # Overriding a port the plugin never declares would open a firewall
            # grant to a port nothing serves — almost certainly a typo'd name.
            raise ManifestError(
                f"plugin_ports '{p}': plugin declares no host_port (it has no host-side service to re-point)")
        # The manifest override wins over the plugin's default; either way the
        # SAME value feeds the firewall grant and the url placeholder below.
        resolved_port = plugin_ports.get(p, hp if not _falsy(hp) else None)
        if resolved_port is not None:
            host_ports.append(resolved_port)

        # ${HOST_PORT} in a remote url OR a local server's command/args → the
        # resolved port. Substituted here, host-side at derive time, NOT left
        # as a ${VAR} ref for the agent: only Claude expands those reliably
        # (cursor/pi can't), and this has to work for every agent. The
        # command/args fields cover the local-bridge-dials-host case
        # (rhinomcp), so a plugin_ports: override re-points the dial target
        # and the firewall grant together for locals exactly as it re-points
        # a remote url.
        # Rebuilt into NEW dicts/lists rather than assigned into the spec:
        # plugin_files belongs to the caller (and the test suite reuses one
        # module-level fixture across cases), so mutating it in place would
        # leak the resolved port into every later read of the same plugin.
        substituted = {}
        for n, spec in mcp.items():
            if not any(HOST_PORT_REF in v for v in _host_port_ref_fields(spec)):
                continue
            if resolved_port is None:
                raise ManifestError(
                    f"plugin '{p}' mcp server '{n}': {'url' if 'url' in spec else 'command/args'} "
                    f"uses {HOST_PORT_REF} but the plugin declares no host_port to substitute")
            port = str(resolved_port)
            new_spec = dict(spec)
            for key in ("url", "command"):
                v = new_spec.get(key)
                if isinstance(v, str) and HOST_PORT_REF in v:
                    new_spec[key] = v.replace(HOST_PORT_REF, port)
            if isinstance(new_spec.get("args"), list):
                new_spec["args"] = [a.replace(HOST_PORT_REF, port) if isinstance(a, str) else a
                                    for a in new_spec["args"]]
            substituted[n] = new_spec
        if substituted:
            mcp = {**mcp, **substituted}

        uniform = {}
        for n, spec in mcp.items():
            config_spec = {k: v for k, v in spec.items() if k != "requires"}
            requires = spec.get("requires") or []
            if requires:
                servers_by_name[n] = {"spec": config_spec, "requires": requires}
                is_remote = "command" not in config_spec
                for slot in requires:
                    if slot not in server_slots:
                        server_slots.append(slot)
                    if is_remote and slot not in remote_server_slots:
                        remote_server_slots.append(slot)
            else:
                uniform[n] = config_spec
        # One line of compact JSON per uniform plugin — the --build-payload
        # contract does not need empty objects for fully required plugins.
        if uniform:
            plugin_mcp_entries.append(json.dumps(uniform, separators=(",", ":"), ensure_ascii=False))

    # No agent-vs-plugin collision checks HERE: every agent's state volumes and
    # mount paths were seeded into compose_volume_names/compose_mount_paths
    # before the plugin loop, so a colliding plugin was already rejected above
    # (with the plugin named as the offender) before this loop can run.
    agent_volumes = {}
    for agent_name in enabled_agent_dirs:
        for state in agents[agent_name]["state_dirs"]:
            agent_volumes[state["volume"]] = f"{VOLUME_ROOT}/{state['path']}"
        # Agent egress folds into the firewall allowlist exactly like plugin
        # egress, but only for ENABLED agents — the per-agent grant that lets
        # the blanket "agent APIs" zone shrink over time.
        for d in agents[agent_name]["egress"]:
            add_egress_domain(d)

    out["PLUGIN_MCP_ENTRIES"] = "".join(e + "\n" for e in plugin_mcp_entries)
    # The whole generated compose overlay, not just the pairs: the YAML shape
    # (and the entrypoint's PLUGIN_VOLUME_PATHS contract inside it) is logic
    # worth unit-testing, and up.sh should only place the file, not format it.
    out["PLUGIN_COMPOSE_YAML"] = plugin_compose_overlay(plugin_volumes)
    out["AGENTS_COMPOSE_YAML"] = agent_compose_overlay(agent_volumes)
    # One "name<TAB>command<TAB>plugin" line per declared service of an
    # ENABLED plugin, sorted by name (order-independent of the plugin list,
    # same reasoning as the compose overlays above). up.sh reads this with the
    # same `while IFS=$'\t' read` idiom it already uses for REPOS/AGENT_SECRETS
    # and hands each (name, command) pair to src/plugin_services.py.
    out["PLUGIN_SERVICES"] = "".join(
        f"{name}\t{plugin_services[name]['command']}\t{plugin_services[name]['plugin']}\n"
        for name in sorted(plugin_services)
    )
    # Core egress broker (not plugin-gated): host port for filing blocked egress.
    if enable_egress_broker == "true":
        host_ports.append(8816)
    # Sorted + deduped so the firewall grant string is order-independent of the
    # plugin list and two plugins sharing a port don't double up the grant.
    out["HOST_MCP_PORTS"] = ",".join(str(p) for p in sorted(set(host_ports)))
    # Required server definitions and the slots whose values must be handed to
    # the wiring exec for literal remote-agent configs. Env-only slots are not
    # included in AGENT_SERVER_SLOTS. AGENT_SERVER_REMOTE_SLOTS is the subset
    # feeding a REMOTE server — the only slots up.sh puts on the `docker exec`
    # argv, since a LOCAL server reads its ${SLOT} from the agent's own env.
    out["AGENT_SERVERS_JSON"] = json.dumps(servers_by_name, separators=(",", ":"), ensure_ascii=False)
    out["AGENT_SERVER_SLOTS"] = " ".join(server_slots)
    out["AGENT_SERVER_REMOTE_SLOTS"] = " ".join(remote_server_slots)

    # ── Hybrid secret resolution ─────────────────────────────────────────────
    # common_secrets declares the optional default source for a slot. An
    # agent_secrets record either replaces that source for one agent or
    # explicitly disables the slot for that agent.
    common = manifest.get("common_secrets")
    defaults = {}
    if not _falsy(common):
        if isinstance(common, list):
            for slot in _word_list(common, "common_secrets"):
                if slot not in secret_slots:
                    raise ManifestError(
                        f"common_secrets slot '{slot}': no enabled plugin declares that secret slot")
                defaults[slot] = slot
        elif isinstance(common, dict):
            for slot, source in common.items():
                source = _scalar(source, f"common_secrets.{slot}")
                if slot not in secret_slots:
                    raise ManifestError(
                        f"common_secrets slot '{slot}': no enabled plugin declares that secret slot")
                if not REF_RE.match(source):
                    raise ManifestError(
                        f"common_secrets slot '{slot}': source '{source}' is not a valid env var name")
                defaults[slot] = source
        else:
            raise ManifestError("manifest common_secrets: must be a list of slots or a map of SLOT: source")
    for slot, source in list(defaults.items()):
        if source not in present_vars:
            plugin, hint = secret_slots[slot]
            detail = hint or f"plugin '{plugin}'"
            print(f"  ⚠ common secret '{source}' not in {secrets_file} — {detail} will not authenticate",
                  file=sys.stderr)
            del defaults[slot]

    explicit = manifest.get("agent_secrets")
    explicit_bindings = []
    if not _falsy(explicit):
        if not isinstance(explicit, list):
            raise ManifestError(
                "manifest agent_secrets: must be a list of {agent, slot, secret} overrides or {agent, slot, disabled: true}")
        for rec in explicit:
            if not isinstance(rec, dict):
                raise ManifestError(
                    "agent_secrets: each entry must be a map with agent, slot, and exactly one of secret or disabled: true")
            agent = _scalar(rec.get("agent"), "agent_secrets.agent")
            slot = _scalar(rec.get("slot"), "agent_secrets.slot")
            has_secret = "secret" in rec
            disabled = rec.get("disabled", False)
            if not isinstance(disabled, bool):
                raise ManifestError("agent_secrets.disabled must be true or false")
            if not agent or not slot or has_secret == disabled:
                raise ManifestError(
                    "agent_secrets: each entry needs agent, slot, and exactly one of secret or disabled: true")
            source = _scalar(rec.get("secret"), "agent_secrets.secret") if has_secret else ""
            if has_secret and not source:
                raise ManifestError("agent_secrets.secret must be a non-empty env var name")
            extra = ",".join(k for k in rec if k not in ("agent", "slot", "secret", "disabled"))
            if extra:
                raise ManifestError(
                    f"agent_secrets: unsupported field(s): {extra} (only agent, slot, secret, disabled)")
            explicit_bindings.append((agent, slot, None if disabled else source))

    seen_binds = set()
    overrides = {}
    for agent, slot, source in sugar_bindings + explicit_bindings:
        if agent not in agent_names:
            raise ManifestError(
                f"agent_secrets: unknown agent '{agent}' (one of {', '.join(sorted(agent_names))})")
        if slot not in secret_slots:
            raise ManifestError(
                f"agent_secrets: slot '{slot}' is not a secret of any enabled plugin")
        if source is not None and source not in present_vars:
            raise ManifestError(
                f"agent_secrets: secret '{source}' (for {agent}/{slot}) not found in {secrets_file} "
                "(agent_secrets sources must be non-empty variables)")
        if (agent, slot) in seen_binds:
            raise ManifestError(f"agent_secrets: {agent} is bound to slot '{slot}' more than once")
        seen_binds.add((agent, slot))
        overrides[(agent, slot)] = source

    enabled_agents = []
    seen_enabled_bins = set()
    for tool in tools:
        if not isinstance(tool, str):
            continue
        spec = agents.get(tool)
        if spec is None or spec["mcp"] is None:
            continue
        binary = spec["binary"]
        if binary in seen_enabled_bins:
            continue
        seen_enabled_bins.add(binary)
        enabled_agents.append(binary)

    # Explicit overrides retain manifest order. Defaults fill every enabled
    # agent not overridden or disabled, so a disabled entry intentionally
    # leaves no key or server configuration behind.
    agent_secret_records = []
    for agent, slot, source in sugar_bindings + explicit_bindings:
        if source is not None:
            agent_secret_records.append((agent, slot, source))
    for slot, source in defaults.items():
        for agent in enabled_agents:
            if (agent, slot) in overrides:
                continue
            agent_secret_records.append((agent, slot, source))
    out["AGENT_SECRETS"] = "".join(f"{a}\t{s}\t{src}\n" for a, s, src in agent_secret_records)

    # An enabled plugin slot with no effective credential is inert. Warn rather
    # than fail: listing a plugin before adding a default/override is deliberate
    # for some manifests.
    bound_slots = {slot for _, slot, _ in agent_secret_records}
    for slot, (plugin, _) in secret_slots.items():
        if slot not in bound_slots:
            print(f"  ⚠ plugin '{plugin}' declares slot {slot} but no common default or "
                  "agent override enables it — it is inert (wired for no agent)",
                  file=sys.stderr)

    # All plugin credentials are now composed as effective per-agent records.
    # Keep the output variable empty for the stable up.sh/keyfiles interface.
    out["PLUGIN_ENV_SECRETS"] = ""

    # ── remote.notify: ntfy egress + env passthrough ────────────────────
    ntfy_url = ""
    ntfy_topic = ""
    if remote_notify == "ntfy":
        ntfy_url = env.get("NTFY_URL") or ""
        if not ntfy_url:
            raise ManifestError(
                f"manifest has remote.notify: ntfy but NTFY_URL is missing from {secrets_file}")
        if any(c in ntfy_url for c in ('#', '"', "'")):
            raise ManifestError(
                "NTFY_URL must be a bare origin (no '#', quotes) — put the topic in NTFY_TOPIC")
        # Host = URL minus scheme, path, userinfo, port — the path strip must
        # precede the userinfo strip so an '@' in a path can't masquerade as
        # userinfo (same order as the old sed).
        host = re.sub(r"^[A-Za-z]+://", "", ntfy_url)
        host = re.sub(r"/.*$", "", host)
        host = re.sub(r"^.*@", "", host)
        host = re.sub(r":[0-9]+$", "", host)
        if not host:
            raise ManifestError(f"cannot parse a host from NTFY_URL '{ntfy_url}'")
        if IPV4_RE.match(host):
            # IP literal: the domain allowlist is dnsmasq-driven, so an IP
            # host must go through the CIDR path or the push is firewalled.
            if f"{host}/32" not in cidr_items:
                cidr_items.append(f"{host}/32")
        else:
            add_egress_domain(host)
        ntfy_topic = env.get("NTFY_TOPIC") or ""
    out["CONTAINER_NTFY_URL"] = ntfy_url
    out["CONTAINER_NTFY_TOPIC"] = ntfy_topic

    out["EGRESS"] = ",".join(egress_items)
    out["EGRESS_CIDRS"] = ",".join(cidr_items)
    return out


def read_stdin_docs(stream):
    """Line 1: manifest JSON. Then '<name>\\t<json>' per plugins/*/plugin.yml file,
    with '!' in place of the JSON when yq could not parse the file. Then a
    literal '---agents---' line, then '<name>\\t<json>' per agents/*/agent.yml."""
    first = stream.readline()
    if not first.strip():
        raise ManifestError("no manifest JSON on stdin")
    try:
        manifest = json.loads(first)
    except ValueError as e:
        raise ManifestError(
            f"manifest did not convert to valid JSON ({e}) — is the manifest YAML valid? (see any yq error above)")
    # yq maps an EMPTY yaml file to null; treat as empty manifest.
    if manifest is None:
        manifest = {}
    plugin_files = {}
    agent_files = {}
    in_agents = False
    saw_agents_sentinel = False
    for line in stream:
        if not line.strip():
            continue
        if line.strip() == "---agents---":
            if saw_agents_sentinel:
                raise ManifestError("agents section appears more than once on stdin")
            saw_agents_sentinel = True
            in_agents = True
            continue
        name, sep, doc = line.partition("\t")
        if not sep:
            raise ManifestError(
                f"unexpected document after the manifest (a stray '---' making it multi-document?): {line.strip()[:120]}")
        if doc.strip() == "!":
            if in_agents:
                raise ManifestError(
                    f"agent file '{name}' is unreadable on stdin (yq could not parse agents/{name}/agent.yml)")
            plugin_files[name] = UNREADABLE
            continue
        try:
            parsed = json.loads(doc)
        except ValueError as e:
            kind = "agent" if in_agents else "plugin"
            raise ManifestError(f"{kind} file '{name}' is not valid JSON ({e})")
        if in_agents:
            agent_files[name] = parsed
        else:
            plugin_files[name] = parsed
    if not saw_agents_sentinel:
        raise ManifestError("agents section missing — up.sh and manifest.py are out of sync")
    if not agent_files:
        raise ManifestError("agents section is empty — expected at least one agent descriptor")
    return manifest, plugin_files, agent_files


def main(argv):
    if "--derive" not in argv:
        print("Error: manifest.py requires --derive (see module docstring)", file=sys.stderr)
        return 2
    try:
        manifest, plugin_files, agent_files = read_stdin_docs(sys.stdin)
        derived = derive(manifest, plugin_files, agent_files, os.environ)
    except ManifestError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(derived.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
