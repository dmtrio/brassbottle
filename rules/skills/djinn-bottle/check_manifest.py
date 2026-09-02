#!/usr/bin/env python3
"""check_manifest.py — pre-flight a djinn container manifest before it ships.

Two independent passes, both reported in one run:

  1. REAL VALIDATION. Feeds the draft to brassbottle's own src/manifest.py —
     the exact code up.sh runs — so a manifest that would abort bring-up on
     the Mac fails here instead. Secret VALUES never exist in a container, so
     every secret var the draft NAMES is declared present: this pass checks
     structure and references, never whether your secrets.env is populated.
     manifest.py's own '⚠' lines are surfaced as warnings whether it exits 0
     or not — an inert plugin (a slot no agent binds) is reported on success,
     and that is exactly the misconfiguration worth catching.
     Skipped with a loud note when no brassbottle checkout is reachable.

  2. HOST-PORT COLLISION SCAN. manifest.py validates one manifest in
     isolation and cannot see the fleet, but host ports are exclusive among
     RUNNING containers: two claiming ssh 2222 or browser 8814 both come up
     and the second one's publish silently fails. Every sibling manifest is
     read for its effective ports — explicit (ssh.port, plugin_ports) AND
     implicit (a plugin's host_port default in
     plugins/<name>/plugin.yml, which a manifest never mentions, plus the
     deprecated capabilities: {gateway|proxyman|browser} sugar).

     Severity follows what is knowable. A clash with a SIBLING is a warning:
     nothing here knows whether the two ever run at once, and a fleet may
     deliberately share a port between containers that never run together.
     A manifest clashing with ITSELF is an error — that container can never
     come up, whatever else is running.

Exit status: 0 clean (warnings allowed), 1 errors found, 2 could not run.

YAML is parsed by yq (baked into the image at /usr/local/bin/yq, and the same
converter up.sh uses) rather than PyYAML, which is not guaranteed present.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# The browser launcher derives a container's Chrome debug port from its bridge
# port. Documented in TEMPLATE.yml as the reason to keep a bridge below 9222.
BROWSER_DEBUG_OFFSET = 408
BROWSER_BRIDGE_CEILING = 9222
# Deprecated capabilities: flags that manifest.py still folds into plugins:,
# host ports and all (manifest.py: "sugar_plugins").
CAPABILITY_SUGAR = ("gateway", "proxyman", "browser")

BRASSBOTTLE_CANDIDATES = (
    "/workspace/repos/brassbottle",
    os.path.expanduser("~/git/brassbottle"),
)


class Unusable(Exception):
    """The checker cannot run at all (missing yq, unreadable draft)."""


# ── YAML in ──────────────────────────────────────────────────────────────────

def yq_json(path):
    """YAML file → Python object, via yq. Raises Unusable on a parse failure."""
    if not shutil.which("yq"):
        raise Unusable("yq not found on PATH (expected /usr/local/bin/yq)")
    try:
        out = subprocess.run(["yq", "-o=json", "-I=0", str(path)],
                             capture_output=True, text=True, check=True).stdout
    except subprocess.CalledProcessError as e:
        detail = " ".join((e.stderr or "").split()) or "yq exited %d" % e.returncode
        raise Unusable("%s: not valid YAML — %s" % (path, detail))
    out = out.strip()
    if not out:
        return {}
    # -I=0 puts each YAML document on its own line, so >1 line means the file
    # holds a '---' separator. json.loads would raise "Extra data"; manifest.py
    # has its own error for this, but the sibling scan must degrade to a skip.
    if len(out.splitlines()) > 1:
        raise Unusable("%s: multiple YAML documents (a stray '---'?) — "
                       "a manifest is exactly one document" % path)
    try:
        doc = json.loads(out)
    except json.JSONDecodeError as e:
        raise Unusable("%s: yq produced non-JSON output — %s" % (path, e))
    # A manifest must be a mapping. yq happily emits a list or a bare scalar,
    # and manifest.py's own type errors read better than a traceback here.
    return doc if isinstance(doc, dict) else {}


def _get(mapping, *path, default=None):
    """Nested .get that survives a section written as the wrong YAML type."""
    cur = mapping
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def as_port(value):
    """A port as manifest.py sees it, or None.

    manifest.py runs ssh.port and plugin_ports values through _scalar, which
    passes a STRING through untouched — `port: "2223"` reaches the host as
    2223. Treating only int as a port would silently miss that collision."""
    if isinstance(value, bool):
        return None                      # YAML `port: true` is not a port
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def flag_true(value):
    """Mirror manifest.py's _raw_flag: anything rendering as 'true' is on,
    so `browser: "true"` (a quoted string) enables it just as `browser: true`
    does."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    return str(value).strip().lower() == "true"


