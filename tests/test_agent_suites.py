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
directory is appended to ``sys.path`` before its modules execute, and the repo's
``src/`` + ``tests/`` + ``agents/`` dirs are prepended first so ``import agent_test_kit``
and ``import manifest`` resolve to the repo modules before any agent-local
``manifest.py`` decoy. Modules are registered under an agent-name-based name so
two agents can both ship a ``test_wiring.py``.
"""
import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).parent.parent
AGENTS = REPO / "agents"
SRC_MANIFEST = REPO / "src" / "manifest.py"

for path in (REPO / "src", REPO / "tests", REPO / "agents"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def agent_test_files():
    return sorted(AGENTS.glob("*/test_*.py"))


def agent_dirs_with_descriptor():
    return sorted(
        p.parent.name
        for p in AGENTS.glob("*/agent.yml")
        if p.is_file()
    )


def _load(path: Path):
    """Import one agent test module under a collision-proof agent-based name."""
    agent_dir = str(path.parent)
    if agent_dir not in sys.path:
        sys.path.append(agent_dir)
    agent = path.parent.name
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

    def test_yq_is_available(self):
        if shutil.which("yq") is not None:
            return
        self.fail(
            "yq not installed — agent wiring contract tests did NOT run "
            "(up.sh requires yq; a skip here would report green while testing nothing)"
        )

    def test_every_agent_with_descriptor_ships_wiring_contract(self):
        missing = []
        for name in agent_dirs_with_descriptor():
            agent_dir = AGENTS / name
            if not (agent_dir / "test_wiring.py").is_file():
                missing.append(f"{name}: missing test_wiring.py")
            elif not (agent_dir / "golden" / "scenario").is_file():
                missing.append(f"{name}: missing golden/scenario")
        if missing:
            self.fail(
                "agents with agent.yml must ship test_wiring.py and golden/scenario "
                f"(see agents/README.md): {'; '.join(missing)}"
            )

    def test_agent_dir_does_not_shadow_src_manifest(self):
        repo_paths = [str(REPO / "src"), str(REPO / "tests"), str(REPO / "agents")]
        with tempfile.TemporaryDirectory() as tmp:
            decoy_dir = Path(tmp) / "decoy-agent"
            decoy_dir.mkdir()
            (decoy_dir / "manifest.py").write_text("SHADOW = True\n")
            with patch.object(sys, "path", repo_paths + sys.path + [str(decoy_dir)]):
                backup = {
                    k: sys.modules[k]
                    for k in ("manifest", "wire_plugins")
                    if k in sys.modules
                }
                with patch.dict(sys.modules, backup):
                    for name in ("manifest", "wire_plugins"):
                        sys.modules.pop(name, None)
                    import manifest  # noqa: F401
                    self.assertEqual(manifest.__file__, str(SRC_MANIFEST.resolve()))

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
