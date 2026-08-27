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
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Add src/ to path for import
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import manifest
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


class TestRenderNamedEnvRefServer(unittest.TestCase):
    """Tests for _render_named_env_ref_server — the named-ref (bearerTokenEnvVar
    / bearer_token_env_var) rendering path shared by kimi and codex."""

    SLOT = "TOKEN"

    def test_bearer_header_renders_field_and_strips_authorization(self):
        spec = {"url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN}"}}
        rendered = wire_plugins._render_named_env_ref_server(
            spec, [self.SLOT], "bearer_token_env_var", "srv", "codex")
        self.assertEqual(rendered["headers"], {})
        self.assertEqual(rendered["bearer_token_env_var"], "TOKEN")

    def test_other_headers_survive_untouched(self):
        spec = {"url": "https://example.test/mcp",
                "headers": {"Authorization": "Bearer ${TOKEN}", "X-Extra": "kept"}}
        rendered = wire_plugins._render_named_env_ref_server(
            spec, [self.SLOT], "bearer_token_env_var", "srv", "codex")
        self.assertEqual(rendered["headers"], {"X-Extra": "kept"})

    def test_non_bearer_header_scheme_rejected(self):
        """A named env_refs field always means bearer auth (the agent CLI
        builds Authorization: Bearer <value> itself) — a spec whose auth
        actually rides a different header must be rejected, not silently
        rendered as Bearer auth against a server that requires something else."""
        spec = {"url": "https://example.test/mcp",
                "headers": {"X-API-Key": "${TOKEN}"}}
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins._render_named_env_ref_server(
                spec, [self.SLOT], "bearer_token_env_var", "srv", "codex")
        self.assertIn("requires headers.Authorization", str(cm.exception))

    def test_authorization_header_not_exactly_bearer_marker_rejected(self):
        """Authorization present but not the canonical 'Bearer ${SLOT}' shape
        (e.g. extra text, wrong scheme) must also be rejected."""
        spec = {"url": "https://example.test/mcp",
                "headers": {"Authorization": "Token ${TOKEN}"}}
        with self.assertRaises(wire_plugins.WireError):
            wire_plugins._render_named_env_ref_server(
                spec, [self.SLOT], "bearer_token_env_var", "srv", "codex")


class TestWriteAgentServer(QuietTestCase):
    """Tests for literal rendering and write_agent_server / warn_agent_server."""

    SPEC = {"url": "https://mcp-obsidian.dmetr.io/mcp",
            "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}
    SLOT = "OBSIDIAN_ANNOTATED_KEY"

    def test_cursor_agent_missing_file_creates_with_literal_key(self):
        """cursor-agent on missing file → creates ~/.cursor/mcp.json mode 0600,
        the ${SLOT} ref substituted with the literal key."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output = io.StringIO()
            entry = wire_plugins._literal_agent_config("url", self.SPEC, {self.SLOT: "MYKEY123"})
            with contextlib.redirect_stdout(output):
                wire_plugins.write_agent_server(
                    "cursor-agent", ".cursor/mcp.json", "obsidian-annotated", entry, home)

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

            entry = wire_plugins._literal_agent_config("url", self.SPEC, {self.SLOT: "KEY"})
            wire_plugins.write_agent_server(
                "cursor-agent", ".cursor/mcp.json", "obsidian-annotated", entry, home)

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

            entry = wire_plugins._literal_agent_config("url", self.SPEC, {self.SLOT: "KEY"})
            wire_plugins.write_agent_server(
                "cursor-agent", ".cursor/mcp.json", "obsidian-annotated", entry, home)

            data = json.loads(mcp_path.read_text())
            self.assertEqual(list(data["mcpServers"].keys()), ["obsidian-annotated"])

    def test_httpurl_dialect_uses_httpurl_key(self):
        """The httpUrl dialect renders key 'httpUrl', never 'url'. No shipped
        agent uses it today (gemini, which did, was retired) — the dialect is
        still supported, so it is pinned against a synthetic descriptor."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            entry = wire_plugins._literal_agent_config("httpUrl", self.SPEC, {self.SLOT: "GKEY"})
            wire_plugins.write_agent_server(
                "httpurl-agent", ".httpurl/settings.json", "obsidian-annotated", entry, home)

            data = json.loads((home / ".httpurl" / "settings.json").read_text())
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

            entry = wire_plugins._literal_agent_config("type-http", self.SPEC, {self.SLOT: "PIKEY"})
            wire_plugins.write_agent_server(
                "pi", ".pi/agent/mcp.json", "obsidian-annotated", entry, home)

            data = json.loads(pi_path.read_text())
            self.assertIn("keepme", data["mcpServers"])
            entry = data["mcpServers"]["obsidian-annotated"]
            self.assertEqual(entry["type"], "http")
            self.assertEqual(entry["url"], "https://mcp-obsidian.dmetr.io/mcp")
            self.assertEqual(entry["headers"], {"Authorization": "Bearer PIKEY"})

    def test_serverurl_dialect_uses_serverurl_key(self):
        """agy accepts only 'serverUrl' for a remote server — it rejects both
        'url' and 'httpUrl' by name — and reads headers alongside it."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            entry = wire_plugins._literal_agent_config("serverUrl", self.SPEC, {self.SLOT: "AKEY"})
            wire_plugins.write_agent_server(
                "agy", ".gemini/config/mcp_config.json", "obsidian-annotated", entry, home)

            data = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
            entry = data["mcpServers"]["obsidian-annotated"]
            self.assertIn("serverUrl", entry)
            self.assertNotIn("url", entry)
            self.assertNotIn("httpUrl", entry)
            self.assertEqual(entry["serverUrl"], "https://mcp-obsidian.dmetr.io/mcp")
            self.assertEqual(entry["headers"], {"Authorization": "Bearer AKEY"})

    def test_codex_warns_writes_no_file(self):
        """codex (warn_agent_server) prints a warning and writes no file."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.warn_agent_server(
                    "codex", ".codex/config.toml", "obsidian-annotated", self.SLOT)

            self.assertIn("codex agent-scoped server 'obsidian-annotated' not yet wired",
                          output.getvalue())
            self.assertIn("OBSIDIAN_ANNOTATED_KEY", output.getvalue())
            self.assertFalse((home / ".codex" / "config.toml").exists())

    def test_literal_renderer_rejects_unknown_dialect(self):
        with self.assertRaises(wire_plugins.WireError):
            wire_plugins._literal_agent_config("mcpServers", self.SPEC, {self.SLOT: "x"})


