"""One bad plugin must not cost every agent its MCP wiring.

Two failure modes, both previously fatal or silent:

1. An agent whose descriptor cannot express a server's auth scheme (kimi's
   `bearerTokenEnvVar` against an `X-API-Key` remote) used to raise out of
   build_payload, aborting wiring for EVERY agent. It was then narrowed to
   skipping that one agent/server pair; now it does not fail at all — the agent
   takes the mcp-remote shim, which expresses any header.
2. A local server whose binary never made it into the image used to wire
   anyway, surfacing later as an agent MCP server that would not start, with
   nothing in the up.sh output explaining why. It is now dropped with a
   warning naming the plugin and the binary.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import wire_plugins  # noqa: E402

REPO = Path(__file__).parent.parent

REMOTE_XAPIKEY = {"url": "http://host.docker.internal:8814/mcp",
                  "headers": {"X-API-Key": "${BROWSER_KEY}"}}
REMOTE_BEARER = {"url": "https://example.test/mcp",
                 "headers": {"Authorization": "Bearer ${TOK}"}}


def _agent(binary, **over):
    a = {"binary": binary, "config_path": f".{binary}/mcp.json",
         "format": "json", "dialect": "mcpServers", "env_refs": False,
         "strategy": "", "settings": {}}
    a.update(over)
    return a


class NonBearerRemoteFallsBackToShimTests(unittest.TestCase):
    """Fix 1, at the root: an unrenderable auth scheme costs nobody anything."""

    def _build(self, servers, agents, effective):
        env = {
            "AGENTS_MCP_JSON": json.dumps(agents),
            "AGENT_SERVERS_JSON": json.dumps(servers),
            # agent \t slot \t source-env, one binding per line.
            "AGENT_SECRETS": "\n".join(
                f"{a}\t{s}\tKEY_{a.upper()}" for a, s in effective),
            "PLUGIN_MCP_ENTRIES": "",
        }
        err = io.StringIO()
        with redirect_stderr(err):
            payload = wire_plugins.build_payload(env)
        return payload, err.getvalue()

    def _wire(self, payload, agents):
        """Render the payload for real — the fallback happens container-side."""
        with tempfile.TemporaryDirectory() as tmp:
            home, ws = Path(tmp), Path(tmp) / "workspace"
            (ws / "repos").mkdir(parents=True)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                wire_plugins.run({**payload, "agents": agents}, home, ws, {})
            return {a["binary"]: (home / a["config_path"]).read_text()
                    for a in agents
                    if (home / a["config_path"]).exists()}, err.getvalue()

    def test_xapikey_remote_reaches_every_bound_agent(self):
        """The exact shape that aborted `djinn up coding-brassbottle`.

        X-API-Key is not a bearer header, so kimi's `bearerTokenEnvVar` cannot
        express it — which is why the payload build died. kimi now gets the
        server over the shim, and claude is unaffected either way.
        """
        agents = [_agent("kimi", env_refs="bearerTokenEnvVar"),
                  _agent("cursor-agent", env_refs=False, dialect="url")]
        payload, _ = self._build(
            {"browser": {"spec": REMOTE_XAPIKEY, "requires": ["BROWSER_KEY"]}},
            agents, [("kimi", "BROWSER_KEY"), ("cursor-agent", "BROWSER_KEY")],
        )
        entry = next(s for s in payload["agent_servers"] if s["name"] == "browser")
        self.assertIn("kimi", entry["ref"])
        self.assertIn("cursor-agent", entry["local"])

        configs, err = self._wire(payload, agents)
        for binary, raw in configs.items():
            self.assertIn("mcp-remote", raw, binary)
            self.assertIn("X-API-Key: ${BROWSER_KEY}", raw, binary)
        self.assertIn("kimi", err, "the fallback is announced, not silent")

    def test_bearer_remote_still_renders_natively(self):
        """The fallback must be narrow — a legal bearer remote stays native."""
        agents = [_agent("kimi", env_refs="bearerTokenEnvVar")]
        payload, _ = self._build(
            {"obs": {"spec": REMOTE_BEARER, "requires": ["TOK"]}},
            agents, [("kimi", "TOK")],
        )
        configs, _ = self._wire(payload, agents)
        raw = configs["kimi"]
        self.assertIn('"bearerTokenEnvVar": "TOK"', raw)
        self.assertNotIn("mcp-remote", raw)

    def test_no_agent_is_ever_dropped_for_an_auth_scheme(self):
        payload, _ = self._build(
            {"browser": {"spec": REMOTE_XAPIKEY, "requires": ["BROWSER_KEY"]}},
            [_agent("kimi", env_refs="bearerTokenEnvVar")],
            [("kimi", "BROWSER_KEY")],
        )
        self.assertEqual(payload["agent_servers"][0]["ref"], ["kimi"])


class MissingLocalBinaryTests(unittest.TestCase):
    """Fix 2: drop the server, warn, keep wiring everything else."""

    def test_missing_command_is_detected(self):
        self.assertTrue(wire_plugins._command_missing(
            {"command": "definitely-not-installed-xyz", "args": []}))

    def test_present_command_is_not_flagged(self):
        self.assertFalse(wire_plugins._command_missing({"command": "sh"}))

    def test_empty_or_absent_command_counts_as_missing(self):
        self.assertTrue(wire_plugins._command_missing({"command": ""}))
        self.assertTrue(wire_plugins._command_missing({}))

    def test_only_the_broken_server_is_dropped(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            kept = wire_plugins._drop_missing_local(
                {"good": {"command": "sh", "args": []},
                 "broken": {"command": "definitely-not-installed-xyz"},
                 "remote": {"url": "https://example.test/mcp"}},
                "plugins", os.environ["PATH"])
        self.assertEqual(set(kept), {"good", "remote"},
                         "a remote spec has no command and must pass through")
        out = buf.getvalue()
        self.assertIn("broken", out)
        self.assertIn("definitely-not-installed-xyz", out)
        self.assertNotIn("good", out)

    def test_run_skips_broken_local_and_still_wires_the_rest(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        (home / ".ag").mkdir(parents=True)
        payload = {
            "agents": [_agent("ag", config_path=".ag/mcp.json")],
            "plugin_mcp_entries": [
                {"good": {"command": "sh", "args": ["-c", "true"]}},
                {"broken": {"command": "definitely-not-installed-xyz"}},
            ],
            "agent_servers": [],
        }
        buf = io.StringIO()
        with redirect_stdout(buf):
            wire_plugins.run(payload, str(home), str(tmp / "ws"),
                             {"PATH": os.environ["PATH"]})
        written = json.loads((home / ".ag" / "mcp.json").read_text())
        self.assertIn("good", written["mcpServers"])
        self.assertNotIn("broken", written["mcpServers"])
        self.assertIn("broken", buf.getvalue())


    def test_no_path_means_no_check(self):
        """Without a caller-supplied PATH the check is SKIPPED, not guessed.

        Wiring must not depend on what happens to be installed wherever the
        code runs — that made golden fixtures pass in a dev container and fail
        in CI. Production always supplies one (main() passes os.environ).
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            kept = wire_plugins._drop_missing_local(
                {"broken": {"command": "definitely-not-installed-xyz"}},
                "plugins", None)
        self.assertEqual(set(kept), {"broken"})
        self.assertEqual(buf.getvalue(), "", "silent — nothing was checked")


