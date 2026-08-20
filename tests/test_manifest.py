#!/usr/bin/env python3
"""Unit tests for src/manifest.py (Phase 2 of the Python extraction).

Table-driven: every validation rule the old up.sh bash enforced is a row
here, with the EXACT error message the bash produced (parity was verified
against the extracted old code before the port landed). The yq/jq semantic
quirks (`//` on false, exact tools matching, agent-suffix case
order) get dedicated pins so a future "cleanup" can't change them silently.
"""

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import manifest as m
import wire_plugins

MODULE = Path(__file__).parent.parent / "src" / "manifest.py"

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
PROXYMAN = {"host_port": 8813,
            "secrets": {"PROXYMAN_BRIDGE_KEY": {
                        "hint": "proxyman (run ./service.sh proxyman once)"}},
            "mcp": {"proxyman": {"url": "http://host.docker.internal:8813/mcp",
                                 "headers": {"X-API-Key": "${PROXYMAN_BRIDGE_KEY}"},
                                 "requires": ["PROXYMAN_BRIDGE_KEY"]}}}
BROWSER = {"host_port": 8814,
           "secrets": {"RESEARCH_BROWSER_KEY": {
                       "hint": "browser (run ./service.sh browser once)"}},
           "mcp": {"browser": {"url": "http://host.docker.internal:${HOST_PORT}/mcp",
                               "headers": {"X-API-Key": "${RESEARCH_BROWSER_KEY}"},
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
        ("remote without ssh", {"remote": {"tmux": True}}, None,
         "manifest has remote: but no ssh: section — remote access rides the SSH login path (add ssh.port)"),
        ("bad notify kind", {"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "slack"}}, None,
         "remote.notify must be 'ntfy' (got 'slack')"),
        ("notify without tmux", {"ssh": {"port": 22}, "remote": {"notify": "ntfy"}}, None,
         "remote.notify requires remote.tmux: true (the idle monitor runs inside the tmux session)"),
        ("malformed mosh ports",
         {"ssh": {"port": 22}, "remote": {"mosh": True, "mosh_ports": "9:banana"}}, None,
         "remote.mosh_ports must be START:END (got '9:banana')"),
        ("mosh ports below 1024",
         {"ssh": {"port": 22}, "remote": {"mosh": True, "mosh_ports": "500:600"}}, None,
         "remote.mosh_ports '500:600' out of range (need 1024 <= START <= END <= 65535)"),
        ("mosh ports above 65535",
         {"ssh": {"port": 22}, "remote": {"mosh": True, "mosh_ports": "60000:70000"}}, None,
         "remote.mosh_ports '60000:70000' out of range (need 1024 <= START <= END <= 65535)"),
        ("mosh ports inverted",
         {"ssh": {"port": 22}, "remote": {"mosh": True, "mosh_ports": "3000:2000"}}, None,
         "remote.mosh_ports '3000:2000' out of range (need 1024 <= START <= END <= 65535)"),
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
         {"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "ntfy"}}, ENV,
         "manifest has remote.notify: ntfy but NTFY_URL is missing from /sec/secrets.env"),
        ("ntfy url with hash",
         {"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "ntfy"}},
         dict(ENV, NTFY_URL="https://x.com/#frag"),
         "NTFY_URL must be a bare origin (no '#', quotes) — put the topic in NTFY_TOPIC"),
        ("ntfy url unparseable",
         {"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "ntfy"}},
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


class TestYqSemanticsPins(unittest.TestCase):
    """The jq/yq quirks the port must NOT silently fix."""

    def test_alternative_operator_fires_on_false(self):
        d = derive({"plugins": False, "repos": False, "memory": False, "tools": False})
        self.assertEqual(d["PLUGINS"], "")
        self.assertEqual(d["REPOS"], "")
        self.assertEqual(d["MEM_LIMIT"], "2g")
        self.assertEqual(d["INSTALL_AIDER"], "true")  # default tool set

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

    def test_tools_match_is_exact_string_equality(self):
        d = derive({"tools": ["claude-code"]})
        self.assertEqual(d["INSTALL_CLAUDE"], "false")
        self.assertEqual(d["INSTALL_CODEX"], "false")

    def test_agents_contains_is_substring_match(self):
        d = derive({"agents": ["claude-code"]})
        self.assertEqual(d["INSTALL_CLAUDE"], "true")   # jq contains() quirk
        self.assertEqual(d["INSTALL_CODEX"], "false")

    def test_agents_and_tools_together_is_error_even_when_false(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"agents": False, "tools": False})
        self.assertEqual(
            str(cm.exception),
            "manifest sets both agents: and tools: — use agents: (tools: is a deprecated alias)")

    def test_tools_alias_emits_deprecation_warning(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = derive({"tools": ["codex"]})
        self.assertEqual(d["INSTALL_CODEX"], "true")
        self.assertEqual(d["INSTALL_CLAUDE"], "false")
        self.assertIn(
            "  ⚠ tools: is deprecated — rename the key to agents: (same values; tools: will be removed)",
            err.getvalue())

    def test_agents_false_defaults_to_default_tool_set(self):
        d = derive({"agents": False})
        self.assertEqual(d["INSTALL_AIDER"], "true")

    def test_capabilities_sugar_only_literal_true_maps_to_plugin(self):
        # capabilities: gateway/proxyman/browser are deprecated sugar now; only
        # the literal boolean true maps onto the plugin (yq `// false` raw-flag
        # semantics preserved: "yes"/1 do NOT enable it).
        files = {"gateway": GATEWAY, "proxyman": PROXYMAN, "browser": BROWSER}
        d = derive({"capabilities": {"gateway": "yes", "proxyman": 1, "browser": True}},
                   plugin_files=files)
        self.assertEqual(d["PLUGINS"], "browser")          # only browser: true
        self.assertEqual(d["HOST_MCP_PORTS"], "8814")      # browser's host_port
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


class TestGitIdentity(unittest.TestCase):
    # GH_TOKEN_VARS mirrors the set up.sh scans from secrets.env (names only).
    ENV = {"GH_TOKEN_VARS": "GH_TOKEN_hank GH_TOKEN_vendor GH_TOKEN_v2"}

    def _d(self, git):
        return derive({"git": git}, env=dict(self.ENV))

    def test_absent_git_identity_is_empty(self):
        d = derive({})
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "")
        self.assertEqual(d["GIT_ORG_TOKENS"], "")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "")

    def test_default_token_source(self):
        d = self._d({"token": "GH_TOKEN_hank"})
        self.assertEqual(d["GIT_TOKEN_SOURCE"], "GH_TOKEN_hank")

    def test_default_token_missing_var_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"token": "GH_TOKEN_nope"})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.token: GH_TOKEN_nope not found in secrets.env")

    def test_per_org_token_routing_and_canonical_var(self):
        d = self._d({"token": "GH_TOKEN_hank",
                     "orgs": {"vendor": {"token": "GH_TOKEN_vendor",
                                         "name": "Vendor Bot", "email": "bot@vendor.io"}}})
        # owner<TAB>canonical_var<TAB>source_var — canonical is GH_TOKEN_<owner>.
        self.assertEqual(d["GIT_ORG_TOKENS"], "vendor\tGH_TOKEN_vendor\tGH_TOKEN_vendor\n")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "vendor\tVendor Bot\tbot@vendor.io\n")

    def test_hyphenated_owner_sanitizes_to_underscore(self):
        # canonical var replaces '-' with '_'; the source var name is unchanged.
        d = self._d({"orgs": {"acme-corp": {"token": "GH_TOKEN_v2"}}})
        self.assertEqual(d["GIT_ORG_TOKENS"], "acme-corp\tGH_TOKEN_acme_corp\tGH_TOKEN_v2\n")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "acme-corp\t\t\n")

    def test_mixed_case_owner_folds_to_lowercase(self):
        # github owners are case-insensitive; the router derives the owner from
        # the clone URL, so the emitted owner + canonical var fold to lowercase
        # (a `PlanetExpress` manifest key must route a `planetexpress/*` clone).
        d = self._d({"orgs": {"PlanetExpress": {"token": "GH_TOKEN_v2",
                                                "name": "Leela Bot"}}})
        self.assertEqual(d["GIT_ORG_TOKENS"],
                         "planetexpress\tGH_TOKEN_planetexpress\tGH_TOKEN_v2\n")
        self.assertEqual(d["GIT_ORG_IDENTITIES"], "planetexpress\tLeela Bot\t\n")

    def test_case_insensitive_duplicate_owner_hard_fails(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"orgs": {"Acme": {"token": "GH_TOKEN_v2"},
                              "acme": {"token": "GH_TOKEN_vendor"}}})
        self.assertEqual(
            str(cm.exception),
            "manifest git identity failed validation:\n"
            "  git.orgs: duplicate owner 'acme' (case-insensitive clash with 'Acme')")

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
            "  git.orgs.vendor: unsupported field(s): tokne (only token, name, email)")

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
            "  git.orgs: must be a map of <owner>: {token, name, email}")

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


