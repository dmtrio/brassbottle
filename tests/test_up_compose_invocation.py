"""Guards the `docker compose up` invocation in up.sh.

The invocation is a chain of env-var prefixes joined by trailing backslashes,
ending in the `docker compose` call. That shape has a silent failure mode:
a backslash-newline splices the following line in, so a comment or blank line
inserted mid-chain turns every prefix above it into a commented-out no-op and
compose runs with all of those variables unset. `bash -n` still passes, and so
does any grep that only looks for the flag — the damage is only visible at
runtime as `invalid spec: :/artifacts: empty section between colons`.

These tests splice the continuations into logical lines and assert the env
prefixes and the compose call really are one command.
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UP_SH = REPO_ROOT / "up.sh"
COMPOSE_LOCAL = REPO_ROOT / "compose" / "docker-compose.local.yml"

# Variables the compose files interpolate; if the prefix chain is broken these
# reach compose as blank strings and produce malformed mounts/ports.
REQUIRED_PREFIXES = [
    "CONTAINER_NAME",
    "AGENTS_ENABLED",
    "PLUGINS_ENABLED",
    "ARTIFACTS_PATH",
    "BROWSER_TMP_PATH",
    "KEYS_PATH",
    "RULES_PATH",
    "IMAGE_TAG",
    "REMOTE_JUMP",
    "REMOTE_SHELL",
    "DJINN_JUMP_IP",
    "JUMP_AUTHORIZED_KEY",
    "EGRESS_BROKER_HOST",
]


def logical_lines(text):
    """Splice backslash-continued physical lines into logical lines."""
    lines, current = [], ""
    for physical in text.splitlines():
        current += physical
        if current.endswith("\\"):
            current = current[:-1]  # keep splicing
        else:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)
    return lines


class TestComposeInvocation(unittest.TestCase):
    def setUp(self):
        self.lines = logical_lines(UP_SH.read_text())
        matches = [l for l in self.lines if re.search(r"\bdocker compose\b.*\bup\b", l)]
        self.assertEqual(
            len(matches), 1, "expected exactly one `docker compose ... up` invocation"
        )
        self.invocation = matches[0]

    def test_project_directory_is_pinned(self):
        """Without this, `context: .` resolves to compose/ and the build fails."""
        self.assertIn(
            '--project-directory "$SCRIPT_DIR"',
            self.invocation,
            "compose must be pinned to the repo root, else the build context "
            "resolves to compose/ where there is no Dockerfile",
        )

    def test_env_prefixes_reach_the_compose_call(self):
        """The prefix chain and the compose call must be ONE logical line."""
        for var in REQUIRED_PREFIXES:
            with self.subTest(var=var):
                self.assertRegex(
                    self.invocation,
                    rf"\b{var}=",
                    f"{var} is not part of the compose command's logical line — "
                    "a comment or blank line has broken the backslash chain, so "
                    "compose will run with this variable unset",
                )

    def test_no_comment_swallowed_by_a_continuation(self):
        """A commented-out fragment inside the chain means the splice broke."""
        self.assertNotRegex(
            self.invocation,
            r"#",
            "the compose invocation's logical line contains a '#' — a comment "
            "was spliced in by a trailing backslash, commenting out the rest "
            "of the command",
        )

    def test_jump_key_is_resolved_before_compose(self):
        text = UP_SH.read_text()
        marker = 'jump_host.py" authorized-key'
        self.assertIn(
            marker,
            text,
            "up.sh must resolve the jump public key via jump_host.py authorized-key",
        )
        compose_idx = text.find("docker compose")
        key_idx = text.find(marker)
        self.assertLess(
            key_idx,
            compose_idx,
            "the jump key resolution must run before the docker compose invocation",
        )
        self.assertIn(
            'if JUMP_AUTHORIZED_KEY="$(',
            text,
            "key resolution must use the non-fatal `if VAR=$(...)` shape so set -e cannot abort",
        )
        self.assertIn(
            'if [ "$REMOTE_JUMP" != "true" ] && [ -z "$SSH_PORT" ]; then',
            text,
            "the skip must require BOTH remote.jump: false and no published ssh: — "
            "remote_access.py appends the jump key in published mode too",
        )

    def test_jump_ip_is_resolved_after_ensure_net_before_compose(self):
        """Mirror of the jump-key ordering pin: up.sh resolves DJINN_JUMP_IP
        with `jump_host.py ip` (live-bridge derivation), which needs the
        bridge ensure_net created/verified."""
        text = UP_SH.read_text()
        marker = 'jump_host.py" ip 2>"$JUMP_IP_ERR"'
        self.assertIn(
            marker,
            text,
            "up.sh must resolve the jump address via jump_host.py ip",
        )
        ensure_idx = text.find('src/ensure_net.py" "$DESIRED_SUBNET"')
        ip_idx = text.find(marker)
        compose_idx = text.find("docker compose")
        self.assertLess(ensure_idx, ip_idx, "ensure_net must run before the jump IP resolution")
        self.assertLess(ip_idx, compose_idx, "the jump IP resolution must run before docker compose")
        self.assertIn(
            'if JUMP_IP="$(',
            text,
            "jump IP resolution must use the non-fatal `if VAR=$(...)` shape so set -e cannot abort",
        )

    def test_egress_broker_host_is_resolved_after_ensure_net_before_compose(self):
        """The bottle's firewall grant and EGRESS_BROKER_HOST env must name the
        address the broker is ACTUALLY on: egress start derives it from the
        LIVE djinn-net bridge when that has drifted from DJINN_SUBNET, so up.sh
        must resolve it with the same `egress_service.py ip` resolver (after
        ensure_net created/read the bridge), not the manifest-derived
        desired-subnet value."""
        text = UP_SH.read_text()
        marker = 'egress_service.py" ip 2>"$EGRESS_HOST_ERR"'
        self.assertIn(
            marker,
            text,
            "up.sh must resolve the egress broker address via egress_service.py ip",
        )
        ensure_idx = text.find('src/ensure_net.py" "$DESIRED_SUBNET"')
        ip_idx = text.find(marker)
        compose_idx = text.find("docker compose")
        self.assertLess(ensure_idx, ip_idx, "ensure_net must run before the egress broker host resolution")
        self.assertLess(ip_idx, compose_idx, "the egress broker host resolution must run before docker compose")
        self.assertIn(
            'if EGRESS_BROKER_HOST="$(',
            text,
            "broker host resolution must use the non-fatal `if VAR=$(...)` shape so set -e cannot abort",
        )
        # Non-fatal on resolver failure: the manifest-derived desired-subnet
        # value is kept as the fallback, and the resolver's stderr is surfaced.
        self.assertIn(
            'EGRESS_BROKER_HOST="$EGRESS_BROKER_HOST_MANIFEST"',
            text,
            "on resolver failure the manifest-derived fallback must be kept",
        )
        self.assertIn(
            '⚠ egress: $line',
            text,
            "resolver stderr must be surfaced with an `⚠ egress:` prefix",
        )

    def test_compose_env_passes_the_resolved_egress_broker_host(self):
        """The compose invocation must carry the RESOLVED variable, not a
        re-derivation from the desired subnet."""
        self.assertIn(
            'EGRESS_BROKER_HOST="$EGRESS_BROKER_HOST"',
            self.invocation,
            "compose must receive the resolved EGRESS_BROKER_HOST variable",
        )
        self.assertNotIn(
            'EGRESS_BROKER_HOST="${EGRESS_BROKER_HOST:-}"',
            self.invocation,
            "the raw manifest-derived default must not be passed to compose",
        )

    def test_egress_broker_host_is_defined_on_the_disabled_path(self):
        """With the broker disabled nothing derives EGRESS_BROKER_HOST, yet the
        compose hand-off expands it unconditionally. up.sh must define it on
        every path: the resolver block is extracted verbatim and run under
        `bash -u` with ENABLE_EGRESS_BROKER=false, which fails on an unbound
        variable the moment the initialisation line goes missing."""
        import subprocess
        text = UP_SH.read_text()
        start = text.index("# \u2500\u2500 Egress broker host resolution")
        end = text.index("\nfi\n", start) + len("\nfi\n")
        block = text[start:end]
        self.assertIn("egress_service.py\" ip", block)
        self.assertIn('EGRESS_BROKER_HOST="${EGRESS_BROKER_HOST:-}"', block)
        script = (
            "set -eu\n"
            "ENABLE_EGRESS_BROKER=false\n"
            "unset EGRESS_BROKER_HOST\n"
            + block
            + '\nprintf "host=[%s]" "$EGRESS_BROKER_HOST"\n'
        )
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "host=[]")

    def test_compose_agents_enabled_default_is_fail_closed(self):
        compose_text = COMPOSE_LOCAL.read_text()
        self.assertIn(
            'AGENTS_ENABLED: "${AGENTS_ENABLED-}"',
            compose_text,
            "compose default must be empty unless AGENTS_ENABLED is explicitly exported "
            "(up.sh always sets it from manifest derive)",
        )

    def test_compose_plugins_enabled_default_is_fail_closed(self):
        compose_text = COMPOSE_LOCAL.read_text()
        self.assertIn(
            'PLUGINS_ENABLED: "${PLUGINS_ENABLED-}"',
            compose_text,
            "compose default must be empty unless PLUGINS_ENABLED is explicitly exported "
            "(up.sh always sets it from manifest derive)",
        )


if __name__ == "__main__":
    unittest.main()
