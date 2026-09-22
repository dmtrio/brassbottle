#!/usr/bin/env python3
"""Typed version-selection contracts (PLN - Managed Versions on Bottle Up,
Step 1 — Dark).

This module holds the catalog schema, the candidate/build-receipt models, the
canonical hash, the manifest `versions:` override validator, and the status
model for the version-management PLN. It is deliberately DARK: nothing in
up.sh, manifest.py, the Dockerfile, or compose reads it yet. Step 1 delivers
the contracts and their tests; Step 2 builds the `versions.yml` inventory and
audit on them; Step 7 wires them into `djinn up`. The Dark pin in
tests/test_version_contracts.py fails if manifest.py or up.sh ever references
this module before the integration step says so.

Records and their states (PLN §2, §4):

  requested  a policy was asked for — catalog policy as possibly overridden
             by the bottle manifest's schema-validated `versions:` section
  resolved   exact versions/commits/assets were resolved for every enabled
             managed component → a candidate record; a candidate always
             carries resolution evidence, so "requested without resolved"
             is not a persistable shape
  built      an image was built from that candidate → a build receipt whose
             candidate_hash pins the exact candidate
  running    the running image/container labels match that receipt

  external   the catalog says brassbottle does not select or install it
  deferred   inventoried, intentionally kept with existing behavior

external and deferred are terminal classifications, never lifecycle steps: a
catalog component with an external/deferred policy never enters a candidate.

The two presentation rules the PLN's minimum safety contract requires — a
candidate is never called built without a matching receipt, and a built image
is never called running without a matching running observation — are enforced
in exactly one place, component_state(), so status output cannot drift from
the stored evidence. transition() guards the raw state machine for callers
that move a component through its lifecycle step by step.

Canonical hashing (PLN §8): canonical_hash() encodes a record as JSON with
sorted keys, no insignificant whitespace, and non-ASCII kept as UTF-8 — the
same input always produces the same hash regardless of dict insertion order
or how the record was built. Timestamps and other volatile fields are
excluded by the caller (Candidate.hash drops `created_at`) so a re-resolution
that found the same versions hashes to the same candidate.

Secret hygiene: candidates and build receipts are persisted on disk
($DJINN_HOME/versions/...). assert_persistable() rejects any map key that
looks like a secret or credential (token, secret, password, credential,
api_key, bearer, auth, ...) anywhere in the record, so a resolution result
that leaked a credential shape fails at construction instead of landing in a
JSON file. Field NAMES are the contract — a field may reference a secret's
NAME (that is how the rest of this repo works), never carry a value that
looks like it belongs under such a name.
"""

import enum
import hashlib
import json
import re


class VersionContractError(Exception):
    """Fatal schema/validation error; callers print 'Error: …' and abort."""


# ── Shared value vocabularies ────────────────────────────────────────────

# The five policies from PLN §4. The first three are MANAGED: brassbottle
# resolves and installs them. external/deferred are inventory classifications.
POLICIES = ("latest", "release-line", "exact", "external", "deferred")
MANAGED_POLICIES = ("latest", "release-line", "exact")

KINDS = ("agent", "plugin", "group")

# Resolver adapters (PLN §5). Versioned and fixture-tested there; the schema
# only fixes the vocabulary so a typo'd adapter name cannot enter a catalog.
SOURCE_TYPES = ("npm", "pypi", "github-release", "git", "release-asset",
                "runtime-line", "none")

# Resolver adapters (PLN §5) a managed component may name. The set mirrors the
# source types minus "none" — a managed component resolves through a real
# adapter, and a typo'd adapter name must fail at catalog load, not at
# resolution time.
RESOLVERS = ("npm", "pypi", "github-release", "git", "release-asset",
             "runtime-line")

# Closure modes (PLN §4): a reviewed lockfile, a reviewed compatibility
# bundle, or no closure (the record, not the claim, carries what a component
# has). external/deferred components are always "none".
CLOSURES = ("lock", "bundle", "none")

# Stable component IDs are lowercase dotted names: agent.aider,
# plugin.serena, base-images. Lowercase keeps candidate hashes and image
# labels free of case-fold questions.
COMPONENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z")

HEX64_RE = re.compile(r"^[0-9a-f]{64}\Z")

CATALOG_SCHEMA = 1


def _yaml_type(v):
    # Same rendering manifest.py uses in its errors, so messages read alike.
    return {list: "list", str: "string", int: "number", float: "number",
            bool: "boolean"}.get(type(v), type(v).__name__)


def _aggregate(section, errors):
    raise VersionContractError(
        f"{section} failed validation:\n" + "\n".join(f"  {e}" for e in errors))


def _nonempty_str(v, what, errors):
    if isinstance(v, str) and v.strip():
        return v
    errors.append(f"{what} must be a non-empty string (got a {_yaml_type(v)})")
    return None


# ── Canonical deterministic hashing ──────────────────────────────────────