class TestHomeConfigPathGuards(QuietTestCase):
    def test_home_config_path_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins._home_config_path(home, "cursor-agent", "/etc/mcp.json")
            self.assertIn("must be home-relative", str(cm.exception))

    def test_home_config_path_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins._home_config_path(home, "cursor-agent", "../escape.json")
            self.assertIn("must not traverse directories", str(cm.exception))

    def test_run_rejects_payload_with_traversal_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "repos").mkdir()
            payload = {
                "agents": [{
                    "binary": "cursor-agent",
                    "config_path": "../escape.json",
                    "format": "json",
                    "dialect": "url",
                    "env_refs": False,
                    "strategy": "",
                }],
                "plugin_mcp_entries": [],
                "agent_servers": [],
            }
            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins.run(payload, home, workspace, {})
            self.assertIn("must not traverse directories", str(cm.exception))


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

            sidecar = config_path.parent / (config_path.name + ".djinn-plugins")
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

            sidecar = config_path.parent / (config_path.name + ".djinn-plugins")
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
            sidecar = config_path.parent / (config_path.name + ".djinn-plugins")
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

            sidecar = config_path.parent / (config_path.name + ".djinn-plugins")
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

            sidecar = config_path.parent / (config_path.name + ".djinn-plugins")
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
            self.assertIn("# >>> djinn plugin MCP", content)
            self.assertIn("[mcp_servers.serena]", content)
            self.assertIn('command = "bash"', content)
            self.assertIn('args = ["-lc","x"]', content)  # compact JSON
            self.assertIn("# <<< djinn plugin MCP", content)
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
            start = content.find("# >>> djinn plugin MCP")
            end = content.find("# <<< djinn plugin MCP") + len("# <<< djinn plugin MCP <<<")
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
                "# >>> djinn plugin MCP <<<\n"
                "[mcp_servers.old]\n"
                "# <<< djinn plugin MCP <<<\n"
                "keep_me = 2\n"
            )

            wire_plugins.wire_codex_toml(config_path, {})

            content = config_path.read_text()
            self.assertIn("keep = 1", content)
            self.assertIn("keep_me = 2", content)
            self.assertNotIn("[mcp_servers.old]", content)
            self.assertNotIn("# >>> djinn plugin MCP", content)

    def test_opening_marker_without_closing_raises_wireerror(self):
        """Opening marker present but no closing marker → WireError, file untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            original = "# >>> djinn plugin MCP <<<\nstuff\n"
            config_path.write_text(original)

            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins.wire_codex_toml(config_path, {})
            self.assertIn("repair the markers", str(cm.exception))
            self.assertEqual(config_path.read_text(), original)

    def test_lone_closing_marker_left_in_place(self):
        """Lone closing marker with no opener is left in place."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("keep = 1\n# <<< djinn plugin MCP <<<\n")

            wire_plugins.wire_codex_toml(config_path, {"p": {"command": "x"}})

            content = config_path.read_text()
            self.assertIn("# <<< djinn plugin MCP <<<", content)
            self.assertIn("[mcp_servers.p]", content)

    def test_content_between_markers_fully_removed(self):
        """Content between markers (stale entries) fully removed on rewire."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                "# >>> djinn plugin MCP <<<\n"
                "[mcp_servers.stale]\n"
                "command = 'old'\n"
                "# <<< djinn plugin MCP <<<\n"
            )

            wire_plugins.wire_codex_toml(config_path, {"new": {"command": "bash"}})

            content = config_path.read_text()
            self.assertNotIn("[mcp_servers.stale]", content)
            self.assertIn("[mcp_servers.new]", content)

    def test_remote_bearer_spec_renders_url_and_token_env_var(self):
        """A remote spec (no 'command') renders codex's native url +
        bearer_token_env_var shape — the field _render_named_env_ref_server
        fills in for a bearer_token_env_var-style agent, never a literal
        secret or a ${VAR} ref."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            plugins = {"obsidian-annotated": {
                "url": "https://mcp-obsidian.dmetr.io/mcp",
                "headers": {},
                "bearer_token_env_var": "OBSIDIAN_ANNOTATED_KEY",
            }}

            wire_plugins.wire_codex_toml(config_path, plugins)

            content = config_path.read_text()
            self.assertIn("[mcp_servers.obsidian-annotated]", content)
            self.assertIn('url = "https://mcp-obsidian.dmetr.io/mcp"', content)
            self.assertIn('bearer_token_env_var = "OBSIDIAN_ANNOTATED_KEY"', content)
            self.assertNotIn("headers", content)
            self.assertNotIn("command", content)

    def test_remote_spec_with_leftover_headers_raises(self):
        """codex has no header passthrough beyond the single bearer slot: a
        remote spec still carrying a header after bearer-extraction is
        unrenderable and must hard-fail, not silently drop the header."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            plugins = {"weird": {
                "url": "https://example.com/mcp",
                "headers": {"X-Extra": "value"},
                "bearer_token_env_var": "SLOT",
            }}

            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins.wire_codex_toml(config_path, plugins)
            self.assertIn("header(s)", str(cm.exception))


class TestRunIntegration(QuietTestCase):
    """Integration tests for the run function."""

    AGENTS_ALL = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if shutil.which("yq") is None:
            raise unittest.SkipTest("yq not available")
        agents_dir = Path(__file__).resolve().parents[1] / "agents"
        descriptors = []
        for f in sorted(agents_dir.glob("*/agent.yml")):
            doc = json.loads(subprocess.run(
                ["yq", "-o=json", str(f)],
                capture_output=True, text=True, check=True).stdout)
            mcp = doc.get("mcp")
            if not isinstance(mcp, dict):
                continue
            descriptors.append({
                "binary": doc["binary"],
                "config_path": mcp["config_path"],
                "format": mcp["format"],
                "dialect": mcp.get("dialect", ""),
                "env_refs": mcp["env_refs"],
                "strategy": mcp.get("strategy", ""),
            })
        if not descriptors:
            raise unittest.SkipTest("no mcp-capable agent descriptors found")
        cls.AGENTS_ALL = descriptors

    def test_full_payload_all_agents_wired(self):
        """Full payload: descriptors drive claude ref, literal trio, and codex ref/local."""
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
                "agents": list(self.AGENTS_ALL),
                "plugin_mcp_entries": [
                    {"myserena": {"command": "bash", "args": ["-c"]}},   # local
                    {"coding": {"url": "http://host.docker.internal:8811/mcp",  # env-remote
                                "headers": {"Authorization": "Bearer ${MCP_GATEWAY_TOKEN}"}}},
                ],
                "agent_servers": [
                    {"name": "obsidian-annotated",
                     "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                              "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}},
                     "requires": ["OBSIDIAN_ANNOTATED_KEY"],
                     "ref": ["claude", "codex"],
                     "literal": [{"agent": "cursor-agent",
                                  "key_envs": {"OBSIDIAN_ANNOTATED_KEY": "IDENTITY_KEY_0"}}],
                     "warn": [],
                     "local": []},
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
            # codex managed block carries the local plugin AND the agent-scoped
            # remote (bearer_token_env_var), but not the ordinary remote plugin
            codex_toml = (home / ".codex" / "config.toml").read_text()
            self.assertIn("[mcp_servers.myserena]", codex_toml)
            self.assertIn("[mcp_servers.obsidian-annotated]", codex_toml)
            self.assertIn('url = "https://mcp-obsidian.dmetr.io/mcp"', codex_toml)
            self.assertIn('bearer_token_env_var = "OBSIDIAN_ANNOTATED_KEY"', codex_toml)
            self.assertNotIn("coding", codex_toml)
            # pi config exists
            self.assertTrue((home / ".pi" / "agent" / "mcp.json").exists())
            # codex config exists
            self.assertTrue((home / ".codex" / "config.toml").exists())

    def test_empty_agents_no_agent_configs_created(self):
        """No payload agents means no agent config wiring runs at all."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()

            payload = {
                "agents": [],
                "plugin_mcp_entries": [],
                "agent_servers": [],
            }

            wire_plugins.run(payload, home, workspace, {})

            self.assertFalse((repos_dir / ".mcp.json").exists())
            # No agent home configs
            self.assertFalse((home / ".cursor").exists())
            self.assertFalse((home / ".pi").exists())
            self.assertFalse((home / ".codex").exists())

    def test_cursor_only_agents_do_not_touch_claude_workspace_artifacts(self):
        """Without a claude_preapprove agent, run() must not generate workspace
        .mcp.json, marker, repo links, or ~/.claude.json."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            repo = repos_dir / "app"
            repo.mkdir()
            (repo / ".git").mkdir()

            payload = {
                "agents": [{
                    "binary": "cursor-agent",
                    "config_path": ".cursor/mcp.json",
                    "format": "json",
                    "dialect": "url",
                    "env_refs": False,
                    "strategy": "",
                }],
                "plugin_mcp_entries": [],
                "agent_servers": [],
            }

            wire_plugins.run(payload, home, workspace, {})

            self.assertFalse((repos_dir / ".mcp.json").exists())
            self.assertFalse((workspace / ".mcp.generated").exists())
            self.assertFalse((repo / ".mcp.json").exists())
            self.assertFalse((home / ".claude.json").exists())
            self.assertTrue((home / ".cursor" / "mcp.json").exists())

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
                "agents": list(self.AGENTS_ALL),
                "plugin_mcp_entries": [],
                "agent_servers": [
                    {"name": "axiom", "spec": spec, "requires": ["AXIOM_TOKEN"],
                     "ref": ["claude"], "literal": [], "warn": [],
                     "local": ["cursor-agent", "codex"]},  # pi NOT bound
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
            # pi: NOT wired (no token → no server)
            pi = json.loads((home / ".pi" / "agent" / "mcp.json").read_text())["mcpServers"]
            self.assertNotIn("axiom", pi)
            # the token itself is never written anywhere — only the ${VAR} ref
            # that mcp-remote substitutes at connect time (never in argv either)
            for f in (workspace / "repos" / ".mcp.json", home / ".cursor" / "mcp.json",
                      home / ".codex" / "config.toml"):
                self.assertIn("${AXIOM_TOKEN}", f.read_text())

    def test_literal_remote_substitutes_multiple_slots(self):
        """A literal-dialect remote server requiring two slots substitutes both
        key envs when writing the agent config."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "repos").mkdir()

            payload = {
                "agents": [{
                    "binary": "cursor-agent",
                    "config_path": ".cursor/mcp.json",
                    "format": "json",
                    "dialect": "url",
                    "env_refs": False,
                    "strategy": "",
                }],
                "plugin_mcp_entries": [],
                "agent_servers": [{
                    "name": "two-slot",
                    "spec": {
                        "url": "https://example.test/mcp",
                        "headers": {
                            "Authorization": "Bearer ${TOKEN_A}",
                            "X-Trace": "${TOKEN_B}:${TOKEN_A}",
                        },
                    },
                    "requires": ["TOKEN_A", "TOKEN_B"],
                    "ref": [],
                    "literal": [{
                        "agent": "cursor-agent",
                        "key_envs": {
                            "TOKEN_A": "IDENTITY_KEY_0",
                            "TOKEN_B": "IDENTITY_KEY_1",
                        },
                    }],
                    "warn": [],
                    "local": [],
                }],
            }
            env = {"IDENTITY_KEY_0": "alpha", "IDENTITY_KEY_1": "beta"}
            wire_plugins.run(payload, home, workspace, env)

            cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
            entry = cursor["mcpServers"]["two-slot"]
            self.assertEqual(entry["headers"]["Authorization"], "Bearer alpha")
            self.assertEqual(entry["headers"]["X-Trace"], "beta:alpha")

    def test_disabled_remote_server_is_removed_from_literal_agent(self):
        """A remote literal-key server (obsidian) wired for cursor on one run must
        be REMOVED — entry and inline credential — when a later run drops it
        (disabled: true / plugin removal), while hand-added and local-plugin
        entries survive. Guards the upsert-only stale-config hole."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            (workspace / "repos").mkdir(parents=True)
            # Pre-seed cursor's config with a hand-added server that must survive.
            (home / ".cursor").mkdir()
            (home / ".cursor" / "mcp.json").write_text(
                json.dumps({"mcpServers": {"hand-added": {"command": "keep-me"}}}))

            spec = {"url": "https://mcp-obsidian.dmetr.io/mcp",
                    "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}
            cursor_only = [{
                "binary": "cursor-agent", "config_path": ".cursor/mcp.json",
                "format": "json", "dialect": "url", "env_refs": False, "strategy": "",
            }]
            enabled = {
                "agents": cursor_only,
                "plugin_mcp_entries": [{"serena": {"command": "bash", "args": ["-lc", "s"]}}],
                "agent_servers": [
                    {"name": "obsidian-annotated", "spec": spec,
                     "requires": ["OBSIDIAN_ANNOTATED_KEY"], "ref": [],
                     "warn": [], "local": [],
                     "literal": [{"agent": "cursor-agent",
                                  "key_envs": {"OBSIDIAN_ANNOTATED_KEY": "IDENTITY_KEY_0"}}]},
                ],
            }
            wire_plugins.run(enabled, home, workspace, {"IDENTITY_KEY_0": "sekret-token"})

            cfg = home / ".cursor" / "mcp.json"
            cursor = json.loads(cfg.read_text())["mcpServers"]
            self.assertEqual(cursor["obsidian-annotated"]["headers"]["Authorization"],
                             "Bearer sekret-token")   # literal key substituted in
            self.assertIn("serena", cursor)            # local plugin wired
            self.assertIn("hand-added", cursor)        # hand-added preserved

            # Rerun with the server disabled for cursor (no longer in agent_servers).
            disabled = {"agents": cursor_only, "plugin_mcp_entries": [
                {"serena": {"command": "bash", "args": ["-lc", "s"]}}], "agent_servers": []}
            wire_plugins.run(disabled, home, workspace, {"IDENTITY_KEY_0": "sekret-token"})

            cursor = json.loads(cfg.read_text())["mcpServers"]
            self.assertNotIn("obsidian-annotated", cursor)   # stale server pruned
            self.assertNotIn("sekret-token", cfg.read_text())  # inline credential gone
            self.assertIn("serena", cursor)            # local plugin still wired
            self.assertIn("hand-added", cursor)        # hand-added still preserved

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
                "# >>> djinn plugin MCP >>>\n"
                "[mcp_servers.old]\n"
                "# <<< djinn plugin MCP <<<\n"
                "# >>> djinn plugin MCP >>>\n"
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
                "# <<< djinn plugin MCP <<<\n"
                "# >>> djinn plugin MCP >>>\n"
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


class TestWireCodexSettingsBlock(QuietTestCase):
    """config_settings render as a SECOND managed block at the head of the
    file — bare TOML keys are only top-level when nothing has opened a table
    above them, which is the mirror image of why the MCP block sits at the
    tail."""

    PLUGINS = {"serena": {"command": "bash", "args": ["-lc", "x"]}}
    SETTINGS = {"sandbox_mode": "danger-full-access"}

    def test_settings_precede_every_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            content = config_path.read_text()
            self.assertLess(content.index("sandbox_mode"), content.index("[mcp_servers."))
            self.assertIn('sandbox_mode = "danger-full-access"', content)
            self.assertIn("# >>> djinn codex settings", content)
            self.assertIn("# <<< djinn codex settings", content)
            self.assertEqual(os.stat(config_path).st_mode & 0o777, 0o600)

    def test_hand_added_content_survives_between_the_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('model = "gpt-5.6-terra"\n[projects."/workspace"]\ntrust_level = "trusted"\n')
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            content = config_path.read_text()
            self.assertIn('model = "gpt-5.6-terra"', content)
            self.assertIn('[projects."/workspace"]', content)
            self.assertLess(content.index("# <<< djinn codex settings"), content.index("model ="))
            self.assertLess(content.index('trust_level'), content.index("# >>> djinn plugin MCP"))

    def test_rerun_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text("keep = 1\n")
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            first = config_path.read_text()
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            self.assertEqual(config_path.read_text(), first)
            self.assertEqual(first.count("sandbox_mode"), 1)

    def test_dropping_settings_strips_the_stale_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, {})
            content = config_path.read_text()
            self.assertNotIn("sandbox_mode", content)
            self.assertNotIn("djinn codex settings", content)
            self.assertIn("[mcp_servers.serena]", content)

    def test_no_settings_renders_no_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS)
            self.assertNotIn("djinn codex settings", config_path.read_text())

    def test_unclosed_settings_block_raises_and_leaves_file_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            original = "# >>> djinn codex settings >>>\nuser_config = 1\n"
            config_path.write_text(original)
            with self.assertRaises(wire_plugins.WireError) as cm:
                wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            self.assertIn("repair the markers", str(cm.exception))
            self.assertEqual(config_path.read_text(), original)

    def test_scalar_types_render_as_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            wire_plugins.wire_codex_toml(
                config_path, {}, {"b": True, "off": False, "n": 7, "s": 'a"b'})
            content = config_path.read_text()
            # sorted keys, TOML literals (not Python repr)
            self.assertIn("b = true\nn = 7\noff = false\ns = \"a\\\"b\"\n", content)

    def test_hand_written_managed_key_is_dropped_not_duplicated(self):
        """~/.codex is a persisted volume, so the hand edit this mechanism
        replaces outlives the rebuild that starts managing the key. Two
        top-level sandbox_mode lines would make codex reject its own config."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                'sandbox_mode = "workspace-write"\nmodel = "gpt-5.6-terra"\n')
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            content = config_path.read_text()
            self.assertEqual(content.count("sandbox_mode"), 1)
            self.assertNotIn("workspace-write", content)
            self.assertIn('model = "gpt-5.6-terra"', content)

    def test_same_key_inside_a_table_is_left_alone(self):
        """A bare key under a [table] is a different key — only the top-level
        region shadows the managed block."""
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text(
                '[profiles.safe]\nsandbox_mode = "workspace-write"\n')
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            content = config_path.read_text()
            self.assertIn('[profiles.safe]', content)
            self.assertIn('workspace-write', content)
            self.assertEqual(content.count("sandbox_mode"), 2)

    def test_unmanaged_keys_are_never_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('sandbox_mode_extra = 1\nmodel = "x"\n')
            wire_plugins.wire_codex_toml(config_path, self.PLUGINS, self.SETTINGS)
            content = config_path.read_text()
            self.assertIn("sandbox_mode_extra = 1", content)
            self.assertIn('model = "x"', content)

    def test_log_names_only_the_blocks_actually_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with contextlib.redirect_stdout(io.StringIO()) as out:
                wire_plugins.wire_codex_toml(config_path, {}, self.SETTINGS)
            self.assertIn("1 agent setting(s)", out.getvalue())
            self.assertNotIn("plugin MCP server", out.getvalue())
            with contextlib.redirect_stdout(io.StringIO()) as out:
                wire_plugins.wire_codex_toml(config_path, self.PLUGINS, {})
            self.assertIn("1 plugin MCP server(s)", out.getvalue())
            self.assertNotIn("agent setting", out.getvalue())

    def test_payload_settings_require_the_codex_strategy(self):
        entry = {"binary": "kimi", "config_path": ".kimi/mcp.json", "format": "json",
                 "dialect": "mcpServers", "env_refs": True, "strategy": "",
                 "settings": {"sandbox_mode": "danger-full-access"}}
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins._normalize_agent_mcp_entry(entry, "agents[0]")
        self.assertIn("only rendered by strategy codex_managed_block", str(cm.exception))

    def test_payload_settings_reject_non_bare_key(self):
        entry = {"binary": "codex", "config_path": ".codex/config.toml", "format": "toml",
                 "dialect": "", "env_refs": False, "strategy": "codex_managed_block",
                 "settings": {"not a key": 1}}
        with self.assertRaises(wire_plugins.WireError) as cm:
            wire_plugins._normalize_agent_mcp_entry(entry, "agents[0]")
        self.assertIn("bare TOML key", str(cm.exception))

    def test_payload_settings_default_to_empty_when_absent(self):
        entry = {"binary": "codex", "config_path": ".codex/config.toml", "format": "toml",
                 "dialect": "", "env_refs": False, "strategy": "codex_managed_block"}
        self.assertEqual(
            wire_plugins._normalize_agent_mcp_entry(entry, "agents[0]")["settings"], {})


