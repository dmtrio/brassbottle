"""Run every agent's own tests as part of the repo-wide suite.

Convention: an agent may keep wiring contract tests beside its descriptor, as
``agents/<name>/test_*.py``. They then run both ways —

    cd agents/<name> && python3 -m unittest discover    # while hacking on it
    python3 -m unittest discover -s tests                # everything, in CI

with nothing to register: drop the file in and it is picked up. Test files are
kept out of the image by ``agents/*/test_*.py`` in .dockerignore.

Why load by path instead of ``loader.discover()``: discover() requires its
``top_level_dir`` to sit inside the tree being scanned, so pointing it at a
sibling directory raises "Path must be within the project" on 3.9. Each agent
directory goes on ``sys.path`` before its modules execute, and the repo's
``src/`` + ``tests/`` + ``agents/`` dirs are on ``sys.path`` first so ``import agent_test_kit``
and the machinery under test resolve. Modules are registered under a unique
name so two agents can both ship a ``test_wiring.py``.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent
AGENTS = REPO / "agents"

for path in (REPO / "src", REPO / "tests", REPO / "agents"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def agent_test_files():
    return sorted(AGENTS.glob("*/test_*.py"))


def _load(path: Path):
    """Import one agent test module under a collision-proof name."""
    agent = path.parent.name
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    name = f"agenttests_{agent}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AgentTestDiscovery(unittest.TestCase):
    """Guard the loader itself.

    The dangerous failure here is silent: if the glob or the import breaks, the
    suite still reports OK while quietly covering nothing. These assert that
    discovery found files and that each one actually contributed test cases.
    """

    def test_discovery_finds_agent_test_files(self):
        agents_with_tests = {p.parent.name for p in agent_test_files()}
        self.assertTrue(
            agents_with_tests,
            "no agents/*/test_*.py found — the loader would silently cover "
            "nothing. If no agent ships tests any more, delete this file.",
        )

    def test_every_agent_test_file_yields_test_cases(self):
        loader = unittest.TestLoader()
        for path in agent_test_files():
            with self.subTest(agent=path.parent.name, file=path.name):
                count = loader.loadTestsFromModule(_load(path)).countTestCases()
                self.assertGreater(count, 0, f"{path} defines no test cases")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(AgentTestDiscovery))
    for path in agent_test_files():
        suite.addTests(loader.loadTestsFromModule(_load(path)))
    return suite
