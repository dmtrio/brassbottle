#!/usr/bin/env python3
"""version_audit.py — the read-only selector-surface audit (inventory/audit
layer, Dark read-only phase).

`djinn versions audit` checks the version-selection catalog (versions.yml)
against the repository it lives in:

  1. every location in version-locations.yml names a real catalog component;
  2. every non-external component owns at least one declared location;
  3. every declared selector location still exists, and every pinned
     location's anchor (the exact version/commit it pins) still appears in
     its file — deleting a declared location, or changing a version field on
     either the installer or the catalog side, fails;
  4. every agent/plugin descriptor directory (agents/*/agent.yml,
     plugins/*/plugin.yml) is owned by a catalog location — a new descriptor
     that enters the tree without a catalog record fails;
  5. no owned installer path carries a floating runtime launcher
     (`npx`/`uvx`) — a managed runtime command must exec a locally
     installed binary, never a live package launcher;
  6. scoped pattern discovery (the backstop): the installer surfaces listed
     in version-locations.yml are scanned for selector syntax, and hit lines
     are accounted one-to-one: each declared record with a `find` anchor is
     one consumable allowance for exactly one matching hit line in its file
     (longest anchor first, then location id; several records may carry the
     same find for repeated identical lines). A location path alone covers
     nothing, so a NEW selector line inside an already-declared file — a
     second npm install in the Dockerfile, a new pip line in a descriptor —
     finds no unused record and is a finding. Whole-file exclusions remain
     allowed. Explicit location records remain authoritative.

The audit does NOT judge resolution, installation, or running state: it is
an inventory gate, deliberately scoped to declared agent/plugin ownership
paths plus the declared installer-surface files. It is not a general parser
for every possible selector syntax in the repository, and it performs no
network work.

DARK: nothing in up.sh, manifest.py, the Dockerfile, or compose reads this
module or the inventory files; the only entry point is the read-only `djinn
versions audit` dispatcher, and tests/test_version_inventory.py pins that
up.sh and manifest.py never reference it.

Output lists every catalog component labelled by its policy-derived class —
pinned (exact), floating (latest/release-line), external, deferred — with
the deferral reason for every deferred component, then either "no findings"
or the findings that failed the audit (exit 1).
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from version_contracts import (  # noqa: E402
    Catalog,
    VersionContractError,
)

LOCATION_SCHEMA = 1

# The audit labels and the catalog policy each derives from. This mapping is
# the contract-layer mapping: exact (or a reviewed lock / immutable
# identity) is pinned; latest, release-line, and any otherwise-moving
# selection is floating; external and deferred pass through unchanged.
LABELS = ("pinned", "floating", "external", "deferred")
POLICY_LABEL = {
    "exact": "pinned",
    "latest": "floating",
    "release-line": "floating",
    "external": "external",
    "deferred": "deferred",
}

_LOCATION_FIELDS = frozenset({"path", "component", "find", "note"})
_DISCOVERY_FIELDS = frozenset({"scope", "skip-names", "patterns"})
_DESCRIPTOR_FILES = {"agents": "agent.yml", "plugins": "plugin.yml"}
# Owned installer paths the floating-launcher ban applies to (container-side
# build surfaces only). The ONLY exempt files are those inventoried under
# the explicit host-bridges group — the out-of-scope host-side launchers
# (run.sh/launch.py, which run on the Mac, never in the container). A file
# inventoried under any other group (e.g. gear360's install.sh under
# distro-packages) is a container installer and stays under the ban.
_LAUNCHER_SCAN_DIRS = ("agents", "plugins")
_LAUNCHER_SCAN_SUFFIXES = (".yml", ".sh")
_LAUNCHER_RE = re.compile(r"\b(npx|uvx)\b")
HOST_BRIDGES_COMPONENT = "host-bridges"

# Test fixtures live inside descriptor directories (plugins/*/test_*.py,
# agents/*/test_wiring.py); they quote installer syntax in assertions and
# are never installer surfaces. Pattern discovery skips them, the same way
# it skips documentation and the vendored tree.
_TEST_FILE_RE = re.compile(r"(?:^test_|_test$)")

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_FILE = "versions.yml"
LOCATIONS_FILE = "version-locations.yml"


# ── version-locations.yml validation ─────────────────────────────────────

def _yaml_type(v):
    return {list: "list", str: "string", int: "number", float: "number",
            bool: "boolean"}.get(type(v), type(v).__name__)


def _aggregate(section, errors):
    raise VersionContractError(
        f"{section} failed validation:\n" + "\n".join(f"  {e}" for e in errors))


def _rel_path(value, what, errors):
    """A repo-relative path that cannot escape the repository: no absolute
    forms, no '..' segments. Returns the value when valid."""
    from pathlib import PurePosixPath
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{what}: must be a non-empty string (got a {_yaml_type(value)})")
        return None
    pure = PurePosixPath(value.replace("\\", "/"))
    if pure.is_absolute() or value.startswith("/"):
        errors.append(
            f"{what}: must be repo-relative (got absolute path {value!r})")
        return None
    if ".." in pure.parts:
        errors.append(
            f"{what}: must be repo-relative (got {value!r} with a '..' segment)")
        return None
    if pure.parts and pure.parts[0] == "~":
        errors.append(
            f"{what}: must be repo-relative (got home-relative {value!r})")
        return None
    return value


def validate_locations(doc):
    """Validate the parsed version-locations.yml; returns the validated
    shape: {"locations": {id: record}, "exclusions": [{path, reason}],
    "discovery": {...}}. Structural rules only — component ids are checked
    against the catalog in run_audit()."""
    from version_contracts import COMPONENT_ID_RE

    if not isinstance(doc, dict):
        raise VersionContractError(
            f"locations file must be a map (got a {_yaml_type(doc)})")
    extra = ",".join(sorted(k for k in doc
                            if k not in ("schema", "locations", "exclusions",
                                         "discovery")))
    if extra:
        raise VersionContractError(
            f"locations file: unsupported field(s): {extra} (only schema, "
            "locations, exclusions, discovery)")
    schema = doc.get("schema")
    if isinstance(schema, bool) or not isinstance(schema, int):
        raise VersionContractError(
            f"locations schema: must be the number {LOCATION_SCHEMA}")
    if schema != LOCATION_SCHEMA:
        raise VersionContractError(
            f"locations schema: only schema {LOCATION_SCHEMA} is defined "
            f"(got {schema})")

    errors = []
    locations_doc = doc.get("locations")
    if not isinstance(locations_doc, dict) or not locations_doc:
        raise VersionContractError(
            "locations: must be a non-empty map of location id → location "
            f"map (got a {_yaml_type(locations_doc)})")
    from version_contracts import COMPONENT_ID_RE
    locations = {}
    for loc_id in sorted(locations_doc):
        if not COMPONENT_ID_RE.match(loc_id):
            errors.append(f"location '{loc_id}': illegal id")
            continue
        record = locations_doc[loc_id]
        if not isinstance(record, dict):
            errors.append(
                f"location '{loc_id}': must be a map (got a {_yaml_type(record)})")
            continue
        extra = ",".join(sorted(k for k in record if k not in _LOCATION_FIELDS))
        if extra:
            errors.append(
                f"location '{loc_id}': unsupported field(s): {extra} "
                "(only path, component, find, note)")
            continue
        valid = True
        for key in ("path", "component"):
            val = record.get(key)
            if not isinstance(val, str) or not val.strip():
                errors.append(
                    f"location '{loc_id}' {key}: must be a non-empty string "
                    f"(got a {_yaml_type(val)})")
                valid = False
            elif key == "path" and _rel_path(
                    val, f"location '{loc_id}' path", errors) is None:
                valid = False
        for key in ("find", "note"):  # optional when present
            val = record.get(key)
            if val is not None and (not isinstance(val, str) or not val.strip()):
                errors.append(
                    f"location '{loc_id}' {key}: must be a non-empty string "
                    "when present")
                valid = False
        if valid:
            locations[loc_id] = record
    if errors:
        _aggregate("locations", errors)

    exclusions_doc = doc.get("exclusions", [])
    if not isinstance(exclusions_doc, list):
        raise VersionContractError(
            f"exclusions: must be a list (got a {_yaml_type(exclusions_doc)})")
    errors = []
    exclusions = []
    for i, entry in enumerate(exclusions_doc):
        if not isinstance(entry, dict):
            errors.append(
                f"exclusions[{i}]: must be a map of path + reason "
                f"(got a {_yaml_type(entry)})")
            continue
        extra = ",".join(sorted(k for k in entry if k not in ("path", "reason")))
        if extra:
            errors.append(
                f"exclusions[{i}]: unsupported field(s): {extra} "
                "(only path, reason)")
            continue
        if not isinstance(entry.get("path"), str) or not entry["path"].strip():
            errors.append(f"exclusions[{i}] path: must be a non-empty string")
            continue
        if _rel_path(entry["path"], f"exclusion '{entry['path']}' path",
                     errors) is None:
            continue
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            errors.append(
                f"exclusion '{entry['path']}': reason must be a non-empty "
                "string (an exclusion is a claim, so it states why)")
            continue
        exclusions.append(entry)
    if errors:
        _aggregate("exclusions", errors)

    discovery = doc.get("discovery")
    if not isinstance(discovery, dict):
        raise VersionContractError(
            f"discovery: must be a map (got a {_yaml_type(discovery)})")
    extra = ",".join(sorted(k for k in discovery if k not in _DISCOVERY_FIELDS))
    if extra:
        raise VersionContractError(
            f"discovery: unsupported field(s): {extra} (only scope, "
            "skip-names, patterns)")
    for key in ("scope", "patterns"):
        val = discovery.get(key)
        if not isinstance(val, list) or not val or not all(
                isinstance(e, str) and e.strip() for e in val):
            raise VersionContractError(
                f"discovery {key}: must be a non-empty list of strings "
                f"(got a {_yaml_type(val)})")
    skip = discovery.get("skip-names", [])
    if not isinstance(skip, list) or not all(isinstance(e, str) for e in skip):
        raise VersionContractError(
            "discovery skip-names: must be a list of file names "
            f"(got a {_yaml_type(skip)})")
    errors = []
    for entry in discovery["scope"]:
        _rel_path(entry, f"discovery scope entry {entry!r}", errors)
    if errors:
        _aggregate("discovery", errors)
    for pattern in discovery["patterns"]:
        try:
            re.compile(pattern)
        except re.error as e:
            raise VersionContractError(f"discovery patterns: bad regex: {e}")
    return {"locations": locations, "exclusions": exclusions,
            "discovery": discovery}


# ── The audit ────────────────────────────────────────────────────────────

def label_of(component):
    """The audit classification for one catalog Component — the ONLY place
    the policy → label mapping lives."""
    return POLICY_LABEL[component.policy]


def _fenced_resolve(repo_root, rel):
    """Resolve a declared path under the repo root, refusing anything that
    lands outside it — a '..' that slipped past validation, or a symlink
    whose target escapes the repository. Returns the resolved Path or None."""
    path = (repo_root / rel).resolve()
    if not path.is_relative_to(repo_root):
        return None
    return path


def _owned_dirs(repo_root):
    """Descriptor directories: ({abs dir: (kind, name)}, escapes) for every
    agents/*/ holding an agent.yml and every plugins/*/ holding a
    plugin.yml. A descriptor directory whose symlink target resolves outside
    the repository is reported as an escape and NEVER followed or read —
    its resolved path stays outside the returned map."""
    owned = {}
    escapes = []
    for kind, descriptor in _DESCRIPTOR_FILES.items():
        base = repo_root / kind
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if not child.is_dir():
                continue
            resolved = child.resolve()
            if not resolved.is_relative_to(repo_root):
                escapes.append(child)
                continue  # never follow or read an escaped descriptor dir
            if (child / descriptor).is_file():
                owned[resolved] = (kind, child.name, descriptor)
    return owned, escapes


def _covered_anchors(repo_root, loc_records, exclusions):
    """The universe a discovery hit may legally land in, at ANCHOR
    granularity. Returns (anchors_by_path, excluded_paths): a declared
    location contributes its find anchor to its file — a hit line is covered
    only when the line itself contains one of that file's anchors, so a
    location's path alone covers nothing and a new selector line inside an
    already-declared file must stay a finding — while an exclusion covers
    its whole file. Paths are fenced: anything resolving outside the
    repository covers nothing."""
    anchors = {}
    excluded = set()
    for loc_id, record in loc_records.items():
        path = _fenced_resolve(repo_root, record["path"])
        find = record.get("find")
        if path is None or find is None:
            continue
        anchors.setdefault(path, []).append((loc_id, find))
    for entry in exclusions:
        ex = _fenced_resolve(repo_root, entry["path"])
        if ex is None:
            continue
        if ex.is_file():
            excluded.add(ex)
        elif ex.is_dir():
            for p in ex.rglob("*"):
                if p.is_file() and p.resolve().is_relative_to(repo_root):
                    excluded.add(p.resolve())
    return anchors, excluded


def run_audit(repo_root, catalog, locations):
    """Audit the repo tree against the catalog + location inventory.
    Returns the finding list (empty = pass). Read-only: never writes."""
    repo_root = Path(repo_root).resolve()
    loc_records = locations["locations"]
    exclusions = locations["exclusions"]
    discovery = locations["discovery"]
    findings = []

    # 1. Every location names a real catalog component.
    for loc_id in sorted(loc_records):
        component_id = loc_records[loc_id]["component"]
        if component_id not in catalog.components:
            findings.append(
                f"location '{loc_id}': component '{component_id}' is not in "
                f"the catalog (known: {', '.join(sorted(catalog.components))})")

    # 2. Every non-external component owns at least one location. external
    #    components select nothing, so zero locations is their normal shape.
    by_component = set()
    for record in loc_records.values():
        by_component.add(record["component"])
    for component_id, component in sorted(catalog.components.items()):
        if component.policy != "external" and component_id not in by_component:
            findings.append(
                f"component '{component_id}' ({component.policy}): no "
                "selector location declared — an owned selector must be "
                "inventoried, and an external one must say so in the catalog")

    # 3. Every declared location still exists; pinned anchors and exact
    #    constraints still match — changing a version field on either side
    #    (installer or catalog) fails. Every path is fenced: a location
    #    whose symlink target escapes the repository is reported, never read.
    for loc_id in sorted(loc_records):
        record = loc_records[loc_id]
        path = _fenced_resolve(repo_root, record["path"])
        if path is None:
            findings.append(
                f"location '{loc_id}': resolves outside the repository "
                f"({record['path']}) — escaped locations are never read")
            continue
        if not path.is_file():
            findings.append(
                f"location '{loc_id}': declared selector location "
                f"{record['path']} no longer exists")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        find = record.get("find")
        if find is not None and find not in text:
            findings.append(
                f"location '{loc_id}': anchored selector text no longer in "
                f"{record['path']} (expected {find!r}) — the installer or "
                "the catalog changed without the other")
        component = catalog.components.get(record["component"])
        if component is not None and component.policy == "exact":
            if component.constraint not in text:
                findings.append(
                    f"location '{loc_id}': the catalog pins "
                    f"{record['component']} at {component.constraint!r}, which "
                    f"no longer appears in {record['path']} — the installer or "
                    "the catalog changed without the other")

    # 4. Descriptor coverage: every agent/plugin descriptor directory is
    #    owned by a location whose component is the matching kind AND names
    #    this exact descriptor as its owner. Coverage by an unrelated
    #    component (a group, or a path borrowed from elsewhere) does not
    #    count. This is the add-a-descriptor pin.
    owned, escaped_dirs = _owned_dirs(repo_root)
    for child in escaped_dirs:
        findings.append(
            f"descriptor directory {child.relative_to(repo_root)}: symlink "
            "target outside the repository — never read or covered")
    for directory, (kind, name, _descriptor) in sorted(owned.items()):
        desc_path = f"{kind}/{name}/{_DESCRIPTOR_FILES[kind]}"
        singular = kind[:-1]  # agents→agent, plugins→plugin
        if not any(
                (component := catalog.components.get(record["component"]))
                is not None and component.kind == singular
                and component.owner == desc_path
                for record in loc_records.values()):
            rel = directory.relative_to(repo_root)
            findings.append(
                f"undocumented {singular} descriptor: {rel}/ carries "
                f"{_DESCRIPTOR_FILES[kind]} — every agent/plugin descriptor "
                "must be catalogued in versions.yml (kind and owner matching "
                "this descriptor) with a selector location in "
                "version-locations.yml, or explicitly excluded")

    # 5. Floating-launcher ban on owned installer paths. Exempt ONLY the
    #    files inventoried under the explicit host-bridges group — the
    #    host-side run.sh/launch.py launchers that run on the Mac, never in
    #    the container. A file declared under any other group (gear360's
    #    install.sh under distro-packages) is a container installer and
    #    stays under the ban.
    bridge_paths = set()
    managed_paths = set()
    for record in loc_records.values():
        component = catalog.components.get(record["component"])
        if component is None:
            continue
        resolved = _fenced_resolve(repo_root, record["path"])
        if resolved is None:
            continue
        if component.id == HOST_BRIDGES_COMPONENT:
            bridge_paths.add(resolved)
        if component.kind in ("agent", "plugin"):
            managed_paths.add(resolved)
    for directory, (kind, name, _descriptor) in sorted(owned.items()):
        if kind not in _LAUNCHER_SCAN_DIRS:
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.suffix not in _LAUNCHER_SCAN_SUFFIXES:
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(repo_root):
                findings.append(
                    f"{path.relative_to(repo_root)}: symlink target outside "
                    "the repository — escaped files are never read")
                continue
            if resolved in bridge_paths and resolved not in managed_paths:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                findings.append(
                    f"{path.relative_to(repo_root)}: unreadable ({e})")
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if _LAUNCHER_RE.search(line):
                    findings.append(
                        f"floating runtime launcher in "
                        f"{path.relative_to(repo_root)}:{lineno}: owned "
                        "installers must not launch through npx/uvx (a "
                        "managed runtime resolves at build, never live)")

    # 6. Scoped pattern discovery (the backstop). Coverage is per anchor:
    #    every non-comment hit line must carry a declared find anchor for
    #    its path (or live in a whole-file exclusion).
    compiled = [re.compile(p) for p in discovery["patterns"]]
    skip_names = set(discovery.get("skip-names", []))
    anchors_by_path, excluded_paths = _covered_anchors(
        repo_root, loc_records, exclusions)
    for entry in discovery["scope"]:
        scope_root = _fenced_resolve(repo_root, entry)
        if scope_root is None:
            findings.append(
                f"discovery scope entry escapes the repository: {entry}")
            continue
        if scope_root.is_file():
            candidates = [scope_root]
        elif scope_root.is_dir():
            candidates = [p for p in scope_root.rglob("*")
                          if p.is_file() and ".git" not in p.parts]
        else:
            findings.append(
                "discovery scope entry does not exist: "
                f"{scope_root.relative_to(repo_root)}")
            continue
        for path in candidates:
            if path.name in skip_names or path.suffix == ".md":
                continue
            if path.suffix == ".py" and _TEST_FILE_RE.search(path.stem):
                continue  # test fixtures quote installer syntax
            if path.suffix == ".pyc" or "__pycache__" in path.parts:
                continue  # generated bytecode
            resolved = path.resolve()
            if not resolved.is_relative_to(repo_root):
                findings.append(
                    f"{path.relative_to(repo_root)}: symlink target outside "
                    "the repository — escaped files are never read")
                continue
            if resolved in excluded_paths:
                continue  # whole-file exclusion
            records = anchors_by_path.get(resolved, [])
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # One-to-one accounting: each declared record with a find is one
            # consumable allowance. Longest anchor first, then location id —
            # deterministic — and each takes its lowest unaccounted hit line,
            # so repeated legitimate identical lines need one record each
            # and a NEW identical line finds no unused record and fails.
            records = sorted(records, key=lambda r: (-len(r[1]), r[0]))
            hit_lines = []  # (lineno, line) for every non-comment pattern hit
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue  # comment prose, never a selector
                if any(pattern.search(line) for pattern in compiled):
                    hit_lines.append((lineno, line))
            accounted = set()
            for _loc_id, find in records:
                for lineno, line in hit_lines:
                    if lineno in accounted:
                        continue
                    if find in line:
                        accounted.add(lineno)
                        break
            for lineno, line in hit_lines:
                if lineno in accounted:
                    continue
                findings.append(
                    "undiscovered selector location: "
                    f"{path.relative_to(repo_root)}:{lineno} — no unused "
                    "record anchors this selector line; declare it in "
                    f"{LOCATIONS_FILE} or extend the exclusions")
    return findings


# ── Report ───────────────────────────────────────────────────────────────

def format_report(catalog, locations, findings):
    """The operator-facing report: every component labelled, deferred
    reasons stated, then findings (or the clean summary line)."""
    lines = [f"versions audit — catalog {CATALOG_FILE}, locations "
             f"{LOCATIONS_FILE}"]
    paths_by_component = {}
    for record in locations["locations"].values():
        paths_by_component.setdefault(record["component"], set()).add(
            record["path"])
    counts = {label: 0 for label in LABELS}
    for component_id in sorted(catalog.components):
        counts[label_of(catalog.components[component_id])] += 1
    lines.append("  " + "  ".join(
        f"{label}: {counts[label]}" for label in LABELS))
    for label in LABELS:
        lines.append(f"{label}:")
        for component_id in sorted(catalog.components):
            component = catalog.components[component_id]
            if label_of(component) != label:
                continue
            paths = sorted(paths_by_component.get(component_id, []))
            tail = ""
            if component.policy == "deferred":
                tail = f" — deferred: {component.deferral_reason}"
            elif component.policy == "external":
                tail = " — external: brassbottle selects and installs nothing"
            loc = f" ({', '.join(paths)})" if paths else " (no owned selector)"
            lines.append(f"  {component_id}{loc}{tail}")
    if findings:
        lines.append(f"AUDIT FAILED — {len(findings)} finding(s):")
        for finding in findings:
            lines.append(f"  ✗ {finding}")
    else:
        lines.append(
            "no findings — every descriptor and owned installer path is "
            "catalogued, excluded, external, or deferred")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────

def load_yaml(path):
    """YAML → dict via yq (the repo's only YAML reader: up.sh and the tests
    use the same converter, so no YAML dialect is mirrored in Python)."""
    path = Path(path)
    try:
        proc = subprocess.run(["yq", "-o=json", "-I=0", str(path)],
                              capture_output=True, text=True)
    except FileNotFoundError:
        raise VersionContractError(
            "yq is required (brew install yq / static binary on Linux)")
    if proc.returncode != 0:
        raise VersionContractError(
            f"yq could not read {path}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise VersionContractError(f"{path}: yq produced invalid JSON ({e})")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="djinn versions audit",
        description="Read-only audit of the version-selection inventory.")
    parser.add_argument("subcommand", nargs="?", default="audit",
                        choices=["audit"],
                        help="only 'audit' exists; it never mutates anything")
    parser.add_argument("--repo", metavar="ROOT", default=str(REPO_ROOT),
                        help="repository root to audit (default: this repo)")
    parser.add_argument("--catalog", metavar="FILE", default=None,
                        help="catalog file (default: <repo>/versions.yml)")
    parser.add_argument("--locations", metavar="FILE", default=None,
                        help="location inventory (default: "
                             "<repo>/version-locations.yml)")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    catalog_file = Path(args.catalog or Path(args.repo) / CATALOG_FILE)
    locations_file = Path(args.locations or Path(args.repo) / LOCATIONS_FILE)
    try:
        catalog = Catalog.from_dict(load_yaml(catalog_file))
        locations = validate_locations(load_yaml(locations_file))
    except VersionContractError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    findings = run_audit(repo_root, catalog, locations)
    print(format_report(catalog, locations, findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
