"""Guard the herdr install layer's position: above the plugin bake loop.

A plugin's `install:` block may call herdr, and install: blocks run in the
plugin bake RUN — so the herdr binary and its baked config.toml must already
exist in earlier layers. A Dockerfile reorder that dropped the herdr install
below the loop would fail only at build time, inside some plugin's install:
block, where the error reads as a broken plugin rather than a broken
base-image order. This pins the order by parsing the Dockerfile text.
"""

import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")

# The herdr download URL appears exactly once, inside the herdr install RUN;
# the config COPY and the plugin bake loop are identified by their own unique
# lines. Comment lines are stripped first so a comment mentioning a marker
# cannot stand in for the instruction (the same rule the Dockerfile parser
# applies, and the same trap tests/test_plugin_gate_execution.py documents).
_LINES = [l for l in (raw.strip() for raw in DOCKERFILE.splitlines())
          if l and not l.startswith("#")]


def _instructions():
    """Logical instructions, backslash-continuations joined — the boundaries
    the Dockerfile parser actually sees (same shape as
    test_dockerfile_cache_clean._instructions)."""
    out, cur = [], []
    for line in _LINES:
        cur.append(line)
        if not line.endswith("\\"):
            out.append("\n".join(cur))
            cur = []
    if cur:
        out.append("\n".join(cur))
    return out

HERDR_URL = "https://github.com/herdrdev/herdr/releases/download"
HERDR_CONFIG_COPY = "COPY --chown=$USERNAME:$USERNAME src/herdr-config.toml"
PLUGIN_BAKE_COPY = "COPY --chown=$USERNAME:$USERNAME plugins /opt/plugins"
PLUGIN_BAKE_LOOP = "for f in /opt/plugins/*/plugin.yml"
# Three RUNs loop over agent.yml; only the install loop runs the install: block.
AGENT_INSTALL_LOOP_BODY = "/tmp/agent-install.sh"


def _arg_names(instruction):
    """Names an ARG instruction declares: `ARG A=1 B \\\n C=2` declares A, B, C.
    Anything else declares none."""
    tokens = instruction.replace("\\\n", " ").split()
    if not tokens or tokens[0] != "ARG":
        return []
    return [t.split("=", 1)[0] for t in tokens[1:]]


def _positions(*markers):
    """Line index of each marker, failing loudly if any is missing."""
    return [_index(m) for m in markers]


def _index(marker):
    hits = [i for i, l in enumerate(_LINES) if marker in l]
    if not hits:
        raise AssertionError(f"marker not found in Dockerfile: {marker!r}")
    if len(hits) > 1:
        raise AssertionError(f"marker appears {len(hits)}x in Dockerfile: {marker!r}")
    return hits[0]


class DockerfileLayerOrderTests(unittest.TestCase):
    def test_herdr_download_run_precedes_plugin_bake_loop(self):
        herdr_run, bake_loop = _positions(HERDR_URL, PLUGIN_BAKE_LOOP)
        self.assertLess(herdr_run, bake_loop)

    def test_herdr_config_copy_precedes_plugin_bake_loop(self):
        herdr_copy, bake_loop = _positions(HERDR_CONFIG_COPY, PLUGIN_BAKE_LOOP)
        self.assertLess(herdr_copy, bake_loop)

    def test_herdr_layers_precede_plugin_copy_and_bake_run(self):
        """The COPY of the plugin tree, not just the loop body, comes after."""
        herdr_run, herdr_copy, bake_copy = _positions(
            HERDR_URL, HERDR_CONFIG_COPY, PLUGIN_BAKE_COPY)
        self.assertLess(herdr_run, bake_copy)
        self.assertLess(herdr_copy, bake_copy)

    def test_exactly_one_herdr_download_run_exists(self):
        """A stray second install (a leftover from a move, say) would re-download
        and could silently disagree on version or sha."""
        hits = [i for i in _instructions()
                if i.startswith("RUN") and HERDR_URL in i]
        self.assertEqual(len(hits), 1)

    def test_herdr_download_run_uses_sudo(self):
        """The install runs as coder (above USER root), so writes to
        /usr/local/bin must go through sudo, like the yq install beside it."""
        idx = _index(HERDR_URL)
        run_idx = next(i for i in range(idx, -1, -1) if _LINES[i].startswith("RUN"))
        # Join the instruction's continuation lines, comments already stripped.
        block, i = [], run_idx
        while True:
            block.append(_LINES[i])
            if not _LINES[i].rstrip().endswith("\\"):
                break
            i += 1
        block = "\n".join(block)
        self.assertIn("sudo curl", block)
        self.assertIn("sudo chmod 755 /usr/local/bin/herdr", block)


