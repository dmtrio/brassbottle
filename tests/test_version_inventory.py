#!/usr/bin/env python3
"""Unit tests for src/version_audit.py (version-selection inventory audit).

Pins the inventory audit: descriptor coverage (adding a descriptor fails),
pinned anchors (changing a version field fails), declared-location
existence (deleting a location fails), the floating-launcher ban (adding a
floating runtime launcher fails), scoped pattern discovery, and the report
listing every classification — pinned, floating, external, deferred — with
a reason for every deferred item. Also pins the Dark contract: up.sh and
manifest.py never reference the audit module, and one integration test runs
the audit against this repository's real inventory (skipped without yq).
"""

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
REPO_ROOT = SRC.parent
sys.path.insert(0, str(SRC))
import version_audit as audit  # noqa: E402
import version_contracts as v  # noqa: E402


def catalog_doc():
    """A small in-memory catalog covering every policy and kind."""
    return {
        "schema": 1,
        "components": {
            "agent.aider": {
                "name": "Aider", "kind": "agent", "owner": "agents/aider/agent.yml",
                "enabled_when": "always", "policy": "latest", "resolver": "pypi",
                "platform": "linux",
                "source": {"type": "pypi", "location": "pypi.org/aider-chat"},
            },
            "agent.pi": {
                "name": "pi coding agent", "kind": "agent",
                "owner": "agents/pi/agent.yml", "enabled_when": "always",
                "policy": "latest", "resolver": "npm", "platform": "linux",
                "source": {"type": "npm",
                           "location": "registry.npmjs.org/@earendil-works/pi-coding-agent"},
            },
            "agent.pi-mcp-adapter": {
                "name": "pi MCP adapter", "kind": "agent",
                "owner": "agents/pi/agent.yml", "enabled_when": "always",
                "policy": "exact", "constraint": "2.32.1", "resolver": "npm",
                "platform": "linux",
                "source": {"type": "npm",
                           "location": "registry.npmjs.org/pi-mcp-adapter"},
            },
            "plugin.annotated-watch": {
                "name": "Annotated Watch", "kind": "plugin",
                "owner": "plugins/annotated-watch/plugin.yml",
                "enabled_when": "always", "policy": "external",
            },
            "plugin.archex": {
                "name": "Archex", "kind": "plugin",
                "owner": "plugins/archex/plugin.yml", "enabled_when": "always",
                "policy": "latest", "resolver": "pypi", "platform": "linux",
                "source": {"type": "pypi", "location": "pypi.org/archex"},
            },
            "plugin.tinfoil": {
                "name": "Tinfoil", "kind": "plugin",
                "owner": "plugins/tinfoil/plugin.yml", "enabled_when": "always",
                "policy": "exact", "constraint": "0.1.2", "resolver": "npm",
                "platform": "linux", "closure": "lock",
                "source": {"type": "npm",
                           "location": "registry.npmjs.org/@tinfoilsh/pi-provider"},
            },
            "base-images": {
                "name": "Base images", "kind": "group",
                "enabled_when": "always", "policy": "deferred",
                "deferral_reason": "base-image tags are out of managed scope",
            },
            "base-tools": {
                "name": "Shared-image toolchain", "kind": "group",
                "enabled_when": "always", "policy": "deferred",
                "deferral_reason": "toolchain layers keep existing behavior",
            },
            "service-images": {
                "name": "Service images", "kind": "group",
                "enabled_when": "always", "policy": "deferred",
                "deferral_reason": "service-image pins keep existing behavior",
            },
        },
    }


