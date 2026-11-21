import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAS_MCP = True
except ImportError:
    HAS_MCP = False


@unittest.skipUnless(HAS_MCP, "Install requirements-mcp.txt to run MCP integration tests")
class MCPIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_handshake_tools_and_snapshot(self):
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "garo_bridge.py"), "mcp", "--sample", str(ROOT / "sample_status.json")])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.serverInfo.name, "garo-local")
                tools = await session.list_tools()
                self.assertEqual({t.name for t in tools.tools}, {"get_charger_status", "get_charger_diagnostics"})
                self.assertTrue(all(t.annotations.readOnlyHint for t in tools.tools))
                result = await session.call_tool("get_charger_status", {})
                self.assertFalse(result.isError)
                data = json.loads(result.content[0].text)
                self.assertEqual(data["serial_number"], 1234567)
                self.assertEqual(data["connector_label"], "Charging finished")
                self.assertEqual(result.structuredContent, data)
                self.assertEqual(data["source"]["kind"], "sample")
                self.assertFalse(data["power_reading_usable_as_live"])
                diagnostic = await session.call_tool("get_charger_diagnostics", {})
                self.assertEqual(json.loads(diagnostic.content[0].text)["raw"]["currentChargingPower"], 2300)
                unknown = await session.call_tool("set_current_limit", {"amps": 32})
                self.assertTrue(unknown.isError)

    async def test_charger_error_reaches_the_client_as_a_tool_error(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "bad.json"
            invalid.write_text('{"unexpected":"data"}')
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "garo_bridge.py"), "mcp", "--sample", str(invalid)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("get_charger_status", {})
                    self.assertTrue(result.isError)


if __name__ == "__main__":
    unittest.main()
