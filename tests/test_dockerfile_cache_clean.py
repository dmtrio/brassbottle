"""Guard Dockerfile install layers purge package-manager caches in the same RUN.

Build caches (~/.cache/uv, ~/.cache/pip, ~/.npm/_cacache) must not ship in the
image layer; cleaning in a later RUN does not shrink earlier layers.
"""

import re
import unittest
from pathlib import Path

from tests.dockerfile_lib import instructions as _instructions

REPO = Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")



_INSTRUCTIONS = _instructions(DOCKERFILE)


def _run_containing(marker: str) -> str:
    """The single RUN instruction containing `marker` — one image layer."""
    hits = [i for i in _INSTRUCTIONS if i.startswith("RUN") and marker in i]
    if not hits:
        raise AssertionError(f"no RUN instruction contains {marker!r}")
    if len(hits) > 1:
        raise AssertionError(f"{marker!r} appears in {len(hits)} RUN instructions")
    return hits[0]


class DockerfileCacheCleanTests(unittest.TestCase):
    def test_pip_packages_run_purges_pip_cache_in_same_layer(self):
        block = _run_containing("pip3 install pipenv playwright")
        self.assertIn("pip3 cache purge", block)
        self.assertNotIn("pip cache purge", block)

    def test_uv_install_run_cleans_uv_cache_in_same_layer(self):
        block = _run_containing("uv/install.sh")
        self.assertIn("uv cache clean", block)

    def test_plugin_bake_run_cleans_uv_npm_and_pip_in_same_layer(self):
        block = _run_containing("/tmp/plugin-install.sh")
        for cmd in ("uv cache clean", "npm cache clean --force", "pip3 cache purge"):
            self.assertIn(cmd, block)
        # Purge must follow the install loop, not precede it.
        loop_end = block.index("done;")
        for cmd in ("uv cache clean", "npm cache clean --force", "pip3 cache purge"):
            self.assertGreater(block.index(cmd), loop_end)

    def test_agent_bake_run_cleans_npm_and_pip_in_same_layer(self):
        block = _run_containing("/tmp/agent-install.sh")
        self.assertIn("npm cache clean --force", block)
        self.assertIn("pip3 cache purge", block)
        loop_end = block.index("done;")
        self.assertGreater(block.index("npm cache clean --force"), loop_end)
        self.assertGreater(block.index("pip3 cache purge"), loop_end)

    def test_runtime_tool_paths_are_not_deleted(self):
        forbidden = (
            "rm -rf $HOME/.cache/uv",
            "rm -rf ~/.cache/uv",
            "rm -rf $HOME/.local/share/uv",
            "rm -rf ~/.local/share/uv",
            "rm -rf $HOME/.fnm",
            "rm -rf ~/.fnm",
            "rm -rf $HOME/.local/bin",
            "rm -rf ~/.local/bin",
        )
        for pattern in forbidden:
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, DOCKERFILE)


if __name__ == "__main__":
    unittest.main()