def locations_doc():
    """The location inventory matching catalog_doc(), with anchors on every
    scanned selector line (the anchor-granularity discovery contract)."""
    return {
        "schema": 1,
        "locations": {
            "agent.aider": {
                "path": "agents/aider/agent.yml", "component": "agent.aider",
                "find": "pip3 install aider-chat",
            },
            "agent.pi": {
                "path": "agents/pi/agent.yml", "component": "agent.pi",
                "find": "npm install -g @earendil-works/pi-coding-agent",
            },
            "agent.pi-mcp-adapter": {
                "path": "agents/pi/agent.yml",
                "component": "agent.pi-mcp-adapter",
                "find": "pi-mcp-adapter@2.32.1",
            },
            "plugin.annotated-watch": {
                "path": "plugins/annotated-watch/plugin.yml",
                "component": "plugin.annotated-watch",
            },
            "plugin.archex": {
                "path": "plugins/archex/plugin.yml", "component": "plugin.archex",
                "find": "uv tool install archex",
            },
            "plugin.tinfoil": {
                "path": "plugins/tinfoil/plugin.yml", "component": "plugin.tinfoil",
                "find": "@tinfoilsh/pi-provider@0.1.2",
            },
            "plugin.tinfoil.deps": {
                "path": "plugins/tinfoil/plugin.yml", "component": "plugin.tinfoil",
                "find": "dependencies.tinfoil=1.2.1 dependencies.zod=4.6.3",
            },
            "plugin.tinfoil.ci": {
                "path": "plugins/tinfoil/plugin.yml", "component": "plugin.tinfoil",
                "find": "npm ci --omit=dev",
            },
            "base-images.dockerfile": {
                "path": "Dockerfile", "component": "base-images",
                "find": "FROM ubuntu:24.04",
            },
            "base-tools.pip": {
                "path": "Dockerfile", "component": "base-tools",
                "find": "pip3 install pipenv",
            },
            "service-images.ci": {
                "path": "ci.yml", "component": "service-images",
                "find": "docker pull ghcr.io/garethgeorge/backrest:v1.14.1",
            },
        },
        "exclusions": [
            {"path": "src/admin_vendor",
             "reason": "vendored upstream file; no live selector executes here"},
        ],
        "discovery": {
            "scope": ["agents", "plugins", "Dockerfile", "ci.yml"],
            "skip-names": ["package-lock.json"],
            "patterns": [r"\bnpm\s+install\b", r"\bpip3?\s+install\b",
                         r"\buv\s+tool\s+install\b", r"\b(npx|uvx)\b",
                         r"\bgit\s+clone\b", r"(?m)^FROM\s+\S+",
                         r"\bdocker\s+pull\s+\S+"],
        },
    }


class FixtureRepo:
    """A minimal repo tree the audit can run against (no yq involved: the
    catalog and locations pass through the in-memory validators)."""

    AIDER = ("binary: aider\ninstall: |\n  pip3 install aider-chat\n")
    PI = ("binary: pi\ninstall: |\n"
          "  npm install -g @earendil-works/pi-coding-agent\n"
          "  pi install npm:pi-mcp-adapter@2.32.1\n")
    ANNOTATED = "secrets:\n  KEY: {}\n"
    ARCHEX = ("install: |\n  uv tool install archex\n"
              "mcp:\n  archex:\n    command: archex\n")
    TINFOIL = ("install: |\n"
               "  npm pack @tinfoilsh/pi-provider@0.1.2\n"
               "  npm pkg set dependencies.tinfoil=1.2.1 dependencies.zod=4.6.3\n"
               "  npm ci --omit=dev\n")
    DOCKERFILE = "FROM ubuntu:24.04\nRUN pip3 install pipenv\n"
    CI_PULL = ("jobs:\n  smoke:\n    steps:\n"
               "      - run: docker pull ghcr.io/garethgeorge/backrest:v1.14.1\n")

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="djinn-audit-fixture-"))
        (self.root / "agents/aider").mkdir(parents=True)
        (self.root / "agents/pi").mkdir(parents=True)
        (self.root / "plugins/annotated-watch").mkdir(parents=True)
        (self.root / "plugins/archex").mkdir(parents=True)
        (self.root / "plugins/tinfoil").mkdir(parents=True)
        (self.root / "src/admin_vendor").mkdir(parents=True)
        (self.root / "agents/aider/agent.yml").write_text(self.AIDER)
        (self.root / "agents/pi/agent.yml").write_text(self.PI)
        (self.root / "plugins/annotated-watch/plugin.yml").write_text(self.ANNOTATED)
        (self.root / "plugins/archex/plugin.yml").write_text(self.ARCHEX)
        (self.root / "plugins/tinfoil/plugin.yml").write_text(self.TINFOIL)
        (self.root / "Dockerfile").write_text(self.DOCKERFILE)
        (self.root / "ci.yml").write_text(self.CI_PULL)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def catalog(self):
        return v.Catalog.from_dict(catalog_doc())

    def locations(self):
        return audit.validate_locations(locations_doc())

    def audit(self, catalog=None, locations=None):
        return audit.run_audit(self.root, catalog or self.catalog(),
                               locations or self.locations())

    def cleanup(self):
        shutil.rmtree(self.root)


class MappingTests(unittest.TestCase):
    def test_every_policy_maps_to_exactly_one_label(self):
        self.assertEqual(
            audit.POLICY_LABEL,
            {"exact": "pinned", "latest": "floating",
             "release-line": "floating", "external": "external",
             "deferred": "deferred"})
        self.assertEqual(sorted(audit.POLICY_LABEL), sorted(v.POLICIES))

    def test_label_of_derives_from_policy(self):
        catalog = v.Catalog.from_dict(catalog_doc())
        self.assertEqual(audit.label_of(catalog.components["agent.aider"]),
                         "floating")
        self.assertEqual(
            audit.label_of(catalog.components["agent.pi-mcp-adapter"]), "pinned")
        self.assertEqual(
            audit.label_of(catalog.components["plugin.annotated-watch"]),
            "external")
        self.assertEqual(audit.label_of(catalog.components["base-images"]),
                         "deferred")


class CleanAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FixtureRepo()

    def tearDown(self):
        self.fixture.cleanup()

    def test_clean_fixture_has_no_findings(self):
        self.assertEqual(self.fixture.audit(), [])

    def test_report_lists_every_classification_with_deferral_reasons(self):
        report = audit.format_report(self.fixture.catalog(),
                                     self.fixture.locations(), [])
        for label in ("pinned", "floating", "external", "deferred"):
            self.assertIn(label, report)
        self.assertIn("base-image tags are out of managed scope", report)
        self.assertIn("agent.pi-mcp-adapter", report)
        self.assertIn("plugin.tinfoil", report)

    def test_external_component_may_have_no_location(self):
        # plugin.annotated-watch is external: selecting nothing is legal.
        findings = self.fixture.audit()
        self.assertFalse(any("annotated-watch" in f for f in findings))


class MutationPinTests(unittest.TestCase):
    """Each mutation the version-selection mutation contract names must
    make the audit fail."""

    def setUp(self):
        self.fixture = FixtureRepo()

    def tearDown(self):
        self.fixture.cleanup()

    def test_adding_agent_descriptor_fails(self):
        self.fixture.write("agents/newcli/agent.yml",
                           "binary: newcli\ninstall: |\n  pip3 install newcli\n")
        findings = self.fixture.audit()
        self.assertTrue(any("undocumented agent descriptor" in f
                            for f in findings), findings)

    def test_adding_plugin_descriptor_fails(self):
        self.fixture.write("plugins/newtool/plugin.yml", "install: |\n  true\n")
        findings = self.fixture.audit()
        self.assertTrue(any("undocumented plugin descriptor" in f
                            for f in findings), findings)

    def test_descriptor_with_no_catalog_entry_fails(self):
        # A descriptor added to the location file but absent from the catalog
        # is the other half of the mutation: the audit still fails.
        doc = locations_doc()
        doc["locations"]["agent.newcli"] = {
            "path": "agents/newcli/agent.yml", "component": "agent.newcli",
            "find": "pip3 install newcli"}
        self.fixture.write("agents/newcli/agent.yml",
                           "binary: newcli\ninstall: |\n  pip3 install newcli\n")
        findings = self.fixture.audit(
            catalog=self.fixture.catalog(),
            locations=audit.validate_locations(doc))
        self.assertTrue(any("not in the catalog" in f for f in findings),
                        findings)

    def test_changing_version_field_in_installer_fails(self):
        # Bump the pinned pi-mcp-adapter spec without touching the catalog.
        self.fixture.write("agents/pi/agent.yml",
                           self.fixture.PI.replace("2.32.1", "2.33.0"))
        findings = self.fixture.audit()
        self.assertTrue(any("anchored selector text no longer in" in f
                            for f in findings), findings)

    def test_changing_version_field_in_catalog_fails(self):
        # Change the catalog's exact constraint without touching the installer.
        doc = catalog_doc()
        doc["components"]["agent.pi-mcp-adapter"]["constraint"] = "2.33.1"
        findings = self.fixture.audit(
            catalog=v.Catalog.from_dict(doc), locations=self.fixture.locations())
        self.assertTrue(any("the catalog pins" in f for f in findings), findings)

    def test_deleting_declared_selector_location_fails(self):
        (self.fixture.root / "agents/pi/agent.yml").unlink()
        findings = self.fixture.audit()
        self.assertTrue(any("no longer exists" in f for f in findings), findings)

    def test_adding_floating_runtime_launcher_fails(self):
        self.fixture.write(
            "plugins/tinfoil/plugin.yml",
            self.fixture.TINFOIL + "mcp:\n  t:\n    command: npx\n"
            "    args: [foo]\n")
        findings = self.fixture.audit()
        self.assertTrue(any("floating runtime launcher" in f
                            for f in findings), findings)

    def test_floating_launcher_in_owned_install_script_fails(self):
        self.fixture.write("plugins/gear360/install.sh", "exec uvx serve\n")
        # gear360 is not in this fixture's catalog, so name the finding via a
        # catalogued plugin instead: extend tinfoil's directory.
        self.fixture.write("plugins/tinfoil/install.sh", "exec uvx serve\n")
        findings = self.fixture.audit()
        self.assertTrue(any("floating runtime launcher" in f
                            for f in findings), findings)

    def test_overlapping_group_and_managed_plugin_still_fails(self):
        # A path shared between a managed plugin and an inventoried group
        # (here: the tinfoil descriptor also declared under a group) is NOT
        # exempt from the launcher ban — only group-only host-side files are.
        doc = locations_doc()
        extra = catalog_doc()
        extra["components"]["distro-packages"] = {
            "name": "Distro packages", "kind": "group",
            "enabled_when": "always", "policy": "deferred",
            "deferral_reason": "out of managed scope"}
        doc["locations"]["distro-packages.tinfoil"] = {
            "path": "plugins/tinfoil/plugin.yml", "component": "distro-packages",
            "find": "golang"}
        self.fixture.write(
            "plugins/tinfoil/plugin.yml",
            self.fixture.TINFOIL + "mcp:\n  t:\n    command: npx\n    args: [x]\n")
        findings = self.fixture.audit(
            catalog=v.Catalog.from_dict(extra),
            locations=audit.validate_locations(doc))
        self.assertTrue(any("floating runtime launcher" in f
                            for f in findings), findings)

    def test_container_installer_under_non_bridge_group_still_fails(self):
        # gear360/install.sh is declared under distro-packages — a group, but
        # NOT the host-bridges group. It is a container installer, so a
        # floating launcher in it must fail; exemption belongs only to the
        # explicit host-bridges component.
        doc = locations_doc()
        extra = catalog_doc()
        extra["components"]["distro-packages"] = {
            "name": "Distro packages", "kind": "group",
            "enabled_when": "always", "policy": "deferred",
            "deferral_reason": "out of managed scope"}
        doc["locations"]["distro-packages.installer"] = {
            "path": "plugins/archex/install-helper.sh",
            "component": "distro-packages"}
        self.fixture.write("plugins/archex/install-helper.sh", "exec npx -y x\n")
        findings = self.fixture.audit(
            catalog=v.Catalog.from_dict(extra),
            locations=audit.validate_locations(doc))
        self.assertTrue(any("floating runtime launcher" in f
                            for f in findings), findings)

    def test_group_only_launcher_file_is_exempt(self):
        # A host-side launcher file owned ONLY by the explicit host-bridges
        # group is out of the ban's scope (it runs on the Mac, never in the
        # container).
        doc = locations_doc()
        extra = catalog_doc()
        extra["components"]["host-bridges"] = {
            "name": "Host bridges", "kind": "group", "enabled_when": "always",
            "policy": "deferred", "deferral_reason": "host-side, out of scope"}
        doc["locations"]["host-bridges.launch"] = {
            "path": "plugins/browser/launch.py", "component": "host-bridges",
            "find": "exec npx -y serve"}
        self.fixture.write("plugins/browser/launch.py", 'exec npx -y serve\n')
        findings = self.fixture.audit(
            catalog=v.Catalog.from_dict(extra),
            locations=audit.validate_locations(doc))
        self.assertEqual(findings, [])

    def test_descriptor_covered_by_wrong_component_fails(self):
        # Coverage must come from a component of the matching kind naming
        # this exact descriptor as its owner — a group (or any unrelated
        # component) pointing at the descriptor does not cover it.
        doc = locations_doc()
        extra = catalog_doc()
        extra["components"]["distro-packages"] = {
            "name": "Distro packages", "kind": "group",
            "enabled_when": "always", "policy": "deferred",
            "deferral_reason": "out of managed scope"}
        self.fixture.write("agents/newcli/agent.yml",
                           "binary: newcli\ninstall: |\n  pip3 install newcli\n")
        doc["locations"]["agent.newcli"] = {
            "path": "agents/newcli/agent.yml", "component": "distro-packages"}
        findings = self.fixture.audit(
            catalog=v.Catalog.from_dict(extra),
            locations=audit.validate_locations(doc))
        self.assertTrue(any("undocumented agent descriptor" in f
                            for f in findings), findings)

    def test_new_selector_file_in_catalogued_dir_fails(self):
        # A new selector file dropped into an already-catalogued owned path
        # is not covered by that directory's membership: discovery must see
        # it as an undeclared selector location.
        self.fixture.write("plugins/archex/new-install.sh",
                           "curl -fsSL https://example.com/i.sh | bash\n"
                           "npm install -g thing\n")
        findings = self.fixture.audit()
        self.assertTrue(any("undiscovered selector location" in f
                            for f in findings), findings)

    def test_new_selector_file_in_catalogued_dir_declared_is_clean(self):
        # The same file, explicitly declared: no finding.
        self.fixture.write("plugins/archex/new-install.sh",
                           "curl -fsSL https://example.com/i.sh | bash\n")
        doc = locations_doc()
        doc["locations"]["plugin.archex.extra"] = {
            "path": "plugins/archex/new-install.sh",
            "component": "plugin.archex", "find": "example.com/i.sh"}
        self.assertEqual(self.fixture.audit(
            locations=audit.validate_locations(doc)), [])

    def test_new_selector_line_in_declared_dockerfile_fails(self):
        # A NEW selector line inside an already-declared file (the Dockerfile
        # is inventoried) fails: coverage is per anchor, not per file.
        self.fixture.write("Dockerfile", self.fixture.DOCKERFILE
                           + "RUN npm install -g newthing\n")
        findings = self.fixture.audit()
        self.assertTrue(any("undiscovered selector location" in f
                            and "Dockerfile" in f for f in findings), findings)

    def test_new_selector_line_in_declared_descriptor_fails(self):
        # A new pip line in an already-inventoried descriptor fails.
        self.fixture.write("agents/aider/agent.yml", self.fixture.AIDER
                           + "  pip3 install extra\n")
        findings = self.fixture.audit()
        self.assertTrue(any("undiscovered selector location" in f
                            and "agents/aider/agent.yml" in f
                            for f in findings), findings)

    def test_new_anchored_selector_line_is_accepted(self):
        # The same kind of mutation, WITH its anchor declared first: no
        # finding. The inventory, not the file, is authoritative.
        self.fixture.write("agents/aider/agent.yml", self.fixture.AIDER
                           + "  pip3 install aider-extra\n")
        doc = locations_doc()
        doc["locations"]["agent.aider.extra"] = {
            "path": "agents/aider/agent.yml", "component": "agent.aider",
            "find": "pip3 install aider-extra"}
        self.assertEqual(self.fixture.audit(
            locations=audit.validate_locations(doc)), [])

    def test_tinfoil_dependency_pin_mutation_fails(self):
        # Changing a Tinfoil runtime dependency pin without updating the
        # inventory fails its anchor.
        self.fixture.write("plugins/tinfoil/plugin.yml", self.fixture.TINFOIL
                           .replace("dependencies.tinfoil=1.2.1",
                                    "dependencies.tinfoil=1.3.0"))
        findings = self.fixture.audit()
        self.assertTrue(any("anchored selector text no longer in" in f
                            for f in findings), findings)

    def test_symlinked_descriptor_directory_is_reported_not_read(self):
        # A descriptor DIRECTORY symlinked outside the repository is fenced:
        # the audit reports the escape and never follows or reads it (no
        # crash, no external descriptor read).
        import os
        outside = self.fixture.root.parent / "outside-audit-agent"
        outside.mkdir(exist_ok=True)
        (outside / "agent.yml").write_text("binary: evil\ninstall: |\n  true\n")
        link = self.fixture.root / "agents/external"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlinks unavailable on this platform")
        findings = self.fixture.audit()  # must not raise
        self.assertTrue(any("agents/external" in f
                            and "outside the repository" in f
                            for f in findings), findings)
        (outside / "agent.yml").unlink()
        outside.rmdir()

    def test_duplicate_identical_selector_line_fails(self):
        # One-to-one accounting: a SECOND identical npm install line in an
        # already-declared descriptor has no unused record and fails.
        self.fixture.write("agents/pi/agent.yml", self.fixture.PI
                           + "  npm install -g @earendil-works/pi-coding-agent\n")
        findings = self.fixture.audit()
        self.assertTrue(any("no unused record anchors" in f
                            for f in findings), findings)

    def test_duplicate_identical_selector_line_with_second_record_is_clean(self):
        # The same mutation WITH a second record carrying the same find:
        # clean — repeated legitimate identical lines get one record each.
        self.fixture.write("agents/pi/agent.yml", self.fixture.PI
                           + "  npm install -g @earendil-works/pi-coding-agent\n")
        doc = locations_doc()
        doc["locations"]["agent.pi.second"] = {
            "path": "agents/pi/agent.yml", "component": "agent.pi",
            "find": "npm install -g @earendil-works/pi-coding-agent"}
        self.assertEqual(self.fixture.audit(
            locations=audit.validate_locations(doc)), [])

    def test_mutating_ci_docker_pull_selector_fails(self):
        # Changing an existing CI docker-pull selector (Backrest v1.14.1 →
        # v9.99.9) fails its anchor. Pre-fix, no record and no docker-pull
        # pattern existed, so this mutation passed the audit.
        self.fixture.write("ci.yml", self.fixture.CI_PULL
                           .replace("v1.14.1", "v9.99.9"))
        findings = self.fixture.audit()
        self.assertTrue(any("anchored selector text no longer in" in f
                            for f in findings), findings)

    def test_additional_undeclared_docker_pull_line_fails(self):
        # A NEW docker-pull selector line in a declared CI file with no
        # unused record is a finding (one-to-one accounting; the narrow
        # docker-pull discovery pattern makes it visible).
        self.fixture.write("ci.yml", self.fixture.CI_PULL
                           + "      - run: docker pull fosrl/newt:2.0.0\n")
        findings = self.fixture.audit()
        self.assertTrue(any("no unused record anchors" in f
                            and "ci.yml" in f for f in findings), findings)

    def test_launcher_comment_is_not_a_finding(self):
        self.fixture.write("plugins/tinfoil/plugin.yml",
                           self.fixture.TINFOIL + "# never floating npx here\n")
        self.assertEqual(self.fixture.audit(), [])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = FixtureRepo()

    def tearDown(self):
        self.fixture.cleanup()

    def test_undiscovered_installer_file_is_a_finding(self):
        # A selector in a file that is neither a declared location, a
        # catalogued descriptor directory, nor excluded is a finding.
        self.fixture.write("compose/extra.yml", "RUN npm install -g thing\n")
        doc = locations_doc()
        doc["discovery"]["scope"] = ["agents", "plugins", "Dockerfile", "compose"]
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertTrue(any("undiscovered selector location" in f
                            for f in findings), findings)

    def test_declared_location_covers_the_hit(self):
        # The same file, declared WITH an anchor on the hit line: no finding.
        self.fixture.write("compose/covered.yml", "RUN npm install -g thing\n")
        doc = locations_doc()
        doc["locations"]["base-images.compose"] = {
            "path": "compose/covered.yml", "component": "base-images",
            "find": "npm install -g thing"}
        doc["discovery"]["scope"] = ["agents", "plugins", "Dockerfile", "compose"]
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertEqual(findings, [])

    def test_declared_path_without_anchor_does_not_cover_the_hit(self):
        # Declaring the file alone is not enough: the hit line must carry an
        # anchor. Anchor granularity is what makes new selector lines in
        # already-known files visible.
        self.fixture.write("compose/undeclared-line.yml", "RUN npm install -g thing\n")
        doc = locations_doc()
        doc["locations"]["base-images.compose"] = {
            "path": "compose/undeclared-line.yml", "component": "base-images"}
        doc["discovery"]["scope"] = ["agents", "plugins", "Dockerfile", "compose"]
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertTrue(any("undiscovered selector location" in f
                            for f in findings), findings)

    def test_exclusion_covers_the_hit(self):
        self.fixture.write("compose/excluded.yml", "RUN npm install -g thing\n")
        doc = locations_doc()
        doc["exclusions"].append({
            "path": "compose/excluded.yml",
            "reason": "generated file regenerated on every build"})
        doc["discovery"]["scope"] = ["agents", "plugins", "Dockerfile", "compose"]
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertEqual(findings, [])

    def test_test_fixtures_are_skipped(self):
        # A descriptor-directory test file quoting installer syntax is not a
        # finding — discovery avoids test fixtures.
        self.fixture.write("plugins/tinfoil/test_plugin.py",
                           'PLUGIN = "npm install -g thing"\n')
        self.assertEqual(self.fixture.audit(), [])

    def test_missing_scope_entry_is_a_finding(self):
        doc = locations_doc()
        doc["discovery"]["scope"] = ["agents", "plugins", "Dockerfile", "nope"]
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertTrue(any("scope entry does not exist" in f
                            for f in findings), findings)

    def test_symlink_target_outside_repo_is_reported_and_never_read(self):
        # A declared location whose symlink target escapes the repository is
        # fenced: the audit reports the escape and never reads the file (so
        # the outside content cannot satisfy an anchor or cover a hit).
        import os
        outside = self.fixture.root.parent / "outside-audit-secrets.txt"
        outside.write_text("npm install -g outside-thing\n")
        link = self.fixture.root / "plugins/archex/leak.yml"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlinks unavailable on this platform")
        doc = locations_doc()
        doc["locations"]["plugin.archex.leak"] = {
            "path": "plugins/archex/leak.yml", "component": "plugin.archex",
            "find": "outside-thing"}
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertTrue(any("resolves outside the repository" in f
                            for f in findings), findings)
        self.assertFalse(any("undiscovered selector location" in f
                             and "leak.yml" in f for f in findings), findings)
        outside.unlink()

    def test_symlinked_scope_entry_escapes_are_reported(self):
        import os
        outside = self.fixture.root.parent / "outside-audit-dir"
        outside.mkdir(exist_ok=True)
        (outside / "x.yml").write_text("npm install -g y\n")
        link = self.fixture.root / "scope-link"
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("symlinks unavailable on this platform")
        doc = locations_doc()
        doc["discovery"]["scope"] = ["agents", "plugins", "Dockerfile",
                                     "scope-link"]
        findings = self.fixture.audit(
            locations=audit.validate_locations(doc))
        self.assertTrue(any("scope entry escapes the repository" in f
                            for f in findings), findings)
        outside.joinpath("x.yml").unlink()
        outside.rmdir()


