"""Load plugins/gear360's tests into the repo-wide suite.

The gear360 tests live next to the code they cover, in plugins/gear360/, so
they can be run straight from that directory while hacking on the plugin:

    cd plugins/gear360 && python3 -m unittest test_gear360

`unittest discover -s tests` only scans this directory, so pull them in here.
The module is loaded by path rather than via `loader.discover`, whose
`top_level_dir` must sit inside the project being discovered.
"""
import importlib.util
import sys
from pathlib import Path

PLUGIN = Path(__file__).parent.parent / "plugins" / "gear360"


def load_tests(loader, tests, pattern):
    sys.path.insert(0, str(PLUGIN))
    spec = importlib.util.spec_from_file_location(
        "gear360_plugin_tests", PLUGIN / "test_gear360.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return loader.loadTestsFromModule(module)
