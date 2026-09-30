"""Contract for the playwright plugin: descriptor, install block, and the
tool-allowlist proxy in front of @playwright/mcp."""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
import manifest

PLUGIN_DIR = Path(__file__).parent
PLUGIN_PATH = PLUGIN_DIR / "plugin.yml"
FILTER_PATH = PLUGIN_DIR / "mcp_filter.py"

_spec = importlib.util.spec_from_file_location("playwright_mcp_filter", FILTER_PATH)
mcp_filter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp_filter)

# The host gateway's --tools list, written out: the gateway plugin is deleted
# by a later plan, so this must not depend on its files existing.
GATEWAY_TOOLS = {
    "browser_click", "browser_close", "browser_console_messages", "browser_drag",
    "browser_drop", "browser_evaluate", "browser_file_upload", "browser_fill_form",
    "browser_handle_dialog", "browser_hover", "browser_navigate",
    "browser_navigate_back", "browser_network_request", "browser_network_requests",
    "browser_press_key", "browser_resize", "browser_select_option",
    "browser_snapshot", "browser_tabs", "browser_take_screenshot", "browser_type",
    "browser_wait_for",
}


def load_real_descriptor():
    """Parse plugin.yml the way up.sh does (yq -o=json)."""
    if shutil.which("yq") is None:
        raise AssertionError("yq not installed — descriptor contract did NOT run")
    out = subprocess.run(["yq", "-o=json", "-I=0", ".", str(PLUGIN_PATH)],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


CLAUDE_AGENT = {"binary": "claude", "install": "npm install -g claude",
                "mcp": {"config_path": ".mcp.json", "format": "json",
                        "dialect": "mcpServers", "env_refs": True}}


class Descriptor(unittest.TestCase):
    def test_derives_a_local_stdio_server_through_the_filter(self):
        plugin = load_real_descriptor()
        derived = manifest.derive({"plugins": ["playwright"]},
                                  {"playwright": plugin},
                                  {"claude": CLAUDE_AGENT},
                                  env={"SECRETS_FILE": "/sec/secrets.env"})
        entries = json.loads(derived["PLUGIN_MCP_ENTRIES"])
        spec = entries["playwright"]
        self.assertEqual(spec["command"], "python3")
        self.assertEqual(spec["args"][:3],
                         ["/opt/plugins/playwright/mcp_filter.py", "--", "playwright-mcp"])
        for flag in ("--headless", "--no-sandbox", "--isolated"):
            self.assertIn(flag, spec["args"])
        self.assertNotIn("url", spec)

    def test_adds_no_egress_and_no_host_port(self):
        plugin = load_real_descriptor()
        self.assertNotIn("egress", plugin)
        self.assertNotIn("host_port", plugin)
        derived = manifest.derive({"plugins": ["playwright"]},
                                  {"playwright": plugin},
                                  {"claude": CLAUDE_AGENT},
                                  env={"SECRETS_FILE": "/sec/secrets.env"})
        self.assertEqual(derived["HOST_MCP_PORTS"], "")
        self.assertNotIn("playwright", derived["EGRESS"])

    def test_a_local_server_without_install_is_rejected(self):
        plugin = load_real_descriptor()
        del plugin["install"]
        with self.assertRaises(manifest.ManifestError):
            manifest.derive({"plugins": ["playwright"]}, {"playwright": plugin},
                            {"claude": CLAUDE_AGENT},
                            env={"SECRETS_FILE": "/sec/secrets.env"})


class InstallBlock(unittest.TestCase):
    def setUp(self):
        self.install = load_real_descriptor()["install"]

    def test_is_valid_bash(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(self.install)
            f.flush()
            subprocess.run(["bash", "-n", f.name], check=True, timeout=30)

    def test_pins_an_exact_version(self):
        m = re.search(r"^PW_MCP_VERSION=(\S+)$", self.install, re.M)
        self.assertIsNotNone(m, "no PW_MCP_VERSION pin")
        self.assertRegex(m.group(1), r"^\d+\.\d+\.\d+$")
        self.assertIn('@playwright/mcp@${PW_MCP_VERSION}', self.install)
        self.assertNotIn("@latest", self.install)

    def test_bakes_chromium_and_its_system_libraries(self):
        self.assertIn("install-deps chromium", self.install)
        self.assertIn("cli.js\" install --no-shell chromium", self.install)

    def test_fails_the_build_if_the_server_or_filter_is_broken(self):
        self.assertIn("playwright-mcp --version", self.install)
        self.assertIn("py_compile /opt/plugins/playwright/mcp_filter.py", self.install)


class Allowlist(unittest.TestCase):
    def test_matches_the_gateway_tool_list_exactly(self):
        self.assertEqual(set(mcp_filter.ALLOWED_TOOLS), GATEWAY_TOOLS)

    def test_excludes_the_rce_tool(self):
        self.assertNotIn("browser_run_code_unsafe", mcp_filter.ALLOWED_TOOLS)

    def test_gateway_launcher_still_agrees_while_it_exists(self):
        run_sh = REPO / "plugins" / "gateway" / "run.sh"
        if not run_sh.exists():
            self.skipTest("gateway plugin removed")
        tool_lines = [line for line in run_sh.read_text().splitlines() if line.startswith("TOOLS=")]
        tools = set(re.findall(r"browser_[a-z_]+", "\n".join(tool_lines)))
        self.assertEqual(set(mcp_filter.ALLOWED_TOOLS), tools)


class FilterMessages(unittest.TestCase):
    def quiet(self, fn, *a):
        with redirect_stderr(io.StringIO()) as err:
            return fn(*a), err.getvalue()

    def test_refuses_a_disallowed_call_without_forwarding_it(self):
        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "browser_run_code_unsafe", "arguments": {"code": "SECRET"}}}
        (fwd, replies), err = self.quiet(mcp_filter.filter_client_message, msg, set())
        self.assertIsNone(fwd)
        self.assertEqual(replies[0]["id"], 7)
        self.assertEqual(replies[0]["error"]["code"], -32602)
        self.assertIn("browser_run_code_unsafe", err)
        self.assertNotIn("SECRET", err)  # arguments are never logged

    def test_forwards_an_allowed_call_untouched(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "browser_navigate", "arguments": {"url": "https://x"}}}
        fwd, replies = mcp_filter.filter_client_message(msg, set())
        self.assertIs(fwd, msg)
        self.assertEqual(replies, [])

    def test_a_call_with_no_name_is_refused(self):
        for params in (None, {}, {"name": None}, "x"):
            msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
            (fwd, replies), _ = self.quiet(mcp_filter.filter_client_message, msg, set())
            self.assertIsNone(fwd)
            self.assertEqual(len(replies), 1)

    def test_a_batch_is_filtered_elementwise(self):
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "browser_tabs"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "browser_run_code_unsafe"}},
        ]
        (fwd, replies), _ = self.quiet(mcp_filter.filter_client_message, batch, set())
        self.assertEqual([m["id"] for m in fwd], [1])
        self.assertEqual([r["id"] for r in replies], [2])

    def test_tools_list_result_is_stripped_to_the_allowlist(self):
        ids = set()
        mcp_filter.filter_client_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, ids)
        resp = {"jsonrpc": "2.0", "id": 3, "result": {"tools": [
            {"name": "browser_navigate"}, {"name": "browser_run_code_unsafe"},
            {"name": "browser_cookie_list"}, {"name": "browser_snapshot"}]}}
        out, err = self.quiet(mcp_filter.filter_server_message, resp, ids)
        self.assertEqual([t["name"] for t in out["result"]["tools"]],
                         ["browser_navigate", "browser_snapshot"])
        self.assertIn("4 tools in, 2 out", err)

    def test_other_server_messages_pass_through(self):
        ids = {json.dumps(3)}
        for msg in ({"jsonrpc": "2.0", "id": 9, "result": {"tools": [{"name": "browser_run_code_unsafe"}]}},
                    {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}):
            self.assertEqual(mcp_filter.filter_server_message(json.loads(json.dumps(msg)), ids), msg)


