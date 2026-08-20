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
            bl.profile_dir("/home/testuser", "job-hunt"),
            Path("/home/testuser/browser-profiles/job-hunt"),
        )

    def test_browser_tmp_dir(self):
        self.assertEqual(
            bl.browser_tmp_dir("/home/testuser", "job-hunt"),
            Path("/home/testuser/browser-tmp/job-hunt"),
        )

    def test_api_key_var(self):
        self.assertEqual(bl.api_key_var("job-hunt"), "RESEARCH_BROWSER_KEY_job_hunt")
        self.assertEqual(bl.api_key_var("personal-site"), "RESEARCH_BROWSER_KEY_personal_site")


class TestBridgeCommand(unittest.TestCase):
    """The exec'd command — the part that carries the whole feature."""

    def _build(self):
        return bl.build_bridge_command(
            8815, 9223, "deadbeef", Path("/base/browser-tmp/job-hunt"),
            base_env={"PATH": "/usr/bin", "TMPDIR": "/leftover"})

    def test_tmpdir_points_at_the_containers_exchange_dir(self):
        # This is the entire scoping mechanism; it must override any inherited
        # TMPDIR, not merely be set when absent.
        _, env = self._build()
        self.assertEqual(env["TMPDIR"], "/base/browser-tmp/job-hunt")

    def test_inherited_env_is_preserved(self):
        _, env = self._build()
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_ports_and_key_land_in_argv(self):
        argv, _ = self._build()
        self.assertEqual(argv[argv.index("--port") + 1], "8815")
        self.assertEqual(argv[argv.index("--apiKey") + 1], "deadbeef")
        self.assertIn("http://127.0.0.1:9223", argv)

    def test_separator_splits_proxy_args_from_server_args(self):
        # Everything after "--" is the wrapped server's own command line;
        # mis-placing it would hand mcp-proxy flags to chrome-devtools-mcp.
        argv, _ = self._build()
        sep = argv.index("--")
        self.assertIn("mcp-proxy", argv[:sep])
        self.assertIn("chrome-devtools-mcp", argv[sep:])
        self.assertNotIn("--allowUnrestrictedPaths", argv)


class TestApiKeyVar(unittest.TestCase):
    def test_manifest_binding_wins(self):
        man = {"common_secrets": {"RESEARCH_BROWSER_KEY": "RESEARCH_BROWSER_KEY_job_hunt"}}
        self.assertEqual(bl.api_key_var("job-hunt", man), "RESEARCH_BROWSER_KEY_job_hunt")

    def test_list_form_binding(self):
        man = {"common_secrets": ["RESEARCH_BROWSER_KEY"]}
        self.assertEqual(bl.api_key_var("job-hunt", man), "RESEARCH_BROWSER_KEY")

    def test_derived_fallback_without_binding(self):
        self.assertEqual(bl.api_key_var("job-hunt", {}), "RESEARCH_BROWSER_KEY_job_hunt")
        self.assertEqual(bl.api_key_var("job-hunt"), "RESEARCH_BROWSER_KEY_job_hunt")


class TestBridgePortValidation(unittest.TestCase):
    def test_default_and_low_ports_accepted(self):
        for ok in (8814, 8815, 9221, 1):
            with self.subTest(ok=ok):
                self.assertEqual(bl.validate_bridge_port(ok), ok)

    def test_ports_in_the_cdp_band_rejected(self):
        # bridge 9222 would derive CDP 9630 while colliding with the default
        # container's CDP port — it would attach to the wrong browser.
        for bad in (9222, 9223, 12000):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    bl.validate_bridge_port(bad)

    def test_out_of_range_rejected(self):
        for bad in (0, -1, 70000):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    bl.validate_bridge_port(bad)


class TestReadSecret(unittest.TestCase):
    """secrets.env is SOURCED, matching up.sh — not parsed as text."""

    def _read(self, body, var="KEY"):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "secrets.env"
            f.write_text(body)
            return bl.read_secret(f, var)

    def test_plain_assignment(self):
        self.assertEqual(self._read("KEY=abc123\n"), "abc123")

    def test_double_quoted_value_is_unquoted(self):
        # Text parsing would return '"abc123"' and the bridge would then reject
        # every request the container made with the unquoted key.
        self.assertEqual(self._read('KEY="abc123"\n'), "abc123")

    def test_single_quoted_value_is_unquoted(self):
        self.assertEqual(self._read("KEY='abc123'\n"), "abc123")

    def test_export_prefix(self):
        self.assertEqual(self._read("export KEY=abc123\n"), "abc123")

    def test_absent_var_is_empty(self):
        self.assertEqual(self._read("OTHER=x\n"), "")

    def test_missing_file_is_empty(self):
        self.assertEqual(bl.read_secret(Path("/nonexistent/secrets.env"), "KEY"), "")


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


class TestBrowserAppOverrides(unittest.TestCase):
    """App locations are settable outside the script (./.env → env)."""

    def test_defaults_when_unset(self):
        apps = bl.browser_apps({})
        self.assertEqual(apps["brave"], Path("/Applications/Brave Browser.app"))
        self.assertEqual(apps["chrome"], Path("/Applications/Google Chrome.app"))

    def test_env_overrides(self):
        apps = bl.browser_apps({"BRAVE_APP": "/Users/me/Applications/Brave.app",
                                "CHROME_APP": "/opt/Chromium.app"})
        self.assertEqual(apps["brave"], Path("/Users/me/Applications/Brave.app"))
        self.assertEqual(apps["chrome"], Path("/opt/Chromium.app"))

    def test_empty_env_value_falls_back_to_default(self):
        self.assertEqual(bl.browser_apps({"BRAVE_APP": ""})["brave"],
                         Path("/Applications/Brave Browser.app"))

    def test_pick_browser_uses_override(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "Custom.app"
            app.mkdir()
            self.assertEqual(bl.pick_browser("brave", {"BRAVE_APP": str(app)}), app)

    def test_pick_browser_accepts_absolute_path(self):
        with tempfile.TemporaryDirectory() as td:
            app = Path(td) / "OneOff.app"
            app.mkdir()
            self.assertEqual(bl.pick_browser(str(app), {}), app)

    def test_auto_prefers_brave_then_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            chrome = Path(td) / "Chrome.app"
            chrome.mkdir()
            env = {"BRAVE_APP": str(Path(td) / "absent.app"), "CHROME_APP": str(chrome)}
            self.assertEqual(bl.pick_browser("auto", env), chrome)

    def test_unknown_choice_rejected(self):
        with self.assertRaises(SystemExit):
            bl.pick_browser("firefox", {})

    def test_missing_app_rejected(self):
        with self.assertRaises(SystemExit):
            bl.pick_browser("brave", {"BRAVE_APP": "/nope/Brave.app"})


class TestContainersDir(unittest.TestCase):
    def test_env_override_wins(self):
        self.assertEqual(
            bl.bottles_dir("/base", env_override="/custom/manifests"),
            Path("/custom/manifests"),
        )

    def test_base_path_bottles_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            base_path = Path(td)
            (base_path / "bottles").mkdir()
            self.assertEqual(bl.bottles_dir(str(base_path)), base_path / "bottles")

    def test_repo_bottles_fallback(self):
        # No $BASE_PATH/bottles → fall back to the repo checkout's dir.
        root = Path(__file__).parent.parent
        with unittest.mock.patch.object(bl, "repo_root", return_value=root):
            with tempfile.TemporaryDirectory() as td:
                self.assertEqual(bl.bottles_dir(td), root / "bottles")


if __name__ == "__main__":
    unittest.main()
