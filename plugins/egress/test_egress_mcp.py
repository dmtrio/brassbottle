#!/usr/bin/env python3
"""Unit tests for plugins/egress/mcp_server.py."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("egress_mcp_server", PLUGIN_DIR / "mcp_server.py")
assert SPEC and SPEC.loader
mcp_server = importlib.util.module_from_spec(SPEC)
sys.modules["egress_mcp_server"] = mcp_server
SPEC.loader.exec_module(mcp_server)


class EgressMcpServerTests(unittest.TestCase):
    def test_tools_list_response(self):
        captured: list[dict] = []

        def fake_write(payload: dict) -> None:
            captured.append(payload)

        with mock.patch.object(mcp_server, "_write_message", side_effect=fake_write):
            mcp_server._handle_request(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
        self.assertEqual(len(captured), 1)
        names = {tool["name"] for tool in captured[0]["result"]["tools"]}
        self.assertEqual(names, {"request_egress", "check_egress"})


if __name__ == "__main__":
    unittest.main()