class TestDerivedValues(unittest.TestCase):
    def test_defaults_on_empty_manifest(self):
        d = derive({})
        self.assertEqual(d["FORGE"], "github")
        self.assertEqual(d["MEM_LIMIT"], "2g")
        self.assertEqual(d["SSH_BIND"], "127.0.0.1")
        self.assertEqual(d["INSTALL_CLAUDE"], "true")
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
        self.assertEqual(d["HOST_MCP_PORTS"], "8811,8814")
        self.assertEqual(derive({"plugins": ["serena"]})["HOST_MCP_PORTS"], "")

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

    def test_mosh_defaults_and_dash_form(self):
        d = derive({"ssh": {"port": 22}, "remote": {"mosh": True}})
        self.assertEqual(d["MOSH_PORTS"], "60000:60010")
        self.assertEqual(d["MOSH_PORTS_DASH"], "60000-60010")

    def test_ntfy_host_strip_order_path_before_userinfo(self):
        env = dict(ENV, NTFY_URL="https://ntfy.example.com/a@b", NTFY_TOPIC="t")
        d = derive({"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "ntfy"}}, env=env)
        # '@' in the PATH must not masquerade as userinfo
        self.assertIn("ntfy.example.com", d["EGRESS"].split(","))
        self.assertEqual(d["CONTAINER_NTFY_URL"], "https://ntfy.example.com/a@b")
        self.assertEqual(d["CONTAINER_NTFY_TOPIC"], "t")

    def test_ntfy_userinfo_and_port_stripped(self):
        env = dict(ENV, NTFY_URL="https://user@h.example.com:8443")
        d = derive({"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "ntfy"}}, env=env)
        self.assertIn("h.example.com", d["EGRESS"].split(","))

    def test_ntfy_ip_literal_goes_to_cidrs(self):
        env = dict(ENV, NTFY_URL="http://10.1.2.3:8080/p")
        d = derive({"ssh": {"port": 22}, "remote": {"tmux": True, "notify": "ntfy"},
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
        d = derive({"tools": ["claude", "aider"]})
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
        d = derive({"tools": ["aider", "pi"]})
        self.assertEqual(d["AGENTS_COMPOSE_YAML"], "")

    def test_agent_egress_folds_into_egress_for_enabled_agents_only(self):
        agents = dict(AGENT_FILES)
        agents["claude"] = dict(AGENT_FILES["claude"], egress=["api.anthropic.com"])
        d = derive({}, agent_files=agents)
        self.assertIn("api.anthropic.com", d["EGRESS"].split(","))
        d = derive({"tools": ["aider"]}, agent_files=agents)
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
        with self.assertRaises(m.ManifestError):
            derive({"ssh": {"port": 22}, "remote": {"mosh": True, "mosh_ports": "2000:3000\n"}})

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
        self.assertEqual(d["HOST_MCP_PORTS"], "1999")
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

    def test_capabilities_sugar_appends_after_explicit(self):
        d = self._d({"plugins": ["serena"], "capabilities": {"browser": True, "gateway": True}})
        self.assertEqual(d["PLUGINS"], "serena gateway browser")


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

    def test_remote_slots_excludes_local_command_servers(self):
        # AGENT_SERVER_SLOTS lists every required slot; AGENT_SERVER_REMOTE_SLOTS
        # is the subset feeding a REMOTE (no-command) server — the only slots
        # up.sh may hand to the wiring exec. A LOCAL command server (like axiom's
        # mcp-remote bridge) reads its ${SLOT} from the agent's own env, so its
        # slot must NOT appear in REMOTE_SLOTS (else the value leaks onto argv).
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
        self.assertEqual(d["AGENT_SERVER_REMOTE_SLOTS"], "REMOTE_TOK")

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
        self.assertEqual(d["HOST_MCP_PORTS"], "8815")

    def test_default_preserved(self):
        d = derive({"plugins": ["browser"]})
        self.assertEqual(d["HOST_MCP_PORTS"], "8814")

    # The browser server declares requires:, so it is only configured when its
    # slot resolves — bind it and mark the source present to see the url.
    WIRED_ENV = {"PRESENT_SECRET_VARS": "RESEARCH_BROWSER_KEY",
                 "SECRETS_FILE": "/sec/secrets.env"}
    BOUND = {"RESEARCH_BROWSER_KEY": "RESEARCH_BROWSER_KEY"}

    def _wired(self, man):
        return derive({**man, "common_secrets": self.BOUND}, env=self.WIRED_ENV)

    def test_host_port_substituted_in_server_url(self):
        d = self._wired({"plugins": ["browser"], "plugin_ports": {"browser": 8815}})
        self.assertIn("host.docker.internal:8815/mcp", d["AGENT_SERVERS_JSON"])
        self.assertNotIn("${HOST_PORT}", d["AGENT_SERVERS_JSON"])

    def test_default_substitutes_plugin_host_port(self):
        d = self._wired({"plugins": ["browser"]})
        self.assertIn("host.docker.internal:8814/mcp", d["AGENT_SERVERS_JSON"])
        self.assertNotIn("${HOST_PORT}", d["AGENT_SERVERS_JSON"])

    def test_substitution_does_not_mutate_plugin_files(self):
        # PLUGIN_FILES is a module-level fixture shared by every test in this
        # file (and in production a plugin doc the caller still owns).
        derive({"plugins": ["browser"], "plugin_ports": {"browser": 8815}})
        self.assertEqual(BROWSER["mcp"]["browser"]["url"],
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
        self.assertEqual(d["HOST_MCP_PORTS"], "2999")
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


if __name__ == "__main__":
    unittest.main()