# ── Plugin host-port defaults ────────────────────────────────────────────────

def plugin_host_ports(brassbottle):
    """{plugin name: default host port} from plugins/<name>/plugin.yml.

    A plugin with no host_port (a local stdio plugin like serena) is absent
    from the result — it claims nothing on the host."""
    ports = {}
    root = os.path.join(brassbottle, "plugins")
    if not os.path.isdir(root):
        return ports
    for name in sorted(os.listdir(root)):
        spec = os.path.join(root, name, "plugin.yml")
        if not os.path.isfile(spec):
            continue
        try:
            port = as_port(_get(yq_json(spec), "host_port"))
        except Unusable:
            continue        # a broken/WIP plugin file must not block the scan
        if port is not None:
            ports[name] = port
    return ports


# ── Effective ports of one manifest ──────────────────────────────────────────

def enabled_plugins(manifest):
    """plugins: plus the deprecated capabilities: sugar, in manifest.py order.

    manifest.py still appends gateway/proxyman/browser to the plugin list when
    the capabilities flag is set, so those containers really do claim the host
    port — a scan that reads only plugins: would miss it entirely."""
    listed = _get(manifest, "plugins", default=[])
    names = [p for p in listed if isinstance(p, str)] \
        if isinstance(listed, list) else []
    caps = _get(manifest, "capabilities", default={})
    if isinstance(caps, dict):
        for cap in CAPABILITY_SUGAR:
            if flag_true(caps.get(cap)) and cap not in names:
                names.append(cap)
    return names


def effective_ports(manifest, defaults):
    """Host ports this manifest claims.

    Returns tcp: port → [what claims it], a LIST because one manifest can
    claim the same port twice (ssh.port 8811 with the gateway plugin, say)
    and that self-collision is the one this script can call fatal. Bottles
    publish no UDP (mosh lives on the jump), so TCP is the whole story."""
    tcp = {}

    def claim(port, source):
        tcp.setdefault(port, []).append(source)

    ssh_port = as_port(_get(manifest, "ssh", "port"))
    if ssh_port is not None:
        claim(ssh_port, "ssh.port")

    overrides = _get(manifest, "plugin_ports", default={})
    if not isinstance(overrides, dict):
        overrides = {}
    for name in enabled_plugins(manifest):
        port = as_port(overrides[name]) if name in overrides \
            else defaults.get(name)
        if port is None:
            continue
        source = "plugin_ports.%s" % name if name in overrides \
            else "plugin %s (default)" % name
        claim(port, source)
        if name == "browser":
            claim(port + BROWSER_DEBUG_OFFSET,
                  "browser debug (%d+%d)" % (port, BROWSER_DEBUG_OFFSET))
    return tcp


# ── Pass 2: collision scan ───────────────────────────────────────────────────

def scan_collisions(draft_name, draft, siblings, defaults):
    """siblings: {container name: manifest}. Returns (errors, warnings)."""
    errors, warnings = [], []
    mine_tcp = effective_ports(draft, defaults)

    # Self-collision: fatal regardless of what else is running.
    for port, sources in sorted(mine_tcp.items()):
        if len(sources) > 1:
            errors.append("host TCP port %d is claimed twice by this manifest: %s"
                          % (port, " and ".join(sources)))

    # Sibling collisions: a warning, because concurrency is unknowable here.
    for name, other in sorted(siblings.items()):
        if name == draft_name:
            continue
        their_tcp = effective_ports(other, defaults)
        for port, sources in sorted(mine_tcp.items()):
            if port in their_tcp:
                warnings.append(
                    "host TCP port %d: %s here also claimed by %s in %s.yml — "
                    "fine only if the two never run at the same time"
                    % (port, sources[0], their_tcp[port][0], name))

    bridge = next((p for p, sources in mine_tcp.items()
                   if any(s.startswith(("plugin_ports.browser", "plugin browser"))
                          for s in sources)), None)
    if bridge is not None and bridge >= BROWSER_BRIDGE_CEILING:
        errors.append(
            "browser bridge port %d is at or above %d — its derived Chrome "
            "debug port (%d) can collide with another container's"
            % (bridge, BROWSER_BRIDGE_CEILING, bridge + BROWSER_DEBUG_OFFSET))

    task = _get(draft, "task")
    if isinstance(task, str) and task and task != draft_name:
        warnings.append(
            "task: '%s' does not match the filename '%s.yml'. TEMPLATE.yml "
            "documents task: as an informational label, so this may well be "
            "deliberate — coding bottles typically keep the two equal, appliance "
            "bottles often do not. Leave it alone unless the "
            "mismatch is accidental" % (task, draft_name))
    return errors, warnings


