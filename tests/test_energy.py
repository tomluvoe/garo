from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import garo_bridge as bridge

SAMPLE_SERIAL = 1234567


def millis(year, month, day, hour=12):
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def energy_payload(stamps, values, serial=SAMPLE_SERIAL, meter=175.625):
    return {"chargeboxSerial": serial, "timestamps": stamps, "values": values, "stopValue": meter}


class EnergyModelTests(unittest.TestCase):
    def test_leap_month_uses_local_dates_and_distinguishes_zero_from_missing(self):
        raw = energy_payload([millis(2024, 1, 31, 23), millis(2024, 2, 2), millis(2024, 2, 29)], [1.25, 0, 2.75])
        history = bridge.normalize_energy(raw, SAMPLE_SERIAL, 2024, 2, ZoneInfo("Europe/Stockholm"))
        self.assertEqual(len(history["days"]), 29)
        self.assertEqual(history["days"][0], {"date": "2024-02-01", "kwh": 1.25})
        self.assertEqual(history["days"][1], {"date": "2024-02-02", "kwh": 0})
        self.assertEqual(history["days"][2], {"date": "2024-02-03", "kwh": None})
        self.assertEqual(history["days"][-1], {"date": "2024-02-29", "kwh": 2.75})
        self.assertEqual(history["total_kwh"], 4)
        self.assertEqual(history["meter_kwh"], 175.625)

    def test_invalid_records_are_rejected(self):
        valid = millis(2024, 2, 1)
        for raw in (
            energy_payload([valid], []),
            energy_payload([valid], [-1]),
            energy_payload([valid, valid], [1, 2]),
            energy_payload([millis(2024, 3, 1)], [1]),
            energy_payload([valid], [1], serial=SAMPLE_SERIAL + 1),
        ):
            with self.subTest(raw=raw), self.assertRaises(bridge.ChargerError):
                bridge.normalize_energy(raw, SAMPLE_SERIAL, 2024, 2, timezone.utc)

    def test_blank_period_and_missing_meter_reading_have_distinct_results(self):
        self.assertEqual(bridge.normalize_energy(energy_payload([], [], meter=0), SAMPLE_SERIAL, 2024, 2, timezone.utc),
                         {"state": "empty"})
        raw = energy_payload([millis(2024, 2, 1)], [0], meter=None)
        result = bridge.normalize_energy(raw, SAMPLE_SERIAL, 2024, 2, timezone.utc)
        self.assertEqual(result["state"], "available")
        self.assertEqual(result["total_kwh"], 0)
        self.assertIsNone(result["meter_kwh"])

    def test_sample_history_is_unavailable_offline(self):
        client = bridge.ChargerClient(sample=ROOT / "sample_status.json")
        self.assertEqual(client.energy_history(2024, 2, timezone.utc)["state"], "unavailable")


class EnergyAPITests(unittest.TestCase):
    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_port

    def setUp(self):
        seen = self.seen = []

        class MockCharger(BaseHTTPRequestHandler):
            def do_POST(self):
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, params))
                month = int(params["month"])
                if month == 4:
                    self.send_error(404)
                    return
                payload = energy_payload([], [])
                if month == 2:
                    payload = energy_payload([millis(2024, 2, 3)], [6.25])
                elif month == 5:
                    payload = energy_payload([], [], serial=SAMPLE_SERIAL + 1)
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        charger_port = self.start_server(MockCharger)
        self.client = bridge.ChargerClient(host="127.0.0.1", port=charger_port, serial=SAMPLE_SERIAL)
        dashboard_port = self.start_server(bridge.dashboard_handler(self.client))
        self.base = f"http://127.0.0.1:{dashboard_port}/api/energy"

    def read_history(self, month=2, query=""):
        with urlopen(f"{self.base}?year=2024&month={month}{query}", timeout=3) as response:
            return json.load(response)

    def test_requests_fixed_meter_and_caches_by_period_including_empty_months(self):
        result = self.read_history(query="&timezone=Europe%2FStockholm")
        self.assertEqual(result["days"][2], {"date": "2024-02-03", "kwh": 6.25})
        self.assertEqual(self.read_history(), result)
        self.assertEqual(self.read_history(query="&refresh=1"), result)
        self.assertEqual(self.seen, [(bridge.ENERGY_PATH, {
            "chargeboxSerial": str(SAMPLE_SERIAL), "year": "2024", "month": "2",
            "meterSerial": "DEFAULT", "resolution": "DAY",
        })])
        saved_at, raw, received_at = self.client.energy_cache[(2024, 2)]
        self.client.energy_cache[(2024, 2)] = (saved_at - bridge.CACHE_SECONDS - 1, raw, received_at)
        self.assertEqual(self.read_history(), result)
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.read_history(query="&refresh=1"), result)
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.read_history(3), {"state": "empty"})
        self.assertEqual(self.read_history(3), {"state": "empty"})
        self.assertEqual(len(self.seen), 3)

    def test_route_validates_inputs_and_rejects_external_or_write_requests(self):
        for query in (
            "", "?year=2024&month=13", "?year=2024&month=2&meter=arbitrary",
            "?year=2024&month=2&month=3", "?year=2024&month=2&timezone=Invalid/Zone",
        ):
            with self.subTest(query=query), self.assertRaises(HTTPError) as result:
                urlopen(self.base + query, timeout=3)
            self.assertEqual(result.exception.code, 400)
        for headers, method, code in (({"Origin": "https://example.com"}, "GET", 403), ({}, "POST", 405)):
            with self.assertRaises(HTTPError) as result:
                urlopen(Request(self.base + "?year=2024&month=2", headers=headers, method=method), timeout=3)
            self.assertEqual(result.exception.code, code)
        self.assertEqual(self.seen, [])

    def test_legacy_firmware_and_bad_upstream_data_are_reported_separately(self):
        self.assertEqual(self.read_history(4)["state"], "unavailable")
        with self.assertRaises(HTTPError) as result:
            self.read_history(5)
        self.assertEqual(result.exception.code, 502)
        self.assertIn("configured charger", json.load(result.exception)["error"])
