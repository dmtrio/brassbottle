#!/usr/bin/env python3
"""Unit tests for src/manifest.py (Phase 2 of the Python extraction).

Table-driven: every validation rule the old up.sh bash enforced is a row
here, with the EXACT error message the bash produced (parity was verified
against the extracted old code before the port landed). The yq/jq semantic
quirks (`//` on false, exact tools matching, agent-suffix case
order) get dedicated pins so a future "cleanup" can't change them silently.
"""

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import manifest as m
import wire_plugins

REPO = Path(__file__).parent.parent
MODULE = REPO / "src" / "manifest.py"

SERENA = {"install": "x", "mcp": {"serena": {"command": "bash", "args": ["-lc", "s"]}},
          "egress": ["blob.core.windows.net"]}
OTHER = {"install": "x", "mcp": {"other-tool": {"command": "python3"}}, "egress": []}
# Remote plugins: no install:, url: config + host_port + a required hybrid slot.
GATEWAY = {"host_port": 8811,
           "secrets": {"MCP_GATEWAY_TOKEN": {
                       "hint": "gateway (run ./service.sh gateway once)"}},
           "mcp": {"coding": {"url": "http://host.docker.internal:8811/mcp",
                              "headers": {"Authorization": "Bearer ${MCP_GATEWAY_TOKEN}"},
                              "requires": ["MCP_GATEWAY_TOKEN"]}}}
MCP_REMOTE_INSTALL = "npm install -g 'mcp-remote@^0.1.38'"
PROXYMAN = {"host_port": 8813,
            "install": MCP_REMOTE_INSTALL,
            "secrets": {"PROXYMAN_BRIDGE_KEY": {
                        "hint": "proxyman (run ./service.sh proxyman once)"}},
            "mcp": {"proxyman": {"command": "mcp-remote",
                                 "args": ["http://host.docker.internal:${HOST_PORT}/mcp",
                                          "--header", "X-API-Key: ${PROXYMAN_BRIDGE_KEY}"],
                                 "requires": ["PROXYMAN_BRIDGE_KEY"]}}}
BROWSER = {"host_port": 8814,
           "install": MCP_REMOTE_INSTALL,
           "secrets": {"RESEARCH_BROWSER_KEY": {
                       "hint": "browser (run ./service.sh browser once)"}},
           "mcp": {"browser": {"command": "mcp-remote",
                               "args": ["http://host.docker.internal:${HOST_PORT}/mcp",
                                        "--header", "X-API-Key: ${RESEARCH_BROWSER_KEY}"],
                               "requires": ["RESEARCH_BROWSER_KEY"]}}}
OBSIDIAN = {"secrets": {"OBSIDIAN_ANNOTATED_KEY": {}},
            "egress": ["mcp-obsidian.dmetr.io"],
            "mcp": {"obsidian-annotated": {
                "url": "https://mcp-obsidian.dmetr.io/mcp",
                "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"},
                "requires": ["OBSIDIAN_ANNOTATED_KEY"]}}}
WATCH = {"secrets": {"ANNOTATED_WATCH_KEY": {}}}
AXIOM = {"secrets": {"AXIOM_TOKEN": {"hint": "axiom token"}},
         "install": "npm install -g mcp-remote",
         "egress": ["mcp.axiom.co"],
         "mcp": {"axiom": {"command": "mcp-remote",
                           "args": ["https://mcp.axiom.co/mcp", "--header",
                                    "Authorization: Bearer ${AXIOM_TOKEN}"],
                           "requires": ["AXIOM_TOKEN"]}}}
PLUGIN_FILES = {"serena": SERENA, "other": OTHER,
                "gateway": GATEWAY, "proxyman": PROXYMAN, "browser": BROWSER,
                "obsidian-annotated": OBSIDIAN, "annotated-watch": WATCH,
                "axiom": AXIOM}
ENV = {"PRESENT_SECRET_VARS": "OBSIDIAN_KEY_me_claude OBSIDIAN_WATCH_KEY_w_pi",
       "SECRETS_FILE": "/sec/secrets.env"}
AGENT_FILES = {
    "aider": {
        "binary": "aider",
        "install": "pip3 install aider-chat --break-system-packages",
    },
    "claude": {
        "binary": "claude",
        "install": "npm install -g @anthropic-ai/claude-code",
        "state_dirs": [{"path": ".claude", "volume": "claude-auth"}],
        "rules_file": ".claude/CLAUDE.md",
        "mcp": {
            "config_path": ".mcp.json",
            "format": "json",
            "dialect": "mcpServers",
            "env_refs": True,
            "strategy": "claude_preapprove",
        },
    },
    "codex": {
        "binary": "codex",
        "install": "npm install -g @openai/codex",
        "state_dirs": [{"path": ".codex", "volume": "codex-auth"}],
        "rules_file": ".codex/AGENTS.md",
        "mcp": {
            "config_path": ".codex/config.toml",
            "format": "toml",
            "env_refs": False,
            "strategy": "codex_managed_block",
        },
    },
    "cursor": {
        "binary": "cursor-agent",
        "install": "curl -fsSL https://cursor.com/install | bash",
        "state_dirs": [
            {"path": ".cursor", "volume": "cursor-state"},
            {"path": ".config/cursor", "volume": "cursor-auth"},
        ],
        "mcp": {
            "config_path": ".cursor/mcp.json",
            "format": "json",
            "dialect": "url",
            "env_refs": False,
        },
    },
    "gemini": {
        "binary": "gemini",
        "install": "npm install -g @google/gemini-cli",
        "state_dirs": [{"path": ".gemini", "volume": "gemini-state"},
                       ],
        "rules_file": ".gemini/GEMINI.md",
        "mcp": {
            "config_path": ".gemini/settings.json",
            "format": "json",
            "dialect": "httpUrl",
            "env_refs": False,
        },
    },
    "pi": {
        "binary": "pi",
        "install": "npm install -g @earendil-works/pi-coding-agent",
        "mcp": {
            "config_path": ".pi/agent/mcp.json",
            "format": "json",
            "dialect": "type-http",
            "env_refs": False,
        },
    },
}


def derive(man, plugin_files=None, env=None, agent_files=None):
    return m.derive(man, PLUGIN_FILES if plugin_files is None else plugin_files,
                    AGENT_FILES if agent_files is None else agent_files,
                    ENV if env is None else env)


def derive_stdin(manifest, plugin_files=None, agent_files=None):
    plugin_files = PLUGIN_FILES if plugin_files is None else plugin_files
    agent_files = AGENT_FILES if agent_files is None else agent_files
    lines = [json.dumps(manifest, separators=(",", ":"))]
    for name in sorted(plugin_files):
        doc = plugin_files[name]
        payload = "!" if doc is m.UNREADABLE else json.dumps(doc, separators=(",", ":"))
        lines.append(f"{name}\t{payload}")
    lines.append("---agents---")
    for name in sorted(agent_files):
        doc = agent_files[name]
        payload = "!" if doc is m.UNREADABLE else json.dumps(doc, separators=(",", ":"))
        lines.append(f"{name}\t{payload}")
    return "\n".join(lines) + "\n"


class TestErrorTable(unittest.TestCase):
    """Every named error, with the exact old-bash message."""

    CASES = [
        ("bad forge", {"forge": "bitbucket"}, None,
         "forge must be github or gitea"),
        ("scalar plugins", {"plugins": "serena"}, None,
         "manifest plugins: must be a list, e.g. plugins: [serena]"),
        ("plugin name charset", {"plugins": ["../evil"]}, None,
         "manifest plugins failed validation:\n"
         "  plugin '../evil': illegal characters (allowed: letters, digits, underscore, dash)"),
        ("missing plugin file", {"plugins": ["ghost"]}, None,
         "manifest plugins failed validation:\n"
         "  plugin 'ghost': no plugin file at plugins/ghost/plugin.yml"),
        ("aggregated plugin errors", {"plugins": ["../evil", "ghost"]}, None,
         "manifest plugins failed validation:\n"
         "  plugin '../evil': illegal characters (allowed: letters, digits, underscore, dash)\n"
         "  plugin 'ghost': no plugin file at plugins/ghost/plugin.yml"),
        ("bad notify kind", {"remote": {"notify": "slack"}}, None,
         "remote.notify must be 'ntfy' (got 'slack')"),
        ("notify with bash rejected", {"remote": {"shell": "bash", "notify": "ntfy"}}, None,
         "remote.notify requires remote.shell: herdr or tmux (bash has no agent monitor)"),
        ("illegal ref char", {"identities": {"obsidian": ["bad-dash_claude"]}}, None,
         "manifest identity references failed validation:\n"
         "  obsidian ref 'bad-dash_claude': illegal characters (allowed: letters, digits, underscore)"),
        ("unknown agent suffix", {"identities": {"obsidian": ["me_nobody"]}}, None,
         "manifest identity references failed validation:\n"
         "  obsidian ref 'me_nobody': suffix is not a known agent (_cursor_agent/_claude/_gemini/_codex/_pi)"),
        ("secret missing", {"identities": {"obsidian": ["gone_claude"]}}, None,
         "manifest identity references failed validation:\n"
         "  obsidian ref 'gone_claude': OBSIDIAN_KEY_gone_claude not found in /sec/secrets.env"),
        ("aggregated identity errors",
         {"identities": {"obsidian": ["bad-dash_claude"], "watch": ["w_nobody"]}}, None,
         "manifest identity references failed validation:\n"
         "  obsidian ref 'bad-dash_claude': illegal characters (allowed: letters, digits, underscore)\n"
        "  watch ref 'w_nobody': suffix is not a known agent (_cursor_agent/_claude/_gemini/_codex/_pi)"),
        ("ntfy url missing",
         {"remote": {"shell": "tmux", "notify": "ntfy"}}, ENV,
         "manifest has remote.notify: ntfy but NTFY_URL is missing from /sec/secrets.env"),
        ("ntfy url with hash",
         {"remote": {"shell": "tmux", "notify": "ntfy"}},
         dict(ENV, NTFY_URL="https://x.com/#frag"),
         "NTFY_URL must be a bare origin (no '#', quotes) — put the topic in NTFY_TOPIC"),
        ("ntfy url unparseable",
         {"remote": {"shell": "tmux", "notify": "ntfy"}},
         dict(ENV, NTFY_URL="https:///path"),
         "cannot parse a host from NTFY_URL 'https:///path'"),
    ]

    def test_error_table(self):
        for name, man, env, message in self.CASES:
            with self.subTest(name):
                with self.assertRaises(m.ManifestError) as cm:
                    derive(man, env=env)
                self.assertEqual(str(cm.exception), message)

    PLUGIN_MCP_CASES = [
        ("dot in server name", {"bad.name": {"command": "x"}},
         "plugin 'p' mcp server 'bad.name': illegal characters in name (allowed: letters, digits, underscore, dash — it becomes a TOML/JSON key)"),
        # No server names are reserved any more (Phase 2): obsidian-annotated is
        # itself a plugin now, caught only by the cross-plugin duplicate check.
        ("non-string command", {"srv": {"command": 1}},
         "plugin 'p' mcp server 'srv': command must be a string (local stdio server)"),
        ("local extra field", {"srv": {"command": "x", "env": {"A": "b"}}},
         "plugin 'p' mcp server 'srv': unsupported field(s) for a local server: env (only command, args, and requires)"),
        ("neither command nor url", {"srv": {"args": ["x"]}},
         "plugin 'p' mcp server 'srv': needs command: (local stdio) or url: (remote http)"),
        ("both command and url", {"srv": {"command": "x", "url": "http://x/mcp"}},
         "plugin 'p' mcp server 'srv': set exactly one of command: (local stdio) or url: (remote http), not both"),
        ("non-string url", {"srv": {"url": 1}},
         "plugin 'p' mcp server 'srv': url must be a string (remote http server)"),
        ("remote headers not a map", {"srv": {"url": "http://x/mcp", "headers": ["a"]}},
         "plugin 'p' mcp server 'srv': headers must be a map of string values"),
        ("remote header non-string value", {"srv": {"url": "http://x/mcp", "headers": {"A": 1}}},
         "plugin 'p' mcp server 'srv': headers must be a map of string values"),
        ("remote extra field", {"srv": {"url": "http://x/mcp", "foo": "b"}},
         "plugin 'p' mcp server 'srv': unsupported field(s) for a remote server: foo (only url, headers, and requires)"),
    ]

    def test_plugin_mcp_error_table(self):
        for name, mcp, message in self.PLUGIN_MCP_CASES:
            with self.subTest(name):
                files = {"p": {"install": "x", "mcp": mcp}}
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"plugins": ["p"]}, plugin_files=files)
                self.assertEqual(str(cm.exception), message)

    # ── BASE_IMAGE_BINS: the install: rule's one exemption ──────────────────
    # mcp-remote used to be installed by three plugins at once, all into the
    # same global npm prefix, so the last install decided the version for all
    # of them and bumping one pin silently retargeted the others. It is now a
    # base tool: the Dockerfile installs it, and a plugin that execs it needs
    # no install: block, the same way nothing installs bash.

    def test_local_server_still_needs_an_install_block(self):
        files = {"p": {"mcp": {"srv": {"command": "some-binary"}}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]}, plugin_files=files)
        self.assertEqual(
            str(cm.exception),
            "plugin 'p': a local (command:) server needs an install: block "
            "(baked into the image), unless it execs a base tool (mcp-remote)")

    def test_base_tool_server_needs_no_install_block(self):
        files = {"p": {"mcp": {"srv": {"command": "mcp-remote",
                                       "args": ["https://x.test/mcp"]}}}}
        derive({"plugins": ["p"]}, plugin_files=files)      # must not raise

    def test_exemption_is_per_server_not_per_plugin(self):
        """A plugin running a base tool AND its own binary still bakes the
        second — otherwise one mcp-remote server would excuse the whole file."""
        files = {"p": {"mcp": {
            "bridge": {"command": "mcp-remote", "args": ["https://x.test/mcp"]},
            "own": {"command": "some-binary"}}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]}, plugin_files=files)
        self.assertIn("needs an install: block", str(cm.exception))

    def test_interpreters_are_not_base_tools(self):
        """REGRESSION GUARD: bash/python3 must never join BASE_IMAGE_BINS.

        The image ships them, so adding them looks harmless — but an entry
        means "exec'ing this IS the whole server", and `command: bash` is a
        wrapper whose real payload sits in args. Excusing it would let a plugin
        declare a server that execs something nothing installed, passing
        exactly the case this rule exists to catch.
        """
        for interp in ("bash", "sh", "python3", "python", "npx", "node"):
            self.assertNotIn(interp, m.BASE_IMAGE_BINS)
        files = {"p": {"mcp": {"srv": {
            "command": "bash", "args": ["-c", "exec never-installed"]}}}}
        with self.assertRaises(m.ManifestError):
            derive({"plugins": ["p"]}, plugin_files=files)

    def test_base_tools_are_actually_installed_by_the_dockerfile(self):
        """The set and the image have to agree; nothing else checks it.

        BASE_IMAGE_BINS is the reason three plugins stopped installing
        mcp-remote. If the Dockerfile layer is ever dropped or renamed, that
        exemption starts excusing a binary no image contains, and the failure
        lands on whichever agent first calls the tool.
        """
        # Comment lines are stripped first. The Dockerfile explains
        # mcp-remote at length, so a whole-file substring search stays green
        # long after the install layer is gone — it would be matching the
        # prose that describes the thing rather than the thing.
        instructions = "\n".join(
            line for line in (REPO / "Dockerfile").read_text().splitlines()
            if not line.lstrip().startswith("#"))
        for name in sorted(m.BASE_IMAGE_BINS):
            self.assertTrue(
                name in instructions,
                f"BASE_IMAGE_BINS claims the image provides {name!r}, but no "
                "Dockerfile instruction installs it (comments do not count)")

    def test_no_plugin_re_pins_a_base_tool(self):
        """REGRESSION: the trap this replaced — one pin per plugin, one prefix."""
        offenders = [
            path.parent.name
            for path in sorted((REPO / "plugins").glob("*/plugin.yml"))
            if any(f"{name}@" in path.read_text()
                   for name in m.BASE_IMAGE_BINS)]
        self.assertEqual(
            offenders, [],
            "these plugins pin a base tool themselves; the Dockerfile owns the "
            "version, and a second install into the same npm prefix silently "
            "wins over it")

    def test_bad_plugin_egress_domain(self):
        for bad in ("https://x.com", "x.com/path", "*.foo.com", "foo", "a b.com"):
            with self.subTest(bad):
                files = {"p": {"install": "x", "egress": [bad]}}
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"plugins": ["p"]}, plugin_files=files)
                self.assertEqual(
                    str(cm.exception),
                    f"plugin 'p' egress entry '{bad}' is not a bare hostname (no scheme, path, port, or wildcard — a domain already covers its subdomains)")

    def test_duplicate_server_name_across_plugins(self):
        files = {"a": {"install": "x", "mcp": {"srv": {"command": "x"}}},
                 "b": {"install": "x", "mcp": {"srv": {"command": "y"}}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["a", "b"]}, plugin_files=files)
        self.assertEqual(str(cm.exception),
                         "multiple enabled plugins define the same MCP server name: srv")


