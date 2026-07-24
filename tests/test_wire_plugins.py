#!/usr/bin/env python3
"""
Unit tests for wire_plugins.py — replaces the mirror-simulation + drift-guard
approach from tests/plugins.test.sh. Pins exact bash/jq/sed semantics that the
module ported into Python: merge order, collision detection, file atomicity,
marker detection, mode preservation, env var interpolation, and idempotency.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Add src/ to path for import
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import wire_plugins


class QuietTestCase(unittest.TestCase):
    """Swallows the module's progress prints (✓/⚠ lines) so the unittest
    output stays readable; tests that assert on messages still work because
    redirect_stdout inside a test simply swaps in its own buffer."""

    def setUp(self):
        self._quiet = contextlib.redirect_stdout(io.StringIO())
        self._quiet.__enter__()

    def tearDown(self):
        self._quiet.__exit__(None, None, None)


class TestMergePluginEntries(unittest.TestCase):
    """Tests for merge_plugin_entries function."""

    def test_two_plugin_entries_merge_preserving_insertion_order(self):
        """Two plugin entry objects merge into one dict preserving insertion order."""
        entries = [
            {"serena": {"command": "bash"}},
            {"obsidian": {"command": "python3"}},
        ]
        result = wire_plugins.merge_plugin_entries(entries)
        self.assertEqual(list(result.keys()), ["serena", "obsidian"])
        self.assertEqual(result["serena"], {"command": "bash"})
        self.assertEqual(result["obsidian"], {"command": "python3"})

    def test_duplicate_server_name_raises_wireerror(self):
        """Same server name in two entries raises WireError with names."""
        entries = [
            {"serena": {"command": "bash"}},
            {"serena": {"command": "python3"}},
        ]
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins.merge_plugin_entries(entries)
        self.assertIn("multiple enabled plugins define the same MCP server name(s): serena", str(cm.exception))

    def test_non_dict_entry_raises_wireerror(self):
        """Non-dict entry in plugin_mcp_entries raises WireError."""
        entries = ["not a dict"]
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins.merge_plugin_entries(entries)
        self.assertIn("plugin_mcp_entries must be JSON objects", str(cm.exception))

    def test_non_dict_server_spec_raises_wireerror(self):
        """A non-dict server spec is rejected here (the choke point), so the
        local/remote classifiers never substring-match a non-dict downstream."""
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins.merge_plugin_entries([{"srv": "commandline"}])
        self.assertIn("spec must be a JSON object", str(cm.exception))


class TestGenerateClaudeMcp(QuietTestCase):
    """Tests for generate_claude_mcp function."""

    def test_no_repos_directory_skips_generation(self):
        """No workspace/repos/ → prints skip message, writes nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            mcp_path = workspace / "repos" / ".mcp.json"
            marker = workspace / ".mcp.generated"

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.generate_claude_mcp(workspace, {}, {})

            self.assertIn("skipping .mcp.json", output.getvalue())
            self.assertIn("does not exist yet", output.getvalue())
            self.assertFalse(mcp_path.exists())
            self.assertFalse(marker.exists())

    def test_existing_mcp_json_without_marker_left_untouched(self):
        """Workspace ships its own repos/.mcp.json (no marker) → left byte-identical."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            mcp_path = repos_dir / ".mcp.json"
            original_content = '{"custom": "config"}\n'
            mcp_path.write_text(original_content)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.generate_claude_mcp(workspace, {"obsidian": True}, {})

            self.assertEqual(mcp_path.read_text(), original_content)
            self.assertFalse((workspace / ".mcp.generated").exists())
            self.assertIn("workspace ships its own .mcp.json", output.getvalue())

    def test_fresh_generation_claude_servers_plus_local_and_remote_plugins(self):
        """Fresh generation: claude-bound agent server first, then a local
        plugin (verbatim) + an env-scoped remote plugin (gains type: http).
        Marker created, idempotent."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            mcp_path = repos_dir / ".mcp.json"
            marker = workspace / ".mcp.generated"

            # claude_servers are already in claude form (ref headers, type: http).
            claude_servers = {"obsidian-annotated": {
                "type": "http", "url": "https://mcp-obsidian.dmetr.io/mcp",
                "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}}
            plugins = {
                "myserena": {"command": "bash", "args": ["-c"]},          # local
                "coding": {"url": "http://host.docker.internal:8811/mcp",  # remote
                           "headers": {"Authorization": "Bearer ${MCP_GATEWAY_TOKEN}"}},
            }

            wire_plugins.generate_claude_mcp(workspace, claude_servers, plugins)

            self.assertTrue(mcp_path.exists())
            self.assertTrue(marker.exists())

            content = mcp_path.read_text()
            self.assertTrue(content.endswith("\n"))
            servers = json.loads(content)["mcpServers"]
            # claude-bound agent server first, then plugins in insertion order
            self.assertEqual(list(servers), ["obsidian-annotated", "myserena", "coding"])
            # local plugin passes through verbatim (no type: injected)
            self.assertEqual(servers["myserena"], {"command": "bash", "args": ["-c"]})
            # remote plugin gains type: http; header ${VAR} refs NOT expanded
            self.assertEqual(servers["coding"], {
                "type": "http",
                "url": "http://host.docker.internal:8811/mcp",
                "headers": {"Authorization": "Bearer ${MCP_GATEWAY_TOKEN}"}})
            self.assertEqual(servers["obsidian-annotated"]["headers"]["Authorization"],
                             "Bearer ${OBSIDIAN_ANNOTATED_KEY}")

            # Rerun with marker present (idempotency check)
            wire_plugins.generate_claude_mcp(workspace, claude_servers, plugins)
            self.assertEqual(mcp_path.read_text(), content)

    def test_no_servers_no_plugins_writes_empty(self):
        """No claude servers, no plugins → writes empty mcpServers."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            mcp_path = repos_dir / ".mcp.json"

            wire_plugins.generate_claude_mcp(workspace, {}, {})

            data = json.loads(mcp_path.read_text())
            self.assertEqual(data["mcpServers"], {})


class TestLinkRepoMcp(QuietTestCase):
    """Tests for link_repo_mcp — relative symlinks from each clone to the
    workspace-level canonical repos/.mcp.json."""

    def test_symlink_created_for_repo_with_git(self):
        """Repo dir with .git → relative symlink to ../.mcp.json."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos = workspace / "repos"
            repos.mkdir()
            (repos / ".mcp.json").write_text('{"mcpServers": {}}\n')
            repo = repos / "alpha"
            repo.mkdir()
            (repo / ".git").mkdir()

            wire_plugins.link_repo_mcp(workspace)

            link = repo / ".mcp.json"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), "../.mcp.json")

    def test_no_symlink_for_dir_without_git(self):
        """Child dir without .git → no .mcp.json symlink."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos = workspace / "repos"
            repos.mkdir()
            (repos / ".mcp.json").write_text('{"mcpServers": {}}\n')
            (repos / "not-a-repo").mkdir()

            wire_plugins.link_repo_mcp(workspace)

            self.assertFalse((repos / "not-a-repo" / ".mcp.json").exists())

    def test_repo_shipped_regular_file_left_alone(self):
        """Repo ships its own regular .mcp.json → left alone with message."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos = workspace / "repos"
            repos.mkdir()
            (repos / ".mcp.json").write_text('{"mcpServers": {"canonical": {}}}\n')
            repo = repos / "beta"
            repo.mkdir()
            (repo / ".git").mkdir()
            shipped = '{"mcpServers": {"shipped": {}}}\n'
            (repo / ".mcp.json").write_text(shipped)

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.link_repo_mcp(workspace)

            self.assertFalse((repo / ".mcp.json").is_symlink())
            self.assertEqual((repo / ".mcp.json").read_text(), shipped)
            self.assertIn("repo beta ships its own .mcp.json", output.getvalue())

    def test_wrong_target_symlink_repointed(self):
        """Existing symlink with a different target → repointed to ../.mcp.json."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos = workspace / "repos"
            repos.mkdir()
            (repos / ".mcp.json").write_text('{"mcpServers": {}}\n')
            repo = repos / "gamma"
            repo.mkdir()
            (repo / ".git").mkdir()
            (repo / ".mcp.json").symlink_to("/somewhere/else/.mcp.json")

            wire_plugins.link_repo_mcp(workspace)

            link = repo / ".mcp.json"
            self.assertTrue(link.is_symlink())
            self.assertEqual(os.readlink(link), "../.mcp.json")

    def test_no_canonical_mcp_json_is_silent_noop(self):
        """No repos/.mcp.json → silent return, no links created."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            repos = workspace / "repos"
            repos.mkdir()
            repo = repos / "alpha"
            repo.mkdir()
            (repo / ".git").mkdir()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.link_repo_mcp(workspace)

            self.assertEqual(output.getvalue(), "")
            self.assertFalse((repo / ".mcp.json").exists())