FAKE_SERVER = textwrap.dedent("""
    import json, sys
    TOOLS = ["browser_navigate", "browser_run_code_unsafe", "browser_cookie_list"]
    for line in sys.stdin:
        m = json.loads(line)
        if isinstance(m, list):
            print(json.dumps([{"jsonrpc": "2.0", "id": x["id"], "result": {"called": x["params"]["name"]}}
                              for x in m if "id" in x]), flush=True)
            continue
        if m.get("method") == "tools/list":
            out = {"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": t} for t in TOOLS]}}
        elif m.get("method") == "tools/call":
            out = {"jsonrpc": "2.0", "id": m["id"], "result": {"called": m["params"]["name"]}}
        else:
            continue
        print(json.dumps(out), flush=True)
""")


class EndToEnd(unittest.TestCase):
    """The real script, over real pipes, in front of a fake MCP server."""

    def run_filter(self, lines):
        with tempfile.TemporaryDirectory() as d:
            server = Path(d) / "server.py"
            server.write_text(FAKE_SERVER)
            proc = subprocess.run(
                [sys.executable, str(FILTER_PATH), "--", sys.executable, str(server)],
                input="".join(lines), capture_output=True, text=True, timeout=30)
        replies = {}
        for line in proc.stdout.splitlines():
            msg = json.loads(line)  # stdout must be clean JSON-RPC only
            if isinstance(msg, list):
                replies.setdefault("batches", []).append(msg)
                continue
            replies[msg["id"]] = msg
        return proc, replies

    def test_list_call_refusal_and_bad_line(self):
        proc, replies = self.run_filter([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n",
            "not json\n",
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "browser_navigate"}}) + "\n",
            json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "browser_run_code_unsafe"}}) + "\n",
        ])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([t["name"] for t in replies[1]["result"]["tools"]], ["browser_navigate"])
        self.assertEqual(replies[2]["result"], {"called": "browser_navigate"})
        self.assertEqual(replies[3]["error"]["code"], -32602)
        self.assertIn("start:", proc.stderr)
        self.assertIn("exit: status 0", proc.stderr)
        self.assertIn("dropped unparseable client line", proc.stderr)

    def call(self, msg_id, tool):
        return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": {"name": tool}}

    def test_mixed_batch_gets_one_array_reply(self):
        proc, replies = self.run_filter([
            json.dumps([self.call(1, "browser_navigate"),
                        self.call(2, "browser_run_code_unsafe")]) + "\n"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(replies["batches"]), 1)
        self.assertEqual(len(replies), 1)  # no stray top-level objects
        by_id = {m["id"]: m for m in replies["batches"][0]}
        self.assertEqual(by_id[1]["result"], {"called": "browser_navigate"})
        self.assertEqual(by_id[2]["error"]["code"], -32602)

    def test_all_refused_batch_gets_a_single_array(self):
        proc, replies = self.run_filter([
            json.dumps([self.call(1, "browser_run_code_unsafe"),
                        self.call(2, "browser_find")]) + "\n"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(replies), 1)
        self.assertEqual([m["id"] for m in replies["batches"][0]], [1, 2])
        self.assertTrue(all(m["error"]["code"] == -32602 for m in replies["batches"][0]))

    def test_usage_error_without_a_server_command(self):
        proc = subprocess.run([sys.executable, str(FILTER_PATH)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
