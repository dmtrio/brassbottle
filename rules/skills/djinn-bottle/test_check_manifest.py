#!/usr/bin/env python3
"""Unit tests for check_manifest.py — python3 -m unittest discover in this dir.

Two things are worth pinning hard. First the port arithmetic: an implicit
plugin default or a quoted port is exactly the collision a human misses, and
on the Mac it surfaces as a silently unpublished port rather than an error.
Second the severity split — a sibling
clash is a warning because nothing here knows whether the two containers ever
run at once, while a manifest clashing with itself can never come up and is
therefore fatal.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import check_manifest as cm  # noqa: E402

DEFAULTS = {"browser": 8814, "gateway": 8811, "proxyman": 8813}


def write(dirpath, name, body):
    path = os.path.join(dirpath, name)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(body).lstrip())
    return path


class Scalars(unittest.TestCase):
    def test_as_port_accepts_what_manifest_py_accepts(self):
        self.assertEqual(cm.as_port(2223), 2223)
        self.assertEqual(cm.as_port("2223"), 2223)      # _scalar passes strings
        self.assertEqual(cm.as_port(" 2223 "), 2223)

    def test_as_port_rejects_non_ports(self):
        for value in (None, True, False, "", "abc", "22a3", [2223], {"p": 1}):
            self.assertIsNone(cm.as_port(value), value)

    def test_flag_true_mirrors_raw_flag(self):
        self.assertTrue(cm.flag_true(True))
        self.assertTrue(cm.flag_true("true"))           # quoted YAML scalar
        self.assertFalse(cm.flag_true(False))
        self.assertFalse(cm.flag_true(None))
        self.assertFalse(cm.flag_true("yes"))


class EnabledPlugins(unittest.TestCase):
    def test_plain_list(self):
        self.assertEqual(cm.enabled_plugins({"plugins": ["serena", "browser"]}),
                         ["serena", "browser"])

    def test_deprecated_capabilities_sugar_counts(self):
        # manifest.py still folds these into plugins:, host port and all.
        self.assertEqual(cm.enabled_plugins({"capabilities": {"browser": True}}),
                         ["browser"])

    def test_sugar_does_not_duplicate_an_explicit_entry(self):
        self.assertEqual(
            cm.enabled_plugins({"plugins": ["browser"],
                                "capabilities": {"browser": True}}),
            ["browser"])

    def test_wrong_types_yield_nothing(self):
        self.assertEqual(cm.enabled_plugins({"plugins": "browser",
                                             "capabilities": []}), [])


class EffectivePorts(unittest.TestCase):
    def test_ssh_port_is_claimed(self):
        tcp, udp = cm.effective_ports({"ssh": {"port": 2223}}, DEFAULTS)
        self.assertEqual(tcp, {2223: ["ssh.port"]})
        self.assertEqual(udp, [])

    def test_quoted_ssh_port_is_still_a_port(self):
        tcp, _ = cm.effective_ports({"ssh": {"port": "2223"}}, DEFAULTS)
        self.assertIn(2223, tcp)

    def test_plugin_default_is_claimed_though_unmentioned(self):
        tcp, _ = cm.effective_ports({"plugins": ["gateway", "serena"]}, DEFAULTS)
        self.assertEqual(tcp, {8811: ["plugin gateway (default)"]})

    def test_plugin_ports_override_wins(self):
        tcp, _ = cm.effective_ports(
            {"plugins": ["browser"], "plugin_ports": {"browser": 8815}}, DEFAULTS)
        self.assertEqual(tcp[8815], ["plugin_ports.browser"])
        self.assertNotIn(8814, tcp)

    def test_browser_also_claims_its_derived_debug_port(self):
        tcp, _ = cm.effective_ports({"plugins": ["browser"]}, DEFAULTS)
        self.assertIn(8814 + 408, tcp)

    def test_capabilities_sugar_claims_the_host_port(self):
        tcp, _ = cm.effective_ports({"capabilities": {"browser": True}}, DEFAULTS)
        self.assertIn(8814, tcp)

    def test_one_port_claimed_twice_keeps_both_sources(self):
        tcp, _ = cm.effective_ports(
            {"ssh": {"port": 8811}, "plugins": ["gateway"]}, DEFAULTS)
        self.assertEqual(len(tcp[8811]), 2)

    def test_wrong_yaml_types_do_not_explode(self):
        tcp, udp = cm.effective_ports(
            {"plugins": "browser", "plugin_ports": ["browser"], "ssh": []},
            DEFAULTS)
        self.assertEqual((tcp, udp), ({}, []))


class Collisions(unittest.TestCase):
    def test_sibling_ssh_clash_warns_because_concurrency_is_unknown(self):
        errors, warnings = cm.scan_collisions(
            "new", {"ssh": {"port": 2222}},
            {"trip-research": {"ssh": {"port": 2222}}}, DEFAULTS)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("2222", warnings[0])
        self.assertIn("trip-research.yml", warnings[0])

    def test_self_collision_is_fatal(self):
        errors, _ = cm.scan_collisions(
            "new", {"ssh": {"port": 8811}, "plugins": ["gateway"]}, {}, DEFAULTS)
        self.assertEqual(len(errors), 1)
        self.assertIn("claimed twice", errors[0])

    def test_implicit_plugin_default_is_seen_across_containers(self):
        _, warnings = cm.scan_collisions(
            "new", {"plugins": ["browser"]},                      # implicit 8814
            {"other": {"plugins": ["browser"],
                       "plugin_ports": {"browser": 8814}}}, DEFAULTS)
        self.assertTrue(any("8814" in w for w in warnings))

    def test_capabilities_sugar_is_seen_across_containers(self):
        _, warnings = cm.scan_collisions(
            "new", {"capabilities": {"browser": True}},
            {"trip-research": {"plugins": ["browser"]}}, DEFAULTS)
        self.assertTrue(any("8814" in w for w in warnings))

    def test_distinct_ports_are_clean(self):
        errors, warnings = cm.scan_collisions(
            "new", {"ssh": {"port": 2224}, "plugins": ["gateway"]},
            {"rhino": {"ssh": {"port": 2223}}}, DEFAULTS)
        self.assertEqual((errors, warnings), ([], []))

    def test_browser_bridge_at_ceiling_is_fatal(self):
        errors, _ = cm.scan_collisions(
            "new", {"plugins": ["browser"], "plugin_ports": {"browser": 9222}},
            {}, DEFAULTS)
        self.assertTrue(any("9222" in e for e in errors))

    def test_a_manifest_never_collides_with_its_own_sibling_entry(self):
        draft = {"ssh": {"port": 2222}}
        errors, warnings = cm.scan_collisions("new", draft, {"new": draft},
                                              DEFAULTS)
        self.assertEqual((errors, warnings), ([], []))

    def test_task_mismatching_filename_warns_without_demanding_a_fix(self):
        errors, warnings = cm.scan_collisions(
            "gear360-stitch", {"task": "media"}, {}, DEFAULTS)
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("informational label", warnings[0])


class SecretRefs(unittest.TestCase):
    def test_collects_every_reference_form(self):
        refs = cm.secret_refs({
            "git": {"token": "GH_TOKEN_fry",
                    "orgs": {"planetexpress": {"token": "GH_TOKEN_pe"}}},
            "common_secrets": {"MCP_GATEWAY_TOKEN": "MCP_GATEWAY_TOKEN_prod"},
            "agent_secrets": [
                {"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY",
                 "secret": "OBSIDIAN_KEY_default_claude"},
                {"agent": "pi", "slot": "OBSIDIAN_ANNOTATED_KEY",
                 "disabled": True},
            ],
        })
        self.assertEqual(refs, {"GH_TOKEN_fry", "GH_TOKEN_pe",
                                "MCP_GATEWAY_TOKEN_prod",
                                "OBSIDIAN_KEY_default_claude"})

    def test_list_form_common_secrets(self):
        self.assertEqual(cm.secret_refs({"common_secrets": ["A_TOKEN"]}),
                         {"A_TOKEN"})

    def test_deprecated_identities_refs_are_expanded(self):
        # manifest.py derives these names and requires them present; missing
        # them here produced a false hard error on a manifest fine on the Mac.
        self.assertEqual(
            cm.secret_refs({"identities": {"obsidian": ["default_claude"],
                                           "watch": ["default_pi"]}}),
            {"OBSIDIAN_KEY_default_claude", "OBSIDIAN_WATCH_KEY_default_pi"})

    def test_empty_manifest_has_no_refs(self):
        self.assertEqual(cm.secret_refs({}), set())


class ValidatorOutput(unittest.TestCase):
    def test_advisories_and_errors_are_separated(self):
        errors, warnings = cm._split_validator_output(
            "  ⚠ plugin 'x' declares slot Y but no agent enables it\n"
            "Error: manifest forge: must be github or gitea\n")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("Error:", errors[0])

    def test_blank_lines_are_dropped(self):
        self.assertEqual(cm._split_validator_output("\n\n"), ([], []))


@unittest.skipUnless(shutil.which("yq"), "yq not installed")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        # No auto-detected checkout: exercises the degraded path deterministically
        # whether or not this machine happens to have brassbottle cloned.
        real = cm.BRASSBOTTLE_CANDIDATES
        cm.BRASSBOTTLE_CANDIDATES = ()
        self.addCleanup(setattr, cm, "BRASSBOTTLE_CANDIDATES", real)

    def test_clean_draft_passes_without_a_brassbottle_checkout(self):
        write(self.dir, "existing.yml", """
            task: existing
            ssh: {port: 2222}
        """)
        draft = write(self.dir, "newbox.yml", """
            task: newbox
            ssh: {port: 2224}
        """)
        errors, warnings, notes = cm.check(draft)
        self.assertEqual(errors, [])
        self.assertTrue(any("no brassbottle checkout" in n for n in notes))

    def test_clash_with_a_real_sibling_file_is_reported(self):
        write(self.dir, "rhino.yml", "task: rhino\nssh: {port: 2223}\n")
        draft = write(self.dir, "newbox.yml", "task: newbox\nssh: {port: 2223}\n")
        _, warnings, _ = cm.check(draft)
        self.assertTrue(any("rhino.yml" in w for w in warnings))

    def test_template_is_not_scanned_as_a_sibling(self):
        write(self.dir, "TEMPLATE.yml", "task: coding\nssh: {port: 2222}\n")
        draft = write(self.dir, "newbox.yml", "task: newbox\nssh: {port: 2222}\n")
        errors, warnings, notes = cm.check(draft)
        self.assertEqual((errors, warnings), ([], []))
        self.assertTrue(any("0 sibling" in n for n in notes))

    def test_multi_document_sibling_is_skipped_not_fatal(self):
        write(self.dir, "weird.yml", "task: weird\n---\ntask: second\n")
        draft = write(self.dir, "newbox.yml", "task: newbox\n")
        errors, warnings, _ = cm.check(draft)
        self.assertEqual(errors, [])
        self.assertTrue(any("skipped sibling weird.yml" in w for w in warnings))

    def test_multi_document_draft_is_unusable(self):
        draft = write(self.dir, "bad.yml", "task: a\n---\ntask: b\n")
        with self.assertRaises(cm.Unusable):
            cm.check(draft)

    def test_unparseable_draft_is_unusable(self):
        draft = write(self.dir, "bad.yml", "task: [unclosed\n")
        with self.assertRaises(cm.Unusable):
            cm.check(draft)

    def test_missing_draft_is_unusable(self):
        with self.assertRaises(cm.Unusable):
            cm.check(os.path.join(self.dir, "absent.yml"))

    def test_missing_manifests_dir_is_unusable_not_a_traceback(self):
        draft = write(self.dir, "a.yml", "task: a\n")
        with self.assertRaises(cm.Unusable):
            cm.check(draft, manifests_dir=os.path.join(self.dir, "nope"))

    def test_explicit_brassbottle_that_is_not_a_checkout_is_loud(self):
        draft = write(self.dir, "a.yml", "task: a\n")
        with self.assertRaises(cm.Unusable):
            cm.check(draft, brassbottle=self.dir)

    def test_main_exit_codes(self):
        write(self.dir, "rhino.yml", "task: rhino\nssh: {port: 2223}\n")
        clean = write(self.dir, "a.yml", "task: a\nssh: {port: 2299}\n")
        warned = write(self.dir, "b.yml", "task: b\nssh: {port: 2223}\n")
        fatal = write(self.dir, "c.yml",
                      "task: c\nssh: {port: 8811}\nplugin_ports: {gateway: 8811}\n"
                      "plugins: [gateway]\n")
        self.assertEqual(cm.main([clean]), 0)
        self.assertEqual(cm.main([warned]), 0)      # a warning is not a failure
        self.assertEqual(cm.main([fatal]), 1)
        self.assertEqual(cm.main([os.path.join(self.dir, "absent.yml")]), 2)


class DerivePayload(unittest.TestCase):
    """manifest.py --derive requires plugins, an ---agents--- separator, then
    agent descriptors — the exact stream up.sh builds. A payload without the
    separator is rejected wholesale ('agents section missing'), so its shape
    is a contract, not a formatting nicety."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.bb = os.path.join(self.dir, "brassbottle")
        os.makedirs(os.path.join(self.bb, "plugins", "gateway"))
        os.makedirs(os.path.join(self.bb, "agents", "claude"))
        write(os.path.join(self.bb, "plugins", "gateway"), "plugin.yml",
              "mcp: {url: 'http://host:8811/mcp'}\nhost_port: 8811\n")
        write(os.path.join(self.bb, "agents", "claude"), "agent.yml",
              "binary: claude\ninstall: |\n  true\n")
        self.draft = write(self.dir, "a.yml", "task: a\n")

    def test_payload_has_separator_then_agent_lines(self):
        lines = cm.build_derive_payload(self.draft, self.bb)
        self.assertIn("---agents---", lines)
        sep = lines.index("---agents---")
        self.assertTrue(lines[0].startswith("{"))       # the manifest json
        plugin_lines = lines[1:sep]
        agent_lines = lines[sep + 1:]
        self.assertEqual(["gateway"], [l.split("\t")[0] for l in plugin_lines])
        self.assertEqual(["claude"], [l.split("\t")[0] for l in agent_lines])
        for l in plugin_lines + agent_lines:            # one-line json or '!'
            doc = l.split("\t", 1)[1]
            self.assertTrue(doc == "!" or doc.startswith("{"))

    def test_unreadable_descriptor_becomes_bang(self):
        write(os.path.join(self.bb, "agents", "claude"), "agent.yml",
              "binary: [unclosed\n")
        lines = cm.build_derive_payload(self.draft, self.bb)
        agent_lines = lines[lines.index("---agents---") + 1:]
        self.assertEqual(agent_lines, ["claude\t!"])


if __name__ == "__main__":
    unittest.main()
