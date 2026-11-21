from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import garo_bridge as bridge

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

SERIAL = 1234567


@contextmanager
def serve(handler):
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            worker.join()


@contextmanager
def mock_charger():
    status = json.loads((ROOT / "sample_status.json").read_text())
    status["mainCharger"]["cableLockMode"] = 0
    outlet = {"serialNumber": SERIAL, "cableLockMode": 0, "minCurrentLimit": 6}
    config = {
        "serialNumber": SERIAL, "master": True, "twinSerial": 0,
        "productId": 88, "firmwareVersion": 7, "firmwareRevision": 15,
        "slaveList": [outlet], "webNetworkPassword": "mock-configuration-secret",
    }
    writes = []
    behavior = {"reply": "accepted"}

    class Charger(BaseHTTPRequestHandler):
        def reply(self, value, code=200):
            body = b"" if code == 204 else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == bridge.CONFIG_PATH:
                self.reply(config)
            elif self.path.startswith(bridge.STATUS_PATH):
                self.reply(status)
            else:
                self.send_error(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(body) if body else None
            writes.append((self.path, payload))
            if behavior["reply"] == "refused":
                self.reply(False)
                return
            if behavior["reply"] == "broken":
                self.send_error(503)
                return
            if behavior["reply"] == "unconfirmed":
                self.reply(True)
                return
            if self.path in (bridge.MODE_PATH + "ALWAYS_ON", bridge.MODE_PATH + "ALWAYS_OFF"):
                status["mode"] = self.path.rsplit("/", 1)[1]
                self.reply(True)
            elif self.path == bridge.CABLE_LOCK_PATH:
                status["mainCharger"]["cableLockMode"] = outlet["cableLockMode"] = payload["cableLockMode"]
                self.reply(None, code=204)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    with serve(Charger) as port:
        yield port, status, config, writes, behavior


class DashboardControlTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        port, self.raw, self.config, self.writes, self.behavior = stack.enter_context(mock_charger())
        self.client = bridge.ChargerClient("127.0.0.1", port, SERIAL)
        dashboard_port = stack.enter_context(serve(bridge.dashboard_handler(self.client)))
        self.base = f"http://127.0.0.1:{dashboard_port}"
        self.headers = {"Origin": self.base, "Content-Type": "application/json"}

    def apply(self, action, payload, headers=None):
        request = Request(self.base + "/api/controls/" + action,
                          data=json.dumps(payload).encode(), headers=self.headers if headers is None else headers)
        with urlopen(request, timeout=4) as response:
            return json.load(response)

    def test_dashboard_confirms_availability_and_lock_changes_and_skips_duplicate_writes(self):
        unavailable = self.apply("availability", {"available": False})
        self.assertTrue(unavailable["changed"])
        self.assertIs(unavailable["status"]["controls"]["available"], False)
        self.assertFalse(self.apply("availability", {"available": False})["changed"])
        self.assertIs(self.apply("availability", {"available": True})["status"]["controls"]["available"], True)
        locked = self.apply("cable-lock", {"locked": True})
        self.assertIs(locked["status"]["controls"]["locked"], True)
        unlocked = self.apply("cable-lock", {"locked": False})
        self.assertIs(unlocked["status"]["controls"]["locked"], False)
        self.assertEqual(self.writes, [
            (bridge.MODE_PATH + "ALWAYS_OFF", None),
            (bridge.MODE_PATH + "ALWAYS_ON", None),
            (bridge.CABLE_LOCK_PATH, {"serialNumber": SERIAL, "cableLockMode": 1, "minCurrentLimit": 6}),
            (bridge.CABLE_LOCK_PATH, {"serialNumber": SERIAL, "cableLockMode": 0, "minCurrentLimit": 6}),
        ])

    def test_only_same_origin_json_boolean_requests_can_write(self):
        for action, value, headers, code in [
            ("availability", {"available": False}, {}, 403),
            ("cable-lock", {"locked": True}, dict(self.headers, Origin="https://example.com"), 403),
            ("cable-lock", {"locked": True}, dict(self.headers, Host="evil.example"), 403),
            ("availability", {"available": False}, {"Origin": self.base, "Content-Type": "text/plain"}, 415),
            ("availability", {"available": "false"}, self.headers, 400),
            ("cable-lock", {"locked": True, "serial": SERIAL + 1}, self.headers, 400),
            ("current-limit", {"amps": 32}, self.headers, 405),
        ]:
            with self.subTest(action=action, code=code), self.assertRaises(HTTPError) as exc:
                self.apply(action, value, headers)
            self.assertEqual(exc.exception.code, code)
        self.assertEqual(self.writes, [])

    def test_unsupported_outlet_group_and_serial_mismatch_block_writes(self):
        self.config["productId"] = 1
        with self.assertRaises(HTTPError) as exc:
            self.apply("cable-lock", {"locked": True})
        self.assertEqual(exc.exception.code, 409)
        self.config["slaveList"].append({"serialNumber": SERIAL + 1})
        with self.assertRaises(HTTPError) as exc:
            self.apply("availability", {"available": False})
        self.assertEqual(exc.exception.code, 409)
        self.config["serialNumber"] = SERIAL + 1
        with self.assertRaises(HTTPError) as exc:
            self.apply("availability", {"available": False})
        self.assertEqual(exc.exception.code, 409)
        self.assertEqual(self.writes, [])

    def test_reported_lock_profile_is_preserved_when_already_enabled(self):
        self.raw["mainCharger"]["cableLockMode"] = 2
        self.assertFalse(self.apply("cable-lock", {"locked": True})["changed"])
        self.assertEqual(self.writes, [])

    def test_rejection_transport_failure_and_mismatched_readback_are_reported(self):
        for behavior, code, message in [
            ("refused", 409, "refused"),
            ("broken", 502, "could not be confirmed"),
            ("unconfirmed", 502, "did not confirm"),
        ]:
            self.behavior["reply"] = behavior
            with patch.object(bridge, "CONTROL_READBACK_DELAYS", (0, 0, 0)), self.assertRaises(HTTPError) as exc:
                self.apply("cable-lock", {"locked": True})
            self.assertEqual(exc.exception.code, code)
            self.assertIn(message, json.load(exc.exception)["error"])
        self.assertEqual(len(self.writes), 3)


@unittest.skipUnless(HAS_MCP, "Install requirements-mcp.txt to run MCP integration tests")
class MCPControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdio_tools_apply_only_requested_settings_and_report_failures(self):
        with mock_charger() as (port, _, _, writes, behavior):
            params = StdioServerParameters(command=sys.executable, args=[
                str(ROOT / "garo_bridge.py"), "mcp", "--host", "127.0.0.1", "--port", str(port), "--serial", str(SERIAL),
            ])
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                self.assertEqual({name for name, tool in tools.items() if tool.annotations.readOnlyHint is False},
                                 {"set_charger_availability", "set_cable_lock"})
                invalid = await session.call_tool("set_cable_lock", {"locked": "false"})
                self.assertTrue(invalid.isError)
                self.assertEqual(writes, [])
                available = await session.call_tool("set_charger_availability", {"available": False})
                self.assertFalse(available.isError)
                self.assertEqual(available.structuredContent, {"changed": True, "available": False})
                locked = await session.call_tool("set_cable_lock", {"locked": True})
                self.assertFalse(locked.isError)
                self.assertEqual(locked.structuredContent, {"changed": True, "locked": True})
                behavior["reply"] = "refused"
                refused = await session.call_tool("set_cable_lock", {"locked": False})
                self.assertTrue(refused.isError)
                self.assertIn("refused", refused.content[0].text)
