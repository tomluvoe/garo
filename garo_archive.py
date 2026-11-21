"""Local storage for the charger's daily energy readings."""

import calendar
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
import math
import os
from pathlib import Path
import sqlite3


def energy_month(year, month, readings, meter):
    if not readings:
        return {"state": "empty"}
    days = [date(year, month, day).isoformat() for day in range(1, calendar.monthrange(year, month)[1] + 1)]
    return {
        "state": "available",
        "days": [{"date": day, "kwh": readings.get(day)} for day in days],
        "total_kwh": math.fsum(readings.values()),
        "meter_kwh": meter,
    }


class EnergyArchive:
    def __init__(self, path, serial, zone):
        self.path = Path(path)
        self.serial = serial
        self.zone = zone
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS archive_sources (
                    charger_serial INTEGER PRIMARY KEY,
                    timezone TEXT NOT NULL,
                    last_collection_at_utc TEXT
                );
                CREATE TABLE IF NOT EXISTS energy_readings (
                    charger_serial INTEGER NOT NULL,
                    meter_serial TEXT NOT NULL,
                    local_day TEXT NOT NULL,
                    kwh REAL NOT NULL,
                    meter_kwh REAL,
                    observed_at_utc TEXT NOT NULL,
                    PRIMARY KEY (charger_serial, meter_serial, local_day)
                );
                CREATE INDEX IF NOT EXISTS energy_by_charger_day
                    ON energy_readings (charger_serial, local_day, observed_at_utc);
            """)
            db.execute("INSERT OR IGNORE INTO archive_sources (charger_serial, timezone) VALUES (?, ?)",
                       (self.serial, self.zone.key))
            stored, = db.execute("SELECT timezone FROM archive_sources WHERE charger_serial = ?",
                                 (self.serial,)).fetchone()
            if stored != self.zone.key:
                raise ValueError(f"This archive uses {stored}. Set the energy time zone to {stored} or use another database.")

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def record(self, history):
        if history["state"] != "available":
            return
        known = [day for day in history["days"] if day["kwh"] is not None]
        latest_day = known[-1]["date"]
        rows = [
            (self.serial, history["meter_serial"], day["date"], day["kwh"],
             history["meter_kwh"] if day["date"] == latest_day else None, history["observed_at_utc"])
            for day in known
        ]
        with self.connection() as db:
            db.executemany("""
                INSERT INTO energy_readings
                    (charger_serial, meter_serial, local_day, kwh, meter_kwh, observed_at_utc)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (charger_serial, meter_serial, local_day) DO UPDATE SET
                    kwh = excluded.kwh,
                    meter_kwh = coalesce(excluded.meter_kwh, energy_readings.meter_kwh),
                    observed_at_utc = excluded.observed_at_utc
                WHERE excluded.observed_at_utc >= energy_readings.observed_at_utc
            """, rows)

    def mark_collection(self):
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.connection() as db:
            db.execute("UPDATE archive_sources SET last_collection_at_utc = ? WHERE charger_serial = ?",
                       (completed_at, self.serial))

    def history(self, year, month):
        first = date(year, month, 1).isoformat()
        last = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        with self.connection() as db:
            collection, = db.execute("SELECT last_collection_at_utc FROM archive_sources WHERE charger_serial = ?",
                                     (self.serial,)).fetchone()
            # A replacement meter may report the same date. Use the last observation for that date.
            rows = db.execute("""
                SELECT local_day, kwh, meter_kwh FROM energy_readings
                WHERE charger_serial = ? AND local_day BETWEEN ? AND ?
                ORDER BY local_day, observed_at_utc, meter_serial
            """, (self.serial, first, last)).fetchall()
        readings = {day: kwh for day, kwh, _ in rows}
        result = energy_month(year, month, readings, rows[-1][2] if rows else None)
        result["archive"] = {"timezone": self.zone.key, "last_collection_at_utc": collection}
        return result