class LocationsSchemaTests(unittest.TestCase):
    def test_round_trip_validation(self):
        doc = locations_doc()
        self.assertEqual(audit.validate_locations(doc)["locations"],
                         doc["locations"])

    def test_rejects_unknown_field(self):
        doc = locations_doc()
        doc["locations"]["agent.aider"]["matcher"] = "x"
        with self.assertRaises(v.VersionContractError) as ctx:
            audit.validate_locations(doc)
        self.assertIn("unsupported field(s): matcher", str(ctx.exception))

    def test_rejects_unknown_schema(self):
        doc = locations_doc()
        doc["schema"] = 2
        with self.assertRaises(v.VersionContractError):
            audit.validate_locations(doc)

    def test_rejects_location_without_path(self):
        doc = locations_doc()
        del doc["locations"]["agent.aider"]["path"]
        with self.assertRaises(v.VersionContractError) as ctx:
            audit.validate_locations(doc)
        self.assertIn("path: must be a non-empty string", str(ctx.exception))

    def test_exclusion_requires_reason(self):
        doc = locations_doc()
        doc["exclusions"].append({"path": "somewhere"})
        with self.assertRaises(v.VersionContractError) as ctx:
            audit.validate_locations(doc)
        self.assertIn("reason must be a non-empty string", str(ctx.exception))

    def test_discovery_requires_patterns(self):
        doc = locations_doc()
        del doc["discovery"]["patterns"]
        with self.assertRaises(v.VersionContractError):
            audit.validate_locations(doc)

    def test_discovery_rejects_bad_regex(self):
        doc = locations_doc()
        doc["discovery"]["patterns"] = ["(["]
        with self.assertRaises(v.VersionContractError) as ctx:
            audit.validate_locations(doc)
        self.assertIn("bad regex", str(ctx.exception))

    def test_location_path_cannot_be_absolute(self):
        doc = locations_doc()
        doc["locations"]["agent.aider"]["path"] = "/etc/passwd"
        with self.assertRaises(v.VersionContractError) as ctx:
            audit.validate_locations(doc)
        self.assertIn("must be repo-relative (got absolute path", str(ctx.exception))

    def test_location_path_cannot_escape_via_dotdot(self):
        doc = locations_doc()
        doc["locations"]["agent.aider"]["path"] = "../outside/agent.yml"
        with self.assertRaises(v.VersionContractError) as ctx:
            audit.validate_locations(doc)
        self.assertIn("must be repo-relative", str(ctx.exception))

    def test_exclusion_path_cannot_escape(self):
        for bad in ("..", "/tmp/vendor", "a/../../secrets.env"):
            doc = locations_doc()
            doc["exclusions"].append({"path": bad, "reason": "because"})
            with self.assertRaises(v.VersionContractError) as ctx:
                audit.validate_locations(doc)
            self.assertIn("repo-relative", str(ctx.exception))

    def test_discovery_scope_cannot_be_absolute_or_escape(self):
        for bad in ("/tmp", "../other-repo", "a/../.."):
            doc = locations_doc()
            doc["discovery"]["scope"] = ["agents", bad]
            with self.assertRaises(v.VersionContractError) as ctx:
                audit.validate_locations(doc)
            self.assertIn("repo-relative", str(ctx.exception))

    def test_component_without_location_fails(self):
        fixture = FixtureRepo()
        try:
            doc = catalog_doc()
            del doc["components"]["plugin.tinfoil"]
            doc["components"]["plugin.ghost"] = {
                "name": "Ghost", "kind": "plugin", "owner": "plugins/ghost/x",
                "enabled_when": "always", "policy": "latest", "resolver": "npm",
                "platform": "linux",
                "source": {"type": "npm", "location": "registry.npmjs.org/ghost"}}
            findings = audit.run_audit(
                fixture.root, v.Catalog.from_dict(doc), fixture.locations())
            self.assertTrue(any("plugin.ghost" in f and "no selector location" in f
                                for f in findings), findings)
        finally:
            fixture.cleanup()