class EnabledSetArgScopeTests(unittest.TestCase):
    """Every RUN below an ARG sees it as env, so its value keys that RUN's
    cache. AGENTS_ENABLED / PLUGINS_ENABLED differ per bottle: declared at the
    top they gave each distinct set a private copy of the whole toolchain
    (apt, node, uv — GBs), sharing only the FROM layer. Each must sit directly
    above the loop that reads it."""

    def _arg_index(self, arg_line):
        ins = _instructions()
        # Any declaration counts, not just this spelling: a bare
        # `ARG PLUGINS_ENABLED`, or `ARG OTHER=1 PLUGINS_ENABLED`, re-added at
        # the top would fork the cache too.
        name = arg_line.split("=")[0].split()[1]
        hits = [i for i, x in enumerate(ins) if name in _arg_names(x)]
        self.assertEqual(len(hits), 1, f"expected exactly one ARG {name}")
        self.assertEqual(ins[hits[0]], arg_line)
        return ins, hits[0]

    def _next_run(self, ins, idx):
        return next(x for x in ins[idx + 1:] if x.startswith("RUN"))

    def test_plugins_enabled_arg_sits_below_the_toolchain(self):
        ins, arg = self._arg_index('ARG PLUGINS_ENABLED=""')
        herdr = next(i for i, x in enumerate(ins) if HERDR_CONFIG_COPY in x)
        self.assertGreater(arg, herdr)

    def test_first_run_below_plugins_enabled_arg_is_the_plugin_loop(self):
        ins, arg = self._arg_index('ARG PLUGINS_ENABLED=""')
        self.assertIn(PLUGIN_BAKE_LOOP, self._next_run(ins, arg))

    def test_first_run_below_agents_enabled_arg_is_the_agent_loop(self):
        ins, arg = self._arg_index('ARG AGENTS_ENABLED=""')
        self.assertIn(AGENT_INSTALL_LOOP_BODY, self._next_run(ins, arg))

    def test_agents_enabled_arg_sits_below_the_plugin_loop(self):
        """So a different agent set leaves the plugin layer cached."""
        ins, arg = self._arg_index('ARG AGENTS_ENABLED=""')
        loop = next(i for i, x in enumerate(ins) if PLUGIN_BAKE_LOOP in x)
        self.assertGreater(arg, loop)


class UserSectionSystemPathTests(unittest.TestCase):
    """Between `USER $USERNAME` and `USER root` the build runs unprivileged, and a
    COPY there lands root-owned. A bare `chmod` on a system path in that
    section fails only at build time ("Operation not permitted"), which the
    unit suites never reach: a COPY of the shim template plus `chmod 644`
    broke CI that way. Any chmod on a path outside the home directory in the
    user section must go through sudo."""

    def _user_section(self):
        ins = _instructions()
        start = next(i for i, x in enumerate(ins) if x.startswith("USER $USERNAME"))
        end = next(i for i, x in enumerate(ins) if x.startswith("USER root"))
        self.assertLess(start, end)
        return ins[start:end]

    def test_system_path_chmod_in_the_user_section_uses_sudo(self):
        offenders = []
        for ins in self._user_section():
            if not ins.startswith("RUN"):
                continue
            for cmd in ins.replace("\\\n", " ").split(";"):
                for part in cmd.split("&&"):
                    part = part.strip()
                    if part.startswith("RUN "):
                        part = part[4:].strip()
                    if part.startswith("chmod ") and "/home/" not in part:
                        offenders.append(part)
        self.assertEqual(offenders, [], "bare chmod on a system path while unprivileged: %r" % offenders)


if __name__ == "__main__":
    unittest.main()