# ── Pass 1: the real validator ───────────────────────────────────────────────

def find_brassbottle(explicit=None):
    """An explicit path that isn't a checkout is an error, never a fallback.

    Falling back would run the weaker no-checkout path under a note the caller
    has no reason to read, and report 'no collisions' for a fleet it never
    scanned — the failure mode this whole script exists to prevent."""
    if explicit:
        if os.path.isfile(os.path.join(explicit, "src", "manifest.py")):
            return explicit
        raise Unusable("--brassbottle %s has no src/manifest.py" % explicit)
    for cand in BRASSBOTTLE_CANDIDATES:
        if os.path.isfile(os.path.join(cand, "src", "manifest.py")):
            return cand
    return None


def secret_refs(manifest):
    """Every secrets.env var NAME the manifest references.

    Declared present when calling manifest.py: a container has no secrets.env,
    so the alternative is every manifest failing on secrets that are in fact
    set on the Mac. Structure is checked; provisioning is the host's job."""
    names = set()
    token = _get(manifest, "git", "token")
    if isinstance(token, str) and token:
        names.add(token)
    orgs = _get(manifest, "git", "orgs", default={})
    if isinstance(orgs, dict):
        for spec in orgs.values():
            tok = _get(spec, "token") if isinstance(spec, dict) else None
            if isinstance(tok, str) and tok:
                names.add(tok)
    common = _get(manifest, "common_secrets", default={})
    if isinstance(common, dict):
        names.update(v for v in common.values() if isinstance(v, str) and v)
    elif isinstance(common, list):
        names.update(v for v in common if isinstance(v, str) and v)
    for entry in _get(manifest, "agent_secrets", default=[]) or []:
        src = _get(entry, "secret") if isinstance(entry, dict) else None
        if isinstance(src, str) and src:
            names.add(src)
    # Deprecated identities: sugar — manifest.py derives OBSIDIAN_KEY_<ref> /
    # OBSIDIAN_WATCH_KEY_<ref> and demands them present, so a manifest still
    # using it would otherwise fail here for secrets it never names outright.
    ids = _get(manifest, "identities", default={})
    if isinstance(ids, dict):
        for key, prefix in (("obsidian", "OBSIDIAN_KEY"),
                            ("watch", "OBSIDIAN_WATCH_KEY")):
            refs = ids.get(key)
            refs = refs if isinstance(refs, list) else \
                (refs.split() if isinstance(refs, str) else [])
            names.update("%s_%s" % (prefix, r) for r in refs if isinstance(r, str))
    return names


def _split_validator_output(stderr):
    """manifest.py writes '⚠' advisories and 'Error:' failures to one stream."""
    errors, warnings = [], []
    for line in (stderr or "").splitlines():
        if not line.strip():
            continue
        (warnings if "⚠" in line else errors).append(line.strip())
    return errors, warnings


def _descriptor_lines(root, filename):
    """name\t<one-line json> per <root>/<dir>/<filename>, '!' if unreadable —
    the same encoding up.sh streams to manifest.py --derive."""
    lines = []
    names = sorted(os.listdir(root)) if os.path.isdir(root) else []
    for name in names:
        spec = os.path.join(root, name, filename)
        if not os.path.isfile(spec):
            continue
        proc = subprocess.run(["yq", "-o=json", "-I=0", spec],
                              capture_output=True, text=True)
        doc = proc.stdout.strip()
        # '!' marks unreadable, exactly as up.sh does: manifest.py errors on it
        # only if THIS manifest lists that plugin/agent.
        lines.append("%s\t%s" % (name, doc if proc.returncode == 0
                                 and len(doc.splitlines()) == 1 else "!"))
    return lines


def build_derive_payload(draft_path, brassbottle):
    """The stdin manifest.py --derive expects: the manifest as one-line json,
    plugin descriptor lines, an ---agents--- separator, agent descriptor
    lines. Mirrors the DERIVED block in up.sh."""
    lines = [subprocess.run(["yq", "-o=json", "-I=0", str(draft_path)],
                            capture_output=True, text=True,
                            check=True).stdout.strip()]
    lines += _descriptor_lines(os.path.join(brassbottle, "plugins"),
                               "plugin.yml")
    lines.append("---agents---")
    lines += _descriptor_lines(os.path.join(brassbottle, "agents"),
                               "agent.yml")
    return lines


