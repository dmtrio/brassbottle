#!/usr/bin/env python3
"""Unit tests for plugins/browser/launch.py (pure logic only)."""

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "plugins" / "browser"))
import launch as bl


class TestBridgePortResolution(unittest.TestCase):
    def test_default_when_manifest_has_no_plugin_ports(self):
        self.assertEqual(bl.resolve_bridge_port({}), 8814)
        self.assertEqual(bl.resolve_bridge_port({}, plugin_host_port=8814), 8814)

    def test_plugin_yml_fallback(self):
        self.assertEqual(bl.resolve_bridge_port({}, plugin_host_port=8815), 8815)

    def test_manifest_override(self):
        man = {"plugin_ports": {"browser": 8815}}
        self.assertEqual(bl.resolve_bridge_port(man), 8815)
        self.assertEqual(bl.resolve_bridge_port(man, plugin_host_port=8814), 8815)

    def test_ignores_non_dict_plugin_ports(self):
        self.assertEqual(bl.resolve_bridge_port({"plugin_ports": None}), 8814)
        self.assertEqual(bl.resolve_bridge_port({"plugin_ports": "8815"}), 8814)

    def test_non_integer_port_is_rejected(self):
        # The launcher reads the manifest directly, so it cannot rely on
        # manifest.py having validated a hand-edited value.
        for bad in ("8815", 8815.5, True, []):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    bl.resolve_bridge_port({"plugin_ports": {"browser": bad}})


class TestCdpPortOffset(unittest.TestCase):
    def test_default_ports(self):
        self.assertEqual(bl.cdp_port(8814), 9222)

    def test_offset_from_custom_bridge(self):
        self.assertEqual(bl.cdp_port(8815), 9223)
        self.assertEqual(bl.cdp_port(8816), 9224)


class TestDerivedPaths(unittest.TestCase):
    def test_profile_dir(self):
        self.assertEqual(
            bl.profile_dir("/home/dev-agent", "job-hunt"),
            Path("/home/dev-agent/browser-profiles/job-hunt"),
        )

    def test_browser_tmp_dir(self):
        self.assertEqual(
            bl.browser_tmp_dir("/home/dev-agent", "job-hunt"),
            Path("/home/dev-agent/browser-tmp/job-hunt"),
        )

    def test_api_key_var(self):
        self.assertEqual(bl.api_key_var("job-hunt"), "RESEARCH_BROWSER_KEY_job_hunt")
        self.assertEqual(bl.api_key_var("personal-site"), "RESEARCH_BROWSER_KEY_personal_site")


class TestContainerNameValidation(unittest.TestCase):
    def test_path_escaping_names_rejected(self):
        # The name is interpolated into the manifest/profile/TMPDIR paths.
        for bad in ("../etc", "a/b", "..", "", "a b", "x;y"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    bl.main([bad])

    def test_valid_names_pass_validation(self):
        for good in ("job-hunt", "coding_docker_dev", "abc123"):
            with self.subTest(good=good):
                self.assertTrue(bl.CONTAINER_NAME_RE.match(good))


class TestContainersDir(unittest.TestCase):
    def test_env_override_wins(self):
        self.assertEqual(
            bl.containers_dir("/base", env_override="/custom/manifests"),
            Path("/custom/manifests"),
        )

    def test_base_path_containers_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            base_path = Path(td)
            (base_path / "containers").mkdir()
            self.assertEqual(bl.containers_dir(str(base_path)), base_path / "containers")

    def test_repo_containers_fallback(self):
        # No $BASE_PATH/containers → fall back to the repo checkout's dir.
        root = Path(__file__).parent.parent
        with unittest.mock.patch.object(bl, "repo_root", return_value=root):
            with tempfile.TemporaryDirectory() as td:
                self.assertEqual(bl.containers_dir(td), root / "containers")


if __name__ == "__main__":
    unittest.main()