class TestPreapproveClaude(QuietTestCase):
    """Tests for preapprove_claude function."""

    def test_no_mcp_json_does_nothing(self):
        """No .mcp.json → silently does nothing, no ~/.claude.json created."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "repos").mkdir()

            wire_plugins.preapprove_claude(home, workspace)

            self.assertFalse((home / ".claude.json").exists())

    def test_mcp_json_present_creates_claude_json(self):
        """No ~/.claude.json → creates it with enabledMcpjsonServers and trust flag."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            mcp_path = repos_dir / ".mcp.json"
            mcp_path.write_text('{"mcpServers": {"coding": {}, "obsidian-annotated": {}}}')

            wire_plugins.preapprove_claude(home, workspace)

            cj = home / ".claude.json"
            self.assertTrue(cj.exists())
            data = json.loads(cj.read_text())
            proj_key = str(repos_dir)
            self.assertIn(proj_key, data["projects"])
            self.assertEqual(
                sorted(data["projects"][proj_key]["enabledMcpjsonServers"]),
                ["coding", "obsidian-annotated"]
            )
            self.assertTrue(data["projects"][proj_key]["hasTrustDialogAccepted"])

    def test_per_project_entries_from_resolved_mcp_json(self):
        """One project entry for repos/ plus one per repo dir; each gets sorted
        enabledMcpjsonServers from the file that dir actually resolves. A repo
        shipping its own .mcp.json gets THAT file's server names."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            repos = workspace / "repos"
            repos.mkdir(parents=True)
            (repos / ".mcp.json").write_text(
                '{"mcpServers": {"coding": {}, "obsidian-annotated": {}}}\n')

            linked = repos / "linked"
            linked.mkdir()
            (linked / ".git").mkdir()
            (linked / ".mcp.json").symlink_to("../.mcp.json")

            shipped = repos / "shipped"
            shipped.mkdir()
            (shipped / ".git").mkdir()
            (shipped / ".mcp.json").write_text(
                '{"mcpServers": {"custom-only": {}, "also": {}}}\n')

            wire_plugins.preapprove_claude(home, workspace)

            data = json.loads((home / ".claude.json").read_text())
            projects = data["projects"]
            self.assertEqual(
                projects[str(repos)]["enabledMcpjsonServers"],
                ["coding", "obsidian-annotated"])
            self.assertEqual(
                projects[str(linked)]["enabledMcpjsonServers"],
                ["coding", "obsidian-annotated"])
            self.assertEqual(
                projects[str(shipped)]["enabledMcpjsonServers"],
                ["also", "custom-only"])
            for key in (str(repos), str(linked), str(shipped)):
                self.assertTrue(projects[key]["hasTrustDialogAccepted"])

    def test_existing_claude_json_preserves_unrelated_keys(self):
        """Existing ~/.claude.json with unrelated keys and project → those survive."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            mcp_path = repos_dir / ".mcp.json"
            mcp_path.write_text('{"mcpServers": {"coding": {}}}')

            cj = home / ".claude.json"
            initial_state = {
                "other_key": "value",
                "projects": {
                    str(repos_dir): {
                        "someField": "survives",
                    }
                }
            }
            cj.write_text(json.dumps(initial_state, indent=2) + "\n")

            wire_plugins.preapprove_claude(home, workspace)

            data = json.loads(cj.read_text())
            self.assertEqual(data["other_key"], "value")
            proj_key = str(repos_dir)
            self.assertEqual(data["projects"][proj_key]["someField"], "survives")
            self.assertEqual(data["projects"][proj_key]["enabledMcpjsonServers"], ["coding"])

    def test_invalid_json_in_claude_json_raises_wireerror(self):
        """Invalid JSON in ~/.claude.json raises WireError."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            mcp_path = repos_dir / ".mcp.json"
            mcp_path.write_text('{"mcpServers": {}}')

            cj = home / ".claude.json"
            cj.write_text("{invalid json")

            with self.assertRaises(wire_plugins.WireError):
                wire_plugins.preapprove_claude(home, workspace)


class TestWriteAgentServer(QuietTestCase):
    """Tests for write_agent_server / warn_agent_server (the generalized,
    per-agent renderer for agent-scoped remote servers)."""

    SPEC = {"url": "https://mcp-obsidian.dmetr.io/mcp",
            "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}
    SLOT = "OBSIDIAN_ANNOTATED_KEY"

    def test_cursor_agent_missing_file_creates_with_literal_key(self):
        """cursor-agent on missing file → creates ~/.cursor/mcp.json mode 0600,
        the ${SLOT} ref substituted with the literal key."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.write_agent_server(
                    "cursor-agent", "obsidian-annotated", self.SPEC, self.SLOT, "MYKEY123", home)

            mcp_path = home / ".cursor" / "mcp.json"
            self.assertTrue(mcp_path.exists())
            self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)
            data = json.loads(mcp_path.read_text())
            self.assertEqual(
                data["mcpServers"]["obsidian-annotated"],
                {"url": "https://mcp-obsidian.dmetr.io/mcp",
                 "headers": {"Authorization": "Bearer MYKEY123"}})
            self.assertIn("cursor-agent MCP config for obsidian-annotated", output.getvalue())

    def test_cursor_agent_existing_file_preserves_plugins(self):
        """cursor-agent on existing file with plugin → plugin preserved."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mcp_path = home / ".cursor" / "mcp.json"
            mcp_path.parent.mkdir(parents=True)
            mcp_path.write_text(json.dumps({"mcpServers": {"myserena": {"command": "bash"}}}))

            wire_plugins.write_agent_server(
                "cursor-agent", "obsidian-annotated", self.SPEC, self.SLOT, "KEY", home)

            data = json.loads(mcp_path.read_text())
            self.assertIn("myserena", data["mcpServers"])
            self.assertIn("obsidian-annotated", data["mcpServers"])

    def test_zero_byte_existing_file_takes_create_path(self):
        """Zero-byte existing file takes create path (pins empty-input jq bug)."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mcp_path = home / ".cursor" / "mcp.json"
            mcp_path.parent.mkdir(parents=True)
            mcp_path.write_text("")  # Zero bytes

            wire_plugins.write_agent_server(
                "cursor-agent", "obsidian-annotated", self.SPEC, self.SLOT, "KEY", home)

            data = json.loads(mcp_path.read_text())
            self.assertEqual(list(data["mcpServers"].keys()), ["obsidian-annotated"])

    def test_gemini_uses_httpurl_key(self):
        """gemini writes ~/.gemini/settings.json with key 'httpUrl' not 'url'."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wire_plugins.write_agent_server(
                "gemini", "obsidian-annotated", self.SPEC, self.SLOT, "GKEY", home)

            data = json.loads((home / ".gemini" / "settings.json").read_text())
            entry = data["mcpServers"]["obsidian-annotated"]
            self.assertIn("httpUrl", entry)
            self.assertNotIn("url", entry)
            self.assertEqual(entry["httpUrl"], "https://mcp-obsidian.dmetr.io/mcp")
            self.assertEqual(entry["headers"], {"Authorization": "Bearer GKEY"})

    def test_pi_merges_http_entry_preserving_others(self):
        """pi gets an explicit type: http entry, merged (not wholesale) so any
        other servers survive; plugin entries are re-merged right after."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            pi_path = home / ".pi" / "agent" / "mcp.json"
            pi_path.parent.mkdir(parents=True)
            pi_path.write_text(json.dumps({"mcpServers": {"keepme": {"command": "x"}}}))

            wire_plugins.write_agent_server(
                "pi", "obsidian-annotated", self.SPEC, self.SLOT, "PIKEY", home)

            data = json.loads(pi_path.read_text())
            self.assertIn("keepme", data["mcpServers"])
            entry = data["mcpServers"]["obsidian-annotated"]
            self.assertEqual(entry["type"], "http")
            self.assertEqual(entry["url"], "https://mcp-obsidian.dmetr.io/mcp")
            self.assertEqual(entry["headers"], {"Authorization": "Bearer PIKEY"})

    def test_codex_warns_writes_no_file(self):
        """codex (warn_agent_server) prints a warning and writes no file."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.warn_agent_server("codex", "obsidian-annotated", self.SLOT)

            self.assertIn("codex agent-scoped server 'obsidian-annotated' not yet wired",
                          output.getvalue())
            self.assertIn("OBSIDIAN_ANNOTATED_KEY", output.getvalue())
            self.assertFalse((home / ".codex" / "config.toml").exists())


class TestWirePluginServersJson(QuietTestCase):
    """Tests for wire_plugin_servers_json function."""

    def test_fresh_creates_config_and_sidecar(self):
        """Fresh (no file): creates config and sidecar with sorted names, mode 0600."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            plugins = {"serena": {"command": "bash"}, "agentic": {"command": "python3"}}

            wire_plugins.wire_plugin_servers_json(config_path, plugins)

            self.assertTrue(config_path.exists())
            self.assertEqual(os.stat(config_path).st_mode & 0o777, 0o600)

            sidecar = config_path.parent / (config_path.name + ".dev-agent-plugins")
            self.assertTrue(sidecar.exists())
            self.assertEqual(os.stat(sidecar).st_mode & 0o777, 0o600)

            sidecar_data = json.loads(sidecar.read_text())
            self.assertEqual(sidecar_data, ["agentic", "serena"])  # sorted

    def test_existing_config_removes_stale_plugin_keeps_identity_and_handadded(self):
        """Existing config with identity, hand-added, stale plugin → stale removed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "obsidian-annotated": {"url": "..."},  # identity
                    "myhandadded": {"command": "custom"},  # hand-added
                    "oldplug": {"command": "removed"}  # stale plugin
                }
            }))

            sidecar = config_path.parent / (config_path.name + ".dev-agent-plugins")
            sidecar.write_text('["oldplug"]\n')

            # Wire with new plugins (no oldplug)
            wire_plugins.wire_plugin_servers_json(config_path, {"newplug": {"command": "new"}})

            data = json.loads(config_path.read_text())
            self.assertNotIn("oldplug", data["mcpServers"])
            self.assertIn("obsidian-annotated", data["mcpServers"])
            self.assertIn("myhandadded", data["mcpServers"])
            self.assertIn("newplug", data["mcpServers"])

    def test_idempotency_two_calls_byte_identical(self):
        """Calling twice yields byte-identical config and sidecar."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            plugins = {"serena": {"command": "bash"}}

            wire_plugins.wire_plugin_servers_json(config_path, plugins)
            content1 = config_path.read_text()
            sidecar = config_path.parent / (config_path.name + ".dev-agent-plugins")
            sidecar_content1 = sidecar.read_text()

            wire_plugins.wire_plugin_servers_json(config_path, plugins)
            content2 = config_path.read_text()
            sidecar_content2 = sidecar.read_text()

            self.assertEqual(content1, content2)
            self.assertEqual(sidecar_content1, sidecar_content2)

    def test_plugin_removal_wired_plugin_gone_identity_survives(self):
        """Removing a plugin: serena removed, identity survives."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text(json.dumps({
                "mcpServers": {
                    "obsidian-annotated": {"url": "..."},
                    "serena": {"command": "bash"}
                }
            }))

            sidecar = config_path.parent / (config_path.name + ".dev-agent-plugins")
            sidecar.write_text('["serena"]\n')

            # Call with empty plugins
            wire_plugins.wire_plugin_servers_json(config_path, {})

            data = json.loads(config_path.read_text())
            self.assertNotIn("serena", data["mcpServers"])
            self.assertIn("obsidian-annotated", data["mcpServers"])
            self.assertEqual(json.loads(sidecar.read_text()), [])

    def test_invalid_json_in_config_raises_wireerror(self):
        """Invalid JSON in config raises WireError naming the file."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text("{invalid")

            with self.assertRaises(wire_plugins.WireError):
                wire_plugins.wire_plugin_servers_json(config_path, {})

    def test_zero_byte_config_takes_create_path(self):
        """Zero-byte config takes create path."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text("")

            wire_plugins.wire_plugin_servers_json(config_path, {"p": {"command": "x"}})

            data = json.loads(config_path.read_text())
            self.assertIn("p", data["mcpServers"])

    def test_zero_byte_sidecar_treated_as_empty_array(self):
        """Zero-byte sidecar treated as [], no error."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text(json.dumps({"mcpServers": {"old": {}}}))

            sidecar = config_path.parent / (config_path.name + ".dev-agent-plugins")
            sidecar.write_text("")

            # Should not raise
            wire_plugins.wire_plugin_servers_json(config_path, {"new": {}})
            data = json.loads(config_path.read_text())
            self.assertIn("old", data["mcpServers"])  # old NOT removed since sidecar was empty
            self.assertIn("new", data["mcpServers"])


class TestWireCodexToml(QuietTestCase):
    """Tests for wire_codex_toml function."""

    def test_missing_file_one_plugin_exact_format(self):
        """Missing file + one plugin → exact format with markers and mode 0600."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            plugins = {"serena": {"command": "bash", "args": ["-lc", "x"]}}

            wire_plugins.wire_codex_toml(config_path, plugins)

            self.assertTrue(config_path.exists())
            self.assertEqual(os.stat(config_path).st_mode & 0o777, 0o600)

            content = config_path.read_text()
            self.assertIn("# >>> dev-agent plugin MCP", content)
            self.assertIn("[mcp_servers.serena]", content)
            self.assertIn('command = "bash"', content)
            self.assertIn('args = ["-lc","x"]', content)  # compact JSON
            self.assertIn("# <<< dev-agent plugin MCP", content)
            self.assertTrue(content.endswith("\n"))

    def test_existing_hand_config_survives_above_block(self):
        """Existing hand config + plugins → hand line survives above block."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("keep = 1\n")

            plugins = {"serena": {"command": "bash", "args": []}}
            wire_plugins.wire_codex_toml(config_path, plugins)

            content = config_path.read_text()
            self.assertTrue(content.startswith("keep = 1\n"))
            self.assertIn("[mcp_servers.serena]", content)

            # Rerun is byte-idempotent
            content1 = content
            wire_plugins.wire_codex_toml(config_path, plugins)
            content2 = config_path.read_text()
            self.assertEqual(content1, content2)

    def test_args_absent_renders_empty_array(self):
        """args absent → renders args = []."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            plugins = {"serena": {"command": "bash"}}  # no args key

            wire_plugins.wire_codex_toml(config_path, plugins)

            content = config_path.read_text()
            self.assertIn("args = []", content)

    def test_toml_escaping_in_command(self):
        """TOML escaping: special chars in command → JSON escapes."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            plugins = {"serena": {"command": 'a"b\\c'}}

            wire_plugins.wire_codex_toml(config_path, plugins)

            content = config_path.read_text()
            # JSON escapes: " → \" and \ → \\
            self.assertIn('command = "a\\"b\\\\c"', content)

    def test_two_plugins_separated_by_blank_line(self):
        """Two plugins → two tables separated by exactly one blank line."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            plugins = {
                "serena": {"command": "bash"},
                "agentic": {"command": "python3"}
            }

            wire_plugins.wire_codex_toml(config_path, plugins)

            content = config_path.read_text()
            # Extract the managed block
            start = content.find("# >>> dev-agent plugin MCP")
            end = content.find("# <<< dev-agent plugin MCP") + len("# <<< dev-agent plugin MCP <<<")
            block = content[start:end]

            # Check two tables with exactly one blank line between them
            self.assertIn("[mcp_servers.serena]", block)
            self.assertIn("[mcp_servers.agentic]", block)
            self.assertIn("args = []\n\n[mcp_servers.agentic]", block)

    def test_empty_plugins_removes_managed_block_keeps_hand_content(self):
        """Empty plugins: managed block REMOVED, hand content survives."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "keep = 1\n"
                "# >>> dev-agent plugin MCP <<<\n"
                "[mcp_servers.old]\n"
                "# <<< dev-agent plugin MCP <<<\n"
                "keep_me = 2\n"
            )

            wire_plugins.wire_codex_toml(config_path, {})

            content = config_path.read_text()
            self.assertIn("keep = 1", content)
            self.assertIn("keep_me = 2", content)
            self.assertNotIn("[mcp_servers.old]", content)
            self.assertNotIn("# >>> dev-agent plugin MCP", content)

    def test_opening_marker_without_closing_raises_wireerror(self):
        """Opening marker present but no closing marker → WireError, file untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            original = "# >>> dev-agent plugin MCP <<<\nstuff\n"
            config_path.write_text(original)

            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins.wire_codex_toml(config_path, {})
            self.assertIn("repair the markers", str(cm.exception))
            self.assertEqual(config_path.read_text(), original)

    def test_lone_closing_marker_left_in_place(self):
        """Lone closing marker with no opener is left in place."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("keep = 1\n# <<< dev-agent plugin MCP <<<\n")

            wire_plugins.wire_codex_toml(config_path, {"p": {"command": "x"}})

            content = config_path.read_text()
            self.assertIn("# <<< dev-agent plugin MCP <<<", content)
            self.assertIn("[mcp_servers.p]", content)

    def test_content_between_markers_fully_removed(self):
        """Content between markers (stale entries) fully removed on rewire."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "# >>> dev-agent plugin MCP <<<\n"
                "[mcp_servers.stale]\n"
                "command = 'old'\n"
                "# <<< dev-agent plugin MCP <<<\n"
            )

            wire_plugins.wire_codex_toml(config_path, {"new": {"command": "bash"}})

            content = config_path.read_text()
            self.assertNotIn("[mcp_servers.stale]", content)
            self.assertIn("[mcp_servers.new]", content)


class TestRunIntegration(QuietTestCase):
    """Integration tests for the run function."""

    def test_full_payload_all_agents_wired(self):
        """Full payload with wire all true, capabilities, plugin, identity → configs generated."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            repo = repos_dir / "app"
            repo.mkdir()
            (repo / ".git").mkdir()

            env = {"IDENTITY_KEY_0": "LITERALKEY"}
            payload = {
                "wire": {
                    "cursor": True,
                    "gemini": True,
                    "pi": True,
                    "codex": True,
                },
                "plugin_mcp_entries": [
                    {"myserena": {"command": "bash", "args": ["-c"]}},   # local
                    {"coding": {"url": "http://host.docker.internal:8811/mcp",  # env-remote
                                "headers": {"Authorization": "Bearer ${MCP_GATEWAY_TOKEN}"}}},
                ],
                "agent_servers": [
                    {"name": "obsidian-annotated", "slot": "OBSIDIAN_ANNOTATED_KEY",
                     "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                              "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}},
                     "claude": True,
                     "literal": [{"agent": "cursor-agent", "key_env": "IDENTITY_KEY_0"}],
                     "warn": ["codex"]},
                ],
            }

            wire_plugins.run(payload, home, workspace, env)

            # repos/.mcp.json generated (claude) — gets obsidian (ref) + BOTH plugins
            self.assertTrue((repos_dir / ".mcp.json").exists())
            claude = json.loads((repos_dir / ".mcp.json").read_text())["mcpServers"]
            self.assertEqual(set(claude), {"obsidian-annotated", "myserena", "coding"})
            self.assertEqual(claude["obsidian-annotated"]["headers"]["Authorization"],
                             "Bearer ${OBSIDIAN_ANNOTATED_KEY}")  # ref, not literal
            # repo dir gets a relative symlink to the canonical file
            self.assertTrue((repo / ".mcp.json").is_symlink())
            self.assertEqual(os.readlink(repo / ".mcp.json"), "../.mcp.json")
            # ~/.claude.json pre-approved for repos/ and the repo dir
            self.assertTrue((home / ".claude.json").exists())
            projects = json.loads((home / ".claude.json").read_text())["projects"]
            self.assertIn(str(repos_dir), projects)
            self.assertIn(str(repo), projects)
            # cursor: obsidian (LITERAL key) + LOCAL plugin only (no remote coding)
            cursor_mcp = home / ".cursor" / "mcp.json"
            self.assertTrue(cursor_mcp.exists())
            cursor_data = json.loads(cursor_mcp.read_text())
            self.assertIn("obsidian-annotated", cursor_data["mcpServers"])
            self.assertIn("myserena", cursor_data["mcpServers"])
            self.assertNotIn("coding", cursor_data["mcpServers"])
            self.assertEqual(
                cursor_data["mcpServers"]["obsidian-annotated"]["headers"]["Authorization"],
                "Bearer LITERALKEY"
            )
            # codex managed block carries the local plugin, not the remote one
            codex_toml = (home / ".codex" / "config.toml").read_text()
            self.assertIn("[mcp_servers.myserena]", codex_toml)
            self.assertNotIn("coding", codex_toml)
            # pi config exists
            self.assertTrue((home / ".pi" / "agent" / "mcp.json").exists())
            # gemini config exists
            self.assertTrue((home / ".gemini" / "settings.json").exists())
            # codex config exists
            self.assertTrue((home / ".codex" / "config.toml").exists())

    def test_wire_flags_false_no_agent_configs_created(self):
        """wire flags false → no agent config files created (claude .mcp.json still generated)."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()

            payload = {
                "wire": {
                    "cursor": False,
                    "gemini": False,
                    "pi": False,
                    "codex": False,
                },
                "capabilities": {},
                "plugin_mcp_entries": [],
                "identities": [],
            }

            wire_plugins.run(payload, home, workspace, {})

            # claude .mcp.json still generated at the workspace canonical path
            self.assertTrue((repos_dir / ".mcp.json").exists())
            # No agent home configs
            self.assertFalse((home / ".cursor").exists())
            self.assertFalse((home / ".gemini").exists())
            self.assertFalse((home / ".pi").exists())
            self.assertFalse((home / ".codex").exists())

    def test_local_agent_scoped_server_wires_only_bound_agents(self):
        """A LOCAL agent-scoped server (axiom mcp-remote) lands in the config of
        each agent in `local` (codex included) and in claude's .mcp.json, but NOT
        in an unbound agent's config. The token is never written into any file."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            (workspace / "repos").mkdir(parents=True)

            spec = {"command": "mcp-remote",
                    "args": ["https://mcp.axiom.co/mcp", "--header",
                             "Authorization: Bearer ${AXIOM_TOKEN}"]}
            payload = {
                "wire": {"cursor": True, "gemini": True, "pi": True, "codex": True},
                "plugin_mcp_entries": [],
                "agent_servers": [
                    {"name": "axiom", "slot": "AXIOM_TOKEN", "spec": spec,
                     "claude": True, "literal": [], "warn": [],
                     "local": ["cursor-agent", "codex"]},  # gemini/pi NOT bound
                ],
            }
            wire_plugins.run(payload, home, workspace, {})

            # claude .mcp.json: axiom present as a command server (verbatim)
            claude = json.loads((workspace / "repos" / ".mcp.json").read_text())["mcpServers"]
            self.assertEqual(claude["axiom"], spec)
            # cursor + codex: wired
            cursor = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]
            self.assertEqual(cursor["axiom"], spec)
            self.assertIn("[mcp_servers.axiom]", (home / ".codex" / "config.toml").read_text())
            # gemini + pi: NOT wired (no token → no server)
            gemini = json.loads((home / ".gemini" / "settings.json").read_text())["mcpServers"]
            self.assertNotIn("axiom", gemini)
            pi = json.loads((home / ".pi" / "agent" / "mcp.json").read_text())["mcpServers"]
            self.assertNotIn("axiom", pi)
            # the token itself is never written anywhere — only the ${VAR} ref
            # that mcp-remote substitutes at connect time (never in argv either)
            for f in (workspace / "repos" / ".mcp.json", home / ".cursor" / "mcp.json",
                      home / ".codex" / "config.toml"):
                self.assertIn("${AXIOM_TOKEN}", f.read_text())

    def test_payload_not_dict_raises_wireerror(self):
        """Payload not a dict raises WireError."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaises(wire_plugins.WireError):
                wire_plugins.run(["not", "a", "dict"], Path(tmp), workspace, {})

    def test_plugin_entry_non_dict_raises_wireerror(self):
        """plugin_mcp_entries containing non-dict raises WireError."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "repos").mkdir()

            payload = {
                "plugin_mcp_entries": ["not a dict"],
            }

            with self.assertRaises(wire_plugins.WireError):
                wire_plugins.run(payload, home, workspace, {})


