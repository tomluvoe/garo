from datetime import date
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from garo_archive import EnergyArchive, energy_month
from garo_bridge import ArchivedEnergy, ChargerError, ENERGY_CACHE_SECONDS

SAMPLE_SERIAL = 1234567


def observation(readings, observed="2024-01-07T12:00:00.000000+00:00", meter="DEMO-METER", stop=120):
    first_day = date.fromisoformat(next(iter(readings)))
    history = energy_month(first_day.year, first_day.month, readings, stop)
    return dict(history, meter_serial=meter, observed_at_utc=observed)


class EnergyArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "data" / "energy.sqlite3"
        self.zone = ZoneInfo("UTC")
        self.archive = EnergyArchive(self.path, SAMPLE_SERIAL, self.zone)

    def test_upsert_rollover_and_restart_preserve_saved_days(self):
        self.archive.record(observation({"2024-01-01": 2, "2024-01-02": 0}, stop=120))
        newer = observation({"2024-01-01": 3, "2024-01-08": 5}, "2024-01-08T12:00:00.000000+00:00", stop=125)
        self.archive.record(newer)
        self.archive.record(newer)
        self.archive.record(observation({"2024-01-01": 1}))
        self.archive.record({"state": "empty"})
        self.archive.mark_collection()

        reopened = EnergyArchive(self.path, SAMPLE_SERIAL, self.zone)
        history = reopened.history(2024, 1)
        self.assertEqual(history["total_kwh"], 8)
        self.assertEqual(history["meter_kwh"], 125)
        self.assertEqual([day["kwh"] for day in history["days"][:8]], [3, 0, None, None, None, None, None, 5])
        self.assertIsNotNone(history["archive"]["last_collection_at_utc"])
        self.assertEqual(reopened.history(2024, 2)["state"], "empty")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_chargers_and_meters_are_separate_without_duplicate_daily_totals(self):
        self.archive.record(observation({"2024-01-01": 2}))
        self.archive.record(observation({"2024-01-01": 5}, "2024-01-09T12:00:00.000000+00:00", meter="DEMO-NEW"))
        self.assertEqual(self.archive.history(2024, 1)["total_kwh"], 5)
        different = EnergyArchive(self.path, SAMPLE_SERIAL + 1, self.zone)
        self.assertEqual(different.history(2024, 1)["state"], "empty")
        with self.assertRaisesRegex(ValueError, "uses UTC"):
            EnergyArchive(self.path, SAMPLE_SERIAL, ZoneInfo("Europe/Stockholm"))

    def test_collection_crosses_a_year_boundary_and_preserves_reads_on_partial_failure(self):
        client = Mock()
        client.energy_history.side_effect = [
            ChargerError("offline"),
            observation({"2025-01-03": 7}),
            observation({"2024-12-31": 8}),
            {"state": "empty"},
        ]
        service = ArchivedEnergy(client, self.archive)
        with patch("sys.stderr", new=io.StringIO()):
            self.assertFalse(service.collect_recent(date(2025, 1, 3)))
        self.assertEqual(self.archive.history(2025, 1)["total_kwh"], 7)
        self.assertIsNone(self.archive.history(2025, 1)["archive"]["last_collection_at_utc"])
        self.assertTrue(service.collect_recent(date(2025, 1, 3)))
        self.assertEqual(self.archive.history(2024, 12)["total_kwh"], 8)
        self.assertEqual(self.archive.history(2025, 1)["total_kwh"], 7)
        self.assertIsNotNone(self.archive.history(2025, 1)["archive"]["last_collection_at_utc"])
        self.assertEqual([call.args[:2] for call in client.energy_history.call_args_list],
                         [(2024, 12), (2025, 1), (2024, 12), (2025, 1)])

    def test_saved_history_does_not_need_charger_and_survives_manual_refresh_failure(self):
        self.archive.record(observation({"2024-01-01": 4}))
        client = Mock()
        client.energy_history.side_effect = ChargerError("offline")
        service = ArchivedEnergy(client, self.archive)
        self.assertEqual(service.history(2024, 1, refresh=False)["total_kwh"], 4)
        client.energy_history.assert_not_called()
        refreshed = service.history(2024, 1, refresh=True)
        self.assertEqual(refreshed["total_kwh"], 4)
        self.assertIn("saved readings", refreshed["notice"])

    def test_worker_runs_on_startup_and_retries_after_a_failed_collection(self):
        service = ArchivedEnergy(Mock(), self.archive)
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        with patch.object(service, "collect_recent", side_effect=[False, True]) as collect:
            service.run(stop, 3600)
        self.assertEqual(collect.call_count, 2)
        self.assertEqual([call.args[0] for call in stop.wait.call_args_list], [ENERGY_CACHE_SECONDS, 3600])
