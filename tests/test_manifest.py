#!/usr/bin/env python3
"""Unit tests for src/manifest.py (Phase 2 of the Python extraction).

Table-driven: every validation rule the old up.sh bash enforced is a row
here, with the EXACT error message the bash produced (parity was verified
against the extracted old code before the port landed). The yq/jq semantic
quirks (`//` on false, contains() substring matching, agent-suffix case
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
# Remote plugins (Plugins v2 Phase 1) — no install:, url: config + host_port +
# an env-scoped secret slot. Mirror the shipped plugins/*/plugin.yml files.
GATEWAY = {"host_port": 8811,
           "secrets": {"MCP_GATEWAY_TOKEN": {"scope": "env",
                       "hint": "gateway (run ./service.sh gateway once)"}},
           "mcp": {"coding": {"url": "http://host.docker.internal:8811/mcp",
                              "headers": {"Authorization": "Bearer ${MCP_GATEWAY_TOKEN}"}}}}
PROXYMAN = {"host_port": 8813,
            "secrets": {"PROXYMAN_BRIDGE_KEY": {"scope": "env",
                        "hint": "proxyman (run ./service.sh proxyman once)"}},
            "mcp": {"proxyman": {"url": "http://host.docker.internal:8813/mcp",
                                 "headers": {"X-API-Key": "${PROXYMAN_BRIDGE_KEY}"}}}}
BROWSER = {"host_port": 8814,
           "secrets": {"RESEARCH_BROWSER_KEY": {"scope": "env",
                       "hint": "browser (run ./service.sh browser once)"}},
           "mcp": {"browser": {"url": "http://host.docker.internal:8814/mcp",
                               "headers": {"X-API-Key": "${RESEARCH_BROWSER_KEY}"}}}}
# Agent-scoped plugins (Plugins v2 Phase 2). OBSIDIAN is a remote server with an
# agent-scoped slot; WATCH is env-only (agent-scoped slot, no mcp server).
OBSIDIAN = {"secrets": {"OBSIDIAN_ANNOTATED_KEY": "agent"},
            "egress": ["mcp-obsidian.dmetr.io"],
            "mcp": {"obsidian-annotated": {
                "url": "https://mcp-obsidian.dmetr.io/mcp",
                "headers": {"Authorization": "Bearer ${OBSIDIAN_ANNOTATED_KEY}"}}}}
WATCH = {"secrets": {"ANNOTATED_WATCH_KEY": "agent"}}
# AXIOM is agent-scoped WITH a global fallback (scope: agent, global: true) and a
# LOCAL stdio server (mcp-remote bridge): a global AXIOM_TOKEN wires every enabled
# agent, AXIOM_KEY_<agent> gates/overrides one. Local ⇒ needs an install: block.
AXIOM = {"secrets": {"AXIOM_TOKEN": {"scope": "agent", "global": True,
                                     "hint": "axiom token"}},
         "install": "npm install -g mcp-remote",
         "egress": ["mcp.axiom.co"],
         "mcp": {"axiom": {"command": "mcp-remote",
                           "args": ["https://mcp.axiom.co/mcp", "--header",
                                    "Authorization: Bearer ${AXIOM_TOKEN}"]}}}
PLUGIN_FILES = {"serena": SERENA, "other": OTHER,
                "gateway": GATEWAY, "proxyman": PROXYMAN, "browser": BROWSER,
                "obsidian-annotated": OBSIDIAN, "annotated-watch": WATCH,
                "axiom": AXIOM}
ENV = {"SECRET_KEY_VARS": "OBSIDIAN_KEY_me_claude OBSIDIAN_WATCH_KEY_w_pi",
       "SECRETS_FILE": "/sec/secrets.env"}


def derive(man, plugin_files=None, env=None):
    return m.derive(man, PLUGIN_FILES if plugin_files is None else plugin_files,
                    ENV if env is None else env)


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
         "  obsidian ref 'me_nobody': suffix is not a known agent (_claude/_codex/_pi/_gemini/_cursor_agent)"),
        ("secret missing", {"identities": {"obsidian": ["gone_claude"]}}, None,
         "manifest identity references failed validation:\n"
         "  obsidian ref 'gone_claude': OBSIDIAN_KEY_gone_claude not found in /sec/secrets.env"),
        ("aggregated identity errors",
         {"identities": {"obsidian": ["bad-dash_claude"], "watch": ["w_nobody"]}}, None,
         "manifest identity references failed validation:\n"
         "  obsidian ref 'bad-dash_claude': illegal characters (allowed: letters, digits, underscore)\n"
         "  watch ref 'w_nobody': suffix is not a known agent (_claude/_codex/_pi/_gemini/_cursor_agent)"),
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
         "plugin 'p' mcp server 'srv': unsupported field(s) for a local server: env (only command and args)"),
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
         "plugin 'p' mcp server 'srv': unsupported field(s) for a remote server: foo (only url and headers)"),
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

    def test_tools_contains_is_substring_match(self):
        d = derive({"tools": ["claude-code"]})
        self.assertEqual(d["INSTALL_CLAUDE"], "true")   # jq contains() quirk
        self.assertEqual(d["INSTALL_CODEX"], "false")

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
        self.assertEqual(m.agent_for_ref("x_cursor_agent"), "cursor-agent")
        self.assertEqual(m.agent_for_ref("weird_claude_cursor_agent"), "cursor-agent")
        self.assertEqual(m.agent_for_ref("cursor_agent"), "")  # no leading _
        self.assertEqual(m.agent_for_ref("a_pi"), "pi")
        self.assertEqual(m.agent_for_ref("nope"), "")


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
        stream = io.StringIO('{"plugins": ["p"]}\np\t{"mcp": {}}\n')
        man, files = m.read_stdin_docs(stream)
        self.assertEqual(man, {"plugins": ["p"]})
        self.assertEqual(files, {"p": {"mcp": {}}})

    def test_read_stdin_null_manifest_is_empty(self):
        man, files = m.read_stdin_docs(io.StringIO("null\n"))
        self.assertEqual(man, {})

    def test_read_stdin_errors(self):
        with self.assertRaises(m.ManifestError):
            m.read_stdin_docs(io.StringIO(""))
        with self.assertRaises(m.ManifestError):
            m.read_stdin_docs(io.StringIO("{bad\n"))
        with self.assertRaises(m.ManifestError):
            m.read_stdin_docs(io.StringIO('{}\nno-tab-here\n'))

    def test_main_derive_end_to_end(self):
        out = subprocess.run(
            [sys.executable, str(MODULE), "--derive"],
            input='{"memory": "3g"}\n', capture_output=True, text=True,
            env={"SECRETS_FILE": "/s", "PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 0)
        self.assertIn("MEM_LIMIT=3g\n", out.stdout)

    def test_main_error_goes_to_stderr_exit_1(self):
        out = subprocess.run(
            [sys.executable, str(MODULE), "--derive"],
            input='{"forge": "bad"}\n', capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout, "")
        self.assertIn("Error: forge must be github or gitea", out.stderr)


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
        self.assertEqual(d["PLUGIN_MCP_ENTRIES"], "{}\n")

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
        man, files = m.read_stdin_docs(io.StringIO('{}\nbroken\t!\n'))
        self.assertIs(files["broken"], m.UNREADABLE)
        with self.assertRaises(m.ManifestError) as cm:
            m.read_stdin_docs(io.StringIO('{}\n{"second": "doc"}\n'))
        self.assertIn("stray '---'", str(cm.exception))


class TestPluginsV2Phase1(unittest.TestCase):
    """Local/remote inference, host_port, install-iff-local, secret slots,
    common_secrets binding, and the capabilities: sugar."""

    def test_remote_plugin_full_derivation(self):
        d = derive({"plugins": ["gateway"]})
        self.assertEqual(d["PLUGINS"], "gateway")
        self.assertEqual(d["HOST_MCP_PORTS"], "8811")
        self.assertEqual(json.loads(d["PLUGIN_MCP_ENTRIES"].strip()), GATEWAY["mcp"])
        # env-scoped secret → one SLOT<tab>SOURCE<tab>HINT record
        self.assertEqual(
            d["PLUGIN_ENV_SECRETS"],
            "MCP_GATEWAY_TOKEN\tMCP_GATEWAY_TOKEN\tgateway (run ./service.sh gateway once)\n")
        # remote servers add no domain egress (they dial host.docker.internal)
        self.assertEqual(d["EGRESS"], "")

    def test_install_required_only_for_local_servers(self):
        # local without install: → error
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]},
                   plugin_files={"p": {"mcp": {"s": {"command": "x"}}}})
        self.assertIn("needs an install: block", str(cm.exception))
        # remote without install: → fine
        derive({"plugins": ["p"]},
               plugin_files={"p": {"host_port": 9000, "mcp": {"s": {"url": "http://host.docker.internal:9000/mcp"}}}})
        # egress-only plugin (no mcp) needs no install: either
        derive({"plugins": ["p"]}, plugin_files={"p": {"egress": ["a.com"]}})

    def test_host_port_only_with_remote_and_integer(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]},
                   plugin_files={"p": {"install": "x", "host_port": 8811,
                                       "mcp": {"s": {"command": "x"}}}})
        self.assertIn("host_port is only valid with a remote", str(cm.exception))
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]},
                   plugin_files={"p": {"host_port": "8811",
                                       "mcp": {"s": {"url": "http://h/mcp"}}}})
        self.assertIn("host_port must be an integer", str(cm.exception))
        # out-of-range (typo like 88111) is a named error, not a bogus grant
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]},
                   plugin_files={"p": {"host_port": 88111,
                                       "mcp": {"s": {"url": "http://h/mcp"}}}})
        self.assertIn("out of range (1-65535)", str(cm.exception))

    def test_secret_scope_validation(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"]},
                   plugin_files={"p": {"secrets": {"TOK": "bogus"}}})
        self.assertIn("scope must be 'env' or 'agent'", str(cm.exception))
        # bare string scope form works
        d = derive({"plugins": ["p"]},
                   plugin_files={"p": {"secrets": {"TOK": "env"}}})
        self.assertEqual(d["PLUGIN_ENV_SECRETS"], "TOK\tTOK\t\n")

    def test_duplicate_secret_slot_across_plugins(self):
        files = {"a": {"secrets": {"TOK": "env"}}, "b": {"secrets": {"TOK": "env"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["a", "b"]}, plugin_files=files)
        self.assertIn("declared by more than one enabled plugin", str(cm.exception))

    def test_common_secrets_map_repoints_env_slot(self):
        d = derive({"plugins": ["gateway"], "common_secrets": {"MCP_GATEWAY_TOKEN": "GW_PROD"}})
        self.assertEqual(
            d["PLUGIN_ENV_SECRETS"],
            "MCP_GATEWAY_TOKEN\tGW_PROD\tgateway (run ./service.sh gateway once)\n")

    def test_common_secrets_list_passthrough(self):
        d = derive({"plugins": ["serena"], "common_secrets": ["PLAYWRIGHT_KEY"]})
        self.assertEqual(d["PLUGIN_ENV_SECRETS"], "PLAYWRIGHT_KEY\tPLAYWRIGHT_KEY\t\n")

    def test_common_secrets_remap_unknown_slot_errors(self):
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["serena"], "common_secrets": {"NOPE": "SRC"}})
        self.assertIn("no enabled plugin declares an env-scoped secret", str(cm.exception))

    def test_agent_scoped_slot_in_common_secrets_is_hard_error(self):
        files = {"p": {"secrets": {"KEY": "agent"}}}
        with self.assertRaises(m.ManifestError) as cm:
            derive({"plugins": ["p"], "common_secrets": {"KEY": "SRC"}}, plugin_files=files)
        self.assertIn("is agent-scoped", str(cm.exception))

    def test_agent_scoped_slot_is_not_an_env_secret(self):
        # declared, but not composed into PLUGIN_ENV_SECRETS (Phase 2 binds it)
        d = derive({"plugins": ["p"]}, plugin_files={"p": {"secrets": {"KEY": "agent"}}})
        self.assertEqual(d["PLUGIN_ENV_SECRETS"], "")

    def test_capabilities_sugar_dedups_with_explicit_plugin(self):
        d = derive({"plugins": ["gateway"], "capabilities": {"gateway": True}})
        self.assertEqual(d["PLUGINS"], "gateway")   # not "gateway gateway"

    def test_capabilities_sugar_appends_after_explicit(self):
        d = derive({"plugins": ["serena"], "capabilities": {"browser": True, "gateway": True}})
        self.assertEqual(d["PLUGINS"], "serena gateway browser")  # explicit, then sugar order


class TestPluginsV2Phase2(unittest.TestCase):
    """Agent-scoped secrets: agent_secrets bindings, the identities: sugar,
    agent-server routing, and the derived AGENT_* variables."""

    ENV = {"SECRET_KEY_VARS": "OBSIDIAN_KEY_me_claude OBSIDIAN_KEY_x_cursor_agent "
                              "OBSIDIAN_WATCH_KEY_me_claude PLAYWRIGHT_KEY",
           "SECRETS_FILE": "/sec/secrets.env"}

    def _d(self, man):
        return m.derive(man, PLUGIN_FILES, self.ENV)

    def test_explicit_agent_secrets_full_derivation(self):
        d = self._d({"plugins": ["obsidian-annotated", "annotated-watch"],
                     "agent_secrets": [
                         {"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_me_claude"},
                         {"agent": "claude", "slot": "ANNOTATED_WATCH_KEY", "secret": "OBSIDIAN_WATCH_KEY_me_claude"}]})
        self.assertEqual(
            d["AGENT_SECRETS"],
            "claude\tOBSIDIAN_ANNOTATED_KEY\tOBSIDIAN_KEY_me_claude\n"
            "claude\tANNOTATED_WATCH_KEY\tOBSIDIAN_WATCH_KEY_me_claude\n")
        # obsidian has a server (agent-scoped); watch does not
        self.assertEqual(d["AGENT_SERVER_SLOTS"], "OBSIDIAN_ANNOTATED_KEY")
        servers = json.loads(d["AGENT_SERVERS_JSON"])
        self.assertEqual(servers["OBSIDIAN_ANNOTATED_KEY"]["name"], "obsidian-annotated")
        self.assertEqual(servers["OBSIDIAN_ANNOTATED_KEY"]["spec"], OBSIDIAN["mcp"]["obsidian-annotated"])
        # agent-scoped plugins never land in the uniform plugin_mcp_entries
        self.assertEqual(d["PLUGIN_MCP_ENTRIES"], "")
        self.assertEqual(d["EGRESS"], "mcp-obsidian.dmetr.io")

    def test_unknown_agent_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["obsidian-annotated"],
                     "agent_secrets": [{"agent": "nope", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_me_claude"}]})
        self.assertIn("unknown agent 'nope'", str(cm.exception))

    def test_slot_not_agent_scoped_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["gateway"],
                     "agent_secrets": [{"agent": "claude", "slot": "MCP_GATEWAY_TOKEN", "secret": "OBSIDIAN_KEY_me_claude"}]})
        self.assertIn("not an agent-scoped secret", str(cm.exception))

    def test_agent_secret_source_missing(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["obsidian-annotated"],
                     "agent_secrets": [{"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_gone"}]})
        msg = str(cm.exception)
        self.assertIn("not found in /sec/secrets.env", msg)
        # names the source-var scope so a set-but-unscanned var isn't blamed as unset
        self.assertIn("OBSIDIAN_KEY_* / OBSIDIAN_WATCH_KEY_*", msg)

    def test_enabled_agent_plugin_without_binding_warns_inert(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = self._d({"plugins": ["obsidian-annotated"]})  # no agent_secrets
        self.assertEqual(d["AGENT_SECRETS"], "")
        self.assertIn("inert (wired for no agent)", err.getvalue())
        self.assertIn("OBSIDIAN_ANNOTATED_KEY", err.getvalue())

    def test_duplicate_agent_slot_binding_rejected(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["obsidian-annotated"],
                     "agent_secrets": [
                         {"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_me_claude"},
                         {"agent": "claude", "slot": "OBSIDIAN_ANNOTATED_KEY", "secret": "OBSIDIAN_KEY_x_cursor_agent"}]})
        self.assertIn("bound to slot 'OBSIDIAN_ANNOTATED_KEY' more than once", str(cm.exception))

    def test_agent_scoped_plugin_multiple_slots_rejected(self):
        files = dict(PLUGIN_FILES, bad={"secrets": {"A": "agent", "B": "agent"},
                                        "mcp": {"s": {"url": "http://h/mcp"}}})
        with self.assertRaises(m.ManifestError) as cm:
            m.derive({"plugins": ["bad"]}, files, self.ENV)
        self.assertIn("only one agent secret slot", str(cm.exception))

    def test_agent_scoped_plugin_multiple_servers_rejected(self):
        files = dict(PLUGIN_FILES, bad={"secrets": {"A": "agent"},
                                        "mcp": {"s1": {"url": "http://h/1"}, "s2": {"url": "http://h/2"}}})
        with self.assertRaises(m.ManifestError) as cm:
            m.derive({"plugins": ["bad"]}, files, self.ENV)
        self.assertIn("at most one mcp server", str(cm.exception))

    def test_agent_scoped_local_server_allowed(self):
        # An agent-scoped LOCAL (command:) server is valid now (axiom's
        # mcp-remote bridge) — it routes per-agent like a remote one, and still
        # requires an install: block (baked into the image).
        files = dict(PLUGIN_FILES, bad={"install": "x", "secrets": {"A": "agent"},
                                        "mcp": {"s": {"command": "bash"}}})
        d = m.derive({"plugins": ["bad"],
                      "agent_secrets": [{"agent": "claude", "slot": "A",
                                         "secret": "OBSIDIAN_KEY_me_claude"}]},
                     files, self.ENV)
        servers = json.loads(d["AGENT_SERVERS_JSON"])
        self.assertEqual(servers["A"]["spec"], {"command": "bash"})
        self.assertEqual(d["AGENT_SECRETS"], "claude\tA\tOBSIDIAN_KEY_me_claude\n")

    def test_agent_scoped_local_server_still_needs_install(self):
        files = dict(PLUGIN_FILES, bad={"secrets": {"A": "agent"},
                                        "mcp": {"s": {"command": "bash"}}})
        with self.assertRaises(m.ManifestError) as cm:
            m.derive({"plugins": ["bad"]}, files, self.ENV)
        self.assertIn("needs an install: block", str(cm.exception))

    def test_watch_is_env_only_no_server(self):
        d = self._d({"plugins": ["annotated-watch"],
                     "agent_secrets": [{"agent": "pi", "slot": "ANNOTATED_WATCH_KEY", "secret": "OBSIDIAN_WATCH_KEY_me_claude"}]})
        self.assertEqual(d["AGENT_SERVER_SLOTS"], "")        # no server
        self.assertEqual(d["AGENT_SERVERS_JSON"], "{}")
        self.assertEqual(d["AGENT_SECRETS"],
                         "pi\tANNOTATED_WATCH_KEY\tOBSIDIAN_WATCH_KEY_me_claude\n")


class TestAgentScopedGlobalFallback(unittest.TestCase):
    """scope: agent, global: true (axiom): a global token wires every enabled
    agent through the agent-scoped path; per-agent AXIOM_KEY_<agent> overrides.
    The five enabled MCP agents (DEFAULT_TOOLS minus aider) resolve in a fixed
    order: claude, codex, pi, gemini, cursor-agent."""

    SECRETS_FILE = "/sec/secrets.env"

    def _d(self, man, key_vars):
        return m.derive(man, PLUGIN_FILES,
                        {"SECRET_KEY_VARS": key_vars, "SECRETS_FILE": self.SECRETS_FILE})

    def test_global_token_wires_every_enabled_agent(self):
        # Just AXIOM_TOKEN set (no per-agent keys, no agent_secrets): all five
        # MCP agents are bound from the global token, in enabled order.
        d = self._d({"plugins": ["axiom"]}, "AXIOM_TOKEN")
        self.assertEqual(
            d["AGENT_SECRETS"],
            "claude\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "codex\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "pi\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "gemini\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "cursor-agent\tAXIOM_TOKEN\tAXIOM_TOKEN\n")
        # It is an agent-scoped server, not a uniform plugin entry.
        self.assertEqual(d["AGENT_SERVER_SLOTS"], "AXIOM_TOKEN")
        servers = json.loads(d["AGENT_SERVERS_JSON"])
        self.assertEqual(servers["AXIOM_TOKEN"]["name"], "axiom")
        self.assertEqual(servers["AXIOM_TOKEN"]["spec"], AXIOM["mcp"]["axiom"])
        self.assertEqual(d["PLUGIN_MCP_ENTRIES"], "")
        self.assertEqual(d["EGRESS"], "mcp.axiom.co")

    def test_per_agent_key_overrides_global(self):
        # cursor-agent gets its own key; the rest fall back to the global token.
        # The explicit binding wins and appears once (no duplicate synthetic).
        d = self._d({"plugins": ["axiom"],
                     "agent_secrets": [{"agent": "cursor-agent", "slot": "AXIOM_TOKEN",
                                        "secret": "AXIOM_KEY_cursor"}]},
                    "AXIOM_TOKEN AXIOM_KEY_cursor")
        self.assertEqual(
            d["AGENT_SECRETS"],
            "cursor-agent\tAXIOM_TOKEN\tAXIOM_KEY_cursor\n"
            "claude\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "codex\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "pi\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "gemini\tAXIOM_TOKEN\tAXIOM_TOKEN\n")

    def test_no_global_only_explicit_binding_wired(self):
        # Global token NOT set: no synthetic fallback, only the explicit
        # per-agent binding survives (AXIOM_KEY_* is now a valid source).
        d = self._d({"plugins": ["axiom"],
                     "agent_secrets": [{"agent": "cursor-agent", "slot": "AXIOM_TOKEN",
                                        "secret": "AXIOM_KEY_cursor"}]},
                    "AXIOM_KEY_cursor")
        self.assertEqual(d["AGENT_SECRETS"],
                         "cursor-agent\tAXIOM_TOKEN\tAXIOM_KEY_cursor\n")

    def test_restricted_tools_narrows_fallback(self):
        # Only the enabled tools get a synthetic global binding.
        d = m.derive({"plugins": ["axiom"], "tools": ["claude", "cursor"]},
                     PLUGIN_FILES,
                     {"SECRET_KEY_VARS": "AXIOM_TOKEN", "SECRETS_FILE": self.SECRETS_FILE})
        self.assertEqual(
            d["AGENT_SECRETS"],
            "claude\tAXIOM_TOKEN\tAXIOM_TOKEN\n"
            "cursor-agent\tAXIOM_TOKEN\tAXIOM_TOKEN\n")

    def test_nothing_set_is_inert_and_warns(self):
        import contextlib
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            d = self._d({"plugins": ["axiom"]}, "")   # neither global nor per-agent
        self.assertEqual(d["AGENT_SECRETS"], "")
        self.assertIn("inert (wired for no agent)", err.getvalue())
        self.assertIn("AXIOM_TOKEN", err.getvalue())

    def test_common_secrets_passthrough_still_valid(self):
        # Legacy `common_secrets: [AXIOM_TOKEN]` keeps working (list passthrough
        # delivers the shared env var); the global fallback also wires all agents.
        d = self._d({"plugins": ["axiom"], "common_secrets": ["AXIOM_TOKEN"]},
                    "AXIOM_TOKEN")
        self.assertEqual(d["PLUGIN_ENV_SECRETS"], "AXIOM_TOKEN\tAXIOM_TOKEN\t\n")
        self.assertEqual(d["AGENT_SECRETS"].count("\n"), 5)

    def test_global_flag_rejected_on_env_scope(self):
        files = dict(PLUGIN_FILES,
                     bad={"secrets": {"X": {"scope": "env", "global": True}},
                          "mcp": {"s": {"url": "http://h/mcp", "headers": {}}}})
        with self.assertRaises(m.ManifestError) as cm:
            m.derive({"plugins": ["bad"]}, files,
                     {"SECRET_KEY_VARS": "", "SECRETS_FILE": self.SECRETS_FILE})
        self.assertIn("global: true is only valid with scope: agent", str(cm.exception))

    def test_global_flag_must_be_bool(self):
        files = dict(PLUGIN_FILES,
                     bad={"secrets": {"X": {"scope": "agent", "global": "yes"}},
                          "mcp": {"s": {"url": "http://h/mcp", "headers": {}}}})
        with self.assertRaises(m.ManifestError) as cm:
            m.derive({"plugins": ["bad"]}, files,
                     {"SECRET_KEY_VARS": "", "SECRETS_FILE": self.SECRETS_FILE})
        self.assertIn("global must be true or false", str(cm.exception))

    def test_source_error_message_names_axiom_vars(self):
        with self.assertRaises(m.ManifestError) as cm:
            self._d({"plugins": ["axiom"],
                     "agent_secrets": [{"agent": "claude", "slot": "AXIOM_TOKEN",
                                        "secret": "AXIOM_KEY_gone"}]}, "AXIOM_TOKEN")
        self.assertIn("AXIOM_KEY_*", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