def canonical_json(record):
    """The one JSON encoding hashes are computed over: sorted keys, tight
    separators, UTF-8 text. Two records that differ only in dict insertion
    order (or in which of two equivalent spellings built them) encode
    identically; any difference in content changes the bytes and the hash."""
    try:
        text = json.dumps(record, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as e:
        raise VersionContractError(f"record is not canonically encodable: {e}")
    return text


def canonical_hash(record):
    """SHA-256 over canonical_json(). Bare lowercase hex — the same shape the
    PLN's candidates/<sha256>.json filename and image labels use."""
    return hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


# ── Secret hygiene for persisted records ─────────────────────────────────

# Field names that carry (or look like they carry) a secret value. Segment
# matching keeps ordinary words safe: `auth` matches "auth" and "auth_token"
# but never "author", because the name must start, end, or be underscore-
# separated at the match.
_SECRET_FIELD_RE = re.compile(
    r"(?:^|_)(token|secret|password|passwd|credential|api_key|apikey|"
    r"access_key|private_key|client_secret|bearer|auth|authorization|"
    r"session)(?:$|_)", re.IGNORECASE)


def assert_persistable(record, context="record"):
    """Reject records that must never be written to disk with a secret-shaped
    field anywhere inside them. Candidates and build receipts are the
    persisted records (PLN §2); everything reachable from them passes through
    here, so a resolution result that leaked e.g. an auth_token fails at
    construction rather than landing in $DJINN_HOME/versions/."""
    _assert_persistable_walk(record, "", context)


def _assert_persistable_walk(node, path, context):
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise VersionContractError(
                    f"{context}{path}: map keys must be strings "
                    f"(got a {type(key).__name__})")
            if _SECRET_FIELD_RE.search(key):
                raise VersionContractError(
                    f"{context}{path}.{key}: secret or credential-like field "
                    "names are never persisted (record the policy/resolution "
                    "that needs it, never the credential itself)")
            _assert_persistable_walk(value, f"{path}.{key}", context)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            _assert_persistable_walk(value, f"{path}[{i}]", context)


# ── Catalog schema (root versions.yml, PLN §4) ───────────────────────────

_COMPONENT_FIELDS = frozenset({
    "name", "kind", "source", "owner", "enabled_when", "policy", "constraint",
    "resolver", "platform", "closure", "bundle", "migration", "deferral_reason",
})
_SOURCE_FIELDS = frozenset({"type", "location"})


def _component_from_doc(component_id, doc, errors):
    """Validate one components/<id> entry; returns the field dict or None
    (with the error already appended) when the entry is unusable."""
    if not COMPONENT_ID_RE.match(component_id):
        errors.append(
            f"component '{component_id}': illegal id (lowercase letters, "
            "digits, dot, underscore, dash; must start with a letter or digit)")
        return None
    if not isinstance(doc, dict):
        errors.append(
            f"component '{component_id}': must be a map (got a {_yaml_type(doc)})")
        return None
    extra = ",".join(sorted(k for k in doc if k not in _COMPONENT_FIELDS))
    if extra:
        errors.append(
            f"component '{component_id}': unsupported field(s): {extra} "
            f"(supported: {', '.join(sorted(_COMPONENT_FIELDS))})")
        return None

    policy = doc.get("policy")
    if policy not in POLICIES:
        errors.append(
            f"component '{component_id}': policy must be one of "
            f"{', '.join(POLICIES)} (got {policy!r})")
        return None
    managed = policy in MANAGED_POLICIES

    fields = {"policy": policy}
    for key in ("name", "enabled_when"):
        if _nonempty_str(doc.get(key), f"component '{component_id}' {key}",
                         errors) is None:
            return None
        fields[key] = doc[key]

    kind = doc.get("kind")
    if kind not in KINDS:
        errors.append(
            f"component '{component_id}': kind must be one of "
            f"{', '.join(KINDS)} (got {kind!r})")
        return None
    fields["kind"] = kind

    owner = doc.get("owner")
    if kind == "group":
        # An inventoried group (base images, apt, host bridges) has no single
        # installer owner — owner: must stay unset so the audit never reads a
        # pretended owner where none exists.
        if owner is not None:
            errors.append(
                f"component '{component_id}': a group carries no installer "
                "owner (drop owner: or declare it kind: agent|plugin)")
            return None
        fields["owner"] = None
    else:
        if _nonempty_str(owner, f"component '{component_id}' owner", errors) is None:
            return None
        fields["owner"] = owner

    source = doc.get("source")
    if source is None:
        fields["source"] = None
    else:
        if not isinstance(source, dict):
            errors.append(
                f"component '{component_id}' source: must be a map "
                f"(got a {_yaml_type(source)})")
            return None
        extra = ",".join(sorted(k for k in source if k not in _SOURCE_FIELDS))
        if extra:
            errors.append(
                f"component '{component_id}' source: unsupported field(s): "
                f"{extra} (only type and location)")
            return None
        if source.get("type") not in SOURCE_TYPES:
            errors.append(
                f"component '{component_id}' source.type: must be one of "
                f"{', '.join(SOURCE_TYPES)} (got {source.get('type')!r})")
            return None
        if _nonempty_str(source.get("location"),
                         f"component '{component_id}' source.location",
                         errors) is None:
            return None
        fields["source"] = {"type": source["type"], "location": source["location"]}

    # Fields whose presence is decided by the policy: managed components
    # resolve on a platform through a named adapter; external/deferred ones
    # are deliberately outside that machinery.
    for key in ("constraint", "resolver", "platform"):
        val = doc.get(key)
        if managed:
            if key == "constraint" and policy == "latest":
                if val is not None and _nonempty_str(
                        val, f"component '{component_id}' {key}", errors) is None:
                    return None
                fields[key] = val if val is not None else None
                continue
            if key == "resolver" and val not in RESOLVERS:
                errors.append(
                    f"component '{component_id}' resolver: must be one of "
                    f"{', '.join(RESOLVERS)} (got {val!r})")
                return None
            if _nonempty_str(val, f"component '{component_id}' {key}", errors) is None:
                return None
            fields[key] = val
        else:
            if val is not None:
                errors.append(
                    f"component '{component_id}': a {policy} component carries "
                    f"no {key}: (brassbottle does not manage it)")
                return None
            fields[key] = None

    closure = doc.get("closure")
    if closure is None:
        closure = "none"
    if closure not in CLOSURES:
        errors.append(
            f"component '{component_id}': closure must be one of "
            f"{', '.join(CLOSURES)} (got {closure!r})")
        return None
    if policy not in MANAGED_POLICIES and closure != "none":
        errors.append(
            f"component '{component_id}': a {policy} component has closure: none")
        return None
    fields["closure"] = closure

    bundle = doc.get("bundle")
    if closure == "bundle":
        if _nonempty_str(bundle, f"component '{component_id}' bundle", errors) is None:
            return None
        fields["bundle"] = bundle
    elif bundle is not None:
        errors.append(
            f"component '{component_id}': bundle: is only valid with "
            "closure: bundle")
        return None
    else:
        fields["bundle"] = None

    migration = doc.get("migration")
    if policy in MANAGED_POLICIES:
        if migration is not None and _nonempty_str(
                migration, f"component '{component_id}' migration", errors) is None:
            return None
        fields["migration"] = migration
    elif migration is not None:
        errors.append(
            f"component '{component_id}': a {policy} component carries no "
            "migration: (nothing is migrated)")
        return None
    else:
        fields["migration"] = None

    deferral = doc.get("deferral_reason")
    if policy == "deferred":
        if _nonempty_str(deferral,
                         f"component '{component_id}' deferral_reason",
                         errors) is None:
            return None
        fields["deferral_reason"] = deferral
    elif deferral is not None:
        errors.append(
            f"component '{component_id}': deferral_reason: is only valid for "
            "policy: deferred (a managed component migrates or stays)")
        return None
    else:
        fields["deferral_reason"] = None

    return fields


class State(enum.Enum):
    """The six states the Claim distinguishes. REQUESTED→RESOLVED→BUILT→RUNNING
    is the managed lifecycle; EXTERNAL and DEFERRED are terminal catalog
    classifications a managed component never passes through."""
    REQUESTED = "requested"
    RESOLVED = "resolved"
    BUILT = "built"
    RUNNING = "running"
    EXTERNAL = "external"
    DEFERRED = "deferred"

    @classmethod
    def from_policy(cls, policy):
        """The classification a catalog policy imposes on a component. Only
        the two inventory policies classify — managed policies have lifecycle
        states instead."""
        if policy not in ("external", "deferred"):
            raise VersionContractError(
                f"policy {policy} has a lifecycle, not a classification — "
                "derive its state from evidence with component_state()")
        return cls(policy)

    def __str__(self):
        return self.value


# Allowed lifecycle moves (PLN §3): resolution precedes build evidence, build
# evidence precedes running evidence. RUNNING→REQUESTED starts the next
# candidate cycle on a subsequent up; external/deferred never transition.
ALLOWED_TRANSITIONS = {
    State.REQUESTED: frozenset({State.RESOLVED}),
    State.RESOLVED: frozenset({State.BUILT}),
    State.BUILT: frozenset({State.RUNNING}),
    State.RUNNING: frozenset({State.REQUESTED}),
    State.EXTERNAL: frozenset(),
    State.DEFERRED: frozenset(),
}


def transition(current, target):
    """Move one step along the lifecycle or raise. This guards raw state
    changes; the evidence-backed derivation lives in component_state()."""
    if not isinstance(current, State) or not isinstance(target, State):
        raise VersionContractError(
            "transition() takes two State values (got "
            f"{type(current).__name__}, {type(target).__name__})")
    allowed = ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise VersionContractError(
            f"cannot present a {current} component as {target} "
            f"(allowed from {current}: "
            f"{', '.join(str(s) for s in sorted(allowed, key=str)) or 'nothing'})")
    return target


class Component:
    """One catalog entry. Built only through Catalog.from_dict() — every
    field is already validated, so attribute access needs no re-checking."""

    __slots__ = ("id", "name", "kind", "source", "owner", "enabled_when",
                 "policy", "constraint", "resolver", "platform", "closure",
                 "bundle", "migration", "deferral_reason")

    def __init__(self, **fields):
        for name in self.__slots__:
            setattr(self, name, fields[name])

    def __eq__(self, other):
        return isinstance(other, Component) and all(
            getattr(self, name) == getattr(other, name)
            for name in self.__slots__)

    def __hash__(self):
        return hash(tuple(getattr(self, name) for name in self.__slots__))

    def __repr__(self):
        body = ", ".join(f"{name}={getattr(self, name)!r}" for name in self.__slots__)
        return f"Component({body})"

    def to_dict(self):
        doc = {}
        for name in self.__slots__:
            if name in ("id",):
                continue
            value = getattr(self, name)
            if value is not None:
                doc[name] = value
        return doc


class Catalog:
    """The validated in-memory root versions.yml (PLN §4)."""

    def __init__(self, schema, components):
        self.schema = schema
        self.components = components  # dict id → Component (validated)

    def __eq__(self, other):
        return (isinstance(other, Catalog) and self.schema == other.schema
                and self.components == other.components)

    def digest(self):
        """Canonical hash of the whole catalog — the value candidates record
        as catalog_digest so a candidate can name the catalog it was resolved
        against."""
        return canonical_hash(self.to_dict())

    def to_dict(self):
        return {
            "schema": self.schema,
            "components": {cid: comp.to_dict()
                           for cid, comp in self.components.items()},
        }

    @classmethod
    def from_dict(cls, doc):
        if not isinstance(doc, dict):
            raise VersionContractError(
                f"catalog must be a map (got a {_yaml_type(doc)})")
        extra = ",".join(sorted(k for k in doc if k not in ("schema", "components")))
        if extra:
            raise VersionContractError(
                f"catalog: unsupported field(s): {extra} (only schema and components)")
        schema = doc.get("schema")
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise VersionContractError(
                f"catalog schema: must be the number {CATALOG_SCHEMA}")
        if schema != CATALOG_SCHEMA:
            raise VersionContractError(
                f"catalog schema: only schema {CATALOG_SCHEMA} is defined (got {schema})")
        components_doc = doc.get("components")
        if not isinstance(components_doc, dict) or not components_doc:
            raise VersionContractError(
                "catalog components: must be a non-empty map of component id → "
                f"component map (got a {_yaml_type(components_doc)})")
        errors = []
        components = {}
        for component_id in sorted(components_doc):
            fields = _component_from_doc(component_id, components_doc[component_id],
                                         errors)
            if fields is not None:
                components[component_id] = Component(id=component_id, **fields)
        if errors:
            _aggregate("catalog", errors)
        return cls(schema, components)


# ── Manifest `versions:` overrides (PLN §4) ──────────────────────────────

OVERRIDE_FIELDS = frozenset({"policy", "constraint"})


class Override:
    """One bottle-level override: the fields the manifest set. policy/constraint
    fall back to the catalog when the manifest does not name them."""

    __slots__ = ("policy", "constraint")

    def __init__(self, policy, constraint):
        self.policy = policy
        self.constraint = constraint

    def __eq__(self, other):
        return (isinstance(other, Override)
                and self.policy == other.policy
                and self.constraint == other.constraint)

    def __repr__(self):
        return f"Override(policy={self.policy!r}, constraint={self.constraint!r})"


class ManifestVersions:
    """The validated `versions:` section of a bottle manifest."""

    __slots__ = ("overrides",)

    def __init__(self, overrides):
        self.overrides = overrides  # {component_id: Override}

    def __eq__(self, other):
        return isinstance(other, ManifestVersions) and self.overrides == other.overrides

    def effective(self, component):
        """(policy, constraint) for a catalog Component after overrides:
        the manifest's values where given, the catalog's otherwise."""
        o = self.overrides.get(component.id)
        if o is None:
            return component.policy, component.constraint
        return o.policy, o.constraint


def validate_manifest_versions(versions_val, catalog):
    """Validate a manifest's `versions:` value against an in-memory Catalog.

    Dark per Step 1: this takes the parsed objects themselves — no manifest.py
    wiring, no stdin format. Unknown component IDs and unsupported policy
    fields are hard errors (PLN §4: they fail before resolution); a policy
    switch to release-line/exact must bring its constraint; external and
    deferred components cannot be overridden at all."""
    if versions_val is None:
        return ManifestVersions({})
    if not isinstance(versions_val, dict):
        raise VersionContractError(
            "manifest versions: must be a map of component overrides, e.g. "
            "versions: {agent.claude: {policy: exact, constraint: 1.2.3}}")
    errors = []
    overrides = {}
    for component_id in versions_val:
        component = catalog.components.get(component_id)
        if component is None:
            errors.append(
                f"versions override '{component_id}': no such component in the "
                "catalog (known: "
                f"{', '.join(sorted(catalog.components)) or 'none'})")
            continue
        val = versions_val[component_id]
        if not isinstance(val, dict):
            errors.append(
                f"versions override '{component_id}': must be a map of "
                f"policy/value fields (got a {_yaml_type(val)})")
            continue
        extra = ",".join(sorted(k for k in val if k not in OVERRIDE_FIELDS))
        if extra:
            errors.append(
                f"versions override '{component_id}': unsupported field(s): "
                f"{extra} (only policy and constraint)")
            continue
        if component.policy not in MANAGED_POLICIES:
            errors.append(
                f"versions override '{component_id}': the component is "
                f"{component.policy} in the catalog and its policy cannot be "
                "overridden (change the catalog instead)")
            continue
        policy = val.get("policy")
        if policy is not None:
            if not isinstance(policy, str) or policy not in POLICIES:
                errors.append(
                    f"versions override '{component_id}': policy must be one of "
                    f"{', '.join(POLICIES)} (got {policy!r})")
                continue
            if policy not in MANAGED_POLICIES:
                errors.append(
                    f"versions override '{component_id}': policy {policy} is not "
                    "an override target — a bottle manages its enabled "
                    "components, it never externalizes or defers one")
                continue
        else:
            policy = component.policy
        constraint = val.get("constraint")
        if constraint is not None:
            if not isinstance(constraint, str) or not constraint.strip():
                errors.append(
                    f"versions override '{component_id}': constraint must be a "
                    f"non-empty string (got a {_yaml_type(constraint)})")
                continue
            if policy not in MANAGED_POLICIES:
                errors.append(
                    f"versions override '{component_id}': a {policy} component "
                    "takes no constraint")
                continue
        else:
            # Keeping the catalog's constraint only makes sense while the
            # policy is unchanged: a policy switch re-declares what it wants.
            constraint = component.constraint if policy == component.policy else None
        if policy in ("release-line", "exact") and not constraint:
            errors.append(
                f"versions override '{component_id}': policy {policy} requires "
                "a constraint (the version, commit or digest it pins)")
            continue
        overrides[component_id] = Override(policy, constraint)
    if errors:
        _aggregate("manifest versions", errors)
    return ManifestVersions(overrides)


# ── Candidate record (PLN §2: candidates/<sha256>.json) ──────────────────

CANDIDATE_FIELDS = frozenset({
    "requested", "resolved", "enabled", "platform", "catalog_digest", "created_at",
})
_REQUESTED_FIELDS = frozenset({"policy", "constraint"})
_RESOLUTION_FIELDS = frozenset({"version", "commit", "digest", "asset"})
_ASSET_FIELDS = frozenset({"name", "sha256"})


def _validate_resolution(component_id, resolution, errors):
    """One resolved: entry — the exact version/commit/asset the adapter found.
    At least one identity field must be present: a resolution that names
    nothing cannot later be distinguished from any other build."""
    if not isinstance(resolution, dict):
        errors.append(
            f"resolved '{component_id}': must be a map of identity fields "
            f"(got a {_yaml_type(resolution)})")
        return None
    extra = ",".join(sorted(k for k in resolution if k not in _RESOLUTION_FIELDS))
    if extra:
        errors.append(
            f"resolved '{component_id}': unsupported field(s): {extra} "
            "(only version, commit, digest, asset)")
        return None
    if not any(resolution.get(f) for f in ("version", "commit", "digest")):
        errors.append(
            f"resolved '{component_id}': must carry at least one identity field "
            "(version, commit or digest)")
        return None
    for field in ("version", "commit", "digest"):
        val = resolution.get(field)
        if val is not None and _nonempty_str(
                val, f"resolved '{component_id}' {field}", errors) is None:
            return None
    asset = resolution.get("asset")
    if asset is not None:
        if not isinstance(asset, dict):
            errors.append(
                f"resolved '{component_id}' asset: must be a map "
                f"(got a {_yaml_type(asset)})")
            return None
        extra = ",".join(sorted(k for k in asset if k not in _ASSET_FIELDS))
        if extra:
            errors.append(
                f"resolved '{component_id}' asset: unsupported field(s): {extra} "
                "(only name and sha256)")
            return None
        if _nonempty_str(asset.get("name"),
                         f"resolved '{component_id}' asset.name", errors) is None:
            return None
        sha = asset.get("sha256")
        if sha is not None and (not isinstance(sha, str) or not HEX64_RE.match(sha)):
            errors.append(
                f"resolved '{component_id}' asset.sha256: must be 64 lowercase "
                f"hex characters (got {sha!r})")
            return None
    return resolution


def _validate_candidate(doc):
    # Secret-shaped field names are rejected before any structural rule: a
    # credential that leaks into a resolution result must fail as the secret
    # it is, not as an "unsupported field".
    assert_persistable(doc, "candidate")
    if not isinstance(doc, dict):
        raise VersionContractError(
            f"candidate must be a map (got a {_yaml_type(doc)})")
    extra = ",".join(sorted(k for k in doc if k not in CANDIDATE_FIELDS))
    if extra:
        raise VersionContractError(
            f"candidate: unsupported field(s): {extra} "
            f"(supported: {', '.join(sorted(CANDIDATE_FIELDS))})")

    errors = []
    requested = doc.get("requested")
    if not isinstance(requested, dict) or not requested:
        raise VersionContractError(
            "candidate requested: must be a non-empty map of component id → "
            f"{_yaml_type(requested)}")
    for component_id, req in requested.items():
        if not COMPONENT_ID_RE.match(component_id):
            errors.append(
                f"requested '{component_id}': illegal component id (lowercase "
                "letters, digits, dot, underscore, dash)")
            continue
        if not isinstance(req, dict):
            errors.append(
                f"requested '{component_id}': must be a map (got a {_yaml_type(req)})")
            continue
        extra = ",".join(sorted(k for k in req if k not in _REQUESTED_FIELDS))
        if extra:
            errors.append(
                f"requested '{component_id}': unsupported field(s): {extra} "
                "(only policy and constraint)")
            continue
        policy = req.get("policy")
        if policy not in MANAGED_POLICIES:
            errors.append(
                f"requested '{component_id}': policy must be one of "
                f"{', '.join(MANAGED_POLICIES)} — external and deferred "
                "components never enter a candidate (got "
                f"{policy if policy is None else repr(policy)})")
            continue
        constraint = req.get("constraint")
        if constraint is not None and _nonempty_str(
                constraint, f"requested '{component_id}' constraint", errors) is None:
            continue
        if policy in ("release-line", "exact") and not constraint:
            errors.append(
                f"requested '{component_id}': policy {policy} requires a constraint")
            continue
    if errors:
        _aggregate("candidate requested", errors)

    resolved = doc.get("resolved")
    if not isinstance(resolved, dict):
        raise VersionContractError(
            f"candidate resolved: must be a map (got a {_yaml_type(resolved)})")
    unknown = sorted(set(resolved) - set(requested))
    if unknown:
        raise VersionContractError(
            "candidate resolved: components without a requested policy: "
            + ", ".join(unknown))
    for component_id in sorted(resolved):
        _validate_resolution(component_id, resolved[component_id], errors)
    if errors:
        _aggregate("candidate resolved", errors)
    missing = sorted(set(requested) - set(resolved))
    if missing:
        raise VersionContractError(
            "candidate resolved: every requested component needs a resolution "
            "— a candidate is resolution evidence, so requested without "
            "resolved is not a persistable shape (missing: "
            + ", ".join(missing) + ")")

    enabled = doc.get("enabled")
    if not isinstance(enabled, list) or not all(isinstance(e, str) for e in enabled):
        raise VersionContractError(
            f"candidate enabled: must be a list of component ids "
            f"(got a {_yaml_type(enabled)})")
    # enabled must name exactly the requested set: the candidate is per-bottle
    # evidence for the components this up enabled — nothing more, nothing less.
    if sorted(enabled) != sorted(requested):
        raise VersionContractError(
            "candidate enabled: must name exactly the requested components "
            f"(requested: {', '.join(sorted(requested))}; "
            f"enabled: {', '.join(sorted(enabled)) or 'none'})")

    platform = doc.get("platform")
    if not isinstance(platform, str) or not platform.strip():
        raise VersionContractError(
            "candidate platform: must be a non-empty string (the target "
            "platform resolution and the closures were computed for)")
    catalog_digest = doc.get("catalog_digest")
    if not isinstance(catalog_digest, str) or not HEX64_RE.match(catalog_digest):
        raise VersionContractError(
            "candidate catalog_digest: must be 64 lowercase hex characters "
            "(the Catalog.digest() the resolution ran against)")
    # created_at is volatile (excluded from the hash), so it may be absent —
    # but when present it must be a real value.
    if doc.get("created_at") is not None and not isinstance(
            doc["created_at"], str):
        raise VersionContractError(
            f"candidate created_at: must be a string (got a "
            f"{_yaml_type(doc['created_at'])})")
    return doc


def build_candidate(requested, resolved, enabled, platform, catalog_digest,
                    created_at=None):
    """Construct a Candidate from its parts (all already validated shapes)."""
    doc = {"requested": requested, "resolved": resolved, "enabled": enabled,
           "platform": platform, "catalog_digest": catalog_digest}
    if created_at is not None:
        doc["created_at"] = created_at
    return Candidate(_validate_candidate(doc))


class Candidate:
    """A resolved, persistable candidate record (PLN §2). Hash-except-
    volatile: the candidate hash excludes created_at so re-resolving the same
    versions yields the same candidate hash."""

    def __init__(self, record):
        self.record = record

    @property
    def requested(self):
        return self.record["requested"]

    @property
    def resolved(self):
        return self.record["resolved"]

    @property
    def enabled(self):
        return self.record["enabled"]

    @property
    def platform(self):
        return self.record["platform"]

    @property
    def catalog_digest(self):
        return self.record["catalog_digest"]

    @property
    def hash(self):
        volatile = ("created_at",)
        return canonical_hash({k: v for k, v in self.record.items()
                               if k not in volatile})

    def to_dict(self):
        return self.record

    @classmethod
    def from_dict(cls, doc):
        return cls(_validate_candidate(doc))

    def __eq__(self, other):
        return isinstance(other, Candidate) and self.record == other.record


# ── Build receipt (PLN §2: builds/<image-id>.json) ───────────────────────

RECEIPT_FIELDS = frozenset({"candidate_hash", "image", "observations", "built_at"})
_IMAGE_FIELDS = frozenset({"id", "tag"})
_OBSERVATION_FIELDS = frozenset({"version", "commit", "digest"})


def _validate_receipt(doc):
    # Secret-shaped field names first (see _validate_candidate).
    assert_persistable(doc, "build receipt")
    if not isinstance(doc, dict):
        raise VersionContractError(
            f"build receipt must be a map (got a {_yaml_type(doc)})")
    extra = ",".join(sorted(k for k in doc if k not in RECEIPT_FIELDS))
    if extra:
        raise VersionContractError(
            f"build receipt: unsupported field(s): {extra} "
            f"(supported: {', '.join(sorted(RECEIPT_FIELDS))})")

    candidate_hash = doc.get("candidate_hash")
    if not isinstance(candidate_hash, str) or not HEX64_RE.match(candidate_hash):
        raise VersionContractError(
            "build receipt candidate_hash: must be 64 lowercase hex characters "
            "(the Candidate.hash the image was built from)")

    image = doc.get("image")
    if not isinstance(image, dict):
        raise VersionContractError(
            f"build receipt image: must be a map (got a {_yaml_type(image)})")
    extra = ",".join(sorted(k for k in image if k not in _IMAGE_FIELDS))
    if extra:
        raise VersionContractError(
            f"build receipt image: unsupported field(s): {extra} (only id and tag)")
    if not isinstance(image.get("id"), str) or not image["id"].strip():
        raise VersionContractError(
            "build receipt image.id: must be a non-empty string (the built "
            "image identity the receipt is evidence of)")
    if image.get("tag") is not None and (
            not isinstance(image["tag"], str) or not image["tag"].strip()):
        raise VersionContractError(
            "build receipt image.tag: must be a non-empty string when present")

    observations = doc.get("observations")
    if not isinstance(observations, dict):
        raise VersionContractError(
            f"build receipt observations: must be a map (got a "
            f"{_yaml_type(observations)})")
    errors = []
    for component_id, obs in observations.items():
        if not isinstance(obs, dict):
            errors.append(
                f"observations '{component_id}': must be a map (got a "
                f"{_yaml_type(obs)})")
            continue
        extra = ",".join(sorted(k for k in obs if k not in _OBSERVATION_FIELDS))
        if extra:
            errors.append(
                f"observations '{component_id}': unsupported field(s): {extra} "
                "(only version, commit, digest)")
            continue
        if not any(obs.get(f) for f in ("version", "commit", "digest")):
            errors.append(
                f"observations '{component_id}': must carry at least one "
                "identity field")
            continue
        for field in ("version", "commit", "digest"):
            val = obs.get(field)
            if val is not None and _nonempty_str(
                    val, f"observations '{component_id}' {field}", errors) is None:
                continue
    if errors:
        _aggregate("build receipt observations", errors)

    if _nonempty_str(doc.get("built_at"), "build receipt built_at", errors) is None:
        raise VersionContractError(
            "build receipt built_at: must be a non-empty string (ISO8601 UTC)")
    return doc


def build_receipt(candidate_hash, image, observations, built_at):
    """Construct a validated BuildReceipt."""
    return BuildReceipt(_validate_receipt({
        "candidate_hash": candidate_hash, "image": image,
        "observations": observations, "built_at": built_at,
    }))


class BuildReceipt:
    def __init__(self, record):
        self.record = record

    @property
    def candidate_hash(self):
        return self.record["candidate_hash"]

    @property
    def image(self):
        return self.record["image"]

    @property
    def observations(self):
        return self.record["observations"]

    def matches_candidate(self, candidate):
        return candidate is not None and self.candidate_hash == candidate.hash

    def to_dict(self):
        return self.record

    @classmethod
    def from_dict(cls, doc):
        return cls(_validate_receipt(doc))

    def __eq__(self, other):
        return isinstance(other, BuildReceipt) and self.record == other.record


# ── Running observation (live inspection, PLN §2) ────────────────────────

RUNNING_FIELDS = frozenset({"image_id", "labels", "observed_at"})


def _validate_running(doc):
    if not isinstance(doc, dict):
        raise VersionContractError(
            f"running observation must be a map (got a {_yaml_type(doc)})")
    extra = ",".join(sorted(k for k in doc if k not in RUNNING_FIELDS))
    if extra:
        raise VersionContractError(
            f"running observation: unsupported field(s): {extra} "
            f"(supported: {', '.join(sorted(RUNNING_FIELDS))})")
    if not isinstance(doc.get("image_id"), str) or not doc["image_id"].strip():
        raise VersionContractError(
            "running image_id: must be a non-empty string (the image the "
            "container actually runs)")
    labels = doc.get("labels")
    if not isinstance(labels, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in labels.items()):
        raise VersionContractError(
            "running labels: must be a map of string → string (the image/"
            "container labels live inspection read)")
    if not _nonempty_str(doc.get("observed_at"), "running observed_at", []):
        raise VersionContractError(
            "running observed_at: must be a non-empty string (ISO8601 UTC)")
    return doc


def running_observation(image_id, labels, observed_at):
    """Construct a validated running observation (not persisted — it is live
    inspection input)."""
    return _validate_running({"image_id": image_id, "labels": labels,
                              "observed_at": observed_at})


def confirm_running(receipt, running):
    """Raise unless the running observation is evidence that THIS receipt's
    image is running: same image id, the image carries this build's candidate
    hash label, and every component the receipt observed shows the same
    version live. Returns the validated observation on success."""
    running = _validate_running(running)
    if running["image_id"] != receipt.image["id"]:
        raise VersionContractError(
            f"cannot present a built image as running: the observation names "
            f"image {running['image_id']} but the receipt built "
            f"{receipt.image['id']}")
    label_hash = running["labels"].get("candidate_hash")
    if label_hash is None:
        raise VersionContractError(
            "cannot present a built image as running: the running image "
            "carries no candidate_hash label")
    if label_hash != receipt.candidate_hash:
        raise VersionContractError(
            f"cannot present a built image as running: the image's "
            f"candidate_hash label is {label_hash} but the receipt was built "
            f"from {receipt.candidate_hash}")
    for component_id, obs in sorted(receipt.observations.items()):
        expected = obs.get("version")
        if expected is None:
            # The running labels carry component versions (PLN §2); an
            # observation identified only by commit/digest has no live label
            # to compare against, so it can never confirm running.
            raise VersionContractError(
                f"cannot present a built image as running: the receipt "
                f"observed {component_id} without a version — the running "
                "labels carry component versions, so a version observation "
                "is required to confirm it")
        live = running["labels"].get(component_id)
        if live != expected:
            raise VersionContractError(
                f"cannot present a built image as running: {component_id} "
                f"reports {live!r} live but the receipt observed {expected!r}")
    return running


def component_state(component, candidate=None, receipt=None, running=None):
    """Derive one component's state from the evidence actually supplied —
    the ONLY place states are derived, so status output cannot drift:
      - external/deferred catalog components are their classification;
      - built evidence is a receipt whose candidate_hash matches a candidate
        that REQUESTED this component (a receipt for an unrelated candidate,
        or a component the candidate never covered, is not build evidence);
      - running evidence additionally requires a receipt observation for this
        component and a running observation whose image id, candidate_hash
        label, and component versions match the receipt — an omitted
        observation is absence of evidence, never running evidence;
      - a matching candidate means resolved; without any evidence the
        component is merely requested."""
    if component.policy not in MANAGED_POLICIES:
        return State.from_policy(component.policy)
    if running is not None:
        if receipt is None:
            raise VersionContractError(
                f"cannot present {component.id} as running without a build "
                "receipt — resolution alone is not build or running evidence")
        if candidate is None or not receipt.matches_candidate(candidate):
            raise VersionContractError(
                f"cannot present {component.id} as running: the receipt does "
                "not match the candidate")
        if component.id not in candidate.requested:
            raise VersionContractError(
                f"cannot present {component.id} as running: the candidate "
                "never requested it (components outside the candidate have "
                "no resolution or build evidence)")
        if component.id not in receipt.observations:
            raise VersionContractError(
                f"cannot present {component.id} as running: the receipt "
                "records no component observation for it (an omitted "
                "observation is absence of evidence, not running evidence)")
        confirm_running(receipt, running)
        return State.RUNNING
    if receipt is not None:
        if not receipt.matches_candidate(candidate):
            raise VersionContractError(
                f"cannot present {component.id} as built: the receipt's "
                f"candidate_hash does not match the candidate")
        if component.id not in candidate.requested:
            # The receipt pins a candidate that never covered this component:
            # no build evidence for it. A receipt that nevertheless observes
            # it contradicts its own candidate — refuse that too.
            if component.id in receipt.observations:
                raise VersionContractError(
                    f"cannot present {component.id} as built: the receipt "
                    "observes it but the candidate never requested it")
            return State.REQUESTED
        return State.BUILT
    if candidate is not None and component.id in candidate.requested:
        return State.RESOLVED
    return State.REQUESTED