class DarkPinTests(unittest.TestCase):
    """The Dark contract: the wiring surfaces never reference the audit (or
    the inventory) before the integration step says so."""

    def test_up_sh_and_manifest_never_reference_the_audit(self):
        for path in (REPO_ROOT / "up.sh", REPO_ROOT / "src/manifest.py"):
            text = path.read_text()
            self.assertNotIn("version_audit", text, f"{path} went Dark-exit")
            self.assertNotIn("version-locations", text, f"{path} went Dark-exit")

    def test_up_sh_never_references_the_catalog(self):
        text = (REPO_ROOT / "up.sh").read_text()
        self.assertNotIn("versions.yml", text)
        self.assertNotIn("version_contracts", text)


class GovernanceTests(unittest.TestCase):
    """Workspace rules forbid references to private vault notes (plans,
    LOGs) in code, tests, commit messages, or in-repo docs. The version
    files must be self-contained: contract layer / inventory / audit layer,
    never a plan title or step number."""

    FILES = (
        "src/version_contracts.py", "src/version_audit.py",
        "tests/test_version_contracts.py", "tests/test_version_inventory.py",
        "versions.yml", "version-locations.yml", "src/README.md", "djinn",
    )

    def test_version_files_name_no_private_plan(self):
        # Forbidden tokens are built by concatenation so this test file does
        # not itself contain them. Any "Step <number>" reference is a plan
        # step, not just the two steps this candidate straddles.
        plan_token = "P" + "LN"
        step_re = "Step" + r"\s+\d"
        for rel in self.FILES:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn(plan_token, text,
                             f"{rel} references a private plan")
            self.assertIsNone(re.search(step_re, text),
                              f"{rel} references a plan step")


