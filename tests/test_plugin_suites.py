"""Run every plugin's own tests as part of the repo-wide suite.

Convention: a plugin may keep unit tests beside the code they cover, as
`plugins/<name>/test_*.py`. They then run both ways —

    cd plugins/<name> && python3 -m unittest discover    # while hacking on it
    python3 -m unittest discover -s tests                # everything, in CI

with nothing to register: drop the file in and it is picked up. Test files are
kept out of the image by `plugins/*/test_*.py` in .dockerignore.

Why load by path instead of `loader.discover()`: discover() requires its
`top_level_dir` to sit inside the tree being scanned, so pointing it at a
sibling directory raises "Path must be within the project" on 3.9. Each plugin
directory goes on `sys.path` before its modules execute, so a test can
`import <module_under_test>` directly, and modules are registered under a
unique name so two plugins can both ship a `test_watch.py`.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

PLUGINS = Path(__file__).parent.parent / "plugins"


def plugin_test_files():
    return sorted(PLUGINS.glob("*/test_*.py"))


def _load(path: Path):
    """Import one plugin test module under a collision-proof name."""
    plugin = path.parent.name
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    name = f"plugintests_{plugin}_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class PluginTestDiscovery(unittest.TestCase):
    """Guard the loader itself.

    The dangerous failure here is silent: if the glob or the import breaks, the
    suite still reports OK while quietly covering nothing. These assert that
    discovery found files and that each one actually contributed test cases.
    """

    def test_discovery_finds_plugin_test_files(self):
        plugins_with_tests = {p.parent.name for p in plugin_test_files()}
        self.assertTrue(
            plugins_with_tests,
            "no plugins/*/test_*.py found — the loader would silently cover "
            "nothing. If no plugin ships tests any more, delete this file.",
        )

    def test_every_plugin_test_file_yields_test_cases(self):
        loader = unittest.TestLoader()
        for path in plugin_test_files():
            with self.subTest(plugin=path.parent.name, file=path.name):
                count = loader.loadTestsFromModule(_load(path)).countTestCases()
                self.assertGreater(count, 0, f"{path} defines no test cases")


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(PluginTestDiscovery))
    for path in plugin_test_files():
        suite.addTests(loader.loadTestsFromModule(_load(path)))
    return suite