class AmbientPathDeterminismTests(unittest.TestCase):
    """REGRESSION: wiring output must not depend on the ambient environment.

    The first cut of the missing-binary check called shutil.which against
    whatever PATH the process happened to carry. Wiring then varied by machine:
    in a dev container serena/mcp-remote/codebase-memory-mcp exist so every
    server wired and the golden fixtures matched, while on the CI runner they
    do not exist, the check dropped them, and test_golden_parity failed with
    "full: file bytes changed: home/.claude.json".

    A test asserting the CONTRACT (no path -> no check) can still pass if
    someone reintroduces an ambient fallback inside _command_missing, so pin
    the OBSERVABLE property instead: same payload in, same bytes out, whatever
    is or isn't installed.
    """

    PAYLOAD = {
        "agents": [_agent("ag", config_path=".ag/mcp.json")],
        "plugin_mcp_entries": [
            # 'sh' exists everywhere; the other cannot exist anywhere. If
            # ambient PATH ever leaks back in, these two diverge between runs.
            {"real-bin": {"command": "sh", "args": ["-c", "true"]}},
            {"absent-bin": {"command": "definitely-not-installed-xyz"}},
        ],
        "agent_servers": [],
    }

    def _wire_with_ambient_path(self, ambient):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = tmp / "home"
        (home / ".ag").mkdir(parents=True)
        before = os.environ.get("PATH")
        os.environ["PATH"] = ambient
        try:
            with redirect_stdout(io.StringIO()):
                # env carries no PATH — exactly how every existing test and any
                # non-up.sh caller invokes run().
                wire_plugins.run(self.PAYLOAD, str(home), str(tmp / "ws"), {})
        finally:
            if before is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = before
        return (home / ".ag" / "mcp.json").read_bytes()

    def test_wiring_is_byte_identical_across_ambient_paths(self):
        rich = self._wire_with_ambient_path(os.defpath + ":/usr/local/bin")
        bare = self._wire_with_ambient_path("/nonexistent-path-for-this-test")
        self.assertEqual(rich, bare,
                         "wiring changed with ambient PATH — the missing-binary "
                         "check must only run on a caller-supplied PATH")

    def test_unchecked_wiring_keeps_every_server(self):
        """And the deterministic result is 'wire everything', not 'drop it'."""
        written = json.loads(self._wire_with_ambient_path(os.defpath))
        self.assertEqual(set(written["mcpServers"]), {"real-bin", "absent-bin"})


