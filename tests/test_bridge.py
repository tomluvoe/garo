import copy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import garo_bridge as bridge


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads((ROOT / "sample_status.json").read_text())
        self.now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
        self.source = {"kind": "charger_http", "http_received_at_utc": bridge.iso(self.now)}

    def test_units_and_separate_current_limits(self):
        result = bridge.normalize(self.raw, self.source, self.now)
        self.assertEqual(result["reported"]["current_a"], 10)
        self.assertEqual(result["reported"]["session_energy_kwh"], 6.9)
        self.assertEqual(result["reported"]["total_energy_kwh"], 1256.9)
        self.assertEqual(result["reported"]["current_limit_a"], 20)
        self.assertEqual(result["reported"]["pilot_current_a"], 12)
        self.assertEqual(result["reported"]["factory_limit_a"], 32)

    def test_finished_never_becomes_active_from_retained_power(self):
        result = bridge.normalize(self.raw, self.source, self.now)
        self.assertFalse(result["reported_charging"])
        self.assertFalse(result["power_reading_usable_as_live"])
        self.assertEqual(result["reported"]["power_w"], 2300)
        self.assertTrue(any("retained" in text for text in result["warnings"]))

    def test_corrected_clock_does_not_preserve_old_timestamp_warning(self):
        self.raw["mainCharger"]["lastContact"] = int(self.now.timestamp() * 1000)
        self.raw["mainCharger"]["online"] = True
        self.raw["connector"] = "CHARGING"
        result = bridge.normalize(self.raw, self.source, self.now)
        self.assertEqual(result["freshness"]["status"], "recent_controller_timestamp")
        self.assertTrue(result["power_reading_usable_as_live"])
        self.assertEqual(result["warnings"], [])

    def test_sample_never_becomes_live_even_with_recent_timestamp(self):
        self.raw["mainCharger"].update(lastContact=int(self.now.timestamp() * 1000), online=True)
        self.raw["connector"] = "CHARGING"
        result = bridge.normalize(self.raw, {"kind": "sample"}, self.now)
        self.assertFalse(result["power_reading_usable_as_live"])
        self.assertIn("SAMPLE DATA", result["warnings"][0])

    def test_firmware_states_and_unavailable_meter(self):
        self.raw.update(connector="SEARCH_COMM", mode="SCHEMA")
        result = bridge.normalize(self.raw, self.source, self.now)
        self.assertEqual(result["connector"], "SEARCH_COMM")
        self.assertEqual(result["connector_label"], "Vehicle not connected")
        self.assertEqual(result["mode_label"], "Schedule enabled")
        self.assertFalse(result["connector_alert"])

        self.raw["connector"] = "CABLE_FAULT"
        self.assertTrue(bridge.normalize(self.raw, self.source, self.now)["connector_alert"])

        self.raw.update(connector="CHARGING", currentChargingCurrent=-1)
        self.raw["mainCharger"].update(lastContact=int(self.now.timestamp() * 1000), online=True)
        result = bridge.normalize(self.raw, self.source, self.now)
        self.assertIsNone(result["reported"]["current_a"])
        self.assertIsNone(result["reported"]["power_w"])
        self.assertFalse(result["power_reading_usable_as_live"])

    def test_old_future_and_missing_timestamps(self):
        for delta in [-600, 600]:
            self.raw["mainCharger"]["lastContact"] = int((self.now + timedelta(seconds=delta)).timestamp() * 1000)
            result = bridge.normalize(self.raw, self.source, self.now)
            self.assertEqual(result["freshness"]["status"], "clock_or_freshness_issue")
        self.raw.pop("mainCharger")
        result = bridge.normalize(self.raw, self.source, self.now)
        self.assertEqual(result["freshness"]["status"], "unknown")

    def test_serial_mismatch_fails(self):
        with self.assertRaisesRegex(bridge.ChargerError, "Serial mismatch"):
            bridge.ChargerClient(serial=123, sample=ROOT / "sample_status.json").snapshot()

    def test_invalid_payloads_do_not_look_like_success(self):
        for invalid in [[], {}, {"serialNumber": True}, {"serialNumber": 1234567, "connector": None}]:
            with self.assertRaises(bridge.ChargerError):
                bridge.normalize(invalid, self.source, self.now)
        self.assertIsNone(bridge.number(float("nan")))
        self.assertIsNone(bridge.number(True))
        self.assertIsNone(bridge.timestamp(1e100))

    def test_invalid_numbers_in_raw_diagnostics_fail_before_caching(self):
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / "invalid.json"
            for value in ("1e999", "NaN", "9" * 400):
                with self.subTest(value=value[:20]):
                    sample.write_text(json.dumps(dict(self.raw, extra="bad")).replace('"bad"', value))
                    client = bridge.ChargerClient(sample=sample)
                    with self.assertRaisesRegex(bridge.ChargerError, "JSON number"):
                        client.snapshot()
                    self.assertIsNone(client.cached)

    def test_failed_refresh_clears_prior_success_and_recovers(self):
        client = bridge.ChargerClient(host="127.0.0.1", serial=self.raw["serialNumber"])
        with patch.object(client, "_fetch", side_effect=[(self.raw, self.source), OSError("disconnected"), (self.raw, self.source)]):
            client.snapshot()
            client.last_attempt -= 6
            with self.assertRaisesRegex(bridge.ChargerError, "disconnected"):
                client.snapshot()
            with self.assertRaises(bridge.ChargerError):
                client.snapshot()
            self.assertIsNone(client.cached)
            client.last_attempt -= 6
            self.assertEqual(client.snapshot()[0]["serial_number"], self.raw["serialNumber"])

    def test_device_settings_use_only_charger_keys_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"GARO_CHARGER_PORT": "8081"}, clear=True):
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                "# Test device configuration\nGARO_CHARGER_HOST=127.0.0.1\n"
                "GARO_CHARGER_PORT=8080\nGARO_CHARGER_SERIAL=1234567\nUNRELATED=ignored\n"
            )
            self.assertEqual(bridge.device_settings(dotenv), {
                "GARO_CHARGER_HOST": "127.0.0.1",
                "GARO_CHARGER_PORT": "8081",
                "GARO_CHARGER_SERIAL": "1234567",
            })

    def test_read_only_network_route_and_cache(self):
        data = json.dumps(self.raw).encode()
        seen = []
        truncated = False
        class MockCharger(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.command, self.path, self.headers.get("Cache-Control")))
                self.send_response(200)
                self.send_header("Content-Length", str(len(data) + int(truncated)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockCharger)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = bridge.ChargerClient(host="127.0.0.1", port=server.server_port, serial=self.raw["serialNumber"])
            client.snapshot()
            client.snapshot()
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][0], "GET")
            self.assertTrue(seen[0][1].startswith(bridge.STATUS_PATH + "?_="))
            self.assertIn("no-cache", seen[0][2])
            truncated = True
            client.last_attempt -= bridge.CACHE_SECONDS + 1
            with self.assertRaisesRegex(bridge.ChargerError, "complete response"):
                client.snapshot()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = bridge.ChargerClient(sample=ROOT / "sample_status.json")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.dashboard_handler(cls.client))
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join()

    def test_html_assets_and_api(self):
        for path in ["/", "/app.js", "/controls.js", "/energy.js", "/style.css"]:
            with urlopen(self.base + path) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                if path == "/":
                    page = response.read()
                    self.assertIn(b'data-refresh-seconds="60"', page)
                    self.assertIn(b"Refreshes every 60 seconds", page)
        with urlopen(self.base + "/api/status") as response:
            data = json.load(response)
            self.assertEqual(data["status"]["source"]["kind"], "sample")
            self.assertIsNone(data["status"]["source"]["original_interface"])
            self.assertFalse(data["status"]["power_reading_usable_as_live"])
        with urlopen(Request(self.base, method="HEAD")) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")

    def test_write_and_unexpected_origins_are_rejected(self):
        for request, code in [
            (Request(self.base + "/api/status", method="POST"), 405),
            (Request(self.base + "/api/status", headers={"Origin": "https://example.com"}), 403),
            (Request(self.base + "/api/status", headers={"Origin": f"http://localhost:{self.server.server_port}"}), 403),
            (Request(self.base + "/api/status", headers={"Host": "evil.example"}), 403),
            (Request(self.base + "/../sample_status.json"), 404),
        ]:
            with self.assertRaises(HTTPError) as result:
                urlopen(request)
            self.assertEqual(result.exception.code, code)

    def test_api_error_is_not_served_as_a_success(self):
        with patch.object(self.client, "snapshot", side_effect=bridge.ChargerError("test connection failure")):
            with urlopen(self.base + "/healthz") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), {"status": "ok"})
            self.client.snapshot.assert_not_called()
            with self.assertRaises(HTTPError) as result:
                urlopen(self.base + "/api/status")
            self.assertEqual(result.exception.code, 502)
            data = json.load(result.exception)
            self.assertIn("test connection failure", data["error"])
            self.assertEqual(data["source"]["kind"], "sample")
            self.assertNotIn("status", data)

    def test_forwarded_dashboard_uses_the_browser_port_for_host_and_origin(self):
        handler = bridge.dashboard_handler(self.client, browser_port=18765, refresh_seconds=30)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            url = f"http://127.0.0.1:{server.server_port}/"
            try:
                headers = {"Host": "127.0.0.1:18765", "Origin": "http://127.0.0.1:18765"}
                with urlopen(Request(url, headers=headers)) as response:
                    self.assertEqual(response.status, 200)
                    page = response.read()
                    self.assertIn(b'data-refresh-seconds="30"', page)
                    self.assertIn(b"Refreshes every 30 seconds", page)
                with self.assertRaises(HTTPError) as result:
                    urlopen(url)
                self.assertEqual(result.exception.code, 403)
                headers["Origin"] = f"http://127.0.0.1:{server.server_port}"
                with self.assertRaises(HTTPError) as result:
                    urlopen(Request(url, headers=headers))
                self.assertEqual(result.exception.code, 403)
            finally:
                server.shutdown()
                worker.join()


if __name__ == "__main__":
    unittest.main()
