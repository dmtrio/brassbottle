"""Import shim: ``import agent_test_kit`` from any ``agents/<name>/`` directory.

Prepends the repo's ``src/`` and ``tests/`` dirs, then re-exports the real kit
from ``tests/agent_test_kit.py``.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in ("src", "tests"):
    path = ROOT / sub
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

_impl_path = ROOT / "tests" / "agent_test_kit.py"
_spec = importlib.util.spec_from_file_location("_agent_test_kit_impl", _impl_path)
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["_agent_test_kit_impl"] = _mod
_spec.loader.exec_module(_mod)

load_descriptor = _mod.load_descriptor
wire = _mod.wire
WireResult = _mod.WireResult