class PayloadStdoutIsJsonOnlyTests(unittest.TestCase):
    """REGRESSION: --build-payload's stdout IS the payload. Nothing else.

    main() prints the payload JSON to stdout; up.sh captures it into $PAYLOAD
    and pipes it to the container half. The first cut of the skip warning went
    to stdout, so the JSON was preceded by a warning line, the container half
    died on `invalid JSON payload on stdin`, and `djinn up` aborted under
    set -e anyway — re-breaking the exact bug the skip exists to fix.

    The in-process tests could not catch it: they call build_payload() directly
    under redirect_stdout, which never exercises the stdout CONTRACT. This runs
    the real subprocess entrypoint, so any future print() that forgets
    file=sys.stderr fails here.
    """

    SRC = Path(__file__).parent.parent / "src" / "wire_plugins.py"

    def test_stdout_stays_parseable_with_an_unrenderable_pairing(self):
        env = dict(
            os.environ,
            AGENTS_MCP_JSON=json.dumps([_agent("kimi", env_refs="bearerTokenEnvVar")]),
            AGENT_SERVERS_JSON=json.dumps(
                {"browser": {"spec": REMOTE_XAPIKEY, "requires": ["BROWSER_KEY"]}}),
            AGENT_SECRETS="kimi\tBROWSER_KEY\tSRC",
            PLUGIN_MCP_ENTRIES="",
        )
        proc = subprocess.run(
            [sys.executable, str(self.SRC), "--build-payload"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The whole point: stdout parses, and carries the server.
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["agent_servers"][0]["ref"], ["kimi"])
        self.assertNotIn("⚠", proc.stdout)
        self.assertNotIn("·", proc.stdout)


class ImagePathContractTests(unittest.TestCase):
    """The missing-binary check is only as good as the PATH it resolves on.

    Node globals (mcp-remote, and every other `npm install -g`) live under
    ~/.fnm/aliases/default/bin. Wiring runs via `docker exec`, which inherits
    the image's ENV PATH and NOT a login shell's. If that entry is ever
    dropped from ENV PATH, _command_missing reports every node-based plugin as
    missing and — under skip-with-warning — silently stops wiring working
    servers. That is a worse failure than the one this PR fixes, so pin it.
    """

    def test_env_path_includes_fnm_default_alias_bin(self):
        dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
        env_paths = re.findall(r'(?m)^ENV PATH="([^"]+)"', dockerfile)
        self.assertTrue(env_paths, "no ENV PATH in Dockerfile")
        self.assertTrue(
            any("/.fnm/aliases/default/bin" in p for p in env_paths),
            "ENV PATH must include ~/.fnm/aliases/default/bin so `docker exec` "
            "resolves npm-global binaries; without it _command_missing reports "
            "every node plugin missing and silently drops working servers")


if __name__ == "__main__":
    unittest.main()