class TestNoReservedNames(unittest.TestCase):
    """As of Phase 2 nothing is reserved — every MCP server comes from a plugin
    file. merge_plugin_entries only rejects cross-plugin duplicates."""

    def test_reserved_set_removed(self):
        self.assertFalse(hasattr(wire_plugins, "RESERVED_SERVER_NAMES"))

    def test_former_generated_names_now_pass(self):
        # coding/proxyman/browser AND obsidian-annotated are plugin data now.
        for name in ("coding", "proxyman", "browser", "obsidian-annotated"):
            merged = wire_plugins.merge_plugin_entries([{name: {"url": "http://h/mcp"}}])
            self.assertIn(name, merged)

    def test_ordinary_name_passes(self):
        merged = wire_plugins.merge_plugin_entries([{"serena": {"command": "x"}}])
        self.assertIn("serena", merged)


class TestPreapproveSkipsBadRepoMcpJson(QuietTestCase):
    """A shipped .mcp.json we can't understand must skip pre-approval
    with a warning, not abort the whole wiring run (the file is explicitly
    not ours; the other agents still need their configs)."""

    def _setup(self, tmp, mcp_content):
        home = Path(tmp) / "home"
        home.mkdir()
        workspace = Path(tmp) / "workspace"
        (workspace / "repos").mkdir(parents=True)
        (workspace / "repos" / ".mcp.json").write_text(mcp_content)
        return home, workspace

    def test_no_mcpservers_object_warns_and_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, workspace = self._setup(tmp, "{}")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.preapprove_claude(home, workspace)
            self.assertIn("skipping claude pre-approval", output.getvalue())
            self.assertFalse((home / ".claude.json").exists())

    def test_invalid_json_warns_and_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, workspace = self._setup(tmp, "{not json")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.preapprove_claude(home, workspace)
            self.assertIn("skipping claude pre-approval", output.getvalue())
            self.assertFalse((home / ".claude.json").exists())

    def test_bad_repo_mcp_does_not_abort_other_dirs(self):
        """Unreadable/shapeless per-repo .mcp.json skips that dir only."""
        with tempfile.TemporaryDirectory() as tmp:
            home, workspace = self._setup(tmp, '{"mcpServers": {"coding": {}}}')
            bad = workspace / "repos" / "bad"
            bad.mkdir()
            (bad / ".git").mkdir()
            (bad / ".mcp.json").write_text("{not json")
            good = workspace / "repos" / "good"
            good.mkdir()
            (good / ".git").mkdir()
            (good / ".mcp.json").symlink_to("../.mcp.json")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.preapprove_claude(home, workspace)

            self.assertIn("skipping claude pre-approval", output.getvalue())
            data = json.loads((home / ".claude.json").read_text())
            self.assertIn(str(workspace / "repos"), data["projects"])
            self.assertIn(str(good), data["projects"])
            self.assertNotIn(str(bad), data["projects"])

    def test_symlinked_claude_json_written_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, workspace = self._setup(tmp, '{"mcpServers": {"coding": {}}}')
            target = Path(tmp) / "dotfiles-claude.json"
            target.write_text("{}")
            (home / ".claude.json").symlink_to(target)
            wire_plugins.preapprove_claude(home, workspace)
            self.assertTrue((home / ".claude.json").is_symlink())
            data = json.loads(target.read_text())
            proj = data["projects"][str(workspace / "repos")]
            self.assertEqual(proj["enabledMcpjsonServers"], ["coding"])