@unittest.skipIf(shutil.which("yq") is None, "yq not available")
class RealRepoIntegrationTests(unittest.TestCase):
    """The audit against THIS repository's real inventory — the Claim run:
    it must pass, and its report must list every classification."""

    @classmethod
    def setUpClass(cls):
        catalog = v.Catalog.from_dict(audit.load_yaml(REPO_ROOT / "versions.yml"))
        locations = audit.validate_locations(
            audit.load_yaml(REPO_ROOT / "version-locations.yml"))
        cls.catalog = catalog
        cls.locations = locations
        cls.findings = audit.run_audit(REPO_ROOT, catalog, locations)

    def test_real_repo_has_no_findings(self):
        self.assertEqual(
            self.findings, [],
            "the real inventory must pass its own audit:\n"
            + "\n".join(self.findings))

    def test_report_lists_all_four_classifications(self):
        report = audit.format_report(self.catalog, self.locations, [])
        for label in ("pinned: 4", "floating: 7", "external: 12",
                      "deferred: 12"):
            self.assertIn(label, report)
        self.assertIn("opaque live vendor installer", report)
        self.assertIn("agent.pi-mcp-adapter", report)
        self.assertIn("plugins/tinfoil/package-lock.json", report)
        # The exact constraint itself is pinned where it matters: the
        # catalog + the installer anchor the audit checks.
        self.assertEqual(self.catalog.components["agent.pi-mcp-adapter"].constraint,
                         "2.32.1")

    def test_tinfoil_dependency_pins_are_explicitly_inventoried(self):
        # The three runtime dependency pins (tinfoil 1.2.1, zod 4.6.3,
        # ehbp 0.3.2) are anchored in the install block and in the committed
        # lock — the real audit's clean run already verifies every anchor.
        for loc_id in ("plugin.tinfoil.deps", "plugin.tinfoil.depverify",
                       "plugin.tinfoil.deplock", "plugin.tinfoil.zod-lock",
                       "plugin.tinfoil.ehbp-lock"):
            self.assertIn(loc_id, self.locations["locations"])
            record = self.locations["locations"][loc_id]
            text = (REPO_ROOT / record["path"]).read_text()
            self.assertIn(record["find"], text, loc_id)

    def test_egress_dockerfile_selectors_are_inventoried(self):
        """PIN — egress/Dockerfile is in the discovery scope and every
        selector in it (both base images, both apt installs, npm ci, the
        pinned yq release) has its own record; removing any one leaves a
        hit line unaccounted for and test_real_repo_has_no_findings fails."""
        self.assertIn("egress", self.locations["discovery"]["scope"])
        egress = {loc_id: record
                  for loc_id, record in self.locations["locations"].items()
                  if record["path"] == "egress/Dockerfile"}
        self.assertEqual(
            sorted((record["component"], record["find"])
                   for record in egress.values()),
            sorted([
                ("base-images", "FROM node:22-bookworm-slim"),
                ("base-images", "FROM debian:bookworm-slim"),
                ("distro-packages",
                 "apt-get update && apt-get install -y --no-install-recommends \\"),
                ("distro-packages",
                 "apt-get update && apt-get install -y --no-install-recommends \\"),
                ("service-images", "npm ci"),
                ("base-tools", "YQ_VERSION=v4.44.3"),
                ("base-tools", "yq/releases/download/"),
            ]))
        text = (REPO_ROOT / "egress/Dockerfile").read_text()
        for loc_id, record in egress.items():
            self.assertIn(record["find"], text, loc_id)

    def test_every_baseline_descriptor_is_catalogued(self):
        # The acceptance baseline: every shipped agent and plugin
        # descriptor directory is owned by a catalog location.
        owned, _escapes = audit._owned_dirs(REPO_ROOT)
        self.assertEqual(len(owned), 26)  # 7 agent dirs + 19 plugins
        for directory, (kind, name, _d) in owned.items():
            rel = str(directory.relative_to(REPO_ROOT))
            hits = [record for record in self.locations["locations"].values()
                    if (REPO_ROOT / record["path"]).resolve().is_relative_to(
                        directory)]
            self.assertTrue(
                hits, f"{rel} has no catalog location")

    def test_every_catalog_component_has_a_location_or_is_external(self):
        covered = {record["component"]
                   for record in self.locations["locations"].values()}
        for component_id, component in self.catalog.components.items():
            if component.policy != "external":
                self.assertIn(component_id, covered, component_id)


if __name__ == "__main__":
    unittest.main()
