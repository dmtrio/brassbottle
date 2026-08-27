"""Guard Dockerfile install layers purge package-manager caches in the same RUN.

Build caches (~/.cache/uv, ~/.cache/pip, ~/.npm/_cacache) must not ship in the
image layer; cleaning in a later RUN does not shrink earlier layers.
"""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")

# RUN blocks split on Dockerfile stage comments; each block is one logical layer.
_RUN_BLOCKS = re.split(r"(?m)^# ── ", DOCKERFILE)


def _block_after(header_fragment: str) -> str:
    for block in _RUN_BLOCKS:
        if header_fragment in block:
            return block
    raise AssertionError(f"no Dockerfile section matching {header_fragment!r}")


class DockerfileCacheCleanTests(unittest.TestCase):
    def test_pip_packages_run_purges_pip_cache_in_same_layer(self):
        block = _block_after("Python packages")
        self.assertIn("pip3 install pipenv playwright", block)
        self.assertIn("pip3 cache purge", block)
        self.assertNotIn("pip cache purge", block)

    def test_uv_install_run_cleans_uv_cache_in_same_layer(self):
        block = _block_after("uv (Python package manager)")
        self.assertIn("uv/install.sh", block)
        self.assertIn("uv cache clean", block)

    def test_plugin_bake_run_cleans_uv_npm_and_pip_in_same_layer(self):
        block = _block_after("Plugins (drop-in local MCP tools)")
        self.assertIn("for f in /opt/plugins/*/plugin.yml", block)
        for cmd in ("uv cache clean", "npm cache clean --force", "pip3 cache purge"):
            self.assertIn(cmd, block)
        # Purge must follow the install loop, not precede it.
        loop_end = block.index("done;")
        for cmd in ("uv cache clean", "npm cache clean --force", "pip3 cache purge"):
            self.assertGreater(block.index(cmd), loop_end)

    def test_agent_bake_run_cleans_npm_and_pip_in_same_layer(self):
        block = _block_after("Agents (descriptor-driven install")
        self.assertIn("for f in /opt/agents/*/agent.yml", block)
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