class TestRemoteSchema(unittest.TestCase):
    """remote.jump / remote.shell (PLN - default jump reachability P1)."""

    def test_empty_manifest_defaults_jump_true_shell_herdr(self):
        # PLN - herdr adoption P4 (default flip): the tmux picker is mostly
        # noise (background plugin sessions), herdr is the landing default.
        d = derive({})
        self.assertEqual(d["REMOTE_JUMP"], "true")
        self.assertEqual(d["REMOTE_SHELL"], "herdr")
        self.assertNotIn("REMOTE_TMUX", d)

    def test_shell_tmux_explicit(self):
        d = derive({"remote": {"shell": "tmux"}})
        self.assertEqual(d["REMOTE_SHELL"], "tmux")

    def test_notify_without_explicit_shell_derives_herdr(self):
        # notify: ntfy now works with herdr (event-driven via herdr_notify.py)
        # with no warning or implication — herdr is the default shell.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"notify": "ntfy"}},
                       env=dict(ENV, NTFY_URL="https://ntfy.example.com"))
        self.assertEqual(d["REMOTE_SHELL"], "herdr")
        self.assertEqual(d["REMOTE_NOTIFY"], "ntfy")
        self.assertEqual(err.getvalue(), "")

    def test_notify_with_explicit_tmux_does_not_warn(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"shell": "tmux", "notify": "ntfy"}},
                       env=dict(ENV, NTFY_URL="https://ntfy.example.com"))
        self.assertEqual(d["REMOTE_SHELL"], "tmux")
        self.assertEqual(err.getvalue(), "")

    def test_jump_false(self):
        d = derive({"remote": {"jump": False}})
        self.assertEqual(d["REMOTE_JUMP"], "false")

    def test_jump_non_boolean_rejected(self):
        for bad in ("yes", 1):
            with self.subTest(bad=bad):
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"remote": {"jump": bad}})
                self.assertEqual(str(cm.exception),
                                 f"remote.jump must be true or false (got '{bad}')")

    def test_shell_bash(self):
        d = derive({"remote": {"shell": "bash"}})
        self.assertEqual(d["REMOTE_SHELL"], "bash")

    def test_shell_herdr(self):
        d = derive({"remote": {"shell": "herdr"}})
        self.assertEqual(d["REMOTE_SHELL"], "herdr")

    def test_shell_other_value_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"remote": {"shell": "zsh"}})
        self.assertEqual(str(cm.exception),
                         "remote.shell must be tmux, herdr, or bash (got 'zsh')")

    def test_notify_with_herdr_succeeds(self):
        # notify: ntfy now works with herdr (event-driven via herdr_notify.py)
        d = derive({"remote": {"shell": "herdr", "notify": "ntfy"}},
                   env=dict(ENV, NTFY_URL="https://ntfy.example.com"))
        self.assertEqual(d["REMOTE_SHELL"], "herdr")
        self.assertEqual(d["REMOTE_NOTIFY"], "ntfy")

    def test_tmux_alias_sets_shell_and_warns(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"tmux": True}})
        self.assertEqual(d["REMOTE_SHELL"], "tmux")
        self.assertIn(
            "remote.tmux is deprecated — use remote.shell: tmux "
            "(herdr is the default now; shell: bash opts out)",
            err.getvalue())

    def test_tmux_false_sets_shell_bash_and_warns(self):
        # Old-style explicit opt-out (`remote: {tmux: false}`) must not
        # silently become shell: tmux — it's read as shell: bash instead,
        # with the same deprecation-warning shape as the tmux: true alias.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"tmux": False}})
        self.assertEqual(d["REMOTE_SHELL"], "bash")
        self.assertIn(
            "remote.tmux is deprecated — use remote.shell: bash "
            "(tmux: false is read as shell: bash)",
            err.getvalue())

    def test_tmux_null_has_no_effect_and_no_warning(self):
        # Distinct from tmux: false — YAML null/absent both mean "the key
        # isn't really set", so this stays the ordinary herdr default.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"tmux": None}})
        self.assertEqual(d["REMOTE_SHELL"], "herdr")
        self.assertEqual(err.getvalue(), "")

    def test_tmux_and_shell_both_set_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"remote": {"tmux": True, "shell": "bash"}})
        self.assertEqual(
            str(cm.exception),
            "remote.tmux and remote.shell are both set — drop remote.tmux "
            "(deprecated alias of shell: tmux)")

    def test_unknown_remote_key_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"remote": {"foo": 1, "bar": 2}})
        self.assertEqual(
            str(cm.exception),
            "remote: unsupported field(s): foo,bar "
            "(only jump, shell, notify — and the deprecated tmux, mosh, mosh_ports)")

    def test_notify_ntfy_with_shell_bash_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"remote": {"shell": "bash", "notify": "ntfy"}})
        self.assertEqual(
            str(cm.exception),
            "remote.notify requires remote.shell: herdr or tmux (bash has no agent monitor)")

    def test_notify_ntfy_without_ssh_now_succeeds(self):
        env = dict(ENV, NTFY_URL="https://ntfy.example.com")
        d = derive({"remote": {"shell": "tmux", "notify": "ntfy"}}, env=env)
        self.assertEqual(d["REMOTE_NOTIFY"], "ntfy")
        self.assertEqual(d["CONTAINER_NTFY_URL"], "https://ntfy.example.com")

    def test_mosh_keys_warn_and_derive_nothing(self):
        # Both retired keys present, no ssh: section — no error (no more
        # ssh-required check) and the single warning fires once, not per key.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"mosh": True, "mosh_ports": "9:banana"}})
        self.assertNotIn("REMOTE_MOSH", d)
        self.assertNotIn("MOSH_PORTS", d)
        self.assertNotIn("MOSH_PORTS_DASH", d)
        self.assertEqual(
            err.getvalue().count(
                "remote.mosh is retired — the jump carries mosh (mosh coder@<jump ip>, "
                "then ssh djinn-<bottle>); drop remote.mosh / remote.mosh_ports"),
            1)

    def test_mosh_false_is_silent(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"mosh": False}})
        self.assertNotIn("remote.mosh is retired", err.getvalue())
        self.assertNotIn("REMOTE_MOSH", d)

    def test_mosh_ports_alone_warns(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"remote": {"mosh_ports": "60000:60010"}})
        self.assertNotIn("REMOTE_MOSH", d)
        self.assertNotIn("MOSH_PORTS", d)
        self.assertNotIn("MOSH_PORTS_DASH", d)
        self.assertEqual(
            err.getvalue().count(
                "remote.mosh is retired — the jump carries mosh (mosh coder@<jump ip>, "
                "then ssh djinn-<bottle>); drop remote.mosh / remote.mosh_ports"),
            1)


class TestYqSemanticsPins(unittest.TestCase):
    """The jq/yq quirks the port must NOT silently fix."""

    def test_alternative_operator_fires_on_false(self):
        d = derive({"plugins": False, "repos": False, "memory": False, "agents": False})
        self.assertEqual(d["PLUGINS"], "")
        self.assertEqual(d["PLUGINS_ENABLED"], "")
        self.assertEqual(d["REPOS"], "")
        self.assertEqual(d["MEM_LIMIT"], "2g")
        self.assertEqual(d["AGENTS_ENABLED"], " ".join(sorted(AGENT_FILES)))  # default tool set

    def test_legacy_repo_key_rejected(self):
        # layout v2: any presence of repo: (even null/false) is a hard error.
        for val in ("https://github.com/x/app.git", "", False, None):
            with self.subTest(val=val):
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"repo": val})
                self.assertIn("repos:", str(cm.exception))
                self.assertEqual(
                    str(cm.exception),
                    "manifest repo: is gone — declare repos: [<url>, ...] instead "
                    "(layout v2: each repo clones to /workspace/repos/<name>)")

    def test_agents_match_is_exact_string_equality(self):
        # The rename PR pinned the old jq-substring quirk here; Phase 1's
        # deliberate tightening applies to the agents: key identically.
        d = derive({"agents": ["claude-code"]})
        self.assertEqual(d["AGENTS_ENABLED"], "")
        self.assertEqual(d["AGENTS_MCP_JSON"], "[]")

    def test_tools_key_is_rejected_by_name(self):
        for value in (["codex"], False):
            with self.assertRaises(m.ManifestError) as cm:
                derive({"tools": value})
            self.assertEqual(
                str(cm.exception),
                "manifest tools: was renamed to agents: — update the manifest (same values)")

    def test_agents_false_defaults_to_default_tool_set(self):
        d = derive({"agents": False})
        self.assertIn("aider", d["AGENTS_ENABLED"].split())

    def test_capabilities_sugar_only_literal_true_maps_to_plugin(self):
        # capabilities: gateway/proxyman/browser are deprecated sugar now; only
        # the literal boolean true maps onto the plugin (yq `// false` raw-flag
        # semantics preserved: "yes"/1 do NOT enable it).
        files = {"gateway": GATEWAY, "proxyman": PROXYMAN, "browser": BROWSER}
        d = derive({"capabilities": {"gateway": "yes", "proxyman": 1, "browser": True}},
                   plugin_files=files)
        self.assertEqual(d["PLUGINS"], "browser")          # only browser: true
        self.assertEqual(d["PLUGINS_ENABLED"], "browser")
        self.assertEqual(d["HOST_MCP_PORTS"], "8814,8816")      # browser's host_port + broker
        # the retired CAP_* variables are gone from the derived set
        self.assertNotIn("CAP_GATEWAY", d)
        self.assertNotIn("CAP_BROWSER", d)

    def test_agent_suffix_case_order(self):
        env = dict(
            ENV,
            PRESENT_SECRET_VARS="OBSIDIAN_KEY_weird_claude_cursor_agent OBSIDIAN_KEY_a_pi",
        )
        d = derive({"identities": {"obsidian": ["weird_claude_cursor_agent"]}}, env=env)
        self.assertEqual(
            d["AGENT_SECRETS"],
            "cursor-agent\tOBSIDIAN_ANNOTATED_KEY\tOBSIDIAN_KEY_weird_claude_cursor_agent\n")
        d = derive({"identities": {"obsidian": ["a_pi"]}}, env=env)
        self.assertEqual(d["AGENT_SECRETS"], "pi\tOBSIDIAN_ANNOTATED_KEY\tOBSIDIAN_KEY_a_pi\n")
        with self.assertRaises(m.ManifestError) as cm:
            derive({"identities": {"obsidian": ["cursor_agent"]}}, env=env)
        self.assertIn("suffix is not a known agent", str(cm.exception))