def run_real_validator(draft_path, draft, brassbottle):
    """Returns (errors, warnings) from brassbottle's own manifest.py."""
    manifest_py = os.path.join(brassbottle, "src", "manifest.py")
    lines = build_derive_payload(draft_path, brassbottle)

    refs = secret_refs(draft)
    env = dict(os.environ)
    env.update({
        "PRESENT_SECRET_VARS": " ".join(sorted(refs)),
        "GH_TOKEN_VARS": " ".join(sorted(n for n in refs
                                         if n.startswith("GH_TOKEN"))),
        "SECRETS_FILE": "<host secrets.env — not readable from a container>",
        "GIT_NAME_DEFAULT": "checker", "GIT_EMAIL_DEFAULT": "checker@example",
        # Only consumed when the manifest asks for ntfy; a placeholder keeps
        # remote.notify from failing on the host's value being absent here.
        "NTFY_URL": os.environ.get("NTFY_URL") or "https://ntfy.example",
        "NTFY_TOPIC": os.environ.get("NTFY_TOPIC") or "checker",
    })
    proc = subprocess.run([sys.executable, manifest_py, "--derive"],
                          input="\n".join(lines) + "\n",
                          capture_output=True, text=True, env=env)
    errors, warnings = _split_validator_output(proc.stderr)
    if proc.returncode != 0 and not errors:
        errors = ["manifest.py exited %d with no message" % proc.returncode]
    elif proc.returncode == 0:
        errors = []          # advisories only; a clean exit is a clean exit
    return errors, warnings


# ── Entry point ──────────────────────────────────────────────────────────────

def check(draft_path, manifests_dir=None, brassbottle=None):
    """Returns (errors, warnings, notes)."""
    errors, warnings, notes = [], [], []
    draft_path = os.path.abspath(draft_path)
    if not os.path.isfile(draft_path):
        raise Unusable("no such manifest: %s" % draft_path)
    draft_name = os.path.basename(draft_path)[:-len(".yml")] \
        if draft_path.endswith(".yml") else os.path.basename(draft_path)
    draft = yq_json(draft_path)

    bb = find_brassbottle(brassbottle)
    defaults = {}
    if bb:
        defaults = plugin_host_ports(bb)
        errs, warns = run_real_validator(draft_path, draft, bb)
        errors.extend(errs)
        warnings.extend(warns)
        notes.append("validated against %s/src/manifest.py" % bb)
    else:
        notes.append(
            "no brassbottle checkout found — skipped real validation AND the "
            "implicit plugin-port defaults, so a collision on an unstated "
            "port (browser 8814, gateway 8811, …) would go unseen. Point "
            "--brassbottle at a brassbottle checkout to close that gap.")

    sibling_dir = manifests_dir or os.path.dirname(draft_path)
    if not os.path.isdir(sibling_dir):
        raise Unusable("no such manifests directory: %s" % sibling_dir)
    siblings = {}
    for fname in sorted(os.listdir(sibling_dir)):
        if not fname.endswith(".yml") or fname == "TEMPLATE.yml":
            continue
        path = os.path.join(sibling_dir, fname)
        if os.path.abspath(path) == draft_path:
            continue
        try:
            siblings[fname[:-len(".yml")]] = yq_json(path)
        except Unusable as e:
            warnings.append("skipped sibling %s: %s" % (fname, e))
    notes.append("scanned %d sibling manifest(s) in %s"
                 % (len(siblings), sibling_dir))

    errs, warns = scan_collisions(draft_name, draft, siblings, defaults)
    errors.extend(errs)
    warnings.extend(warns)
    return errors, warnings, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", help="the draft <name>.yml to check")
    ap.add_argument("--manifests-dir",
                    help="directory of sibling manifests to scan for port "
                         "collisions (default: the draft's own directory)")
    ap.add_argument("--brassbottle",
                    help="brassbottle checkout supplying src/manifest.py and "
                         "plugins/*/plugin.yml")
    args = ap.parse_args(argv)

    try:
        errors, warnings, notes = check(args.manifest, args.manifests_dir,
                                        args.brassbottle)
    except Unusable as e:
        print("cannot check: %s" % e, file=sys.stderr)
        return 2

    for note in notes:
        print("note: %s" % note)
    for warn in warnings:
        print("warning: %s" % warn)
    for err in errors:
        print("error: %s" % err, file=sys.stderr)
    print("%s: %d error(s), %d warning(s)"
          % (os.path.basename(args.manifest), len(errors), len(warnings)))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
