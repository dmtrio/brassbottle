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
    input (env):    SECRET_KEY_VARS  — space-separated names of the non-empty
                                       OBSIDIAN_KEY_* / OBSIDIAN_WATCH_KEY_*
                                       vars currently defined (names only —
                                       secret VALUES never reach this process
                                       beyond NTFY_URL/NTFY_TOPIC below)
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
  plugins", `repos: false` means no repos, `tools: false` gets the default
  set. The old scalar `repo:` key is rejected outright (layout v2).
- The old `tools | contains(["claude"])` had jq's SUBSTRING semantics for
  strings inside arrays (tools: [claude-code] enabled claude too). Ported
  as-is; tightening it is a deliberate future change, not a port surprise.
- null entries inside word-split lists (plugins, identity refs) vanish, the
  way the old `join(" ")` + word splitting dropped them — so a trailing
  flow-style comma (`plugins: [serena,]`) keeps working. Comma-joined lists
  (egress, egress_cidrs) keep their empty slots byte-for-byte.
- agent_for_ref suffix matching preserves the old case-statement ORDER:
  _cursor_agent wins before _claude/_codex/_pi/_gemini can match.
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
# Directory name under /workspace/repos/<name> — no slash, no leading dot/dash.
REPO_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
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

DEFAULT_TOOLS = ["claude", "codex", "pi", "gemini", "cursor", "aider"]

# Suffix → agent, in the OLD case-statement order (first match wins).
AGENT_SUFFIXES = (
    ("_cursor_agent", "cursor-agent"),
    ("_claude", "claude"),
    ("_codex", "codex"),
    ("_pi", "pi"),
    ("_gemini", "gemini"),
)

# The MCP-capable agents an agent_secrets binding may name.
AGENT_NAMES = frozenset({"claude", "codex", "pi", "gemini", "cursor-agent"})

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


def agent_for_ref(ref):
    for suffix, agent in AGENT_SUFFIXES:
        if ref.endswith(suffix):
            return agent
    return ""


def _tool_installed(tools, name):
    # jq contains(): a string element "contains" the needle as a SUBSTRING.
    return any(isinstance(t, str) and name in t for t in tools)


def _parse_secret(val, plugin, slot):
    """A plugin secret slot value: either a bare scope string ('env'/'agent')
    or a map {scope: ..., hint: ..., global: ...}. Returns (scope, hint,
    is_global). The hint is shown verbatim in up.sh's 'not in secrets.env'
    warning, so it must be a single line with no tab (PLUGIN_ENV_SECRETS is
    tab/newline-delimited). global: true (agent scope only) means the slot also
    accepts a global env var named after the slot as a per-agent fallback — an
    agent with no explicit agent_secrets binding is wired from that global token
    when it is set (the fallback synthesis in derive(), keyed on
    agent_global_slots)."""
    is_global = False
    if isinstance(val, str):
        scope, hint = val, ""
    elif isinstance(val, dict):
        scope = val.get("scope")
        hint = val.get("hint", "")
        if not isinstance(hint, str):
            raise ManifestError(f"plugin '{plugin}' secret '{slot}': hint must be a string")
        raw_global = val.get("global", False)
        if not isinstance(raw_global, bool):
            raise ManifestError(f"plugin '{plugin}' secret '{slot}': global must be true or false")
        is_global = raw_global
    else:
        raise ManifestError(
            f"plugin '{plugin}' secret '{slot}': must be a scope ('env'/'agent') or a map with a scope: key")
    if scope not in ("env", "agent"):
        got = "no scope:" if scope is None else f"'{scope}'"
        raise ManifestError(
            f"plugin '{plugin}' secret '{slot}': scope must be 'env' or 'agent' (got {got})")
    if is_global and scope != "agent":
        raise ManifestError(
            f"plugin '{plugin}' secret '{slot}': global: true is only valid with scope: agent "
            "(a global fallback fills in agents that have no per-agent key)")
    if "\t" in hint or "\n" in hint:
        raise ManifestError(f"plugin '{plugin}' secret '{slot}': hint must be a single line (no tab/newline)")
    return scope, hint, is_global


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