class TestRepos(unittest.TestCase):
    """layout v2: repos: list → REPOS name<tab>url\\n lines."""

    def test_absent_repos_is_empty(self):
        self.assertEqual(derive({})["REPOS"], "")

    def test_string_entry_strips_git_suffix(self):
        d = derive({"repos": ["https://github.com/x/app.git"]})
        self.assertEqual(d["REPOS"], "app\thttps://github.com/x/app.git\n")

    def test_trailing_slash_url(self):
        d = derive({"repos": ["https://github.com/x/app/"]})
        self.assertEqual(d["REPOS"], "app\thttps://github.com/x/app/\n")

    def test_ssh_style_url_name(self):
        d = derive({"repos": ["git@github.com:org/thing.git"]})
        self.assertEqual(d["REPOS"], "thing\tgit@github.com:org/thing.git\n")

    def test_map_entry_with_explicit_name(self):
        d = derive({"repos": [{"name": "myapp", "url": "https://github.com/x/app.git"}]})
        self.assertEqual(d["REPOS"], "myapp\thttps://github.com/x/app.git\n")

    def test_map_entry_without_name(self):
        d = derive({"repos": [{"url": "https://github.com/x/app.git"}]})
        self.assertEqual(d["REPOS"], "app\thttps://github.com/x/app.git\n")

    def test_map_entry_falsy_name_reads_as_absent(self):
        # yq `//` semantics: name: null / name: false → derive from the URL,
        # matching every other falsy leaf in this module.
        for falsy in (None, False):
            with self.subTest(name=falsy):
                d = derive({"repos": [{"name": falsy, "url": "https://github.com/x/app.git"}]})
                self.assertEqual(d["REPOS"], "app\thttps://github.com/x/app.git\n")

    def test_map_entry_unknown_key_raises(self):
        # A typo'd key must not silently fall back to the URL basename.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [{"nmae": "lib", "url": "https://github.com/x/lib.git"}]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: unsupported field(s): nmae (only name and url)")

    def test_multiple_entries_preserve_manifest_order(self):
        d = derive({"repos": [
            "https://github.com/x/beta.git",
            {"name": "alpha", "url": "https://github.com/x/other.git"},
            "git@github.com:org/gamma.git",
        ]})
        self.assertEqual(
            d["REPOS"],
            "beta\thttps://github.com/x/beta.git\n"
            "alpha\thttps://github.com/x/other.git\n"
            "gamma\tgit@github.com:org/gamma.git\n")

    def test_duplicate_derived_names_raise(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [
                "https://github.com/x/app.git",
                "https://github.com/y/app.git",
            ]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: duplicate name 'app'")

    def test_bad_name_leading_dot_raises(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [{"name": ".hidden", "url": "https://github.com/x/app.git"}]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: illegal name '.hidden' "
            "(must start with letter/digit/underscore; only letters, digits, . _ - thereafter — "
            "it becomes a directory under /workspace/repos)")

    def test_bad_name_slash_raises(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [{"name": "a/b", "url": "https://github.com/x/app.git"}]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: illegal name 'a/b' "
            "(must start with letter/digit/underscore; only letters, digits, . _ - thereafter — "
            "it becomes a directory under /workspace/repos)")

    def test_blank_url_in_map_raises(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [{"name": "x", "url": ""}]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: url must be a non-empty string")

    def test_entry_wrong_type_raises(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [1]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: must be a URL string or {name, url} map (got a number)")

    def test_repos_not_a_list_raises(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": "https://github.com/x/app.git"})
        self.assertEqual(
            str(cm.exception),
            "manifest repos: must be a list of URLs or {name, url} maps")

    def test_url_with_space_raises(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://github.com/x/a pp.git"]})
        self.assertEqual(
            str(cm.exception),
            "manifest repos failed validation:\n"
            "  repos entry: URL 'https://github.com/x/a pp.git' contains whitespace")


class TestCredentialHosts(unittest.TestCase):
    """GIT_CREDENTIAL_HOSTS: the origins (scheme://host[:port], one per line)
    entrypoint.sh installs git-credential-org under — every host the git.hosts
    table names plus every https:// origin in repos:, github included. The
    helper is installed for exactly this set; no host is hard-coded anywhere
    else, and the helper itself carries no special case."""

    def test_declares_nothing_installs_the_cli_host(self):
        # The implicit default row (github.com=GH_TOKEN) always installs the
        # router for the CLI host — today's behaviour, table-derived.
        self.assertEqual(derive({})["GIT_CREDENTIAL_HOSTS"], "https://github.com\n")

    def test_github_only_manifest_yields_github(self):
        d = derive({"repos": ["https://github.com/x/app.git",
                              "https://GitHub.com/y/lib.git"]})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"], "https://github.com\n")
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")

    def test_gitea_repo_yields_its_origin(self):
        d = derive({"forge": "gitea",
                    "repos": ["https://git.example.test/Emergence/filebrowser.git"]})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"],
                         "https://git.example.test\nhttps://github.com\n")

    def test_mixed_hosts_distinct_sorted_lowercased(self):
        d = derive({"repos": [
            "https://github.com/x/app.git",
            "https://Zeta.example.test/a/b.git",
            "https://alpha.example.test:3000/c/d.git",
            "https://alpha.example.test:3000/c/e.git",   # duplicate origin
            "git@gitea.example.test:h/i.git",             # scp-style: no HTTP helper
            "ssh://git@gitea.example.test/j/k.git",       # ssh: no HTTP helper
        ]})
        self.assertEqual(
            d["GIT_CREDENTIAL_HOSTS"],
            "https://alpha.example.test:3000\n"
            "https://github.com\n"
            "https://zeta.example.test\n")

    def test_userinfo_in_url_is_not_part_of_origin(self):
        d = derive({"repos": ["https://bot@git.example.test/x/y.git"]})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"],
                         "https://git.example.test\nhttps://github.com\n")

    def test_explicit_443_collapses_to_the_bare_host(self):
        d = derive({"repos": ["https://github.com:443/x/y.git"]})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"], "https://github.com\n")
        d = derive({"repos": ["https://git.example.test:443/x/y.git"]})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"],
                         "https://git.example.test\nhttps://github.com\n")

    def test_bad_host_is_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://h'$(x)'.test/o/r.git"]})
        self.assertIn("unsupported host", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://git.[a].test/o/r.git"]})
        self.assertIn("unsupported host", str(cm.exception))

    def test_underscore_host_accepted(self):
        d = derive({"repos": ["https://git_internal.lan/o/r.git"]})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"],
                         "https://git_internal.lan\nhttps://github.com\n")

    def test_egress_notice_hosts_drop_base_allowlisted(self):
        # GIT_EGRESS_NOTICE_HOSTS: the subset of GIT_CREDENTIAL_HOSTS the
        # up-time egress notice should check — a host the base allowlist
        # already permits (github.com, mirrored from src/init-firewall.sh's
        # ALLOWED_ZONES) is allowed without capabilities.egress, so warning
        # about it would be false on every bottle.
        d = derive({"repos": ["https://github.com/x/app.git",
                              "https://git.example.test/o/r.git"]})
        self.assertEqual(d["GIT_EGRESS_NOTICE_HOSTS"], "https://git.example.test\n")
        self.assertEqual(derive({})["GIT_EGRESS_NOTICE_HOSTS"], "")
        d = derive({"repos": ["https://github.com:443/x/y.git"]})
        self.assertEqual(d["GIT_EGRESS_NOTICE_HOSTS"], "")

    def test_base_allowlist_zone_covers_subdomains(self):
        # BASE_ALLOWLISTED_GIT_HOSTS names ZONES: the base allowlist covers a
        # host and every subdomain of it (the firewall's dnsmasq zones), so
        # gist.github.com is as base-allowed as github.com itself and the
        # egress notice stays silent for it.
        d = derive({"repos": ["https://gist.github.com/me/x.git"]})
        self.assertEqual(d["GIT_EGRESS_NOTICE_HOSTS"], "")

    def test_base_allowlisted_hosts_are_pinned_against_init_firewall(self):
        # Drift pin: every base-allowlisted git host must appear in
        # src/init-firewall.sh's ALLOWED_ZONES block — the constant mirrors
        # that list, so the two cannot drift apart silently.
        text = (REPO / "src" / "init-firewall.sh").read_text()
        zones = text.split('ALLOWED_ZONES="')[1].split('"')[0].split()
        for host in m.BASE_ALLOWLISTED_GIT_HOSTS:
            self.assertIn(host, zones)

    def test_repos_hosts_and_table_hosts_merge_deduped(self):
        d = derive({"repos": ["https://git.example.test/E/x.git"],
                    "git": {"hosts": {"git.example.test": {"token": "GH_TOKEN_x"},
                                      "Git.Other.Test:443": {"token": "GH_TOKEN_x"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_x"})
        self.assertEqual(
            d["GIT_CREDENTIAL_HOSTS"],
            "https://git.example.test\n"
            "https://git.other.test\n")


class TestTokenRouting(unittest.TestCase):
    """What survives of the old owner-keyed routing checks: the http://
    rejection and the owner/repo path requirement (both still repos:-level
    rules). Tokens themselves route by HOST now — the per-owner canonical-var
    machinery is gone, so a.b/a_b owner spellings no longer collide."""

    def test_http_repo_url_is_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["http://x.example.test/acme/a.git"]})
        self.assertIn("uses http://", str(cm.exception))

    def test_http_repo_on_a_listed_host_is_rejected(self):
        # A token must never go over plain http: an http:// repo on a host the
        # table carries a token for is rejected outright (like every http://
        # repo URL — cleartext credentials are refused, no exceptions).
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["http://git.example.test/acme/a.git"],
                    "git": {"hosts": {"git.example.test": {"token": "GH_TOKEN_x"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_x"})
        self.assertIn("uses http://", str(cm.exception))

    def test_https_repo_url_needs_owner_and_repo(self):
        # The up-time attribution treats the first path segment as owner and
        # the clone failure hint splits the host the same way; a bare host or
        # owner-only URL would silently break both, so require a full
        # owner/repo path.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://git.example.test"]})
        self.assertIn("has no owner/repo path", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://git.example.test/onlyowner"]})
        self.assertIn("has no owner/repo path", str(cm.exception))
        d = derive({"repos": ["https://git.example.test/o/r"]})
        self.assertEqual(d["REPOS"], "r\thttps://git.example.test/o/r\n")

    def test_public_owner_on_two_hosts_never_fires_anything(self):
        # No token routes for openssl: a public owner on two hosts (its own
        # forge + github mirror) must derive fine — each host takes its own
        # table row's credential.
        d = derive({"repos": ["https://github.com/openssl/openssl.git",
                              "https://git.openssl.org/openssl/tools.git"]})
        self.assertEqual(
            d["GIT_CREDENTIAL_HOSTS"],
            "https://git.openssl.org\nhttps://github.com\n")
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")

    def test_same_owner_spelling_cannot_collide_on_a_var_anymore(self):
        # Old spellings 'a.b' and 'a_b' both sanitised to GH_TOKEN_a_b — one
        # would receive the other's token. Tokens are routed by host and use
        # the secrets.env variable exactly as written (no name mangling), so
        # two such owners on ONE host with the same variable simply collapse
        # into one row.
        d = derive({"git": {"orgs": {"a.b": {"token": "GH_TOKEN_a_b", "host": "h.test"},
                                     "a_b": {"token": "GH_TOKEN_a_b", "host": "h.test"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_a_b"})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN h.test=GH_TOKEN_a_b")
        self.assertIn("a.b\t\t\n", d["GIT_ORG_IDENTITIES"])
        self.assertIn("a_b\t\t\n", d["GIT_ORG_IDENTITIES"])

    def test_same_host_two_tokens_from_orgs_is_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"git": {"orgs": {"a.b": {"token": "GH_TOKEN_a_b", "host": "h.test"},
                                     "a_b": {"token": "GH_TOKEN_a_b2", "host": "h.test"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_a_b GH_TOKEN_a_b2"})
        self.assertIn(
            "git.orgs owner 'a.b' and git.orgs owner 'a_b' both set a token for "
            "h.test with different variables (GH_TOKEN_a_b, GH_TOKEN_a_b2)",
            str(cm.exception))

    def test_owner_on_scp_github_and_https_gitea_binds_to_https_host(self):
        # The same owner spelled via scp-style (which never binds — see
        # _ssh_repo_owners) and via https on a different host: only the
        # https-derived host carries the row, and derivation succeeds — the
        # scp/ssh host is unvalidated and never used for routing.
        d = derive({"repos": ["git@github.com:acme/a.git",
                              "https://git.example.test/acme/b.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertEqual(d["GIT_HOST_TOKENS"],
                         "git.example.test=GH_TOKEN_acme github.com=GH_TOKEN")

    def test_scp_owner_with_no_orgs_token_derives(self):
        d = derive({"repos": ["git@h.test:a.b/x.git"]})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")


class TestOrgHosts(unittest.TestCase):
    """Each git.orgs token resolves to exactly one host row: its declared
    host:, else the one https:// host its owner's repos: URLs name — never a
    guessed default (a wrong guess presents a token to the wrong forge)."""

    def test_org_rows_bind_to_repo_host(self):
        d = derive({"repos": ["https://github.com/acme/a.git",
                              "https://git.example.test/Emergence/f.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"},
                                     "Emergence": {"token": "GH_TOKEN_emergence"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme GH_TOKEN_emergence"})
        # Sorted by host; github.com keeps its own row (the org's token there
        # — an orgs row CLAIMS the CLI host, it does not fall back to it).
        self.assertEqual(
            d["GIT_HOST_TOKENS"],
            "git.example.test=GH_TOKEN_emergence github.com=GH_TOKEN_acme")

    def test_org_unlisted_owner_rejected_everywhere(self):
        # No default host, ever: an owner git.orgs routes a token for that
        # appears in no repos: URL and declares no host: is a hard error even
        # in an otherwise github-only bottle — the row decides which host
        # receives the token, and a wrong guess presents a token to the wrong
        # forge.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"git": {"orgs": {"vendor": {"token": "GH_TOKEN_vendor"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_vendor"})
        self.assertIn(
            "git.orgs owner 'vendor': not in repos: — add its repo "
            "(https:// to route this token) or set host: on its git.orgs entry",
            str(cm.exception))

    def test_org_unlisted_owner_rejected_in_mixed_bottle(self):
        # This bottle has a non-github host installed (git.example.test, via
        # OrgA's repo), so OrgB — routed a token but never seen in repos: —
        # can no longer be silently assumed to be a github owner.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://git.example.test/OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga"},
                                     "OrgB": {"token": "GH_TOKEN_orgb"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga GH_TOKEN_orgb"})
        # owner routing folds to lowercase throughout the git.orgs checks (the
        # attribution matches the clone URL's owner), so the message names
        # 'orgb' — the message must never pretend 'orgb' is the literal
        # manifest key (it's "OrgB"), so it never spells a dotted
        # git.orgs.<owner>... path a user could paste back in the wrong case.
        self.assertIn(
            "git.orgs owner 'orgb': not in repos: — add its repo "
            "(https:// to route this token) or set host: on its git.orgs entry",
            str(cm.exception))
        self.assertNotIn("git.orgs.orgb", str(cm.exception))

    def test_org_scp_only_owner_requires_declared_host(self):
        # OrgA's only repos: entry is scp-style (git@host:owner/x.git),
        # which never routes an https token directly, and the host in it is
        # unvalidated — so it never binds. Declaring host: explicitly is
        # required.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["git@git.example.test:OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga"})
        self.assertIn("listed only over scp-style/ssh:///git:// URLs", str(cm.exception))

    def test_org_scp_github_owner_requires_declared_host(self):
        # Even a github-looking scp repo doesn't bind — the host is
        # unvalidated regardless of what it looks like, so host: must still
        # be declared explicitly.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["git@github.com:acme/x.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertIn("listed only over scp-style/ssh:///git:// URLs", str(cm.exception))

    def test_org_ssh_scheme_only_owner_requires_declared_host(self):
        # Same as above but for the ssh:// form (ssh://host/owner/repo.git)
        # rather than scp-style (host:owner/repo.git).
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["ssh://git.example.test/OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga"})
        self.assertIn("listed only over scp-style/ssh:///git:// URLs", str(cm.exception))

    def test_org_scp_userless_owner_requires_declared_host(self):
        # git's scp-like syntax makes the userinfo optional
        # (host:owner/repo.git, no user@) — must still be recognised as
        # scp-style, not misread as an unlisted owner, and still requires
        # host: since it never binds.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["git.example.test:OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga"})
        self.assertIn("listed only over scp-style/ssh:///git:// URLs", str(cm.exception))

    def test_org_git_scheme_only_owner_requires_declared_host(self):
        # git:// is treated the same as ssh:// — neither ever routes an
        # https token directly, nor binds the owner to the host it's
        # spelled on.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["git://git.example.test/OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga"})
        self.assertIn("listed only over scp-style/ssh:///git:// URLs", str(cm.exception))

    def test_org_scp_only_owner_with_declared_host_binds(self):
        # An scp-only owner with an explicit host: binds to the declared
        # host (not the unvalidated scp host, though they happen to agree
        # here) and the router is installed for it.
        d = derive({"repos": ["git@git.example.test:OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga",
                                               "host": "git.example.test:3000"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga"})
        self.assertEqual(
            d["GIT_HOST_TOKENS"],
            "git.example.test:3000=GH_TOKEN_orga github.com=GH_TOKEN")
        self.assertEqual(
            d["GIT_CREDENTIAL_HOSTS"],
            "https://git.example.test:3000\nhttps://github.com\n")

    def test_org_declared_host(self):
        d = derive({"repos": ["https://git.example.test/OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga"},
                                     "OrgB": {"token": "GH_TOKEN_orgb",
                                              "host": "Git.Other.Test:443"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga GH_TOKEN_orgb"})
        self.assertIn("git.other.test=GH_TOKEN_orgb", d["GIT_HOST_TOKENS"])

    def test_org_declared_host_disagrees_with_repos(self):
        # The message must read "git.orgs owner 'orga'", not the case-folded
        # "git.orgs.orga" — the latter looks like a literal manifest key even
        # though 'orga' is owner_lc, not what the user typed (OrgA).
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://git.example.test/OrgA/x.git"],
                    "git": {"orgs": {"OrgA": {"token": "GH_TOKEN_orga",
                                               "host": "github.com"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orga"})
        self.assertIn(
            "git.orgs owner 'orga': host: github.com disagrees with repos: "
            "(git.example.test) — remove host: or fix the repos: URL",
            str(cm.exception))

    def test_orgs_routed_hosts_carry_provenance(self):
        # GIT_ORG_ROUTED_HOSTS: the hosts whose table rows a git.orgs entry
        # supplied. A row that came from git.token is already explicitly
        # stated — an unrelated git.orgs entry on another host must NOT put
        # the CLI host in this list, or the shared-host notice would
        # misattribute the CLI host's row to a git.orgs entry.
        d = derive({"repos": ["https://github.com/acme/a.git",
                              "https://github.com/other/b.git"],
                    "git": {"token": "GH_TOKEN_fry",
                            "orgs": {"emergence": {"token": "GH_TOKEN_x",
                                                    "host": "git.example.test"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_fry GH_TOKEN_x"})
        self.assertEqual(d["GIT_ORG_ROUTED_HOSTS"], "git.example.test")
        d = derive({"repos": ["https://github.com/acme/a.git",
                              "https://github.com/other/b.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertEqual(d["GIT_ORG_ROUTED_HOSTS"], "github.com")
        # An org entry that COLLAPSES into an equal row still resolves to
        # that host.
        d = derive({"repos": ["https://github.com/acme/a.git"],
                    "git": {"token": "GH_TOKEN_acme",
                            "orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertEqual(d["GIT_ORG_ROUTED_HOSTS"], "github.com")
        self.assertEqual(derive({})["GIT_ORG_ROUTED_HOSTS"], "")

    def test_org_owner_on_two_https_hosts_is_ambiguous(self):
        # A token row must land on ONE host; an owner whose https repos: URLs
        # name two hosts is ambiguous and is rejected unless host: says which
        # one. (The old per-owner routing rejected the same shape; the reason
        # is host ambiguity now, not owner-keyed var sharing.)
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["https://github.com/acme/a.git",
                              "https://git.example.test/Acme/b.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertIn(
            "git.orgs owner 'acme': its repos: URLs name more than one host "
            "(git.example.test, github.com)",
            str(cm.exception))

    def test_org_two_host_ambiguity_is_scheme_case_insensitive(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": ["HTTPS://github.com/acme/a.git",
                              "https://git.example.test/acme/b.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertIn("name more than one host", str(cm.exception))

    def test_org_declared_host_resolves_two_host_ambiguity(self):
        # A declared host: that matches one of the owner's https hosts picks
        # the row's host explicitly — the ambiguity is resolved, not guessed.
        d = derive({"repos": ["https://github.com/acme/a.git",
                              "https://git.example.test/Acme/b.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme",
                                              "host": "github.com"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_acme")

    def test_org_bad_declared_host(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"git": {"orgs": {"vendor": {"token": "GH_TOKEN_vendor",
                                                 "host": "h'$(x)'.test"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_vendor"})
        self.assertIn("is not a valid host", str(cm.exception))

    def test_org_empty_host_rejected(self):
        # A present-but-empty host: (or an explicit null) used to be silently
        # ignored (falsy → skipped entirely), leaving the owner to fall
        # through to "owner appears in no https repos:" with no hint that
        # host: was the field actually at fault.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"git": {"orgs": {"vendor": {"token": "GH_TOKEN_vendor",
                                                 "host": ""}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_vendor"})
        self.assertIn("host: must be a non-empty host", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            derive({"git": {"orgs": {"vendor": {"token": "GH_TOKEN_vendor",
                                                 "host": None}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_vendor"})
        self.assertIn("host: must be a non-empty host", str(cm.exception))

    def test_declared_host_installs_router(self):
        # A declared host: with no matching repos: URL never entered the
        # repos:-derived set — the entrypoint must still install the router
        # for it, or the row is never consulted.
        d = derive({"repos": ["https://github.com/x/y.git"],
                    "git": {"orgs": {"OrgB": {"token": "GH_TOKEN_orgb",
                                               "host": "gitea.example.test"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_orgb"})
        self.assertEqual(
            d["GIT_CREDENTIAL_HOSTS"],
            "https://gitea.example.test\nhttps://github.com\n")

    def test_unvalidated_scp_host_never_reaches_credential_hosts(self):
        # Direct injection probe: an scp-style repos: URL whose host segment
        # carries shell metacharacters. _ssh_repo_owners never validates that
        # host against HOST_RE (it's informational only), so it must never
        # reach GIT_HOST_TOKENS or GIT_CREDENTIAL_HOSTS — both of which
        # entrypoint.sh interpolates into shell.
        with self.assertRaises(m.ManifestError) as cm:
            derive({"repos": [{"name": "x", "url": "git@a';id;'b:acme/x.git"}],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertIn("listed only over scp-style/ssh:///git:// URLs", str(cm.exception))
        self.assertNotIn("';id;'", str(cm.exception))
        # With host: declared explicitly, derivation succeeds on the
        # declared host alone — the malicious scp host never surfaces
        # anywhere in the output.
        d = derive({"repos": [{"name": "x", "url": "git@a';id;'b:acme/x.git"}],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme",
                                               "host": "github.com"}}}},
                   env={"GH_TOKEN_VARS": "GH_TOKEN_acme"})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_acme")
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"], "https://github.com\n")
        self.assertNotIn("';id;'", d["GIT_HOST_TOKENS"])
        self.assertNotIn("';id;'", d["GIT_CREDENTIAL_HOSTS"])


class TestGitIdentity(unittest.TestCase):
    """git.token, git.orgs validation, and the implicit default row.

    GH_TOKEN_VARS mirrors the set up.sh scans from secrets.env (names only)."""
    ENV = {"GH_TOKEN_VARS": "GH_TOKEN_hank GH_TOKEN_vendor GH_TOKEN_v2"}

    def _d(self, git):
        return derive({"git": git}, env=dict(self.ENV))

    def test_absent_git_identity_keeps_the_implicit_default(self):
        # A manifest with none of git.hosts/git.token/git.orgs keeps today's
        # implicit row github.com=GH_TOKEN, and GIT_TOKEN_SOURCE stays empty
        # (GH_TOKEN keeps flowing straight from secrets.env).
        d = derive({})
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "")
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "")

    def test_default_token_source_becomes_the_cli_host_row(self):
        d = self._d({"token": "GH_TOKEN_hank"})
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "GH_TOKEN_hank")
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_hank")

    def test_default_token_missing_var_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"token": "GH_TOKEN_nope"})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.token: GH_TOKEN_nope not found in secrets.env")

    def test_default_token_invalid_var_name_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"token": "1TOKEN"})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.token: '1TOKEN' is not a valid env var name")

    def test_orgs_row_lands_on_declared_host(self):
        d = self._d({"orgs": {"vendor": {"token": "GH_TOKEN_vendor", "host": "github.com",
                                         "name": "Vendor Bot", "email": "bot@vendor.io"}}})
        # The org's token becomes the github.com row — and whatever row the
        # CLI host ends up with is what GH_TOKEN exports from, git.orgs-
        # claimed included, so git and gh never act as two identities on the
        # same host.
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "GH_TOKEN_vendor")
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_vendor")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "vendor\tVendor Bot\tbot@vendor.io\n")

    def test_orgs_cli_host_row_serves_two_owners(self):
        # Exactly the two-owner shape: acme's git.orgs token claims the CLI
        # host row; every owner on that host (acme AND other) now
        # authenticates with it, and GH_TOKEN exports from the same variable
        # — one identity for git and gh alike.
        d = derive({"repos": ["https://github.com/acme/a.git",
                              "https://github.com/other/b.git"],
                    "git": {"orgs": {"acme": {"token": "GH_TOKEN_acme"}}}},
                   env=dict(self.ENV, GH_TOKEN_VARS="GH_TOKEN_acme GH_TOKEN"))
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_acme")
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "GH_TOKEN_acme")

    def test_orgs_hyphenated_owner_is_a_plain_row_now(self):
        # Routing is host-keyed and the secrets.env variable name is used
        # exactly as written, so a hyphenated owner needs no sanitised
        # GH_TOKEN_<owner> alias — the token routes by host, not by owner.
        d = self._d({"orgs": {"acme-corp": {"token": "GH_TOKEN_v2", "host": "github.com"}}})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_v2")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "acme-corp\t\t\n")

    def test_orgs_owner_name_case_folds_for_attribution(self):
        # Attribution (not routing) folds to lowercase: a `PlanetExpress`
        # manifest key must still stamp the identity for `planetexpress/*`
        # clones, whose owner case we don't control.
        d = self._d({"orgs": {"PlanetExpress": {"token": "GH_TOKEN_v2", "host": "github.com",
                                                "name": "Leela Bot"}}})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_v2")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "planetexpress\tLeela Bot\t\n")

    def test_case_insensitive_duplicate_owner_hard_fails(self):
        # Owners are case-insensitive (attribution folds to lowercase), so two
        # keys differing only in case are an ambiguity — reject rather than
        # letting the last one win.
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": {"Acme": {"token": "GH_TOKEN_v2"},
                              "acme": {"token": "GH_TOKEN_vendor"}}})
        self.assertIn("git.orgs: duplicate owner 'acme' (case-insensitive clash "
                      "with 'Acme')", str(cm.exception))

    def test_dotted_and_underscored_owners_no_longer_clash(self):
        # No canonical GH_TOKEN_<owner> var exists any more, so 'a.b' and
        # 'a_b' are just two owners; only two entries on ONE HOST with
        # different tokens collide (see TestTokenRouting).
        d = self._d({"orgs": {"a.b": {"token": "GH_TOKEN_v2", "host": "h.test"},
                              "a_b": {"token": "GH_TOKEN_vendor", "host": "h2.test"}}})
        self.assertEqual(
            d["GIT_HOST_TOKENS"],
            "github.com=GH_TOKEN h.test=GH_TOKEN_v2 h2.test=GH_TOKEN_vendor")

    def test_org_missing_token_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": {"vendor": {"name": "Bot"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.orgs.vendor.token: needs token: (a secrets.env var name)")

    def test_org_token_missing_var_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": {"vendor": {"token": "GH_TOKEN_nope"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.orgs.vendor.token: GH_TOKEN_nope not found in secrets.env")

    def test_org_unsupported_field_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": {"vendor": {"token": "GH_TOKEN_vendor", "tokne": "x"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.orgs.vendor: unsupported field(s): tokne (only token, name, email, host)")

    def test_illegal_owner_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": {"-bad": {"token": "GH_TOKEN_vendor"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.orgs: illegal owner '-bad' (a forge org/user name)")

    def test_orgs_wrong_type_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": ["vendor"]})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.orgs: must be a map of <owner>: {token, name, email, host}")

    def test_errors_aggregate(self):
        # Both a bad default and a bad org surface together (aggregated list).
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"token": "GH_TOKEN_nope",
                     "orgs": {"vendor": {"token": "GH_TOKEN_alsonope"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.token: GH_TOKEN_nope not found in secrets.env\n"
            "  git.orgs.vendor.token: GH_TOKEN_alsonope not found in secrets.env")


class TestGitHosts(unittest.TestCase):
    """The git.hosts spelling: one table, one row per host, the secrets.env
    variable name exactly as written."""

    ENV = {"GH_TOKEN_VARS": "GH_TOKEN_fry GH_TOKEN_x"}

    def _d(self, git, repos=(), env=None):
        return derive({"repos": list(repos), "git": git},
                      env=dict(env if env is not None else self.ENV))

    def test_derivation_sorted_space_separated_pairs(self):
        d = self._d({"hosts": {"z.example.test": {"token": "GH_TOKEN_x"},
                               "a.example.test": {"token": "GH_TOKEN_fry"}}})
        self.assertEqual(
            d["GIT_HOST_TOKENS"],
            "a.example.test=GH_TOKEN_fry z.example.test=GH_TOKEN_x")

    def test_host_keys_normalise_case_and_443(self):
        d = self._d({"hosts": {"Git.Example.Test:443": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(d["GIT_HOST_TOKENS"], "git.example.test=GH_TOKEN_x")
        self.assertIn("https://git.example.test", d["GIT_CREDENTIAL_HOSTS"])

    def test_port_survives_normalisation(self):
        d = self._d({"hosts": {"git.example.test:3000": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(d["GIT_HOST_TOKENS"], "git.example.test:3000=GH_TOKEN_x")
        self.assertIn("https://git.example.test:3000", d["GIT_CREDENTIAL_HOSTS"])

    def test_any_secrets_env_variable_name_works(self):
        # token: names a secrets.env variable — ANY non-empty variable
        # secrets.env defines, not just the GH_TOKEN* scan (gitea tokens are
        # commonly named GITEA_*). Names only cross the boundary; values
        # never reach manifest.py.
        env = {"PRESENT_SECRET_VARS": "GH_TOKEN GITEA_TOKEN"}
        d = self._d({"hosts": {"git.example.org": {"token": "GITEA_TOKEN"}}}, env=env)
        self.assertEqual(d["GIT_HOST_TOKENS"], "git.example.org=GITEA_TOKEN")
        d = self._d({"orgs": {"emergence": {"token": "GITEA_TOKEN",
                                             "host": "git.example.org"}}}, env=env)
        self.assertIn("git.example.org=GITEA_TOKEN", d["GIT_HOST_TOKENS"])

    def test_absent_secrets_env_var_still_hard_fails(self):
        env = {"PRESENT_SECRET_VARS": "GH_TOKEN"}
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.org": {"token": "GITEA_TOKEN"}}}, env=env)
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts.git.example.org.token: GITEA_TOKEN not found in secrets.env")

    def test_unset_secret_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {"token": "GH_TOKEN_nope"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts.git.example.test.token: GH_TOKEN_nope not found in secrets.env")

    def test_invalid_var_name_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {"token": "1TOKEN"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts.git.example.test.token: '1TOKEN' is not a valid env var name")

    def test_malformed_host_hard_fails(self):
        for bad in ("bad_host!", "-lead.test", "h'$(x)'.test",
                    "two words.test", ""):
            with self.subTest(host=bad):
                with self.assertRaises(m.ManifestError) as cm:
                    self._d({"hosts": {bad: {"token": "GH_TOKEN_x"}}})
                self.assertIn("is not a valid host", str(cm.exception))

    def test_duplicate_normalised_host_hard_fails(self):
        # Two keys that normalise to the same host are an error even when the
        # tokens agree — the table is one row per host, and the ambiguity is
        # the author's to resolve.
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {"token": "GH_TOKEN_x"},
                               "Git.Example.Test:443": {"token": "GH_TOKEN_x"}}})
        self.assertIn("normalises to the same host as", str(cm.exception))

    def test_list_host_value_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": ["a", "b"]}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts.git.example.test: must be a map with token: (got a list)")

    def test_hosts_section_itself_as_a_list_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": ["git.example.test"]})
        self.assertIn("git.hosts: must be a map of <host>: {token} (got a list)",
                      str(cm.exception))

    def test_unknown_key_under_host_entry_hard_fails(self):
        # name/email became supported fields (per-host author attribution);
        # anything else is still rejected.
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {"token": "GH_TOKEN_x",
                                                    "tokne": "x"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts.git.example.test: unsupported field(s): tokne "
            "(only token, name, email)")

    def test_missing_token_under_host_entry_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts.git.example.test.token: needs token: (a secrets.env var name)")

    def test_mixed_spellings_hosts_plus_token_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {"token": "GH_TOKEN_x"}},
                     "token": "GH_TOKEN_fry"})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.hosts and git.token/git.orgs are both set — they are two "
            "spellings of one routing table; declare git.hosts only")

    def test_mixed_spellings_hosts_plus_orgs_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.test": {"token": "GH_TOKEN_x"}},
                     "orgs": {"vendor": {"token": "GH_TOKEN_vendor", "host": "github.com"}}})
        self.assertIn("declare git.hosts only", str(cm.exception))

    def test_empty_git_hosts_counts_as_undeclared(self):
        # An empty git.hosts map adds no rows and is not a mixed-spelling
        # declaration: git.token still works beside it.
        d = self._d({"hosts": {}, "token": "GH_TOKEN_fry"})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_fry")
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "GH_TOKEN_fry")

    def test_hosts_row_for_the_cli_host_claims_the_export(self):
        d = self._d({"hosts": {"github.com": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "GH_TOKEN_x")
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_x")

    def test_hosts_manifest_gets_exactly_its_rows(self):
        # The implicit default row belongs to manifests WITHOUT git.hosts: a
        # manifest that declares git.hosts gets exactly the rows it declares
        # — no CLI-host row, no https:// credential host for it (unless
        # repos: names it). GH_TOKEN still flows to key files via
        # GIT_TOKEN_SOURCE="" (a later change decides that).
        d = self._d({"hosts": {"git.example.test": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "")
        self.assertEqual(d["GIT_HOST_TOKENS"], "git.example.test=GH_TOKEN_x")
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"], "https://git.example.test\n")

    def test_implicit_row_only_without_git_hosts(self):
        # A manifest WITHOUT git.hosts keeps today's behaviour: the CLI host
        # gets the implicit GH_TOKEN row unless an old spelling already gave
        # it one.
        self.assertEqual(self._d({})["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")
        d = self._d({"token": "GH_TOKEN_fry"})
        self.assertEqual(d["GIT_HOST_TOKENS"], "github.com=GH_TOKEN_fry")
        d = self._d({"orgs": {"emergence": {"token": "GH_TOKEN_x",
                                             "host": "git.example.test"}}})
        self.assertEqual(
            d["GIT_HOST_TOKENS"],
            "git.example.test=GH_TOKEN_x github.com=GH_TOKEN")
        # git.hosts: {} adds no rows and is not a declaration of hosts.
        self.assertEqual(self._d({"hosts": {}})["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")

    def test_token_and_hosts_spellings_derive_identically(self):
        # Guard: git.token: X and git.hosts.github.com.token: X are the same
        # table row — byte-identical derived output.
        a = self._d({"token": "GH_TOKEN_x"})
        b = self._d({"hosts": {"github.com": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(a, b)

    # ── per-host author attribution (GIT_HOST_IDENTITIES) ────────────────

    def test_host_name_and_email_derive_host_identities(self):
        d = self._d({"hosts": {"git.example.org": {"token": "GH_TOKEN_x",
                                                   "name": "Leela Bot",
                                                   "email": "bot@planetexpress.example"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"],
                         "git.example.org\tLeela Bot\tbot@planetexpress.example\n")
        # The token row is unaffected.
        self.assertEqual(d["GIT_HOST_TOKENS"], "git.example.org=GH_TOKEN_x")

    def test_host_identities_sorted_by_normalised_host(self):
        d = self._d({"hosts": {"zeta.test": {"token": "GH_TOKEN_x", "name": "Z"},
                               "Alpha.Test:443": {"token": "GH_TOKEN_x",
                                                  "name": "A"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"],
                         "alpha.test\tA\t\nzeta.test\tZ\t\n")

    def test_host_name_without_email_derives_empty_email_field(self):
        # Same "either may be given alone" behaviour as the per-owner
        # git.orgs.<owner>.name/email: whichever is absent derives as empty.
        d = self._d({"hosts": {"git.example.org": {"token": "GH_TOKEN_x",
                                                   "name": "Leela Bot"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"], "git.example.org\tLeela Bot\t\n")
        d = self._d({"hosts": {"git.example.org": {"token": "GH_TOKEN_x",
                                                   "email": "bot@planetexpress.example"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"],
                         "git.example.org\t\tbot@planetexpress.example\n")

    def test_host_entry_without_author_derives_no_identity_record(self):
        d = self._d({"hosts": {"git.example.org": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"], "")

    def test_no_author_under_any_host_derives_empty_identities_and_unchanged_keys(self):
        d = self._d({"hosts": {"git.example.org": {"token": "GH_TOKEN_x"},
                               "other.test": {"token": "GH_TOKEN_x"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"], "")
        self.assertEqual(d["GIT_HOST_TOKENS"],
                         "git.example.org=GH_TOKEN_x other.test=GH_TOKEN_x")
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "")
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"],
                         "https://git.example.org\nhttps://other.test\n")

    def test_host_author_without_token_is_attribution_only(self):
        # A name/email with no token: is allowed — it records the author and
        # adds NO row to GIT_HOST_TOKENS (no credential exists for the host;
        # private clones fail loudly at the router, as for any row-less host).
        d = self._d({"hosts": {"git.example.org": {"name": "Leela Bot",
                                                   "email": "bot@planetexpress.example"}}})
        self.assertEqual(d["GIT_HOST_IDENTITIES"],
                         "git.example.org\tLeela Bot\tbot@planetexpress.example\n")
        self.assertEqual(d["GIT_HOST_TOKENS"], "")
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "")

    def test_host_author_without_token_and_without_repos_adds_no_credential_host(self):
        # No token row → the host never enters GIT_CREDENTIAL_HOSTS on its
        # own (the router is installed per table row or repos: origin).
        d = self._d({"hosts": {"git.example.org": {"name": "Leela Bot"}}})
        self.assertEqual(d["GIT_CREDENTIAL_HOSTS"], "")

    def test_host_author_with_bad_token_var_still_hard_fails(self):
        # Optional token: is optional, not unvalidated: when it IS given it
        # must still name a set secrets.env variable.
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"hosts": {"git.example.org": {"name": "Leela Bot",
                                                   "token": "GH_TOKEN_nope"}}})
        self.assertIn("GH_TOKEN_nope not found in secrets.env", str(cm.exception))

    def test_declared_nothing_matches_the_implicit_output(self):
        a = derive({})
        self.assertEqual(a["GIT_HOST_TOKENS"], "github.com=GH_TOKEN")
        self.assertEqual(a["GIT_TOKEN_SOURCE"], "")


class TestDerivedValues(unittest.TestCase):
    def test_defaults_on_empty_manifest(self):
        d = derive({})
        self.assertEqual(d["FORGE"], "github")
        self.assertEqual(d["MEM_LIMIT"], "2g")
        self.assertEqual(d["SSH_BIND"], "127.0.0.1")
        self.assertIn("claude", d["AGENTS_ENABLED"].split())
        mcp_agents = json.loads(d["AGENTS_MCP_JSON"])
        self.assertTrue(any(a["binary"] == "claude" for a in mcp_agents))
        self.assertEqual(d["EGRESS"], "")
        self.assertEqual(d["PLUGIN_MCP_ENTRIES"], "")

    def test_git_fallbacks_from_env(self):
        d = derive({}, env=dict(ENV, GIT_NAME_DEFAULT="N", GIT_EMAIL_DEFAULT="e@x"))
        self.assertEqual(d["GIT_USER_NAME"], "N")
        self.assertEqual(d["GIT_USER_EMAIL"], "e@x")
        d = derive({"git": {"name": "M"}}, env=dict(ENV, GIT_NAME_DEFAULT="N"))
        self.assertEqual(d["GIT_USER_NAME"], "M")

    def test_host_mcp_ports_from_plugin_host_port_sorted(self):
        # HOST_MCP_PORTS folds every enabled plugin's host_port, numerically
        # sorted so the firewall grant is independent of plugin list order.
        d = derive({"plugins": ["browser", "gateway"]})
        self.assertEqual(d["HOST_MCP_PORTS"], "8811,8814,8816")
        self.assertEqual(d["ENABLE_EGRESS_BROKER"], "true")
        self.assertEqual(derive({"plugins": ["serena"]})["HOST_MCP_PORTS"], "8816")

    def test_host_mcp_ports_omit_broker_when_disabled(self):
        d = derive({"capabilities": {"egress_broker": False}})
        self.assertEqual(d["ENABLE_EGRESS_BROKER"], "false")
        self.assertEqual(d["HOST_MCP_PORTS"], "")
        d2 = derive({"plugins": ["browser"], "capabilities": {"egress_broker": False}})
        self.assertEqual(d2["HOST_MCP_PORTS"], "8814")

    def test_obsidian_identity_sugar_folds_egress_and_binds(self):
        # identities: sugar enables the obsidian-annotated plugin (whose egress
        # folds in) and produces an agent_secrets binding for the ref's agent.
        d = derive({"identities": {"obsidian": ["me_claude"]}})
        self.assertEqual(d["EGRESS"], "mcp-obsidian.dmetr.io")
        self.assertEqual(d["AGENT_SECRETS"],
                         "claude\tOBSIDIAN_ANNOTATED_KEY\tOBSIDIAN_KEY_me_claude\n")
        self.assertIn("obsidian-annotated", d["PLUGINS"])

    def test_plugin_egress_folds_with_literal_dedup(self):
        files = {"p": {"egress": ["api.foo.com"]}}
        d = derive({"capabilities": {"egress": ["api-foo.com"]}, "plugins": ["p"]},
                   plugin_files=files)
        # api-foo.com must NOT swallow api.foo.com (the old regex-dot bug)
        self.assertEqual(d["EGRESS"], "api-foo.com,api.foo.com")
        d2 = derive({"capabilities": {"egress": ["api.foo.com"]}, "plugins": ["p"]},
                    plugin_files=files)
        self.assertEqual(d2["EGRESS"], "api.foo.com")

    def test_plugin_mcp_entries_one_line_json_per_plugin(self):
        d = derive({"plugins": ["serena", "other"]})
        lines = d["PLUGIN_MCP_ENTRIES"].splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), SERENA["mcp"])
        self.assertEqual(json.loads(lines[1]), OTHER["mcp"])
        self.assertTrue(d["PLUGIN_MCP_ENTRIES"].endswith("\n"))

    def test_ntfy_host_strip_order_path_before_userinfo(self):
        env = dict(ENV, NTFY_URL="https://ntfy.example.com/a@b", NTFY_TOPIC="t")
        d = derive({"remote": {"shell": "tmux", "notify": "ntfy"}}, env=env)
        # '@' in the PATH must not masquerade as userinfo
        self.assertIn("ntfy.example.com", d["EGRESS"].split(","))
        self.assertEqual(d["CONTAINER_NTFY_URL"], "https://ntfy.example.com/a@b")
        self.assertEqual(d["CONTAINER_NTFY_TOPIC"], "t")

    def test_ntfy_userinfo_and_port_stripped(self):
        env = dict(ENV, NTFY_URL="https://user@h.example.com:8443")
        d = derive({"remote": {"shell": "tmux", "notify": "ntfy"}}, env=env)
        self.assertIn("h.example.com", d["EGRESS"].split(","))

    def test_ntfy_ip_literal_goes_to_cidrs(self):
        env = dict(ENV, NTFY_URL="http://10.1.2.3:8080/p")
        d = derive({"remote": {"shell": "tmux", "notify": "ntfy"},
                    "capabilities": {"egress_cidrs": ["10.1.2.3/32"]}}, env=env)
        self.assertEqual(d["EGRESS_CIDRS"], "10.1.2.3/32")  # deduped
        self.assertNotIn("10.1.2.3", d["EGRESS"])


class TestRenderAndStdin(unittest.TestCase):
    def test_render_shell_quoting_round_trips(self):
        d = m.Derived({"A": "plain", "B": "has space", "C": "it's; $HOME `x`"})
        rendered = d.render()
        out = subprocess.run(
            ["bash", "-c", rendered + 'printf "%s|%s|%s" "$A" "$B" "$C"'],
            capture_output=True, text=True)
        self.assertEqual(out.stdout, "plain|has space|it's; $HOME `x`")

    def test_read_stdin_docs(self):
        stream = io.StringIO(
            derive_stdin(
                {"plugins": ["p"]},
                plugin_files={"p": {"mcp": {}}},
                agent_files={"aider": AGENT_FILES["aider"]},
            )
        )
        man, files, agents = m.read_stdin_docs(stream)
        self.assertEqual(man, {"plugins": ["p"]})
        self.assertEqual(files, {"p": {"mcp": {}}})
        self.assertEqual(agents, {"aider": AGENT_FILES["aider"]})

    def test_read_stdin_null_manifest_is_empty(self):
        man, files, agents = m.read_stdin_docs(
            io.StringIO(derive_stdin(None, plugin_files={}, agent_files={"aider": AGENT_FILES["aider"]}))
        )
        self.assertEqual(man, {})
        self.assertEqual(files, {})
        self.assertEqual(agents, {"aider": AGENT_FILES["aider"]})

    def test_read_stdin_errors(self):
        with self.assertRaises(m.ManifestError):
            m.read_stdin_docs(io.StringIO(""))
        with self.assertRaises(m.ManifestError):
            m.read_stdin_docs(io.StringIO("{bad\n"))
        with self.assertRaises(m.ManifestError):
            m.read_stdin_docs(io.StringIO('{}\nno-tab-here\n'))
        with self.assertRaises(m.ManifestError) as cm:
            m.read_stdin_docs(io.StringIO('{}\na\t{}\n'))
        self.assertIn("agents section missing", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            m.read_stdin_docs(io.StringIO('{}\n---agents---\n'))
        self.assertIn("agents section is empty", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            m.read_stdin_docs(io.StringIO('{}\n---agents---\naider\t!\n'))
        self.assertIn("agent file 'aider' is unreadable", str(cm.exception))

    def test_main_derive_end_to_end(self):
        out = subprocess.run(
            [sys.executable, str(MODULE), "--derive"],
            input=derive_stdin({"memory": "3g"}), capture_output=True, text=True,
            env={"SECRETS_FILE": "/s", "PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 0)
        self.assertIn("MEM_LIMIT=3g\n", out.stdout)

    def test_main_error_goes_to_stderr_exit_1(self):
        out = subprocess.run(
            [sys.executable, str(MODULE), "--derive"],
            input=derive_stdin({"forge": "bad"}), capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")
        self.assertIn("Error: forge must be github or gitea", out.stderr)


class TestAgentDescriptorDerivation(unittest.TestCase):
    def test_unknown_agent_descriptor_key_errors(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], nope=True)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("agent 'claude': unsupported field(s): nope", str(cm.exception))

    def test_unknown_agent_mcp_key_errors(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["mcp"] = dict(AGENT_FILES["claude"]["mcp"], nope=True)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("agent 'claude' mcp: unsupported field(s): nope", str(cm.exception))

    def test_non_strategy_literal_dialect_with_env_refs_true_rejected(self):
        agents = dict(AGENT_FILES)
        agents["cursor"] = dict(AGENT_FILES["cursor"])
        agents["cursor"]["mcp"] = dict(AGENT_FILES["cursor"]["mcp"], env_refs=True)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("literal-key rendering", str(cm.exception))

    def test_config_settings_land_in_the_agent_mcp_payload(self):
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(
            AGENT_FILES["codex"],
            config_settings={"sandbox_mode": "danger-full-access", "quiet": True, "n": 3},
        )
        d = derive({"agents": ["codex"]}, agent_files=agents)
        entry = json.loads(d["AGENTS_MCP_JSON"])[0]
        self.assertEqual(
            entry["settings"], {"sandbox_mode": "danger-full-access", "quiet": True, "n": 3})

    def test_agents_without_config_settings_carry_an_empty_map(self):
        d = derive({"agents": ["codex"]}, agent_files=AGENT_FILES)
        self.assertEqual(json.loads(d["AGENTS_MCP_JSON"])[0]["settings"], {})

    def test_config_settings_require_the_codex_strategy(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], config_settings={"a": 1})
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("config_settings requires mcp.strategy codex_managed_block",
                      str(cm.exception))

    def test_config_settings_reject_non_bare_toml_key(self):
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"], config_settings={"not a key": 1})
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("must match [A-Za-z0-9_-]+", str(cm.exception))

    def test_config_settings_reject_non_scalar_value(self):
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"], config_settings={"a": {"b": 1}})
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("must be a string, boolean, or integer", str(cm.exception))

    def test_config_settings_must_be_a_map(self):
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"], config_settings=["a"])
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("config_settings must be a map", str(cm.exception))

    def test_unknown_agent_name_warns_and_is_dropped(self):
        """A manifest naming a retired agent still derives — with a warning, so
        the missing CLI is self-diagnosing rather than silent."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"agents": ["claude", "retired-agent"]}, agent_files=AGENT_FILES)
        self.assertEqual(d["AGENTS_ENABLED"], "claude")
        self.assertIn("agents: 'retired-agent' has no agents/retired-agent/ directory",
                      err.getvalue())

    def test_serverurl_dialect_still_accepted(self):
        """serverUrl (agy) stays a valid descriptor dialect with env_refs false
        and travels in AGENTS_MCP_JSON. Nothing renders from it any more — the
        agents it described take the mcp-remote shim — but descriptors are not
        being churned in the same change."""
        agents = dict(AGENT_FILES)
        agents["cursor"] = dict(AGENT_FILES["cursor"])
        agents["cursor"]["mcp"] = dict(AGENT_FILES["cursor"]["mcp"], dialect="serverUrl")
        d = derive({"agents": ["cursor"]}, agent_files=agents)
        self.assertIn('"dialect":"serverUrl"', d["AGENTS_MCP_JSON"])

    def test_serverurl_dialect_with_env_refs_true_rejected(self):
        agents = dict(AGENT_FILES)
        agents["cursor"] = dict(AGENT_FILES["cursor"])
        agents["cursor"]["mcp"] = dict(
            AGENT_FILES["cursor"]["mcp"], dialect="serverUrl", env_refs=True)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("literal-key rendering", str(cm.exception))

    def test_non_strategy_toml_rejected(self):
        agents = dict(AGENT_FILES)
        agents["cursor"] = dict(AGENT_FILES["cursor"])
        agents["cursor"]["mcp"] = dict(AGENT_FILES["cursor"]["mcp"], format="toml")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("non-strategy wiring requires format json", str(cm.exception))

    def test_non_strategy_json_without_dialect_rejected(self):
        agents = dict(AGENT_FILES)
        agents["cursor"] = dict(AGENT_FILES["cursor"])
        mcp_doc = dict(AGENT_FILES["cursor"]["mcp"])
        del mcp_doc["dialect"]
        agents["cursor"]["mcp"] = mcp_doc
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("non-strategy wiring requires a dialect", str(cm.exception))

    def test_non_strategy_mcpservers_requires_truthy_env_refs(self):
        agents = dict(AGENT_FILES)
        agents["cursor"] = dict(AGENT_FILES["cursor"])
        agents["cursor"]["mcp"] = dict(
            AGENT_FILES["cursor"]["mcp"], dialect="mcpServers", env_refs=False)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("generic-config agent is by definition ref-style", str(cm.exception))

    def test_claude_preapprove_requires_workspace_mcp_path(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["mcp"] = dict(AGENT_FILES["claude"]["mcp"], config_path=".claude/mcp.json")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("strategy claude_preapprove requires config_path '.mcp.json'", str(cm.exception))

    def test_claude_preapprove_requires_truthy_env_refs(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["mcp"] = dict(AGENT_FILES["claude"]["mcp"], env_refs=False)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("requires format json and truthy env_refs", str(cm.exception))

    def test_codex_managed_block_requires_toml(self):
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"])
        agents["codex"]["mcp"] = dict(AGENT_FILES["codex"]["mcp"], format="json")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("strategy codex_managed_block requires format toml", str(cm.exception))

    def test_codex_managed_block_rejects_bare_bool_env_refs(self):
        """codex_managed_block's TOML block has no ${VAR} header expansion, so
        env_refs: true (bare bool ref-passthrough) can never actually be
        rendered — it must be caught here, not silently dropped at wire time."""
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"])
        agents["codex"]["mcp"] = dict(AGENT_FILES["codex"]["mcp"], env_refs=True)
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("only accepts env_refs: false or env_refs: bearer_token_env_var",
                       str(cm.exception))

    def test_codex_managed_block_rejects_unknown_env_refs_string(self):
        """_codex_block_body only ever renders the bearer_token_env_var field —
        any other string (a typo, or an unrelated field name like the spec's
        own 'url' key) must be rejected here, not accepted and silently
        rendered wrong (an overwritten field, or a dropped credential)."""
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"])
        agents["codex"]["mcp"] = dict(
            AGENT_FILES["codex"]["mcp"], env_refs="bearer_token_env")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("only accepts env_refs: false or env_refs: bearer_token_env_var",
                       str(cm.exception))

    def test_duplicate_mcp_binary_rejected(self):
        agents = dict(AGENT_FILES)
        agents["aider"] = dict(AGENT_FILES["aider"], binary="claude")
        agents["aider"]["mcp"] = {
            "config_path": ".aider/mcp.json",
            "format": "json",
            "dialect": "url",
            "env_refs": False,
        }
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("already used by mcp-capable agent", str(cm.exception))

    def test_duplicate_mcp_config_path_rejected(self):
        agents = dict(AGENT_FILES)
        agents["aider"] = dict(AGENT_FILES["aider"], binary="aider2")
        agents["aider"]["mcp"] = {
            "config_path": ".mcp.json",
            "format": "json",
            "dialect": "url",
            "env_refs": False,
        }
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("mcp.config_path '.mcp.json': already used by agent", str(cm.exception))

    def test_traversal_mcp_config_path_rejected(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["mcp"] = dict(AGENT_FILES["claude"]["mcp"], config_path="../x")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("illegal component '..'", str(cm.exception))

    def test_absolute_agent_state_path_is_rejected(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["state_dirs"] = [{"path": "/.claude", "volume": "claude-auth"}]
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("must be home-relative (no leading /)", str(cm.exception))

    def test_duplicate_agent_state_volume_rejected(self):
        agents = dict(AGENT_FILES)
        agents["codex"] = dict(AGENT_FILES["codex"])
        agents["codex"]["state_dirs"] = [{"path": ".codex", "volume": "claude-auth"}]
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("already declared by agent 'claude'", str(cm.exception))

    def test_agents_enabled_and_shim_agents_subset(self):
        d = derive({"agents": ["claude", "aider"]})
        self.assertEqual(d["AGENTS_ENABLED"], "aider claude")
        self.assertEqual(d["SHIM_AGENTS"], "claude")

    def test_agents_compose_yaml_full_rendering(self):
        d = derive({})
        self.assertEqual(
            d["AGENTS_COMPOSE_YAML"],
            "\n".join([
                "# GENERATED by src/manifest.py — do not edit; ./up.sh rewrites it.",
                "# Named volumes for enabled agents' auth/state directories",
                "services:",
                "  djinn:",
                "    volumes:",
                "      - claude-auth:/home/coder/.claude",
                "      - codex-auth:/home/coder/.codex",
                "      - cursor-auth:/home/coder/.config/cursor",
                "      - cursor-state:/home/coder/.cursor",
                "      - gemini-state:/home/coder/.gemini",
                "volumes:",
                "  claude-auth:",
                "  codex-auth:",
                "  cursor-auth:",
                "  cursor-state:",
                "  gemini-state:",
            ])
        )

    def test_agents_compose_yaml_empty_when_no_state_dirs_enabled(self):
        d = derive({"agents": ["aider", "pi"]})
        self.assertEqual(d["AGENTS_COMPOSE_YAML"], "")

    def test_traversal_rules_file_rejected(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], rules_file="../../tmp/owned")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("illegal component '..'", str(cm.exception))

    def test_traversal_state_path_rejected(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["state_dirs"] = [{"path": "../etc", "volume": "claude-auth"}]
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("illegal component '..'", str(cm.exception))

    def test_tab_in_rules_file_rejected(self):
        # These fields are flattened into the build-time agents-index.tsv — a
        # tab or newline in one would corrupt every downstream runtime parse.
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], rules_file=".claude/CLAUDE.md\tx")
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("illegal component", str(cm.exception))

    def test_agent_agent_mount_overlap_rejected(self):
        agents = dict(AGENT_FILES)
        agents["foo"] = {"binary": "foo", "install": "true",
                         "state_dirs": [{"path": ".claude", "volume": "foo-auth"}]}
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("overlaps", str(cm.exception))
        self.assertIn("/home/coder/.claude", str(cm.exception))

    def test_illegal_agent_volume_name_rejected(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"])
        agents["claude"]["state_dirs"] = [{"path": ".claude", "volume": "bad\tvol"}]
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("illegal volume name", str(cm.exception))

    def test_illegal_agent_binary_rejected(self):
        for bad in ("../evil", "a b", "/usr/bin/x"):
            agents = dict(AGENT_FILES)
            agents["claude"] = dict(AGENT_FILES["claude"], binary=bad)
            with self.assertRaises(m.ManifestError) as cm:
                derive({}, agent_files=agents)
            self.assertIn("not a bare command name", str(cm.exception))

    def test_agent_egress_folds_into_egress_for_enabled_agents_only(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], egress=["api.anthropic.com"])
        d = derive({}, agent_files=agents)
        self.assertIn("api.anthropic.com", d["EGRESS"].split(","))
        d = derive({"agents": ["aider"]}, agent_files=agents)
        self.assertNotIn("api.anthropic.com", d["EGRESS"].split(","))

    def test_agent_egress_rejects_non_hostname(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], egress=["https://api.anthropic.com"])
        with self.assertRaises(m.ManifestError) as cm:
            derive({}, agent_files=agents)
        self.assertIn("agent 'claude' egress entry", str(cm.exception))

    def test_agent_plugin_volume_name_collision(self):
        files = {"p": {"volumes": {"claude-auth": "/home/coder/cache"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]}, plugin_files=files)
        self.assertIn("that name is already a compose volume", str(cm.exception))

    def test_agent_plugin_path_overlap_collision(self):
        files = {"p": {"volumes": {"p-cache": "/home/coder/.cursor/cache"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]}, plugin_files=files)
        self.assertIn("collides with the compose mount '/home/coder/.cursor'", str(cm.exception))


class TestReviewFixes(unittest.TestCase):
    """Pins for the code-review findings on this port."""

    def test_trailing_newline_rejected_by_all_validators(self):
        # Python's $ matches before a trailing \n; the port must use \Z.
        files = {"p": {"egress": ["evil.com\n"]}}
        with self.assertRaises(m.ManifestError):
            derive({"plugins": ["p"]}, plugin_files=files)
        files = {"p": {"mcp": {"srv\n": {"command": "x"}}}}
        with self.assertRaises(m.ManifestError):
            derive({"plugins": ["p"]}, plugin_files=files)

    def test_null_entries_drop_from_word_lists(self):
        # plugins: [serena,] parses as [serena, null]; old join+word-split
        # dropped the null — a working manifest must keep working.
        d = derive({"plugins": ["serena", None]})
        self.assertEqual(d["PLUGINS"], "serena")
        # identity refs run through the same _word_list; a trailing-comma null
        # vanishes, leaving a single binding.
        d = derive({"identities": {"obsidian": ["me_claude", None]}})
        self.assertEqual(d["AGENT_SECRETS"],
                         "claude\tOBSIDIAN_ANNOTATED_KEY\tOBSIDIAN_KEY_me_claude\n")

    def test_null_entries_keep_slots_in_comma_lists(self):
        # egress was comma-joined with no word split: empty slots survived.
        d = derive({"capabilities": {"egress": ["a.com", None]}})
        self.assertEqual(d["EGRESS"], "a.com,")

    def test_wrong_typed_sections_are_named_errors(self):
        for key in ("git", "capabilities", "ssh", "remote", "identities"):
            with self.subTest(key):
                with self.assertRaises(m.ManifestError) as cm:
                    derive({key: [{"x": 1}]})
                self.assertIn(f"manifest {key}: must be a map", str(cm.exception))

    def test_sequence_root_plugin_file_is_named_error(self):
        files = {"p": [{"mcp": {"srv": {"command": "x"}}}]}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]}, plugin_files=files)
        self.assertIn("plugins/p/plugin.yml must be a YAML map", str(cm.exception))

    def test_empty_plugin_file_is_valid_noop(self):
        d = derive({"plugins": ["p"]}, plugin_files={"p": None})
        self.assertEqual(d["PLUGIN_MCP_ENTRIES"], "")

    def test_unreadable_plugin_errors_only_when_listed(self):
        files = {"good": {"mcp": {}}, "broken": m.UNREADABLE}
        d = derive({"plugins": ["good"]}, plugin_files=files)  # no error
        self.assertEqual(d["PLUGINS"], "good")
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["broken"]}, plugin_files=files)
        self.assertIn("plugins/broken/plugin.yml is not valid YAML", str(cm.exception))

    def test_non_scalar_leaf_is_named_error(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"memory": ["2g"]})
        self.assertIn("memory must be a single value", str(cm.exception))

    def test_no_reserved_server_names(self):
        # As of Phase 2 nothing is reserved — every MCP server comes from a
        # plugin file. A plugin may legitimately define coding/proxyman/browser
        # AND obsidian-annotated; only cross-plugin duplicates are rejected.
        self.assertFalse(hasattr(wire_plugins, "RESERVED_SERVER_NAMES"))
        for name in ("coding", "proxyman", "browser", "obsidian-annotated"):
            with self.subTest(name):
                files = {"p": {"host_port": 9999,
                               "mcp": {name: {"url": "http://host.docker.internal:9999/mcp"}}}}
                d = derive({"plugins": ["p"]}, plugin_files=files)
                self.assertEqual(json.loads(d["PLUGIN_MCP_ENTRIES"].strip()),
                                 files["p"]["mcp"])

    def test_stdin_unreadable_sentinel_and_multidoc_hint(self):
        man, files, agents = m.read_stdin_docs(io.StringIO(derive_stdin({}, plugin_files={"broken": m.UNREADABLE})))
        self.assertIs(files["broken"], m.UNREADABLE)
        self.assertEqual(agents["aider"]["binary"], "aider")
        with self.assertRaises(m.ManifestError) as cm:
            m.read_stdin_docs(io.StringIO('{}\n{"second": "doc"}\n'))
        self.assertIn("stray '---'", str(cm.exception))


class TestHybridSchemaRules(unittest.TestCase):
    """Plugin-shape and secret-binding validation under the unified hybrid
    schema. Re-expresses the still-valid rules that the retired Phase1/Phase2
    scope-based classes used to cover (install-iff-local, host_port, duplicate
    slots, agent_secrets validation, inert warnings, capabilities: sugar)."""

    def _d(self, man, files=PLUGIN_FILES, env=None):
        return m.derive(man, files, AGENT_FILES, ENV if env is None else env)

    # ── plugin shape: install-iff-local, host_port ───────────────────────
    def test_install_required_only_for_local_servers(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["p"]}, files={"p": {"mcp": {"s": {"command": "x"}}}})
        self.assertIn("needs an install: block", str(cm.exception))
        # remote server needs no install:
        self._d({"plugins": ["p"]},
                files={"p": {"host_port": 9000,
                             "mcp": {"s": {"url": "http://host.docker.internal:9000/mcp"}}}})
        # egress-only plugin (no mcp) needs no install: either
        self._d({"plugins": ["p"]}, files={"p": {"egress": ["a.com"]}})

    def test_local_agent_scoped_server_allowed_and_needs_install(self):
        # axiom's mcp-remote bridge: a LOCAL command server with requires: is
        # valid and routes per-agent (into servers_by_name), but still needs an
        # install: block like any local server.
        good = {"install": "x", "secrets": {"A": {}},
                "mcp": {"s": {"command": "bash", "requires": ["A"]}}}
        d = self._d({"plugins": ["bad"], "common_secrets": ["A"]},
                    files=dict(PLUGIN_FILES, bad=good),
                    env={"PRESENT_SECRET_VARS": "A", "SECRETS_FILE": "/sec/secrets.env"})
        servers = json.loads(d["AGENT_SERVERS_JSON"])
        self.assertEqual(servers["s"]["spec"], {"command": "bash"})
        self.assertIn("claude\tA\tA\n", d["AGENT_SECRETS"])
        # a local server with no install: is still rejected
        bad = {"secrets": {"A": {}}, "mcp": {"s": {"command": "bash", "requires": ["A"]}}}
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["bad"]}, files=dict(PLUGIN_FILES, bad=bad))
        self.assertIn("needs an install: block", str(cm.exception))

    def test_host_port_needs_a_server_and_integer(self):
        # a LOCAL bridge that dials the host may declare host_port (rhinomcp)
        # — but only when a ${HOST_PORT} ref shows the bridge takes the port
        d = self._d({"plugins": ["p"]},
                    files={"p": {"install": "x", "host_port": 1999,
                                 "mcp": {"s": {"command": "bash",
                                               "args": ["-c", "P=${HOST_PORT} exec b"]}}}})
        self.assertEqual(d["HOST_MCP_PORTS"], "1999,8816")
        # ...a local server that never references the port would leave the
        # firewall grant pointing at a port nothing dials (and a plugin_ports:
        # override would move the grant but not the dial target)
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["p"]},
                    files={"p": {"install": "x", "host_port": 1999,
                                 "mcp": {"s": {"command": "x"}}}})
        self.assertIn("needs a ${HOST_PORT} reference", str(cm.exception))
        # ...and with no mcp server at all there is nothing to use the grant
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["p"]},
                    files={"p": {"host_port": 8811, "egress": ["a.com"]}})
        self.assertIn("host_port needs an mcp server", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["p"]},
                    files={"p": {"host_port": "8811", "mcp": {"s": {"url": "http://h/mcp"}}}})
        self.assertIn("host_port must be an integer", str(cm.exception))
        # out-of-range (typo like 88111) is a named error, not a bogus grant
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["p"]},
                    files={"p": {"host_port": 88111, "mcp": {"s": {"url": "http://h/mcp"}}}})
        self.assertIn("out of range (1-65535)", str(cm.exception))

    def test_duplicate_secret_slot_across_plugins(self):
        files = {"a": {"secrets": {"TOK": {}}}, "b": {"secrets": {"TOK": {}}}}
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["a", "b"]}, files=files)
        self.assertIn("declared by more than one enabled plugin", str(cm.exception))

    # ── common_secrets: remap + unknown slot ─────────────────────────────
    def test_common_secrets_map_repoints_slot_into_agent_records(self):
        d = self._d({"plugins": ["gateway"], "common_secrets": {"MCP_GATEWAY_TOKEN": "GW_PROD"}},
                    env={"PRESENT_SECRET_VARS": "GW_PROD", "SECRETS_FILE": "/sec/secrets.env"})
        self.assertIn("claude\tMCP_GATEWAY_TOKEN\tGW_PROD\n", d["AGENT_SECRETS"])

    def test_common_secrets_unknown_slot_errors(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["serena"], "common_secrets": {"NOPE": "SRC"}},
                    env={"PRESENT_SECRET_VARS": "SRC", "SECRETS_FILE": "/sec/secrets.env"})
        self.assertIn("no enabled plugin declares that secret slot", str(cm.exception))

    # ── agent_secrets validation ─────────────────────────────────────────
    def test_unknown_agent_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["obsidian-annotated"],
                     "agent_secrets": [{"agent": "nope", "slot": "OBSIDIAN_ANNOTATED_KEY",
                                        "secret": "OBSIDIAN_KEY_me_claude"}]})
        self.assertIn("unknown agent 'nope'", str(cm.exception))

    def test_agent_secrets_slot_unknown_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["gateway"],
                     "agent_secrets": [{"agent": "claude", "slot": "NOPE",
                                        "secret": "OBSIDIAN_KEY_me_claude"}]})
        self.assertIn("not a secret of any enabled plugin", str(cm.exception))

    def test_agent_secret_source_missing(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["obsidian-annotated"],
                     "agent_secrets": [{"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY",
                                        "secret": "OBSIDIAN_KEY_gone"}]})
        self.assertIn("not found in /sec/secrets.env", str(cm.exception))

    def test_duplicate_agent_slot_binding_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["obsidian-annotated"],
                     "agent_secrets": [
                         {"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_me_claude"},
                         {"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_me_claude"}]})
        self.assertIn("more than once", str(cm.exception))

    def test_enabled_plugin_without_binding_warns_inert(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = self._d({"plugins": ["obsidian-annotated"]})   # no default, no override
        self.assertEqual(d["AGENT_SECRETS"], "")
        self.assertIn("inert (wired for no agent)", err.getvalue())
        self.assertIn("OBSIDIAN_ANNOTATED_KEY", err.getvalue())

    def test_watch_is_env_only_no_server(self):
        d = self._d({"plugins": ["annotated-watch"],
                     "agent_secrets": [{"agent": "pi", "slot": "ANNOTATED_WATCH_KEY",
                                        "secret": "OBSIDIAN_WATCH_KEY_w_pi"}]})
        self.assertEqual(d["AGENT_SERVER_SLOTS"], "")        # no server
        self.assertEqual(d["AGENT_SERVERS_JSON"], "{}")
        self.assertEqual(d["AGENT_SECRETS"],
                         "pi\tANNOTATED_WATCH_KEY\tOBSIDIAN_WATCH_KEY_w_pi\n")

    # ── capabilities: sugar (deprecated but still processed) ─────────────
    def test_capabilities_sugar_dedups_with_explicit_plugin(self):
        d = self._d({"plugins": ["gateway"], "capabilities": {"gateway": True}})
        self.assertEqual(d["PLUGINS"], "gateway")            # not "gateway gateway"
        self.assertEqual(d["PLUGINS_ENABLED"], "gateway")

    def test_capabilities_sugar_appends_after_explicit(self):
        d = self._d({"plugins": ["serena"], "capabilities": {"browser": True, "gateway": True}})
        self.assertEqual(d["PLUGINS"], "serena gateway browser")
        self.assertEqual(d["PLUGINS_ENABLED"], "browser gateway serena")


class TestUniversalHybridSecrets(unittest.TestCase):
    FILES = {
        "p": {
            "install": "x",
            "secrets": {"TOKEN": {"hint": "test token"}, "SECOND": {}},
            "mcp": {
                "one": {"command": "server", "requires": ["TOKEN"]},
                "two": {"command": "server", "requires": ["TOKEN", "SECOND"]},
            },
        },
    }

    def derive(self, manifest, present=""):
        return m.derive(manifest, self.FILES,
                        AGENT_FILES,
                        {"PRESENT_SECRET_VARS": present, "SECRETS_FILE": "/sec/secrets.env"})

    def test_common_default_binds_every_enabled_agent(self):
        d = self.derive({"plugins": ["p"], "common_secrets": ["TOKEN"]}, "TOKEN")
        self.assertEqual(d["AGENT_SECRETS"].count("\n"), 5)
        self.assertIn("claude\tTOKEN\tTOKEN\n", d["AGENT_SECRETS"])
        servers = json.loads(d["AGENT_SERVERS_JSON"])
        self.assertEqual(servers["one"]["requires"], ["TOKEN"])
        self.assertEqual(servers["two"]["requires"], ["TOKEN", "SECOND"])

    def test_local_and_remote_required_servers_both_derive(self):
        # Both spec shapes are legal with requires:. There is no longer a
        # REMOTE_SLOTS subset, because no slot VALUE reaches the wiring exec on
        # either path — a remote server is rendered natively (a ${SLOT} ref the
        # agent expands) or through the mcp-remote shim (which expands it from
        # the agent's own env).
        files = {"mix": {"install": "x",
                         "secrets": {"LOCAL_TOK": {}, "REMOTE_TOK": {}},
                         "mcp": {
                             "bridge": {"command": "mcp-remote", "requires": ["LOCAL_TOK"]},
                             "http": {"url": "https://x.test/mcp", "requires": ["REMOTE_TOK"]},
                         }}}
        d = m.derive({"plugins": ["mix"], "common_secrets": ["LOCAL_TOK", "REMOTE_TOK"]},
                     files, AGENT_FILES,
                     {"PRESENT_SECRET_VARS": "LOCAL_TOK REMOTE_TOK",
                      "SECRETS_FILE": "/sec/secrets.env"})
        self.assertEqual(set(d["AGENT_SERVER_SLOTS"].split()), {"LOCAL_TOK", "REMOTE_TOK"})
        self.assertNotIn("AGENT_SERVER_REMOTE_SLOTS", d)

    def test_override_and_disabled_take_precedence_over_default(self):
        d = self.derive(
            {"plugins": ["p"], "common_secrets": ["TOKEN"],
             "agent_secrets": [
                 {"agent": "cursor-agent", "slot": "TOKEN", "secret": "CURSOR_TOKEN"},
                 {"agent": "pi", "slot": "TOKEN", "disabled": True}]},
            "TOKEN CURSOR_TOKEN")
        self.assertIn("cursor-agent\tTOKEN\tCURSOR_TOKEN\n", d["AGENT_SECRETS"])
        self.assertNotIn("pi\tTOKEN\t", d["AGENT_SECRETS"])
        self.assertEqual(d["AGENT_SECRETS"].count("\n"), 4)

    def test_override_without_common_default_is_agent_only(self):
        d = self.derive(
            {"plugins": ["p"],
             "agent_secrets": [{"agent": "claude", "slot": "TOKEN", "secret": "CLAUDE_TOKEN"}]},
            "CLAUDE_TOKEN")
        self.assertEqual(d["AGENT_SECRETS"], "claude\tTOKEN\tCLAUDE_TOKEN\n")

    def test_missing_common_default_omits_effective_binding(self):
        d = self.derive({"plugins": ["p"], "common_secrets": ["TOKEN"]}, "")
        self.assertEqual(d["AGENT_SECRETS"], "")

    def test_requires_must_name_declared_slot(self):
        files = {"p": {"install": "x", "secrets": {"TOKEN": {}},
                       "mcp": {"one": {"command": "server", "requires": ["MISSING"]}}}}
        with self.assertRaises(m.ManifestError) as cm:
            m.derive({"plugins": ["p"]}, files, AGENT_FILES, {"PRESENT_SECRET_VARS": ""})
        self.assertIn("requires unknown secret slot(s): MISSING", str(cm.exception))

    def test_disabled_and_secret_are_mutually_exclusive(self):
        with self.assertRaises(m.ManifestError) as cm:
            self.derive(
                {"plugins": ["p"], "agent_secrets": [
                    {"agent": "claude", "slot": "TOKEN", "secret": "X", "disabled": True}]},
                "X")
        self.assertIn("exactly one of secret or disabled", str(cm.exception))


class TestPluginPorts(unittest.TestCase):
    """plugin_ports: per-container override of a plugin's host_port. The
    resolved value drives BOTH the firewall grant and the ${HOST_PORT} url."""

    def test_override_applied(self):
        d = derive({"plugins": ["browser"], "plugin_ports": {"browser": 8815}})
        self.assertEqual(d["HOST_MCP_PORTS"], "8815,8816")

    def test_default_preserved(self):
        d = derive({"plugins": ["browser"]})
        self.assertEqual(d["HOST_MCP_PORTS"], "8814,8816")

    # The browser server declares requires:, so it is only configured when its
    # slot resolves — bind it and mark the source present to see substituted args.
    WIRED_ENV = {"PRESENT_SECRET_VARS": "RESEARCH_BROWSER_KEY",
                 "SECRETS_FILE": "/sec/secrets.env"}
    BOUND = {"RESEARCH_BROWSER_KEY": "RESEARCH_BROWSER_KEY"}

    def _wired(self, man):
        return derive({**man, "common_secrets": self.BOUND}, env=self.WIRED_ENV)

    def test_host_port_substituted_in_server_args(self):
        d = self._wired({"plugins": ["browser"], "plugin_ports": {"browser": 8815}})
        self.assertIn("host.docker.internal:8815/mcp", d["AGENT_SERVERS_JSON"])
        self.assertNotIn("${HOST_PORT}", d["AGENT_SERVERS_JSON"])

    def test_default_substitutes_plugin_host_port(self):
        d = self._wired({"plugins": ["browser"]})
        self.assertIn("host.docker.internal:8814/mcp", d["AGENT_SERVERS_JSON"])
        self.assertNotIn("${HOST_PORT}", d["AGENT_SERVERS_JSON"])

    def test_browser_slot_is_an_agent_server_slot(self):
        d = self._wired({"plugins": ["browser"]})
        self.assertIn("RESEARCH_BROWSER_KEY", d["AGENT_SERVER_SLOTS"])

    def test_substitution_does_not_mutate_plugin_files(self):
        # PLUGIN_FILES is a module-level fixture shared by every test in this
        # file (and in production a plugin doc the caller still owns).
        derive({"plugins": ["browser"], "plugin_ports": {"browser": 8815}})
        self.assertEqual(BROWSER["mcp"]["browser"]["args"][0],
                         "http://host.docker.internal:${HOST_PORT}/mcp")

    LOCAL_BRIDGE = {"p": {"install": "x", "host_port": 1999,
                          "mcp": {"s": {"command": "bash",
                                        "args": ["-c", "PORT=${HOST_PORT} exec bridge"]}}}}

    def test_host_port_substituted_into_local_args(self):
        # the local-bridge-dials-host case (rhinomcp): ${HOST_PORT} in a local
        # server's args resolves like a remote url's, and a plugin_ports:
        # override re-points args and the firewall grant together
        d = derive({"plugins": ["p"]}, plugin_files=self.LOCAL_BRIDGE)
        entry = json.loads(d["PLUGIN_MCP_ENTRIES"].strip())
        self.assertEqual(entry["s"]["args"], ["-c", "PORT=1999 exec bridge"])
        d = derive({"plugins": ["p"], "plugin_ports": {"p": 2999}},
                   plugin_files=self.LOCAL_BRIDGE)
        entry = json.loads(d["PLUGIN_MCP_ENTRIES"].strip())
        self.assertEqual(entry["s"]["args"], ["-c", "PORT=2999 exec bridge"])
        self.assertEqual(d["HOST_MCP_PORTS"], "2999,8816")
        # the caller's plugin doc must not have absorbed a substitution
        self.assertIn("${HOST_PORT}", self.LOCAL_BRIDGE["p"]["mcp"]["s"]["args"][1])

    def test_host_port_ref_in_local_without_host_port_errors(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]},
                   plugin_files={"p": {"install": "x",
                                       "mcp": {"s": {"command": "bash",
                                                     "args": ["${HOST_PORT}"]}}}})
        self.assertIn("command/args uses ${HOST_PORT}", str(cm.exception))

    ERROR_CASES = [
        ("not a map", {"plugins": ["browser"], "plugin_ports": [8815]},
         "manifest plugin_ports: must be a map of plugin: port, e.g. plugin_ports: {browser: 8815}"),
        ("plugin not enabled", {"plugins": ["browser"], "plugin_ports": {"ghost": 8815}},
         "plugin_ports 'ghost': not an enabled plugin (add it to plugins: first)"),
        ("non-int value", {"plugins": ["browser"], "plugin_ports": {"browser": "8815"}},
         "plugin_ports 'browser': must be an integer port number"),
        ("bool value", {"plugins": ["browser"], "plugin_ports": {"browser": True}},
         "plugin_ports 'browser': must be an integer port number"),
        ("out of range low", {"plugins": ["browser"], "plugin_ports": {"browser": 0}},
         "plugin_ports 'browser': port 0 out of range (1-65535)"),
        ("out of range high", {"plugins": ["browser"], "plugin_ports": {"browser": 70000}},
         "plugin_ports 'browser': port 70000 out of range (1-65535)"),
        ("no host_port on plugin", {"plugins": ["serena"], "plugin_ports": {"serena": 8815}},
         "plugin_ports 'serena': plugin declares no host_port (it has no host-side service to re-point)"),
    ]

    def test_error_cases(self):
        for name, man, message in self.ERROR_CASES:
            with self.subTest(name):
                with self.assertRaises(m.ManifestError) as cm:
                    derive(man)
                self.assertEqual(str(cm.exception), message)

    def test_host_port_ref_without_resolved_port(self):
        files = {"p": {"mcp": {"browser": {
            "url": "http://host.docker.internal:${HOST_PORT}/mcp"}}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]}, plugin_files=files)
        self.assertEqual(
            str(cm.exception),
            "plugin 'p' mcp server 'browser': url uses ${HOST_PORT} but the plugin "
            "declares no host_port to substitute")


class TestPluginVolumes(unittest.TestCase):
    """volumes: — per-container named volumes declared BY a plugin, rendered
    into a generated compose overlay so compose/ never names a plugin."""

    CACHE = {"install": "x", "mcp": {"cbm": {"command": "cbm"}},
             "volumes": {"cbm-cache": "/home/coder/.cache/cbm"}}
    FILES = {**PLUGIN_FILES, "cache": CACHE}

    def _derive(self, man):
        return derive(man, plugin_files=self.FILES)

    def test_no_volumes_emits_empty_overlay(self):
        # Empty (not a stub document) is the contract up.sh branches on: no
        # declared volume must mean no -f at all, not an inert extra file.
        self.assertEqual(derive({"plugins": ["serena"]})["PLUGIN_COMPOSE_YAML"], "")

    def test_overlay_mounts_and_declares_the_volume(self):
        yaml = self._derive({"plugins": ["cache"]})["PLUGIN_COMPOSE_YAML"]
        self.assertIn("      - cbm-cache:/home/coder/.cache/cbm", yaml)
        self.assertIn("volumes:\n  cbm-cache:", yaml)

    def test_overlay_passes_paths_to_the_entrypoint(self):
        # The entrypoint chowns these; without the env line a fresh volume
        # mounts root-owned and the agent silently cannot write to it.
        yaml = self._derive({"plugins": ["cache"]})["PLUGIN_COMPOSE_YAML"]
        self.assertIn("      - PLUGIN_VOLUME_PATHS=/home/coder/.cache/cbm", yaml)

    def test_volumes_of_unenabled_plugins_are_absent(self):
        self.assertEqual(self._derive({"plugins": ["serena"]})["PLUGIN_COMPOSE_YAML"], "")

    def test_overlay_is_independent_of_plugin_order(self):
        two = {**self.FILES, "other-cache": {
            "volumes": {"a-cache": "/home/coder/.cache/a"}}}
        forward = derive({"plugins": ["cache", "other-cache"]}, plugin_files=two)
        reverse = derive({"plugins": ["other-cache", "cache"]}, plugin_files=two)
        self.assertEqual(forward["PLUGIN_COMPOSE_YAML"], reverse["PLUGIN_COMPOSE_YAML"])
        self.assertIn("PLUGIN_VOLUME_PATHS=/home/coder/.cache/a /home/coder/.cache/cbm",
                      forward["PLUGIN_COMPOSE_YAML"])

    def test_a_plugin_may_declare_volumes_without_a_server(self):
        files = {"p": {"volumes": {"state": "/home/coder/.local/state/p"}}}
        d = derive({"plugins": ["p"]}, plugin_files=files)
        self.assertIn("  state:", d["PLUGIN_COMPOSE_YAML"])

    BAD_PATH = ("plugin 'p' volume 'vol': path '%s' is not an absolute container path "
                "(letters, digits, and . _ - + @ only — no spaces, ':', '$', globs, "
                "'..', or trailing slash)")

    ERROR_CASES = [
        ("not a map", {"volumes": ["cbm-cache"]},
         "plugin 'p' volumes must be a map of NAME: /container/path"),
        ("name charset", {"volumes": {"bad name": "/home/coder/x"}},
         "plugin 'p' volume 'bad name': name must be at least two characters, start "
         "with a letter or digit, and use only letters, digits, underscore, dash "
         "(it becomes a compose volume key)"),
        # Compose reads a 1-char source as a Windows drive letter: the mount
        # loses its source, `compose config` still exits 0, and only `up` fails.
        ("single-character name", {"volumes": {"v": "/home/coder/x"}},
         "plugin 'p' volume 'v': name must be at least two characters, start "
         "with a letter or digit, and use only letters, digits, underscore, dash "
         "(it becomes a compose volume key)"),
        ("name starting with a dash", {"volumes": {"-v": "/home/coder/x"}},
         "plugin 'p' volume '-v': name must be at least two characters, start "
         "with a letter or digit, and use only letters, digits, underscore, dash "
         "(it becomes a compose volume key)"),
        ("compose volume name", {"volumes": {"workspace": "/home/coder/x"}},
         "plugin 'p' volume 'workspace': that name is already a compose volume "
         "(compose would merge into it and remount a real directory)"),
        ("relative path", {"volumes": {"vol": "home/coder/x"}}, BAD_PATH % "home/coder/x"),
        ("path with colon", {"volumes": {"vol": "/home/coder/x:ro"}},
         BAD_PATH % "/home/coder/x:ro"),
        ("path with space", {"volumes": {"vol": "/home/coder/my cache"}},
         BAD_PATH % "/home/coder/my cache"),
        ("path traversal", {"volumes": {"vol": "/home/coder/../etc"}},
         BAD_PATH % "/home/coder/../etc"),
        ("trailing slash", {"volumes": {"vol": "/home/coder/x/"}}, BAD_PATH % "/home/coder/x/"),
        # compose interpolates $VAR in every -f file, so a '$' lets the real
        # mount target differ from the declared one (and can pull in a value
        # from the secrets.env up.sh sourced).
        ("path with a variable reference", {"volumes": {"vol": "/home/coder/${HOME}"}},
         BAD_PATH % "/home/coder/${HOME}"),
        ("path with a bare dollar", {"volumes": {"vol": "/home/coder/$HOME"}},
         BAD_PATH % "/home/coder/$HOME"),
        # The entrypoint's loop must word-split, which also globs: a '*' would
        # chown whatever matches instead of the path that was mounted.
        ("path with a glob star", {"volumes": {"vol": "/home/coder/*"}},
         BAD_PATH % "/home/coder/*"),
        ("path with a glob class", {"volumes": {"vol": "/home/coder/ca[ch]e"}},
         BAD_PATH % "/home/coder/ca[ch]e"),
        ("compose mount path", {"volumes": {"vol": "/home/coder/.claude"}},
         "plugin 'p' volume 'vol': path '/home/coder/.claude' collides with the "
         "compose mount '/home/coder/.claude'"),
        # A volume at a PARENT of a compose mount freezes that tree in a volume;
        # a rebuilt image never reaches the container again.
        ("parent of a compose mount", {"volumes": {"vol": "/home/coder/.config"}},
         "plugin 'p' volume 'vol': path '/home/coder/.config' collides with the "
         "compose mount '/home/coder/.config/cursor'"),
        # ...and one at a CHILD hides live content inside it.
        ("child of a compose mount", {"volumes": {"vol": "/home/coder/.claude/projects"}},
         "plugin 'p' volume 'vol': path '/home/coder/.claude/projects' collides with "
         "the compose mount '/home/coder/.claude'"),
        ("child of the workspace volume", {"volumes": {"vol": "/workspace/repos"}},
         "plugin 'p' volume 'vol': path '/workspace/repos' must be under /home/coder/ "
         "(a volume elsewhere would shadow image content or the workspace)"),
        ("the coder home itself", {"volumes": {"vol": "/home/coder"}},
         "plugin 'p' volume 'vol': path '/home/coder' must be under /home/coder/ "
         "(a volume elsewhere would shadow image content or the workspace)"),
        ("a system binary dir", {"volumes": {"vol": "/usr/local/bin"}},
         "plugin 'p' volume 'vol': path '/usr/local/bin' must be under /home/coder/ "
         "(a volume elsewhere would shadow image content or the workspace)"),
        ("etc", {"volumes": {"vol": "/etc"}},
         "plugin 'p' volume 'vol': path '/etc' must be under /home/coder/ "
         "(a volume elsewhere would shadow image content or the workspace)"),
    ]

    def test_sibling_of_a_compose_mount_is_allowed(self):
        # The overlap test is component-wise: '/home/coder/.curse' must not be
        # read as containing '/home/coder/.cursor' the way a prefix test would.
        d = derive({"plugins": ["p"]},
                   plugin_files={"p": {"volumes": {"curse": "/home/coder/.curse"}}})
        self.assertIn("  curse:", d["PLUGIN_COMPOSE_YAML"])

    def test_error_cases(self):
        for name, doc, message in self.ERROR_CASES:
            with self.subTest(name):
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"plugins": ["p"]}, plugin_files={"p": doc})
                self.assertEqual(str(cm.exception), message)

    def test_two_plugins_cannot_share_a_volume_name(self):
        files = {"a": {"volumes": {"shared": "/home/coder/a"}},
                 "b": {"volumes": {"shared": "/home/coder/b"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["a", "b"]}, plugin_files=files)
        self.assertEqual(
            str(cm.exception),
            "plugin 'b' volume 'shared': already declared by plugin 'a' "
            "(two plugins cannot share one volume)")

    def test_two_plugins_cannot_share_a_mount_path(self):
        files = {"a": {"volumes": {"a-cache": "/home/coder/cache"}},
                 "b": {"volumes": {"b-cache": "/home/coder/cache"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["a", "b"]}, plugin_files=files)
        self.assertEqual(
            str(cm.exception),
            "plugin 'b' volume 'b-cache': path '/home/coder/cache' collides with "
            "'/home/coder/cache', mounted by plugin 'a'")

    def test_one_plugin_cannot_nest_inside_another(self):
        files = {"a": {"volumes": {"a-cache": "/home/coder/cache"}},
                 "b": {"volumes": {"b-cache": "/home/coder/cache/inner"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["a", "b"]}, plugin_files=files)
        self.assertEqual(
            str(cm.exception),
            "plugin 'b' volume 'b-cache': path '/home/coder/cache/inner' collides "
            "with '/home/coder/cache', mounted by plugin 'a'")

    def test_disabled_plugin_does_not_collide(self):
        # Only ENABLED plugins contend for names — two containers can each run
        # a different plugin that happens to want the same volume name.
        files = {"a": {"volumes": {"shared": "/home/coder/a"}},
                 "b": {"volumes": {"shared": "/home/coder/b"}}}
        d = derive({"plugins": ["a"]}, plugin_files=files)
        self.assertIn("  shared:", d["PLUGIN_COMPOSE_YAML"])


class TestPluginServices(unittest.TestCase):
    """services: — in-container processes started (idempotently, restart-
    wrapped) at `up`. Optional; absent everywhere today (Phase 1 Hardening
    PLN §2). Aggregated into PLUGIN_SERVICES the same way volumes/egress fold
    across enabled plugins."""

    SVC = {"install": "x", "mcp": {"cbm": {"command": "cbm"}},
           "services": {"cbm-watch": "cbm --watch"}}
    FILES = {**PLUGIN_FILES, "svc": SVC}

    def _derive(self, man):
        return derive(man, plugin_files=self.FILES)

    def test_absent_services_key_is_a_noop(self):
        # Every existing PLUGIN_FILES fixture declares no services: — this
        # pins zero behavior change for containers that don't opt in.
        self.assertEqual(derive({"plugins": ["serena"]})["PLUGIN_SERVICES"], "")

    def test_services_export_line_shape(self):
        out = self._derive({"plugins": ["svc"]})["PLUGIN_SERVICES"]
        self.assertEqual(out, "cbm-watch\tcbm --watch\tsvc\n")

    def test_a_plugin_may_declare_services_without_a_server(self):
        files = {"p": {"services": {"only-svc": "run-it"}}}
        d = derive({"plugins": ["p"]}, plugin_files=files)
        self.assertEqual(d["PLUGIN_SERVICES"], "only-svc\trun-it\tp\n")

    def test_export_sorted_by_name_independent_of_plugin_order(self):
        two = {"a": {"services": {"zzz": "run-z"}},
               "b": {"services": {"aaa": "run-a"}}}
        forward = derive({"plugins": ["a", "b"]}, plugin_files=two)
        reverse = derive({"plugins": ["b", "a"]}, plugin_files=two)
        self.assertEqual(forward["PLUGIN_SERVICES"], reverse["PLUGIN_SERVICES"])
        self.assertEqual(forward["PLUGIN_SERVICES"],
                         "aaa\trun-a\tb\nzzz\trun-z\ta\n")

    def test_services_of_unenabled_plugins_are_absent(self):
        self.assertEqual(self._derive({"plugins": ["serena"]})["PLUGIN_SERVICES"], "")

    ERROR_CASES = [
        ("not a map", {"services": ["cbm --watch"]},
         "plugin 'p' services must be a map of NAME: command"),
        ("empty command", {"services": {"svc": ""}},
         "plugin 'p' service 'svc': command must be a non-empty string"),
        ("whitespace-only command", {"services": {"svc": "   "}},
         "plugin 'p' service 'svc': command must be a non-empty string"),
        ("non-string command", {"services": {"svc": ["cbm", "--watch"]}},
         "plugin 'p' service 'svc': command must be a non-empty string"),
        ("uppercase name", {"services": {"Svc": "cbm --watch"}},
         "plugin 'p' service 'Svc': illegal characters (allowed: lowercase "
         "letters, digits, dash — it becomes a tmux session name)"),
        ("underscore name", {"services": {"svc_name": "cbm --watch"}},
         "plugin 'p' service 'svc_name': illegal characters (allowed: lowercase "
         "letters, digits, dash — it becomes a tmux session name)"),
        ("empty name", {"services": {"": "cbm --watch"}},
         "plugin 'p' service '': illegal characters (allowed: lowercase "
         "letters, digits, dash — it becomes a tmux session name)"),
        ("dotted name", {"services": {"svc.name": "cbm --watch"}},
         "plugin 'p' service 'svc.name': illegal characters (allowed: lowercase "
         "letters, digits, dash — it becomes a tmux session name)"),
    ]

    def test_error_cases(self):
        for name, doc, message in self.ERROR_CASES:
            with self.subTest(name):
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"plugins": ["p"]}, plugin_files={"p": doc})
                self.assertEqual(str(cm.exception), message)

    def test_two_plugins_cannot_share_a_service_name(self):
        files = {"a": {"services": {"shared": "run-a"}},
                 "b": {"services": {"shared": "run-b"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["a", "b"]}, plugin_files=files)
        self.assertEqual(
            str(cm.exception),
            "plugin 'b' service 'shared': already declared by plugin 'a' "
            "(two plugins cannot share one service name)")

    def test_disabled_plugin_does_not_collide_on_service_name(self):
        # Only ENABLED plugins contend for the tmux/log namespace.
        files = {"a": {"services": {"shared": "run-a"}},
                 "b": {"services": {"shared": "run-b"}}}
        d = derive({"plugins": ["a"]}, plugin_files=files)
        self.assertEqual(d["PLUGIN_SERVICES"], "shared\trun-a\ta\n")

    def test_a_plugin_may_declare_multiple_services(self):
        files = {"p": {"services": {"one": "run-one", "two": "run-two"}}}
        d = derive({"plugins": ["p"]}, plugin_files=files)
        self.assertEqual(d["PLUGIN_SERVICES"], "one\trun-one\tp\ntwo\trun-two\tp\n")


class TestPluginSetup(unittest.TestCase):
    """setup: — one command run once per `up` inside the container, after the
    MCP wiring and before services: (PLN "plugin setup hook" [2/3]). One
    command per plugin — not a map, which would suggest an ordering the
    per-plugin docker exec loop in up.sh does not define."""

    FILES = {"p": {"setup": "herdr integration install claude"}}

    def _derive(self, man, files=None):
        return derive(man, plugin_files=self.FILES if files is None else files)

    def test_absent_setup_key_is_a_noop(self):
        # Every existing PLUGIN_FILES fixture declares no setup: — this pins
        # zero behavior change for containers that don't opt in.
        self.assertEqual(derive({"plugins": ["serena"]})["PLUGIN_SETUP"], "")

    def test_setup_export_line_shape(self):
        out = self._derive({"plugins": ["p"]})["PLUGIN_SETUP"]
        self.assertEqual(out, "p\therdr integration install claude\n")

    def test_export_sorted_by_plugin_independent_of_manifest_order(self):
        two = {"zz": {"setup": "run-z"}, "aa": {"setup": "run-a"}}
        forward = derive({"plugins": ["zz", "aa"]}, plugin_files=two)
        reverse = derive({"plugins": ["aa", "zz"]}, plugin_files=two)
        self.assertEqual(forward["PLUGIN_SETUP"], reverse["PLUGIN_SETUP"])
        self.assertEqual(forward["PLUGIN_SETUP"], "aa\trun-a\nzz\trun-z\n")

    def test_setup_of_unenabled_plugins_is_absent(self):
        d = derive({"plugins": ["serena"]},
                   plugin_files={**PLUGIN_FILES, "p": {"setup": "run-it"}})
        self.assertEqual(d["PLUGIN_SETUP"], "")

    def test_a_plugin_may_declare_setup_alone(self):
        # setup wires what install: already fetched — it needs no mcp: server
        # of its own (and install: is only required by a local command server).
        d = derive({"plugins": ["p"]}, plugin_files=self.FILES)
        self.assertEqual(d["PLUGIN_SETUP"], "p\therdr integration install claude\n")

    ERROR_CASES = [
        ("empty command", {"setup": ""}, "plugin 'p' setup must be a non-empty string"),
        ("whitespace-only command", {"setup": "   "},
         "plugin 'p' setup must be a non-empty string"),
        ("non-string command", {"setup": ["herdr", "install"]},
         "plugin 'p' setup must be a non-empty string"),
        ("tab in command", {"setup": "echo a\tb"},
         "plugin 'p' setup must not contain tabs or newlines (the PLUGIN_SETUP "
         "export is TAB-separated lines; use one command, e.g. "
         "`bash -lc '...'`)"),
        ("newline in command", {"setup": "export FOO=bar\nexec myserver"},
         "plugin 'p' setup must not contain tabs or newlines (the PLUGIN_SETUP "
         "export is TAB-separated lines; use one command, e.g. "
         "`bash -lc '...'`)"),
        ("carriage return in command", {"setup": "echo a\rb"},
         "plugin 'p' setup must not contain tabs or newlines (the PLUGIN_SETUP "
         "export is TAB-separated lines; use one command, e.g. "
         "`bash -lc '...'`)"),
    ]

    def test_error_cases(self):
        for name, doc, message in self.ERROR_CASES:
            with self.subTest(name):
                with self.assertRaises(m.ManifestError) as cm:
                    derive({"plugins": ["p"]}, plugin_files={"p": doc})
                self.assertEqual(str(cm.exception), message)


if __name__ == "__main__":
    unittest.main()


class TestServiceCommandCharset(unittest.TestCase):
    """Review finding: TAB/newline in a command corrupts the TSV export."""

    def _derive(self, cmd):
        files = {**PLUGIN_FILES, "p": {"install": "x", "services": {"svc": cmd}}}
        return derive({"plugins": ["p"]}, plugin_files=files)

    def test_newline_in_command_rejected(self):
        with self.assertRaisesRegex(m.ManifestError, "tabs or newlines"):
            self._derive("export FOO=bar\nexec myserver")

    def test_tab_in_command_rejected(self):
        with self.assertRaisesRegex(m.ManifestError, "tabs or newlines"):
            self._derive("echo a\tb")

    def test_carriage_return_rejected(self):
        with self.assertRaisesRegex(m.ManifestError, "tabs or newlines"):
            self._derive("echo a\rb")

    def test_plain_command_still_accepted(self):
        self._derive("bm mcp --transport streamable-http --port 8801")
