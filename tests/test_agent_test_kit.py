"""Unit tests for agents/agent_test_kit.py (stdlib only)."""
import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "agents"))
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

import agent_test_kit as kit  # noqa: E402


def _tree_text(root: Path) -> str:
    if not root.exists():
        return ""
    parts = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            parts.append(path.read_text(errors="replace"))
        elif path.is_symlink():
            parts.append(os.readlink(path))
    return "\n".join(parts)


class TestAgentTestKit(unittest.TestCase):
    SENTINEL = "HOST_IDENTITY_LEAK_SENTINEL_DO_NOT_COMMIT"

    def setUp(self):
        if shutil.which("yq") is None:
            self.skipTest("yq not available")

    def test_wire_run_env_never_inherits_host_identity_keys(self):
        key = "IDENTITY_KEY_0"
        prior = os.environ.get(key)
        os.environ[key] = self.SENTINEL
        try:
            result = kit.wire("cursor")
            self.assertNotIn(self.SENTINEL, _tree_text(kit.AGENT_SCRATCH_ROOT))
            self.assertEqual(result._run_env.get(key), "TEST_SECRET")
        finally:
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

    def test_golden_regen_requires_wire_fn(self):
        golden = Path(__file__).parent / "_regen_fixture"
        golden.mkdir(exist_ok=True)
        try:
            with patch.dict(os.environ, {"GOLDEN_REGEN": "1"}):
                with self.assertRaisesRegex(RuntimeError, "wire_fn"):
                    kit.assert_matches_golden(None, golden)
        finally:
            if golden.exists():
                shutil.rmtree(golden)


if __name__ == "__main__":
    unittest.main()