def derive(manifest, plugin_files, env):
    """The whole old 'Read manifest' section of up.sh as one function.
    manifest: parsed manifest JSON; plugin_files: {name: parsed plugin JSON,
    or UNREADABLE for a file yq couldn't parse} for every file shipped under
    plugins/; env: os.environ-like mapping."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a YAML mapping")
    out = Derived()
    secrets_file = env.get("SECRETS_FILE", "secrets.env")

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

    # ── Tools ───────────────────────────────────────────────────────────
    tools = manifest.get("tools")
    if _falsy(tools):
        tools = DEFAULT_TOOLS
    if not isinstance(tools, list):
        raise ManifestError("manifest tools: must be a list")
    for var, name in (("INSTALL_CLAUDE", "claude"), ("INSTALL_CODEX", "codex"),
                      ("INSTALL_PI", "pi"), ("INSTALL_GEMINI", "gemini"),
                      ("INSTALL_CURSOR", "cursor"), ("INSTALL_AIDER", "aider")):
        out[var] = "true" if _tool_installed(tools, name) else "false"

    # ── Capabilities: egress firewall keys stay; gateway/proxyman/browser are
    #    deprecated sugar for the equivalent plugins (PLN - Plugins v2). ──────
    caps = _section(manifest, "capabilities")
    egress_items = _comma_list(caps.get("egress"), "capabilities.egress")
    cidr_items = _comma_list(caps.get("egress_cidrs"), "capabilities.egress_cidrs")
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
    secret_vars = set((env.get("SECRET_KEY_VARS") or "").split())
    identity_errors = []
    sugar_bindings = []  # (agent, slot, source) from identities:

    def check_ref(kind, slot, prefix, ref):
        if not REF_RE.match(ref):
            identity_errors.append(
                f"  {kind} ref '{ref}': illegal characters (allowed: letters, digits, underscore)")
            return
        agent = agent_for_ref(ref)
        if not agent:
            identity_errors.append(
                f"  {kind} ref '{ref}': suffix is not a known agent (_claude/_codex/_pi/_gemini/_cursor_agent)")
            return
        var = f"{prefix}_{ref}"
        if var not in secret_vars:
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
    # type: field, decides. host_port: is legal only with a remote server;
    # install: is required iff a local server (the Dockerfile bakes it). A
    # plugin that declares an AGENT-scoped secret is an "agent-server" plugin:
    # its (single, remote) server is wired per bound agent, so it is routed to
    # servers_by_slot instead of the uniform plugin_mcp_entries.
    plugin_mcp_entries = []
    seen_server_names = set()
    host_ports = []
    env_slots = {}         # SLOT -> (plugin, hint)  env-scoped secrets
    agent_slots = {}       # SLOT -> plugin          agent-scoped secrets
    agent_global_slots = {}  # SLOT -> global env var  agent slots w/ global: true
    servers_by_slot = {}   # agent SLOT -> {"name": ..., "spec": {...}}
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

        # Secrets first: knowing which of this plugin's slots are agent-scoped
        # decides how its mcp servers are routed below.
        secrets = doc.get("secrets")
        secrets = {} if _falsy(secrets) else secrets
        if not isinstance(secrets, dict):
            raise ManifestError(f"plugin '{p}' secrets must be a map of SLOT: scope")
        this_agent_slots = []
        for slot, val in secrets.items():
            if not REF_RE.match(slot):
                raise ManifestError(
                    f"plugin '{p}' secret slot '{slot}': illegal characters (must be a shell env var name)")
            if slot in env_slots or slot in agent_slots:
                raise ManifestError(f"secret slot '{slot}' is declared by more than one enabled plugin")
            scope, hint, is_global = _parse_secret(val, p, slot)
            if scope == "env":
                env_slots[slot] = (p, hint)
            else:
                agent_slots[slot] = p
                this_agent_slots.append(slot)
                if is_global:
                    # Global fallback var is the slot itself (the ${SLOT} header
                    # ref) — one token users set in secrets.env for every agent.
                    # NOTE: up.sh's SECRET_KEY_VARS scan must include this var
                    # name, else it never reaches secret_vars and the fallback
                    # silently no-ops. Today only AXIOM_TOKEN is a global slot and
                    # up.sh scans it explicitly; a new global slot needs the scan
                    # widened in lockstep (pinned by tests/plugins.test.sh).
                    agent_global_slots[slot] = slot

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
            if is_local:
                has_local = True
                if not isinstance(spec.get("command"), str):
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': command must be a string (local stdio server)")
                extra = ",".join(k for k in spec if k not in ("command", "args"))
                if extra:
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': unsupported field(s) for a local server: {extra} (only command and args)")
            else:
                has_remote = True
                if not isinstance(spec.get("url"), str):
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': url must be a string (remote http server)")
                headers = spec.get("headers", {})
                if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': headers must be a map of string values")
                extra = ",".join(k for k in spec if k not in ("url", "headers"))
                if extra:
                    raise ManifestError(
                        f"plugin '{p}' mcp server '{n}': unsupported field(s) for a remote server: {extra} (only url and headers)")
            if n in seen_server_names:
                raise ManifestError(
                    f"multiple enabled plugins define the same MCP server name: {n}")
            seen_server_names.add(n)

        install = doc.get("install")
        if has_local and (_falsy(install) or not isinstance(install, str) or not install.strip()):
            raise ManifestError(
                f"plugin '{p}': a local (command:) server needs an install: block (baked into the image)")

        hp = doc.get("host_port")
        if not _falsy(hp):
            if not has_remote:
                raise ManifestError(
                    f"plugin '{p}': host_port is only valid with a remote (url:) server")
            if isinstance(hp, bool) or not isinstance(hp, int):
                raise ManifestError(f"plugin '{p}': host_port must be an integer port number")
            if not 1 <= hp <= 65535:
                raise ManifestError(f"plugin '{p}': host_port {hp} out of range (1-65535)")
            host_ports.append(hp)

        # Route the plugin's mcp servers. Agent-scoped → per-agent wiring
        # (servers_by_slot); everything else → uniform plugin_mcp_entries.
        if this_agent_slots:
            if len(this_agent_slots) > 1:
                raise ManifestError(
                    f"plugin '{p}': an agent-scoped plugin may declare only one agent secret slot (got {', '.join(this_agent_slots)})")
            if len(mcp) > 1:
                raise ManifestError(
                    f"plugin '{p}': an agent-scoped plugin may define at most one mcp server (got {len(mcp)})")
            slot = this_agent_slots[0]
            # The server may be remote (url:, per-agent literal header) OR local
            # (command:, per-agent stdio bridge — e.g. axiom's mcp-remote). Both
            # route here so the per-agent binding gates which agents get it; the
            # shape was already validated in the mcp loop above.
            for n, spec in mcp.items():
                servers_by_slot[slot] = {"name": n, "spec": spec if isinstance(spec, dict) else {}}
        else:
            # One line of compact JSON per plugin — the --build-payload contract.
            plugin_mcp_entries.append(json.dumps(mcp, separators=(",", ":"), ensure_ascii=False))
    out["PLUGIN_MCP_ENTRIES"] = "".join(e + "\n" for e in plugin_mcp_entries)
    # Sorted + deduped so the firewall grant string is order-independent of the
    # plugin list and two plugins sharing a port don't double up the grant.
    out["HOST_MCP_PORTS"] = ",".join(str(p) for p in sorted(set(host_ports)))
    # Agent-scoped server defs (slot → {name, spec}) for the wiring payload, and
    # the slots that actually have a server (env-only watch slots have none).
    out["AGENT_SERVERS_JSON"] = json.dumps(servers_by_slot, separators=(",", ":"), ensure_ascii=False)
    out["AGENT_SERVER_SLOTS"] = " ".join(servers_by_slot.keys())

    # ── agent_secrets: per-agent bindings for agent-scoped slots ────────────
    # Merge the identities: sugar bindings (validated above) with explicit
    # agent_secrets records; validate agent/slot/source and emit one
    # agent<tab>slot<tab>source record per line for up.sh to compose + wire.
    explicit = manifest.get("agent_secrets")
    explicit_bindings = []
    if not _falsy(explicit):
        if not isinstance(explicit, list):
            raise ManifestError("manifest agent_secrets: must be a list of {agent, slot, secret} records")
        for rec in explicit:
            if not isinstance(rec, dict):
                raise ManifestError("agent_secrets: each entry must be a map with agent, slot, secret")
            agent = _scalar(rec.get("agent"), "agent_secrets.agent")
            slot = _scalar(rec.get("slot"), "agent_secrets.slot")
            source = _scalar(rec.get("secret"), "agent_secrets.secret")
            if not agent or not slot or not source:
                raise ManifestError("agent_secrets: each entry needs agent, slot, and secret")
            explicit_bindings.append((agent, slot, source))

    seen_binds = set()
    agent_secret_records = []
    for agent, slot, source in sugar_bindings + explicit_bindings:
        if agent not in AGENT_NAMES:
            raise ManifestError(
                f"agent_secrets: unknown agent '{agent}' (one of {', '.join(sorted(AGENT_NAMES))})")
        if slot not in agent_slots:
            raise ManifestError(
                f"agent_secrets: slot '{slot}' is not an agent-scoped secret of any enabled plugin")
        if source not in secret_vars:
            # secret_vars is the set of per-agent key vars up.sh scanned from
            # secrets.env (OBSIDIAN_(WATCH_)?KEY_*, AXIOM_KEY_*, and a global
            # token like AXIOM_TOKEN) — name that scope so a set-but-unscanned
            # source var reads as a scope limit, not "you forgot to set it".
            raise ManifestError(
                f"agent_secrets: secret '{source}' (for {agent}/{slot}) not found in {secrets_file} "
                "(agent_secrets sources must be scanned key vars: OBSIDIAN_KEY_* / "
                "OBSIDIAN_WATCH_KEY_* / AXIOM_KEY_*, or a plugin's global token var)")
        if (agent, slot) in seen_binds:
            raise ManifestError(f"agent_secrets: {agent} is bound to slot '{slot}' more than once")
        seen_binds.add((agent, slot))
        agent_secret_records.append((agent, slot, source))

    # Global fallback (scope: agent, global: true): fill in every ENABLED agent
    # that has no explicit binding on the slot, sourced from the global token —
    # but only when that token is actually set (present in the scanned
    # secret_vars). This is what makes "set AXIOM_TOKEN → every agent gets axiom"
    # work; explicit per-agent agent_secrets above take precedence (seen_binds).
    enabled_agents = [a for a, t in (
        ("claude", "claude"), ("codex", "codex"), ("pi", "pi"),
        ("gemini", "gemini"), ("cursor-agent", "cursor"),
    ) if _tool_installed(tools, t)]
    for slot, gvar in agent_global_slots.items():
        if gvar not in secret_vars:
            continue
        for agent in enabled_agents:
            if (agent, slot) in seen_binds:
                continue
            seen_binds.add((agent, slot))
            agent_secret_records.append((agent, slot, gvar))
    out["AGENT_SECRETS"] = "".join(f"{a}\t{s}\t{src}\n" for a, s, src in agent_secret_records)

    # An enabled agent-scoped plugin with no binding is inert (wired for no
    # agent) — and, if it has a server, opens egress nothing will reach. Warn
    # rather than fail: listing the plugin without a binding may be deliberate.
    bound_slots = {slot for _, slot, _ in agent_secret_records}
    for slot, plugin in agent_slots.items():
        if slot not in bound_slots:
            print(f"  ⚠ plugin '{plugin}' declares agent-scoped slot {slot} but no "
                  "agent_secrets binding enables it — it is inert (wired for no agent)",
                  file=sys.stderr)

    # ── Env-scoped secret bindings (common_secrets) → required-secret plan ──
    # up.sh composes VALUES (it has secrets.env; this module never sees them):
    # each record is SLOT<tab>SOURCE<tab>HINT. A plugin env slot defaults to a
    # same-named source var; common_secrets (map) re-points it, or (list)
    # passes an extra var through by name.
    common = manifest.get("common_secrets")
    remap = {}
    passthrough = []
    if not _falsy(common):
        if isinstance(common, list):
            passthrough = _word_list(common, "common_secrets")
        elif isinstance(common, dict):
            for slot, source in common.items():
                remap[slot] = _scalar(source, f"common_secrets.{slot}")
        else:
            raise ManifestError("manifest common_secrets: must be a list of names or a map of SLOT: source")
    for slot, source in remap.items():
        if slot in agent_slots:
            raise ManifestError(
                f"common_secrets slot '{slot}' is agent-scoped — bind it under agent_secrets, not common_secrets")
        if slot not in env_slots:
            raise ManifestError(
                f"common_secrets slot '{slot}': no enabled plugin declares an env-scoped secret named '{slot}'")
        if not REF_RE.match(source):
            raise ManifestError(
                f"common_secrets slot '{slot}': source '{source}' is not a valid env var name")
    records = []
    for slot, (plugin, hint) in env_slots.items():
        records.append((slot, remap.get(slot, slot), hint))
    for name in passthrough:
        if name in env_slots:
            continue  # already covered as a plugin slot
        if not REF_RE.match(name):
            raise ManifestError(f"common_secrets passthrough '{name}' is not a valid env var name")
        records.append((name, name, ""))
    out["PLUGIN_ENV_SECRETS"] = "".join(f"{s}\t{src}\t{h}\n" for s, src, h in records)

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
    with '!' in place of the JSON when yq could not parse the file."""
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
    for line in stream:
        if not line.strip():
            continue
        name, sep, doc = line.partition("\t")
        if not sep:
            raise ManifestError(
                f"unexpected document after the manifest (a stray '---' making it multi-document?): {line.strip()[:120]}")
        if doc.strip() == "!":
            plugin_files[name] = UNREADABLE
            continue
        try:
            plugin_files[name] = json.loads(doc)
        except ValueError as e:
            raise ManifestError(f"plugin file '{name}' is not valid JSON ({e})")
    return manifest, plugin_files


def main(argv):
    if "--derive" not in argv:
        print("Error: manifest.py requires --derive (see module docstring)", file=sys.stderr)
        return 2
    try:
        manifest, plugin_files = read_stdin_docs(sys.stdin)
        derived = derive(manifest, plugin_files, os.environ)
    except ManifestError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(derived.render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