class TestWireCodexTomlMarkerEdges(QuietTestCase):
    """Cases where the old grep guard passed but the sed strip silently ate
    the file to EOF — now hard errors (block still open at end of file)."""

    def test_stray_second_opener_after_closed_block_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            original = (
                "# >>> dev-agent plugin MCP >>>\n"
                "[mcp_servers.old]\n"
                "# <<< dev-agent plugin MCP <<<\n"
                "# >>> dev-agent plugin MCP >>>\n"
                "user_config = 1\n"
            )
            config_path.write_text(original)
            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins.wire_codex_toml(config_path, {"p": {"command": "x"}})
            self.assertIn("repair the markers", str(cm.exception))
            self.assertEqual(config_path.read_text(), original)

    def test_closer_above_opener_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            original = (
                "# <<< dev-agent plugin MCP <<<\n"
                "# >>> dev-agent plugin MCP >>>\n"
                "user_config = 1\n"
            )
            config_path.write_text(original)
            with self.assertRaises(wire_plugins.WireError):
                wire_plugins.wire_codex_toml(config_path, {"p": {"command": "x"}})
            self.assertEqual(config_path.read_text(), original)

    def test_crlf_hand_content_preserved_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_bytes(b"keep = 1\r\nalso = 2\r\n")
            wire_plugins.wire_codex_toml(config_path, {"p": {"command": "x"}})
            content = config_path.read_bytes().decode()
            self.assertTrue(content.startswith("keep = 1\r\nalso = 2\r\n"))
            self.assertIn("[mcp_servers.p]", content)