class TestBuildPayload(unittest.TestCase):
    """Host-side payload assembly from descriptor + server env inputs."""

    AGENTS = [
        {"binary": "claude", "config_path": ".mcp.json", "format": "json",
         "dialect": "mcpServers", "env_refs": True, "strategy": "claude_preapprove"},
        {"binary": "cursor-agent", "config_path": ".cursor/mcp.json", "format": "json",
         "dialect": "url", "env_refs": False, "strategy": ""},
    ]
    KIMI_REF_AGENT = [{
        "binary": "kimi",
        "config_path": ".kimi-code/mcp.json",
        "format": "json",
        "dialect": "mcpServers",
        "env_refs": "bearerTokenEnvVar",
        "strategy": "",
    }]

    def test_missing_env_means_everything_off_and_empty(self):
        payload = wire_plugins.build_payload({})
        self.assertEqual(payload["agents"], [])
        self.assertEqual(payload["plugin_mcp_entries"], [])
        self.assertEqual(payload["agent_servers"], [])

    def test_plugin_entries_parsed_per_line_blank_lines_ignored(self):
        env = {
            "AGENTS_MCP_JSON": json.dumps(self.AGENTS, separators=(",", ":")),
            "PLUGIN_MCP_ENTRIES": '{"serena": {"command": "bash"}}\n\n{"other": {"command": "x"}}\n',
        }
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

    def test_invalid_agents_json_raises(self):
        with self.assertRaises(wire_plugins.WireError):
            wire_plugins.build_payload({"AGENTS_MCP_JSON": "{not json"})

    def test_unknown_agent_strategy_raises(self):
        with self.assertRaises(wire_plugins.WireError):
            wire_plugins.build_payload({
                "AGENTS_MCP_JSON": json.dumps([{
                    "binary": "x", "config_path": ".x/mcp.json", "format": "json",
                    "dialect": "url", "env_refs": False, "strategy": "unknown",
                }])
            })

    def test_named_env_refs_remote_requires_single_required_slot(self):
        env = {
            "AGENTS_MCP_JSON": json.dumps(self.KIMI_REF_AGENT, separators=(",", ":")),
            "AGENT_SERVERS_JSON": json.dumps({
                "obsidian-annotated": {
                    "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                             "headers": {"Authorization": "Bearer ${TOKEN_A}"}},
                    "requires": ["TOKEN_A", "TOKEN_B"],
                }
            }),
            "AGENT_SECRETS": (
                "kimi\tTOKEN_A\tSRC\n"
                "kimi\tTOKEN_B\tSRC\n"
            ),
        }
        # SKIP, don't raise: an unsupported pairing costs kimi this one
        # server, never the whole container's wiring (see build_payload).
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            payload = wire_plugins.build_payload(env)
        self.assertEqual(payload["agent_servers"], [],
                         "no agent could take it, so the server drops out")
        out = buf.getvalue()
        self.assertIn("agent server 'obsidian-annotated' cannot be rendered for agent 'kimi'", out)
        self.assertIn("exactly one required slot", out)

    def test_named_env_refs_remote_requires_bearer_header_reference(self):
        env = {
            "AGENTS_MCP_JSON": json.dumps(self.KIMI_REF_AGENT, separators=(",", ":")),
            "AGENT_SERVERS_JSON": json.dumps({
                "obsidian-annotated": {
                    "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                             "headers": {"Authorization": "Bearer STATIC_TOKEN"}},
                    "requires": ["OBSIDIAN_ANNOTATED_KEY"],
                }
            }),
            "AGENT_SECRETS": "kimi\tOBSIDIAN_ANNOTATED_KEY\tSRC\n",
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            payload = wire_plugins.build_payload(env)
        self.assertEqual(payload["agent_servers"], [])
        out = buf.getvalue()
        self.assertIn("agent server 'obsidian-annotated' cannot be rendered for agent 'kimi'", out)
        self.assertIn("requires headers.Authorization", out)
        self.assertIn("${OBSIDIAN_ANNOTATED_KEY}", out)

    def test_round_trips_through_run(self):
        """The payload build_payload emits is exactly what run() consumes: a
        required remote server reaches claude (ref) and cursor (literal); a local
        plugin reaches cursor too."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            workspace = Path(tmp) / "workspace"
            repos = workspace / "repos"
            repo = repos / "app"
            (repo / ".git").mkdir(parents=True)
            env = {"AGENTS_MCP_JSON": json.dumps(self.AGENTS, separators=(",", ":")),
                   "PLUGIN_MCP_ENTRIES": '{"serena": {"command": "bash"}}\n',
                   "AGENT_SERVERS_JSON": json.dumps({"obsidian-annotated": {
                       "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                                "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}},
                       "requires": ["OBSIDIAN_ANNOTATED_KEY"]}}),
                   "AGENT_SECRETS": "claude\tOBSIDIAN_ANNOTATED_KEY\tSRC\n"
                                    "cursor-agent\tOBSIDIAN_ANNOTATED_KEY\tSRC\n",
                   "IDENTITY_SECRETS": "cursor-agent:K0:OBSIDIAN_ANNOTATED_KEY",
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


class TestDescriptorDrivenRoles(unittest.TestCase):
    """Phase-2 seam pins: AGENTS_MCP_JSON descriptors now define role routing."""

    def test_synthetic_descriptors_drive_role_and_order(self):
        agents = [
            {"binary": "alpha-ref", "config_path": ".alpha/mcp.json", "format": "json",
             "dialect": "mcpServers", "env_refs": "shim", "strategy": ""},
            {"binary": "beta-managed", "config_path": ".beta/config.toml", "format": "toml",
             "dialect": "", "env_refs": False, "strategy": "codex_managed_block"},
            {"binary": "gamma-literal", "config_path": ".gamma/mcp.json", "format": "json",
             "dialect": "url", "env_refs": False, "strategy": ""},
        ]
        env = {
            "AGENTS_MCP_JSON": json.dumps(agents, separators=(",", ":")),
            "AGENT_SERVERS_JSON": json.dumps({
                "remote": {"spec": {"url": "https://example.test/mcp",
                                       "headers": {"Authorization": "Bearer ${TOKEN}"}},
                           "requires": ["TOKEN"]},
                "local": {"spec": {"command": "bridge", "args": ["--stdio"]},
                          "requires": ["TOKEN"]},
            }),
            "AGENT_SECRETS": (
                "alpha-ref\tTOKEN\tSRC\n"
                "beta-managed\tTOKEN\tSRC\n"
                "gamma-literal\tTOKEN\tSRC\n"),
            "IDENTITY_SECRETS": "gamma-literal:K0:TOKEN",
        }
        payload = wire_plugins.build_payload(env)
        self.assertEqual([a["binary"] for a in payload["agents"]],
                         ["alpha-ref", "beta-managed", "gamma-literal"])
        servers = {entry["name"]: entry for entry in payload["agent_servers"]}
        self.assertEqual(servers["remote"]["ref"], ["alpha-ref"])
        self.assertEqual(servers["remote"]["warn"], ["beta-managed"])
        self.assertEqual(
            servers["remote"]["literal"],
            [{"agent": "gamma-literal", "key_envs": {"TOKEN": "K0"}}],
        )
        self.assertEqual(servers["local"]["ref"], ["alpha-ref"])
        self.assertEqual(servers["local"]["local"], ["beta-managed", "gamma-literal"])
        self.assertEqual(servers["local"]["warn"], [])
        self.assertEqual(servers["local"]["literal"], [])

    def test_real_descriptor_round_trip_preserves_current_roles(self):
        import manifest
        agents_dir = Path(__file__).resolve().parents[1] / "agents"
        agent_docs = {}
        for f in sorted(agents_dir.glob("*/agent.yml")):
            doc = json.loads(subprocess.run(
                ["yq", "-o=json", str(f)],
                capture_output=True, text=True, check=True).stdout)
            agent_docs[f.parent.name] = doc
        derived = manifest.derive(
            {"agents": ["claude", "codex", "cursor", "pi"]},
            {}, agent_docs, {"PRESENT_SECRET_VARS": "", "SECRETS_FILE": "/sec/secrets.env"},
        )
        env = {
            "AGENTS_MCP_JSON": derived["AGENTS_MCP_JSON"],
            "AGENT_SERVERS_JSON": json.dumps({
                "remote": {"spec": {"url": "https://example.test/mcp",
                                       "headers": {"Authorization": "Bearer ${TOKEN}"}},
                           "requires": ["TOKEN"]},
                "local": {"spec": {"command": "bridge", "args": ["--stdio"]},
                          "requires": ["TOKEN"]},
            }),
            "AGENT_SECRETS": (
                "claude\tTOKEN\tSRC\n"
                "codex\tTOKEN\tSRC\n"
                "cursor-agent\tTOKEN\tSRC\n"
                "pi\tTOKEN\tSRC\n"),
            "IDENTITY_SECRETS": (
                "cursor-agent:K0:TOKEN "
                "pi:K1:TOKEN"),
        }
        payload = wire_plugins.build_payload(env)
        servers = {entry["name"]: entry for entry in payload["agent_servers"]}
        self.assertEqual(servers["remote"]["ref"], ["claude", "codex"])
        self.assertEqual(servers["remote"]["warn"], [])
        self.assertEqual(
            [lit["agent"] for lit in servers["remote"]["literal"]],
            ["cursor-agent", "pi"],
        )
        # codex's env_refs is now a named string (bearer_token_env_var), so a
        # bound LOCAL agent-scoped server routes through the same 'ref' bucket
        # too (rendered verbatim — see run()'s ref handling) rather than 'local'.
        self.assertEqual(servers["local"]["ref"], ["claude", "codex"])
        self.assertEqual(servers["local"]["local"], ["cursor-agent", "pi"])
        self.assertEqual(servers["local"]["warn"], [])
        self.assertEqual(servers["local"]["literal"], [])

    def test_future_ref_agent_mcpservers_no_strategy(self):
        agents = [{
            "binary": "kimi",
            "config_path": ".kimi/mcp.json",
            "format": "json",
            "dialect": "mcpServers",
            "env_refs": "bearerTokenEnvVar",
            "strategy": "",
        }]
        payload = wire_plugins.build_payload({
            "AGENTS_MCP_JSON": json.dumps(agents, separators=(",", ":")),
            "AGENT_SERVERS_JSON": json.dumps({
                "obsidian-annotated": {
                    "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                             "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}},
                    "requires": ["OBSIDIAN_ANNOTATED_KEY"],
                }
            }),
            "AGENT_SECRETS": "kimi\tOBSIDIAN_ANNOTATED_KEY\tSRC\n",
        })
        self.assertEqual(payload["agent_servers"][0]["ref"], ["kimi"])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = Path(tmp) / "workspace"
            home.mkdir()
            workspace.mkdir()
            wire_plugins.run(payload, home, workspace, {})
            kimi = json.loads((home / ".kimi" / "mcp.json").read_text())["mcpServers"]
            self.assertEqual(kimi["obsidian-annotated"]["url"], "https://mcp-obsidian.dmetr.io/mcp")
            self.assertEqual(kimi["obsidian-annotated"]["headers"], {})
            self.assertEqual(
                kimi["obsidian-annotated"]["bearerTokenEnvVar"],
                "OBSIDIAN_ANNOTATED_KEY",
            )


class TestManifestWireParity(unittest.TestCase):
    """Keep manifest.py acceptance and wire_plugins.py runtime support aligned."""

    def _agent_descriptor_for_combo(self, fmt, dialect, env_refs, strategy):
        mcp = {
            "config_path": ".mcp.json" if strategy == "claude_preapprove" else ".combo/mcp.json",
            "format": fmt,
            "env_refs": env_refs,
        }
        if dialect:
            mcp["dialect"] = dialect
        if strategy:
            mcp["strategy"] = strategy
        return {"binary": "combo-agent", "install": "x", "mcp": mcp}

    def _manifest_accepts_combo(self, fmt, dialect, env_refs, strategy):
        agent_files = {"combo": self._agent_descriptor_for_combo(fmt, dialect, env_refs, strategy)}
        try:
            manifest.derive(
                {}, {}, agent_files,
                {"PRESENT_SECRET_VARS": "", "SECRETS_FILE": "/sec/secrets.env"},
            )
        except manifest.ManifestError:
            return False
        return True

    def _round_trip_combo(self, fmt, dialect, env_refs, strategy):
        agent = self._agent_descriptor_for_combo(fmt, dialect, env_refs, strategy)["mcp"]
        env = {
            "AGENTS_MCP_JSON": json.dumps([{
                "binary": "combo-agent",
                "config_path": agent["config_path"],
                "format": agent["format"],
                "dialect": agent.get("dialect", ""),
                "env_refs": agent["env_refs"],
                "strategy": agent.get("strategy", ""),
            }], separators=(",", ":")),
            "PLUGIN_MCP_ENTRIES": json.dumps({"uniform-local": {"command": "uniform-local"}}) + "\n",
            "AGENT_SERVERS_JSON": json.dumps({
                "remote-required": {
                    "spec": {
                        "url": "https://example.test/mcp",
                        "headers": {"Authorization": "Bearer ${TOKEN}"},
                    },
                    "requires": ["TOKEN"],
                },
                "local-required": {
                    "spec": {"command": "bridge", "args": ["--stdio", "${TOKEN}"]},
                    "requires": ["TOKEN"],
                },
            }, separators=(",", ":")),
            "AGENT_SECRETS": "combo-agent\tTOKEN\tTOKEN_SOURCE\n",
            "IDENTITY_SECRETS": "combo-agent:IDENTITY_KEY_0:TOKEN",
            "IDENTITY_KEY_0": "literal-key",
        }
        payload = wire_plugins.build_payload(env)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            workspace = Path(tmp) / "workspace"
            home.mkdir()
            (workspace / "repos" / "repo" / ".git").mkdir(parents=True)
            wire_plugins.run(payload, home, workspace, env)

    def test_mcp_format_dialect_strategy_sets_match_manifest_module(self):
        self.assertEqual(wire_plugins.AGENT_MCP_FORMATS, manifest.AGENT_MCP_FORMATS)
        self.assertEqual(wire_plugins.AGENT_MCP_DIALECTS, manifest.AGENT_MCP_DIALECTS)
        self.assertEqual(wire_plugins.AGENT_MCP_STRATEGIES, manifest.AGENT_MCP_STRATEGIES)
        self.assertEqual(wire_plugins.CODEX_MANAGED_BLOCK_ENV_REFS,
                          manifest.CODEX_MANAGED_BLOCK_ENV_REFS)

    def test_every_manifest_legal_combo_round_trips_build_and_run(self):
        env_ref_variants = [True, False, "ENV_REF"]
        strategy_variants = ["", *sorted(wire_plugins.AGENT_MCP_STRATEGIES)]
        accepted = []
        for fmt in sorted(wire_plugins.AGENT_MCP_FORMATS):
            for dialect in sorted(wire_plugins.AGENT_MCP_DIALECTS):
                for env_refs in env_ref_variants:
                    for strategy in strategy_variants:
                        if self._manifest_accepts_combo(fmt, dialect, env_refs, strategy):
                            accepted.append((fmt, dialect, env_refs, strategy))

        self.assertTrue(accepted, "expected at least one manifest-legal MCP combo")
        for fmt, dialect, env_refs, strategy in accepted:
            with self.subTest(fmt=fmt, dialect=dialect, env_refs=env_refs, strategy=strategy):
                self._round_trip_combo(fmt, dialect, env_refs, strategy)


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
