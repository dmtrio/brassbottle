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


class TestShimRemoteSpec(unittest.TestCase):
    """The compatibility shim: a remote spec rendered as an mcp-remote command.

    Replaced _literal_agent_config, which rendered the same remote server for a
    non-ref agent by substituting the RESOLVED KEY into the header and shaping
    it per dialect. The distinguishing property of every test here is that the
    ${SLOT} reference survives — no key is resolved.
    """

    SPEC = {"url": "https://mcp-obsidian.dmetr.io/mcp",
            "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}

    def test_url_leads_and_each_header_becomes_a_header_flag(self):
        self.assertEqual(wire_plugins._shim_remote_spec(self.SPEC), {
            "command": "mcp-remote",
            "args": ["https://mcp-obsidian.dmetr.io/mcp", "--header",
                     "Authorization: Bearer ${OBSIDIAN_ANNOTATED_KEY}"],
        })

    def test_ref_survives_untouched(self):
        rendered = json.dumps(wire_plugins._shim_remote_spec(self.SPEC))
        self.assertIn("${OBSIDIAN_ANNOTATED_KEY}", rendered)

    def test_expresses_any_header_not_only_bearer(self):
        """The reason this beats the native rendering it falls back from: a
        named env_refs field is bearer-only, the shim is not."""
        rendered = wire_plugins._shim_remote_spec(
            {"url": "http://h.test/mcp", "headers": {"X-API-Key": "${K}"}})
        self.assertEqual(rendered["args"], ["http://h.test/mcp", "--header", "X-API-Key: ${K}"])

    def test_multiple_headers_keep_spec_order(self):
        rendered = wire_plugins._shim_remote_spec({
            "url": "http://h.test/mcp",
            "headers": {"Authorization": "Bearer ${A}", "X-Trace": "${B}"}})
        self.assertEqual(rendered["args"], [
            "http://h.test/mcp",
            "--header", "Authorization: Bearer ${A}",
            "--header", "X-Trace: ${B}"])

    def test_no_headers_is_just_the_url(self):
        self.assertEqual(
            wire_plugins._shim_remote_spec({"url": "http://h.test/mcp"}),
            {"command": "mcp-remote", "args": ["http://h.test/mcp"]})


class TestRenderForAgent(unittest.TestCase):
    """The per-agent transport decision."""

    REMOTE = {"url": "https://e.test/mcp", "headers": {"Authorization": "Bearer ${TOK}"}}
    XAPIKEY = {"url": "https://e.test/mcp", "headers": {"X-API-Key": "${TOK}"}}
    LOCAL = {"command": "bridge", "args": ["--stdio"]}

    def _agent(self, binary, env_refs):
        return {"binary": binary, "config_path": f".{binary}/mcp.json", "format": "json",
                "dialect": "mcpServers", "env_refs": env_refs, "strategy": "", "settings": {}}

    def test_bool_ref_agent_gets_native_remote(self):
        out = wire_plugins._render_for_agent(
            self.REMOTE, ["TOK"], self._agent("claude", True), "s")
        self.assertEqual(out["type"], "http")
        self.assertEqual(out["headers"]["Authorization"], "Bearer ${TOK}")

    def test_named_ref_agent_gets_its_own_native_field(self):
        out = wire_plugins._render_for_agent(
            self.REMOTE, ["TOK"], self._agent("kimi", "bearerTokenEnvVar"), "s")
        self.assertEqual(out["bearerTokenEnvVar"], "TOK")
        self.assertEqual(out["headers"], {})

    def test_non_ref_agent_gets_the_shim(self):
        out = wire_plugins._render_for_agent(
            self.REMOTE, ["TOK"], self._agent("cursor-agent", False), "s")
        self.assertEqual(out["command"], "mcp-remote")

    def test_named_ref_agent_falls_back_to_shim_when_not_bearer(self):
        """The incident, fixed at the root.

        browser's X-API-Key against kimi's bearerTokenEnvVar first aborted the
        whole payload build, and was then narrowed to dropping the server for
        kimi. kimi now simply gets it, over the shim.
        """
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            out = wire_plugins._render_for_agent(
                self.XAPIKEY, ["TOK"], self._agent("kimi", "bearerTokenEnvVar"), "browser")
        self.assertEqual(out["command"], "mcp-remote")
        self.assertIn("X-API-Key: ${TOK}", out["args"])
        self.assertIn("kimi", buf.getvalue())

    def test_multi_slot_remote_falls_back_to_shim(self):
        out = wire_plugins._render_for_agent(
            {"url": "https://e.test/mcp",
             "headers": {"Authorization": "Bearer ${A}", "X-Trace": "${B}"}},
            ["A", "B"], self._agent("kimi", "bearerTokenEnvVar"), "s")
        self.assertEqual(out["command"], "mcp-remote")

    def test_local_spec_passes_through_for_every_role(self):
        for env_refs in (True, False, "bearerTokenEnvVar"):
            with self.subTest(env_refs=env_refs):
                self.assertEqual(
                    wire_plugins._render_for_agent(
                        self.LOCAL, ["TOK"], self._agent("x", env_refs), "s"),
                    self.LOCAL)


class TestWriteAgentServer(QuietTestCase):
    """write_agent_server merges one rendered entry into an agent's config."""

    ENTRY = {"command": "mcp-remote",
             "args": ["https://mcp-obsidian.dmetr.io/mcp", "--header",
                      "Authorization: Bearer ${OBSIDIAN_ANNOTATED_KEY}"]}

    def test_missing_file_creates_config_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wire_plugins.write_agent_server(
                    "cursor-agent", ".cursor/mcp.json", "obsidian-annotated",
                    self.ENTRY, home)
            mcp_path = home / ".cursor" / "mcp.json"
            self.assertEqual(os.stat(mcp_path).st_mode & 0o777, 0o600)
            data = json.loads(mcp_path.read_text())
            self.assertEqual(data["mcpServers"]["obsidian-annotated"], self.ENTRY)
            self.assertIn("${OBSIDIAN_ANNOTATED_KEY}", mcp_path.read_text())
            self.assertIn("cursor-agent MCP config for obsidian-annotated", output.getvalue())

    def test_existing_file_preserves_plugins(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mcp_path = home / ".cursor" / "mcp.json"
            mcp_path.parent.mkdir(parents=True)
            mcp_path.write_text(json.dumps({"mcpServers": {"myserena": {"command": "bash"}}}))
            wire_plugins.write_agent_server(
                "cursor-agent", ".cursor/mcp.json", "obsidian-annotated", self.ENTRY, home)
            data = json.loads(mcp_path.read_text())
            self.assertEqual(set(data["mcpServers"]), {"myserena", "obsidian-annotated"})

    def test_zero_byte_existing_file_takes_create_path(self):
        """Zero-byte existing file takes create path (pins empty-input jq bug)."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            mcp_path = home / ".cursor" / "mcp.json"
            mcp_path.parent.mkdir(parents=True)
            mcp_path.write_text("")  # Zero bytes
            wire_plugins.write_agent_server(
                "cursor-agent", ".cursor/mcp.json", "obsidian-annotated", self.ENTRY, home)
            data = json.loads(mcp_path.read_text())
            self.assertEqual(list(data["mcpServers"].keys()), ["obsidian-annotated"])


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
        """Full payload: claude and codex natively, cursor over the shim."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            repos_dir = workspace / "repos"
            repos_dir.mkdir()
            repo = repos_dir / "app"
            repo.mkdir()
            (repo / ".git").mkdir()

            env = {}
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
                     "local": ["cursor-agent"]},
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
            # cursor: obsidian over the shim + LOCAL plugin only. The uniform
            # REMOTE plugin (coding, no requires:) still reaches Claude alone.
            cursor_mcp = home / ".cursor" / "mcp.json"
            self.assertTrue(cursor_mcp.exists())
            raw_cursor = cursor_mcp.read_text()
            cursor_data = json.loads(raw_cursor)
            self.assertIn("myserena", cursor_data["mcpServers"])
            self.assertNotIn("coding", cursor_data["mcpServers"])
            self.assertEqual(
                cursor_data["mcpServers"]["obsidian-annotated"]["command"], "mcp-remote")
            self.assertIn("${OBSIDIAN_ANNOTATED_KEY}", raw_cursor)
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

    def test_remote_to_local_upgrade_removes_the_literal_key_on_disk(self):
        """A container upgrading across the obsidian-annotated bridge change.

        The previous run wired the server as REMOTE, so a literal-key agent has
        the raw key in its config and a .djinn-servers sidecar naming the server.
        This run ships the same server as LOCAL (command: mcp-remote), which is a
        different wiring path: reconcile_agent_servers must delete the stale
        remote entry BEFORE wire_plugin_servers_json writes the local one, or the
        cleanup pass eats the entry it was supposed to replace and the agent ends
        up with no server at all.

        The key leaving the file is the whole point of the migration, so it is
        asserted on the raw text, not just the parsed entry.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            (workspace / "repos").mkdir(parents=True)
            cursor_mcp = home / ".cursor" / "mcp.json"
            cursor_mcp.parent.mkdir(parents=True)
            # Exactly what the old remote wiring left behind, plus a hand-added
            # server that must survive untouched.
            cursor_mcp.write_text(json.dumps({"mcpServers": {
                "obsidian-annotated": {
                    "url": "https://mcp-obsidian.dmetr.io/mcp",
                    "headers": {"Authorization": "Bearer SECRETKEY"}},
                "hand-added": {"command": "mine", "args": []},
            }}))
            (cursor_mcp.parent / "mcp.json.djinn-servers").write_text('["obsidian-annotated"]\n')

            agent = {"binary": "cursor-agent", "config_path": ".cursor/mcp.json",
                     "format": "json", "dialect": "url", "env_refs": False,
                     "strategy": ""}
            spec = {"command": "mcp-remote",
                    "args": ["https://mcp-obsidian.dmetr.io/mcp", "--header",
                             "Authorization: Bearer ${OBSIDIAN_ANNOTATED_KEY}"]}
            payload = {
                "agents": [agent],
                "plugin_mcp_entries": [],
                "agent_servers": [
                    {"name": "obsidian-annotated", "spec": spec,
                     "requires": ["OBSIDIAN_ANNOTATED_KEY"],
                     "ref": [], "literal": [], "warn": [],
                     "local": ["cursor-agent"]},
                ],
            }

            wire_plugins.run(payload, home, workspace, {})

            raw = cursor_mcp.read_text()
            self.assertNotIn("SECRETKEY", raw)
            servers = json.loads(raw)["mcpServers"]
            self.assertEqual(servers["obsidian-annotated"], spec)
            self.assertEqual(servers["hand-added"], {"command": "mine", "args": []})
            # The server moved sidecars: no longer agent-server-managed, now
            # plugin-managed. A leftover name in the old sidecar would delete it
            # again on the next run.
            self.assertEqual(
                json.loads((cursor_mcp.parent / "mcp.json.djinn-servers").read_text()), [])
            self.assertIn(
                "obsidian-annotated",
                json.loads((cursor_mcp.parent / "mcp.json.djinn-plugins").read_text()))

    def test_remote_to_local_upgrade_is_idempotent(self):
        """Running the post-migration payload twice is a no-op the second time —
        the stale-delete only fires while the old sidecar still names the server."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            (workspace / "repos").mkdir(parents=True)
            spec = {"command": "mcp-remote",
                    "args": ["https://mcp-obsidian.dmetr.io/mcp", "--header",
                             "Authorization: Bearer ${OBSIDIAN_ANNOTATED_KEY}"]}
            payload = {
                "agents": [{"binary": "cursor-agent", "config_path": ".cursor/mcp.json",
                            "format": "json", "dialect": "url", "env_refs": False,
                            "strategy": ""}],
                "plugin_mcp_entries": [],
                "agent_servers": [
                    {"name": "obsidian-annotated", "spec": spec,
                     "requires": ["OBSIDIAN_ANNOTATED_KEY"],
                     "ref": [], "literal": [], "warn": [],
                     "local": ["cursor-agent"]},
                ],
            }
            cursor_mcp = home / ".cursor" / "mcp.json"

            wire_plugins.run(payload, home, workspace, {})
            first = cursor_mcp.read_text()
            wire_plugins.run(payload, home, workspace, {})
            self.assertEqual(cursor_mcp.read_text(), first)
            self.assertEqual(
                json.loads(cursor_mcp.read_text())["mcpServers"]["obsidian-annotated"], spec)

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

    def test_multi_slot_remote_shims_every_header_ref_intact(self):
        """A two-slot remote for a non-ref agent: every header becomes a
        --header flag with its ${SLOT} ref untouched. The predecessor of this
        test asserted the opposite — that both keys were substituted INTO the
        config file — which is the behaviour the shim exists to remove."""
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
                    "local": ["cursor-agent"],
                }],
            }
            # No key envs to supply — there is nothing to substitute here.
            wire_plugins.run(payload, home, workspace, {})

            raw = (home / ".cursor" / "mcp.json").read_text()
            entry = json.loads(raw)["mcpServers"]["two-slot"]
            self.assertEqual(entry, {"command": "mcp-remote", "args": [
                "https://example.test/mcp",
                "--header", "Authorization: Bearer ${TOKEN_A}",
                "--header", "X-Trace: ${TOKEN_B}:${TOKEN_A}"]})

    def test_stale_remote_server_and_its_baked_key_are_pruned(self):
        """A pre-shim config carrying a remote entry with the key INLINED must
        be scrubbed on the next run.

        This is why reconcile_agent_servers and the .djinn-servers sidecar
        outlive the rendering path they belonged to: they are the only thing
        that removes a key an older release wrote to disk, and a container that
        has not run `up` since still has one. Hand-added and local-plugin
        entries must survive the prune.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            workspace = Path(tmp) / "workspace"
            (workspace / "repos").mkdir(parents=True)
            cfg = home / ".cursor" / "mcp.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"mcpServers": {
                "hand-added": {"command": "keep-me"},
                "obsidian-annotated": {
                    "url": "https://mcp-obsidian.dmetr.io/mcp",
                    "headers": {"Authorization": "Bearer sekret-token"}},
            }}))
            (cfg.parent / "mcp.json.djinn-servers").write_text('["obsidian-annotated"]\n')

            cursor_only = [{
                "binary": "cursor-agent", "config_path": ".cursor/mcp.json",
                "format": "json", "dialect": "url", "env_refs": False, "strategy": "",
            }]
            wire_plugins.run({
                "agents": cursor_only,
                "plugin_mcp_entries": [{"serena": {"command": "bash", "args": ["-lc", "s"]}}],
                "agent_servers": [],
            }, home, workspace, {})

            raw = cfg.read_text()
            cursor = json.loads(raw)["mcpServers"]
            self.assertNotIn("obsidian-annotated", cursor)   # stale server pruned
            self.assertNotIn("sekret-token", raw)            # inline credential gone
            self.assertIn("serena", cursor)
            self.assertIn("hand-added", cursor)

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

    def test_named_env_refs_multi_slot_still_wires_via_shim(self):
        """A named env_refs field carries exactly one bearer slot, so a two-slot
        remote cannot be rendered natively for kimi. It is NOT dropped: kimi
        lands in `ref` and the container half renders it as an mcp-remote shim."""
        env = {
            "AGENTS_MCP_JSON": json.dumps(self.KIMI_REF_AGENT, separators=(",", ":")),
            "AGENT_SERVERS_JSON": json.dumps({
                "obsidian-annotated": {
                    "spec": {"url": "https://mcp-obsidian.dmetr.io/mcp",
                             "headers": {"Authorization": "Bearer ${TOKEN_A}",
                                         "X-Trace": "${TOKEN_B}"}},
                    "requires": ["TOKEN_A", "TOKEN_B"],
                }
            }),
            "AGENT_SECRETS": "kimi\tTOKEN_A\tSRC\nkimi\tTOKEN_B\tSRC\n",
        }
        payload = wire_plugins.build_payload(env)
        self.assertEqual(payload["agent_servers"][0]["ref"], ["kimi"])

    def test_named_env_refs_non_bearer_still_wires_via_shim(self):
        """The browser/kimi incident: X-API-Key is not a bearer header, so the
        native rendering does not fit. kimi gets the server anyway, over the
        shim, instead of the build aborting or the server being dropped."""
        env = {
            "AGENTS_MCP_JSON": json.dumps(self.KIMI_REF_AGENT, separators=(",", ":")),
            "AGENT_SERVERS_JSON": json.dumps({
                "browser": {
                    "spec": {"url": "http://host.docker.internal:8814/mcp",
                             "headers": {"X-API-Key": "${BROWSER_KEY}"}},
                    "requires": ["BROWSER_KEY"],
                }
            }),
            "AGENT_SECRETS": "kimi\tBROWSER_KEY\tSRC\n",
        }
        payload = wire_plugins.build_payload(env)
        self.assertEqual(payload["agent_servers"][0]["ref"], ["kimi"])

    def test_round_trips_through_run(self):
        """The payload build_payload emits is exactly what run() consumes: a
        required remote server reaches claude natively and cursor over the shim;
        a local plugin reaches cursor too. Neither carries a key."""
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
                                    "cursor-agent\tOBSIDIAN_ANNOTATED_KEY\tSRC\n"}
            payload = json.loads(json.dumps(wire_plugins.build_payload(env)))
            with contextlib.redirect_stdout(io.StringIO()):
                wire_plugins.run(payload, home, workspace, env)
            raw_cursor = (home / ".cursor" / "mcp.json").read_text()
            cursor = json.loads(raw_cursor)
            # cursor: local plugin + obsidian as an mcp-remote shim, ref intact
            self.assertIn("serena", cursor["mcpServers"])
            self.assertEqual(cursor["mcpServers"]["obsidian-annotated"]["command"],
                             "mcp-remote")
            self.assertIn("${OBSIDIAN_ANNOTATED_KEY}", raw_cursor)
            self.assertNotIn("SECRET", raw_cursor)
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
        }
        payload = wire_plugins.build_payload(env)
        self.assertEqual([a["binary"] for a in payload["agents"]],
                         ["alpha-ref", "beta-managed", "gamma-literal"])
        servers = {entry["name"]: entry for entry in payload["agent_servers"]}
        # env_refs alone decides the bucket, for a remote spec and a local one
        # alike. What each agent then RECEIVES differs (native / shim /
        # verbatim), but that is _render_for_agent's job, not the payload's.
        for name in ("remote", "local"):
            self.assertEqual(servers[name]["ref"], ["alpha-ref"], name)
            self.assertEqual(servers[name]["local"], ["beta-managed", "gamma-literal"], name)
            self.assertEqual(set(servers[name]), {"name", "spec", "requires", "ref", "local"})

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
        }
        payload = wire_plugins.build_payload(env)
        servers = {entry["name"]: entry for entry in payload["agent_servers"]}
        # claude (env_refs true) and codex (bearer_token_env_var, a truthy
        # string) hold a ${SLOT} ref; cursor-agent and pi go through the local
        # path — over the shim for the remote server, verbatim for the local one.
        for name in ("remote", "local"):
            self.assertEqual(servers[name]["ref"], ["claude", "codex"], name)
            self.assertEqual(servers[name]["local"], ["cursor-agent", "pi"], name)

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
