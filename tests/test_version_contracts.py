#!/usr/bin/env python3
"""Unit tests for src/version_contracts.py (PLN - Managed Versions, Step 1).

Pins the Dark contract layer: catalog schema, manifest `versions:` override
validation, candidate/build-receipt records, canonical hashing, and the
status model that keeps requested/resolved/built/running/external/deferred
distinct. Nothing here touches up.sh or manifest.py — the Dark pin at the
bottom fails if either ever references this module before Step 7 wires it.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import version_contracts as v


def catalog_doc():
    """A small but complete in-memory catalog covering every policy."""
    return {
        "schema": 1,
        "components": {
            "agent.aider": {
                "name": "Aider", "kind": "agent", "owner": "agent.aider",
                "enabled_when": "always", "policy": "latest",
                "resolver": "npm", "platform": "linux/amd64",
                "source": {"type": "npm", "location": "registry.npmjs.org/aider-chat"},
            },
            "agent.claude": {
                "name": "Claude", "kind": "agent", "owner": "agent.claude",
                "enabled_when": "always", "policy": "release-line",
                "constraint": "2.x", "resolver": "npm",
                "platform": "linux/amd64",
                "source": {"type": "npm", "location": "registry.npmjs.org/@anthropic-ai/claude-code"},
            },
            "plugin.browser": {
                "name": "Browser", "kind": "plugin", "owner": "plugin.browser",
                "enabled_when": "always", "policy": "external",
            },
            "base-images": {
                "name": "Base images", "kind": "group",
                "enabled_when": "always", "policy": "deferred",
                "deferral_reason": "inventoried, existing behavior (out of PLN scope)",
            },
        },
    }


def make_catalog(**over):
    doc = catalog_doc()
    doc["components"].update(over.pop("extra_components", {}))
    doc.update(over)
    return v.Catalog.from_dict(doc)


def make_candidate(catalog, **over):
    requested = over.pop("requested", {"agent.aider": {"policy": "latest"}})
    resolved = over.pop("resolved", {"agent.aider": {"version": "1.9.4"}})
    enabled = over.pop("enabled", list(requested))
    digest = over.pop("catalog_digest", catalog.digest())
    return v.build_candidate(
        requested=requested, resolved=resolved, enabled=enabled,
        platform=over.pop("platform", "linux/amd64"), catalog_digest=digest,
        **over)


def make_receipt(candidate, **over):
    return v.build_receipt(
        candidate_hash=over.pop("candidate_hash", candidate.hash),
        image=over.pop("image", {"id": "sha256:" + "a" * 64}),
        observations=over.pop("observations", {"agent.aider": {"version": "1.9.4"}}),
        built_at=over.pop("built_at", "2026-09-22T00:00:00Z"))


def running_for(receipt, **over):
    return v.running_observation(
        image_id=over.pop("image_id", receipt.image["id"]),
        labels=over.pop("labels", {"candidate_hash": receipt.candidate_hash,
                                   "agent.aider": "1.9.4"}),
        observed_at=over.pop("observed_at", "2026-09-22T01:00:00Z"))


class CatalogSchemaTests(unittest.TestCase):
    def test_every_policy_loads(self):
        catalog = make_catalog()
        self.assertEqual(catalog.components["agent.aider"].policy, "latest")
        self.assertEqual(catalog.components["plugin.browser"].policy, "external")
        self.assertEqual(catalog.components["base-images"].policy, "deferred")

    def test_round_trip_is_lossless(self):
        catalog = make_catalog()
        self.assertEqual(v.Catalog.from_dict(catalog.to_dict()), catalog)

    def test_digest_is_canonical(self):
        # Same catalog rebuilt from reordered components digests identically.
        doc = catalog_doc()
        doc["components"] = dict(reversed(list(doc["components"].items())))
        self.assertEqual(make_catalog().digest(), v.Catalog.from_dict(doc).digest())

    def test_rejects_unknown_schema_version(self):
        doc = catalog_doc()
        doc["schema"] = 2
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("only schema 1 is defined (got 2)", str(ctx.exception))

    def test_rejects_unknown_top_level_field(self):
        doc = catalog_doc()
        doc["policies"] = {}
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("unsupported field(s): policies", str(ctx.exception))

    def test_rejects_unknown_component_field(self):
        doc = catalog_doc()
        doc["components"]["agent.aider"]["constrant"] = "typo"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("unsupported field(s): constrant", str(ctx.exception))

    def test_rejects_bad_policy(self):
        doc = catalog_doc()
        doc["components"]["agent.aider"]["policy"] = "newest"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("policy must be one of", str(ctx.exception))

    def test_rejects_illegal_component_id(self):
        doc = catalog_doc()
        doc["components"]["Agent.Aider!"] = doc["components"]["agent.aider"]
        del doc["components"]["agent.aider"]
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("illegal id", str(ctx.exception))

    def test_deferred_requires_deferral_reason(self):
        doc = catalog_doc()
        del doc["components"]["base-images"]["deferral_reason"]
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("deferral_reason", str(ctx.exception))

    def test_deferral_reason_forbidden_on_managed(self):
        doc = catalog_doc()
        doc["components"]["agent.aider"]["deferral_reason"] = "why not"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("only valid for policy: deferred", str(ctx.exception))

    def test_exact_requires_constraint(self):
        doc = catalog_doc()
        aid = doc["components"]["agent.aider"]
        aid["policy"] = "exact"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("component 'agent.aider' constraint", str(ctx.exception))

    def test_external_rejects_managed_only_fields(self):
        doc = catalog_doc()
        doc["components"]["plugin.browser"]["resolver"] = "npm"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("carries no resolver:", str(ctx.exception))

    def test_group_cannot_claim_an_installer_owner(self):
        doc = catalog_doc()
        doc["components"]["base-images"]["owner"] = "Dockerfile"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("carries no installer owner", str(ctx.exception))

    def test_agent_requires_owner(self):
        doc = catalog_doc()
        del doc["components"]["agent.aider"]["owner"]
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("owner", str(ctx.exception))

    def test_bundle_requires_closure_bundle(self):
        doc = catalog_doc()
        doc["components"]["agent.aider"]["bundle"] = "compat/aider"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        self.assertIn("only valid with closure: bundle", str(ctx.exception))

    def test_errors_aggregate_per_catalog(self):
        doc = catalog_doc()
        doc["components"]["agent.aider"]["name"] = ""
        doc["components"]["agent.claude"]["resolver"] = "guess"
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Catalog.from_dict(doc)
        text = str(ctx.exception)
        self.assertIn("catalog failed validation:", text)
        self.assertIn("agent.aider", text)
        self.assertIn("agent.claude", text)


class ManifestOverrideTests(unittest.TestCase):
    """Pin: malformed and unknown override rejection."""

    def test_absent_versions_means_no_overrides(self):
        result = v.validate_manifest_versions(None, make_catalog())
        self.assertEqual(result, v.ManifestVersions({}))

    def test_valid_policy_and_constraint_override(self):
        result = v.validate_manifest_versions(
            {"agent.aider": {"policy": "exact", "constraint": "1.9.4"}},
            make_catalog())
        self.assertEqual(
            result.effective(make_catalog().components["agent.aider"]),
            ("exact", "1.9.4"))

    def test_constraint_only_override_keeps_catalog_policy(self):
        result = v.validate_manifest_versions(
            {"agent.claude": {"constraint": "2.1"}}, make_catalog())
        self.assertEqual(
            result.effective(make_catalog().components["agent.claude"]),
            ("release-line", "2.1"))

    def test_unknown_component_id_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"agent.nope": {"policy": "exact", "constraint": "1"}}, make_catalog())
        self.assertIn(
            "versions override 'agent.nope': no such component in the catalog",
            str(ctx.exception))
        self.assertIn("known: agent.aider, agent.claude, base-images, plugin.browser",
                      str(ctx.exception))

    def test_unknown_override_field_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"agent.aider": {"policy": "exact", "version": "1"}}, make_catalog())
        self.assertIn("unsupported field(s): version (only policy and constraint)",
                      str(ctx.exception))

    def test_malformed_non_map_versions_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(["agent.aider"], make_catalog())
        self.assertIn("manifest versions: must be a map", str(ctx.exception))

    def test_malformed_non_map_override_value_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions({"agent.aider": "exact"}, make_catalog())
        self.assertIn(
            "versions override 'agent.aider': must be a map of policy/value "
            "fields (got a string)", str(ctx.exception))

    def test_bad_policy_value_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"agent.aider": {"policy": "newest"}}, make_catalog())
        self.assertIn("policy must be one of", str(ctx.exception))

    def test_override_cannot_externalize_or_defer(self):
        for policy in ("external", "deferred"):
            with self.assertRaises(v.VersionContractError) as ctx:
                v.validate_manifest_versions(
                    {"agent.aider": {"policy": policy}}, make_catalog())
            self.assertIn("not an override target", str(ctx.exception))

    def test_external_component_cannot_be_overridden(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"plugin.browser": {"policy": "latest"}}, make_catalog())
        self.assertIn(
            "versions override 'plugin.browser': the component is external in "
            "the catalog and its policy cannot be overridden", str(ctx.exception))

    def test_deferred_component_cannot_be_overridden(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"base-images": {"policy": "latest"}}, make_catalog())
        self.assertIn(
            "the component is deferred in the catalog and its policy cannot "
            "be overridden", str(ctx.exception))

    def test_policy_switch_to_release_line_requires_constraint(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"agent.aider": {"policy": "release-line"}}, make_catalog())
        self.assertIn("policy release-line requires a constraint", str(ctx.exception))

    def test_constraint_must_be_a_non_empty_string(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"agent.aider": {"constraint": ""}}, make_catalog())
        self.assertIn("constraint must be a non-empty string", str(ctx.exception))

    def test_errors_aggregate(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.validate_manifest_versions(
                {"agent.nope": {"policy": "exact"},
                 "agent.aider": {"bogus": True}}, make_catalog())
        text = str(ctx.exception)
        self.assertIn("manifest versions failed validation:", text)
        self.assertIn("agent.nope", text)
        self.assertIn("agent.aider", text)


class CanonicalHashTests(unittest.TestCase):
    """Pin: deterministic hash behavior."""

    def test_same_content_different_insertion_order_same_hash(self):
        a = {"b": 1, "a": {"y": 2, "x": [1, 2, {"k": "v"}]}}
        b = {"a": {"x": [1, 2, {"k": "v"}], "y": 2}, "b": 1}
        self.assertEqual(v.canonical_hash(a), v.canonical_hash(b))

    def test_unicode_is_stable_utf8(self):
        self.assertEqual(v.canonical_hash({"name": "café"}),
                         v.canonical_hash({"name": "café"}))
        self.assertNotEqual(v.canonical_hash({"name": "café"}),
                            v.canonical_hash({"name": "cafe"}))

    def test_any_content_change_changes_the_hash(self):
        base = {"policy": "latest"}
        self.assertNotEqual(v.canonical_hash(base),
                            v.canonical_hash({"policy": "exact"}))

    def test_hash_is_64_lowercase_hex(self):
        digest = v.canonical_hash({"a": 1})
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_non_finite_numbers_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.canonical_hash({"x": float("nan")})
        self.assertIn("not canonically encodable", str(ctx.exception))

    def test_candidate_hash_ignores_created_at(self):
        catalog = make_catalog()
        a = make_candidate(catalog)
        b = v.build_candidate(
            requested=a.requested, resolved=a.resolved, enabled=a.enabled,
            platform=a.platform, catalog_digest=a.catalog_digest,
            created_at="2026-09-23T10:00:00Z")
        self.assertEqual(a.hash, b.hash)

    def test_candidate_hash_changes_when_resolution_changes(self):
        catalog = make_catalog()
        a = make_candidate(catalog)
        b = v.build_candidate(
            requested=a.requested,
            resolved={"agent.aider": {"version": "1.9.5"}},
            enabled=a.enabled, platform=a.platform,
            catalog_digest=a.catalog_digest)
        self.assertNotEqual(a.hash, b.hash)


class CandidateTests(unittest.TestCase):
    def test_valid_candidate_builds_and_round_trips(self):
        catalog = make_catalog()
        candidate = make_candidate(catalog)
        self.assertEqual(v.Candidate.from_dict(candidate.to_dict()), candidate)

    def test_candidate_carries_the_catalog_digest(self):
        catalog = make_catalog()
        self.assertEqual(make_candidate(catalog).catalog_digest, catalog.digest())

    def test_requested_without_resolved_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "latest"}},
                resolved={}, enabled=["agent.aider"],
                platform="linux/amd64", catalog_digest=make_catalog().digest())
        self.assertIn("requested without resolved is not a persistable shape",
                      str(ctx.exception))
        self.assertIn("missing: agent.aider", str(ctx.exception))

    def test_resolved_without_requested_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "latest"}},
                resolved={"agent.aider": {"version": "1"}, "agent.codex": {"version": "2"}},
                enabled=["agent.aider"], platform="linux/amd64",
                catalog_digest=make_catalog().digest())
        self.assertIn("components without a requested policy: agent.codex",
                      str(ctx.exception))

    def test_resolution_without_identity_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "latest"}},
                resolved={"agent.aider": {}},
                enabled=["agent.aider"], platform="linux/amd64",
                catalog_digest=make_catalog().digest())
        self.assertIn("must carry at least one identity field", str(ctx.exception))

    def test_external_never_enters_a_candidate(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"plugin.browser": {"policy": "external"}},
                resolved={"plugin.browser": {"version": "1"}},
                enabled=["plugin.browser"], platform="linux/amd64",
                catalog_digest=make_catalog().digest())
        self.assertIn("external and deferred components never enter a candidate",
                      str(ctx.exception))

    def test_enabled_must_match_requested(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "latest"}},
                resolved={"agent.aider": {"version": "1.9.4"}},
                enabled=["agent.claude"], platform="linux/amd64",
                catalog_digest=make_catalog().digest())
        self.assertIn("must name exactly the requested components", str(ctx.exception))

    def test_bad_catalog_digest_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "latest"}},
                resolved={"agent.aider": {"version": "1.9.4"}},
                enabled=["agent.aider"], platform="linux/amd64",
                catalog_digest="d1")
        self.assertIn("candidate catalog_digest: must be 64 lowercase hex",
                      str(ctx.exception))

    def test_bad_asset_sha256_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "exact", "constraint": "1.0"}},
                resolved={"agent.aider": {
                    "version": "1.0", "asset": {"name": "aider.tgz", "sha256": "zz"}}},
                enabled=["agent.aider"], platform="linux/amd64",
                catalog_digest=make_catalog().digest())
        self.assertIn("asset.sha256: must be 64 lowercase hex", str(ctx.exception))

    def test_unknown_candidate_field_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.Candidate.from_dict({"bogus": True})
        self.assertIn("candidate: unsupported field(s): bogus", str(ctx.exception))


class ReceiptTests(unittest.TestCase):
    def test_valid_receipt_round_trips(self):
        candidate = make_candidate(make_catalog())
        receipt = make_receipt(candidate)
        self.assertEqual(v.BuildReceipt.from_dict(receipt.to_dict()), receipt)

    def test_receipt_pinned_to_its_candidate(self):
        catalog = make_catalog()
        candidate = make_candidate(catalog)
        self.assertTrue(make_receipt(candidate).matches_candidate(candidate))
        other = make_candidate(catalog, resolved={"agent.aider": {"version": "1.9.5"}})
        self.assertFalse(make_receipt(candidate).matches_candidate(other))

    def test_bad_candidate_hash_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_receipt(candidate_hash="abc", image={"id": "sha256:img"},
                            observations={}, built_at="2026-09-22T00:00:00Z")
        self.assertIn("must be 64 lowercase hex characters", str(ctx.exception))

    def test_image_id_required(self):
        candidate = make_candidate(make_catalog())
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_receipt(candidate_hash=candidate.hash, image={},
                            observations={}, built_at="2026-09-22T00:00:00Z")
        self.assertIn("image.id: must be a non-empty string", str(ctx.exception))

    def test_built_at_required(self):
        candidate = make_candidate(make_catalog())
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_receipt(candidate_hash=candidate.hash, image={"id": "x"},
                            observations={}, built_at="")
        self.assertIn("built_at: must be a non-empty string", str(ctx.exception))

    def test_observation_needs_identity(self):
        candidate = make_candidate(make_catalog())
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_receipt(candidate_hash=candidate.hash, image={"id": "x"},
                            observations={"agent.aider": {}},
                            built_at="2026-09-22T00:00:00Z")
        self.assertIn("must carry at least one identity field", str(ctx.exception))

    def test_unknown_receipt_field_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.BuildReceipt.from_dict({"bogus": True})
        self.assertIn("build receipt: unsupported field(s): bogus", str(ctx.exception))


class StatusModelTests(unittest.TestCase):
    """Pin: transitions cannot present resolved as built/running."""

    def setUp(self):
        self.catalog = make_catalog()
        self.component = self.catalog.components["agent.aider"]

    def test_requested_without_evidence(self):
        self.assertEqual(v.component_state(self.component), v.State.REQUESTED)

    def test_candidate_means_resolved(self):
        candidate = make_candidate(self.catalog)
        self.assertEqual(
            v.component_state(self.component, candidate=candidate),
            v.State.RESOLVED)

    def test_resolved_cannot_present_as_built_without_receipt(self):
        candidate = make_candidate(self.catalog)
        # The presentation guard refuses to derive built from a candidate:
        # a receipt built from a DIFFERENT candidate (changed resolution)
        # must not let resolution count as built.
        other = make_candidate(self.catalog,
                               resolved={"agent.aider": {"version": "1.9.5"}})
        receipt = make_receipt(other)
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(self.component, candidate=candidate, receipt=receipt)
        self.assertIn("cannot present agent.aider as built", str(ctx.exception))

    def test_resolved_cannot_present_as_running_without_receipt(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(self.component, candidate=candidate, running=running_for(receipt))
        self.assertIn("cannot present agent.aider as running without a build receipt",
                      str(ctx.exception))

    def test_running_requires_matching_receipt(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        other = make_candidate(self.catalog,
                               resolved={"agent.aider": {"version": "1.9.5"}})
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(self.component, candidate=other, receipt=receipt,
                              running=running_for(receipt))
        self.assertIn("the receipt does not match the candidate", str(ctx.exception))

    def test_full_chain_presents_running(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        running = running_for(receipt)
        self.assertEqual(
            v.component_state(self.component, candidate, receipt, running),
            v.State.RUNNING)

    def test_running_image_id_mismatch_rejected(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(
                self.component, candidate, receipt,
                running_for(receipt, image_id="sha256:" + "b" * 64))
        self.assertIn("the observation names image", str(ctx.exception))

    def test_running_without_candidate_hash_label_rejected(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(
                self.component, candidate, receipt,
                running_for(receipt, labels={"agent.aider": "1.9.4"}))
        self.assertIn("carries no candidate_hash label", str(ctx.exception))

    def test_running_with_stale_candidate_hash_label_rejected(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(
                self.component, candidate, receipt,
                running_for(receipt, labels={"candidate_hash": "0" * 64,
                                             "agent.aider": "1.9.4"}))
        self.assertIn("candidate_hash label is", str(ctx.exception))

    def test_running_component_version_drift_rejected(self):
        candidate = make_candidate(self.catalog)
        receipt = make_receipt(candidate)
        with self.assertRaises(v.VersionContractError) as ctx:
            v.component_state(
                self.component, candidate, receipt,
                running_for(receipt, labels={"candidate_hash": candidate.hash,
                                             "agent.aider": "1.9.5"}))
        self.assertIn("reports '1.9.5' live but the receipt observed '1.9.4'",
                      str(ctx.exception))

    def test_external_and_deferred_are_their_own_states(self):
        self.assertEqual(
            v.component_state(self.catalog.components["plugin.browser"]),
            v.State.EXTERNAL)
        self.assertEqual(
            v.component_state(self.catalog.components["base-images"]),
            v.State.DEFERRED)

    def test_transition_matrix(self):
        self.assertEqual(v.transition(v.State.REQUESTED, v.State.RESOLVED),
                         v.State.RESOLVED)
        self.assertEqual(v.transition(v.State.RESOLVED, v.State.BUILT),
                         v.State.BUILT)
        self.assertEqual(v.transition(v.State.BUILT, v.State.RUNNING),
                         v.State.RUNNING)
        self.assertEqual(v.transition(v.State.RUNNING, v.State.REQUESTED),
                         v.State.REQUESTED)

    def test_transitions_refuse_skips_and_backwards(self):
        # resolved cannot jump straight to running: build evidence is missing.
        with self.assertRaises(v.VersionContractError) as ctx:
            v.transition(v.State.RESOLVED, v.State.RUNNING)
        self.assertIn("cannot present a resolved component as running", str(ctx.exception))
        with self.assertRaises(v.VersionContractError):
            v.transition(v.State.RESOLVED, v.State.REQUESTED)
        with self.assertRaises(v.VersionContractError):
            v.transition(v.State.RUNNING, v.State.RESOLVED)
        with self.assertRaises(v.VersionContractError):
            v.transition(v.State.REQUESTED, v.State.BUILT)

    def test_external_and_deferred_never_transition(self):
        with self.assertRaises(v.VersionContractError):
            v.transition(v.State.EXTERNAL, v.State.RUNNING)
        with self.assertRaises(v.VersionContractError):
            v.transition(v.State.DEFERRED, v.State.REQUESTED)

    def test_from_policy_rejects_managed_policies(self):
        with self.assertRaises(v.VersionContractError):
            v.State.from_policy("latest")


class PersistableRecordTests(unittest.TestCase):
    """Pin: secrets or credential-like fields are rejected from persisted
    records — candidates and build receipts never reach disk with one."""

    def test_candidate_rejects_credential_like_resolution_field(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_candidate(
                requested={"agent.aider": {"policy": "latest"}},
                resolved={"agent.aider": {"version": "1.9.4",
                                          "auth_token": "shh"}},
                enabled=["agent.aider"], platform="linux/amd64",
                catalog_digest=make_catalog().digest())
        self.assertIn("secret or credential-like field", str(ctx.exception))
        self.assertIn("resolved.agent.aider.auth_token", str(ctx.exception))

    def test_receipt_rejects_credential_like_field(self):
        candidate = make_candidate(make_catalog())
        with self.assertRaises(v.VersionContractError) as ctx:
            v.build_receipt(candidate_hash=candidate.hash,
                            image={"id": "x", "registry_password": "shh"},
                            observations={}, built_at="2026-09-22T00:00:00Z")
        self.assertIn("image.registry_password", str(ctx.exception))

    def test_from_dict_rejects_secrets_too(self):
        with self.assertRaises(v.VersionContractError):
            v.Candidate.from_dict({"requested": {"agent.api_key": {"policy": "latest"}},
                                   "resolved": {}, "enabled": [],
                                   "platform": "p", "catalog_digest": "0" * 64})

    def test_ordinary_field_names_stay_allowed(self):
        candidate = v.build_candidate(
            requested={"agent.aider": {"policy": "latest"}},
            resolved={"agent.aider": {"version": "1.9.4", "digest": "a" * 64}},
            enabled=["agent.aider"], platform="linux/amd64",
            catalog_digest=make_catalog().digest(), created_at="2026-09-22T00:00:00Z")
        self.assertEqual(v.Candidate.from_dict(candidate.to_dict()), candidate)

    def test_non_string_map_keys_rejected(self):
        with self.assertRaises(v.VersionContractError) as ctx:
            v.assert_persistable({1: "x"})
        self.assertIn("map keys must be strings", str(ctx.exception))


class DarkPinTests(unittest.TestCase):
    """Step 1 is Dark: nothing outside this module and its tests may reference
    it. manifest.py and up.sh are the surfaces Step 7 must touch on purpose —
    a stray import or call there changes `up` behavior ahead of the plan."""

    def test_up_and_manifest_do_not_reference_version_contracts(self):
        repo = Path(__file__).parent.parent
        for name in ("up.sh", "src/manifest.py", "Dockerfile",
                     "compose/docker-compose.local.yml"):
            text = (repo / name).read_text(encoding="utf-8")
            self.assertNotIn("version_contracts", text,
                             f"{name} references version_contracts — Step 1 "
                             "is Dark; wire it only in Step 7")

    def test_module_is_stdlib_only(self):
        source = (Path(__file__).parent.parent / "src" / "version_contracts.py"
                  ).read_text(encoding="utf-8")
        imports = [line.split()[1] for line in source.splitlines()
                   if line.startswith("import ")]
        self.assertEqual(sorted(imports), ["enum", "hashlib", "json", "re"])


if __name__ == "__main__":
    unittest.main()
