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


if __name__ == "__main__":
    unittest.main()