@unittest.skip("Replaced by universal hybrid payload tests")
class TestBuildPayload(unittest.TestCase):
    """Host-side payload assembly: strict [ = \"true\" ] boolean semantics and
    the env-var contract with up.sh."""

    OBS_SERVERS = {"OBSIDIAN_ANNOTATED_KEY": {
        "name": "obsidian-annotated",
        "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                 "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}}}

    def test_only_literal_true_enables_flags(self):
        env = {"WIRE_CURSOR": "true", "WIRE_GEMINI": "yes", "WIRE_PI": "1",
               "WIRE_CODEX": "True"}
        payload = wire_plugins.build_payload(env)
        self.assertEqual(payload["wire"],
                         {"cursor": True, "gemini": False, "pi": False, "codex": False})
        # capabilities is gone entirely — obsidian is an agent_server now.
        self.assertNotIn("capabilities", payload)

    def test_agent_servers_assembled_from_json_and_triples(self):
        env = {"AGENT_SERVERS_JSON": json.dumps(self.OBS_SERVERS),
               "IDENTITY_AGENTS": "claude::OBSIDIAN_ANNOTATED_KEY "
                                  "cursor-agent:IDENTITY_KEY_0:OBSIDIAN_ANNOTATED_KEY "
                                  "codex::OBSIDIAN_ANNOTATED_KEY"}
        payload = wire_plugins.build_payload(env)
        self.assertEqual(len(payload["agent_servers"]), 1)
        e = payload["agent_servers"][0]
        self.assertEqual((e["name"], e["slot"], e["claude"]),
                         ("obsidian-annotated", "OBSIDIAN_ANNOTATED_KEY", True))
        self.assertEqual(e["literal"], [{"agent": "cursor-agent", "key_env": "IDENTITY_KEY_0"}])
        self.assertEqual(e["warn"], ["codex"])

    def test_triple_referencing_unknown_slot_raises(self):
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins.build_payload({"IDENTITY_AGENTS": "claude::NOPE"})
        self.assertIn("no server definition", str(cm.exception))

    def test_missing_env_means_everything_off_and_empty(self):
        payload = wire_plugins.build_payload({})
        self.assertEqual(payload["wire"],
                         {"cursor": False, "gemini": False, "pi": False, "codex": False})
        self.assertEqual(payload["plugin_mcp_entries"], [])
        self.assertEqual(payload["agent_servers"], [])

    def test_plugin_entries_parsed_per_line_blank_lines_ignored(self):
        env = {"PLUGIN_MCP_ENTRIES": '{"serena": {"command": "bash"}}\n\n{"other": {"command": "x"}}\n'}
        payload = wire_plugins.build_payload(env)
        self.assertEqual(payload["plugin_mcp_entries"],
                         [{"serena": {"command": "bash"}}, {"other": {"command": "x"}}])

    def test_invalid_entry_line_raises(self):
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins.build_payload({"PLUGIN_MCP_ENTRIES": "{broken\n"})
        self.assertIn("invalid JSON", str(cm.exception))

    def test_non_object_entry_line_raises(self):
        with self.assertRaises(wire_plugins.WireError):
            wire_plugins.build_payload({"PLUGIN_MCP_ENTRIES": "[1, 2]\n"})

    def test_round_trips_through_run(self):
        """The payload build_payload emits is exactly what run() consumes: an
        agent-scoped server reaches claude (ref) and cursor (literal); a local
        plugin reaches cursor too."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            workspace = Path(tmp) / "workspace"
            repos = workspace / "repos"
            repo = repos / "app"
            (repo / ".git").mkdir(parents=True)
            env = {"WIRE_CURSOR": "true",
                   "PLUGIN_MCP_ENTRIES": '{"serena": {"command": "bash"}}\n',
                   "AGENT_SERVERS_JSON": json.dumps(self.OBS_SERVERS),
                   "IDENTITY_AGENTS": "claude::OBSIDIAN_ANNOTATED_KEY "
                                      "cursor-agent:K0:OBSIDIAN_ANNOTATED_KEY",
                   "K0": "SECRET"}
            payload = json.loads(json.dumps(wire_plugins.build_payload(env)))
            with contextlib.redirect_stdout(io.StringIO()):
                wire_plugins.run(payload, home, workspace, env)
            cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
            # cursor: local plugin + obsidian with the LITERAL key substituted
            self.assertIn("serena", cursor["mcpServers"])
            self.assertEqual(
                cursor["mcpServers"]["obsidian-annotated"]["headers"]["Authorization"],
                "Bearer SECRET")
            mcp = json.loads((repos / ".mcp.json").read_text())
            # claude: obsidian (ref, type:http) then the local plugin
            self.assertEqual(list(mcp["mcpServers"]), ["obsidian-annotated", "serena"])
            self.assertEqual(mcp["mcpServers"]["obsidian-annotated"]["type"], "http")
            self.assertEqual(
                mcp["mcpServers"]["obsidian-annotated"]["headers"]["Authorization"],
                "Bearer ${OBSIDIAN_ANNOTATED_KEY}")
            self.assertTrue((repo / ".mcp.json").is_symlink())
            self.assertEqual(os.readlink(repo / ".mcp.json"), "../.mcp.json")


class TestHybridBuildPayload(unittest.TestCase):
    def test_required_server_needs_every_effective_slot(self):
        env = {
            "AGENT_SERVERS_JSON": json.dumps({
                "remote": {
                    "spec": {"url": "https://example.test/mcp",
                             "headers": {"Authorization": "Bearer ${TOKEN}",
                                         "X-Second": "${SECOND}"}},
                    "requires": ["TOKEN", "SECOND"]},
                "local": {
                    "spec": {"command": "bridge", "args": ["${TOKEN}"]},
                    "requires": ["TOKEN"]},
            }),
            "AGENT_SECRETS": (
                "claude\tTOKEN\tCOMMON\n"
                "cursor-agent\tTOKEN\tCURSOR\n"
                "cursor-agent\tSECOND\tCURSOR_SECOND\n"),
            "IDENTITY_SECRETS": (
                "cursor-agent:IDENTITY_KEY_0:TOKEN "
                "cursor-agent:IDENTITY_KEY_1:SECOND"),
        }
        servers = {entry["name"]: entry for entry in wire_plugins.build_payload(env)["agent_servers"]}
        self.assertTrue(servers["local"]["claude"])
        self.assertEqual(servers["local"]["local"], ["cursor-agent"])
        self.assertFalse(servers["remote"]["claude"])
        self.assertEqual(
            servers["remote"]["literal"],
            [{"agent": "cursor-agent",
              "key_envs": {"TOKEN": "IDENTITY_KEY_0", "SECOND": "IDENTITY_KEY_1"}}])

    def test_local_server_routes_codex_into_local_not_warn(self):
        # A LOCAL (command) agent-scoped server — axiom's mcp-remote bridge —
        # puts every non-claude bound agent, codex INCLUDED, into `local`
        # (codex's TOML supports command servers), never `warn`/`literal`.
        env = {
            "AGENT_SERVERS_JSON": json.dumps({
                "axiom": {"spec": {"command": "mcp-remote",
                                   "args": ["https://mcp.axiom.co/mcp"]},
                          "requires": ["AXIOM_TOKEN"]},
            }),
            "AGENT_SECRETS": (
                "claude\tAXIOM_TOKEN\tCOMMON\n"
                "cursor-agent\tAXIOM_TOKEN\tCOMMON\n"
                "codex\tAXIOM_TOKEN\tCOMMON\n"),
        }
        e = wire_plugins.build_payload(env)["agent_servers"][0]
        self.assertTrue(e["claude"])
        self.assertEqual(sorted(e["local"]), ["codex", "cursor-agent"])
        self.assertEqual(e["literal"], [])
        self.assertEqual(e["warn"], [])

    def test_literal_agent_substitutes_all_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wire_plugins.write_agent_server(
                "cursor-agent", "remote",
                {"url": "https://example.test/mcp",
                 "headers": {"Authorization": "Bearer ${TOKEN}", "X-Second": "${SECOND}"}},
                {"TOKEN": "first", "SECOND": "second"}, home)
            entry = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["remote"]
            self.assertEqual(entry["headers"],
                             {"Authorization": "Bearer first", "X-Second": "second"})


class TestMainSubprocess(unittest.TestCase):
    """Test main() entry point via subprocess."""

    def test_invalid_json_stdin_exits_1_with_error_message(self):
        """main() with invalid JSON on stdin → exit 1, 'Error: invalid JSON payload'."""
        import subprocess
        module_path = Path(__file__).parent.parent / "src" / "wire_plugins.py"
        result = subprocess.run(
            [sys.executable, str(module_path)],
            input=b"{invalid",
            capture_output=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"Error: invalid JSON payload", result.stdout)


if __name__ == "__main__":
    unittest.main()
