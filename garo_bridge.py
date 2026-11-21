#!/usr/bin/env python3
"""Local GARO dashboard and MCP adapter."""
import argparse
import asyncio
from collections import OrderedDict
import copy
import ipaddress
import json
import math
import os
from datetime import datetime, timedelta, timezone
from html import escape
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from garo_archive import EnergyArchive, energy_month

ROOT = Path(__file__).resolve().parent
DEVICE_ENV = ("GARO_CHARGER_HOST", "GARO_CHARGER_PORT", "GARO_CHARGER_SERIAL")
STATUS_PATH = "/servlet/rest/chargebox/status"
ENERGY_PATH = "/servlet/rest/chargebox/energy"
CONFIG_PATH = "/servlet/rest/chargebox/config"
MODE_PATH = "/servlet/rest/chargebox/mode/"
CABLE_LOCK_PATH = "/servlet/rest/chargebox/cablelock"
CONTROL_READBACK_DELAYS = (0, 0.5, 1)
# Product IDs whose entries in the firmware interface advertise an outlet.
OUTLET_PRODUCTS = frozenset({
    3, 6, 9, 12, 15, 16, 19, 22, 25, 26, 27, 30, 33, 34, 35, 36, 37, 38, 39, 40,
    41, 42, 43, 45, 46, 49, 51, 52, 55, 56, 59, 62, 66, 67, 69, 70, 72, 73, 78,
    79, 80, 86, 88, 92, 93, 94, 96, 98, 99, 100, 101, 103, 104, 107, 108, 110,
    113, 114, 122,
})
MAX_BYTES = 1024 * 1024
CACHE_SECONDS = 5
ENERGY_CACHE_SECONDS = 300
ENERGY_CACHE_MONTHS = 12
DEFAULT_ENERGY_INTERVAL_SECONDS = 3600
ENERGY_LOOKBACK_DAYS = 7
FIRST_ENERGY_YEAR = 2017
DEFAULT_REFRESH_SECONDS = 60
MAX_REFRESH_SECONDS = (2**31 - 1) // 1000
DASHBOARD_ASSETS = {
    "/": ("index.html", "text/html"),
    "/app.js": ("app.js", "application/javascript"),
    "/controls.js": ("controls.js", "application/javascript"),
    "/energy.js": ("energy.js", "application/javascript"),
    "/style.css": ("style.css", "text/css"),
}
CONNECTOR_LABELS = {
    "CHANGING": "State changing",
    "INITIALIZATION": "Initializing",
    "AVAILABLE": "Ready to charge",
    "NOT_CONNECTED": "Vehicle not connected",
    "SEARCH_COMM": "Vehicle not connected",
    "CONNECTED": "Waiting for vehicle",
    "CHARGING": "Charging",
    "CHARGING_PAUSED": "Paused by load balancing",
    "DISABLED": "Charging disabled",
    "UNAVAILABLE": "Paused by controller",
    "REMOTE_DISABLED": "Charging disabled remotely",
    "CHARGING_FINISHED": "Charging finished",
    "CHARGING_CANCELLED": "Charging cancelled",
}
CONNECTOR_ALERTS = {
    "RCD_FAULT": "Residual-current fault",
    "OVERHEAT": "Temperature limiting charge",
    "CRITICAL_TEMPERATURE": "Critical temperature",
    "CABLE_FAULT": "Cable fault",
    "LOCK_FAULT": "Cable lock fault",
    "CONTACTOR_FAULT": "Contactor fault",
    "VENT_FAULT": "Ventilation required",
    "DC_ERROR": "DC fault",
    "DC_HARDWARE": "DC detection fault",
    "CP_FAULT": "Control pilot fault",
    "CP_SHORTED": "Control pilot shorted",
}
# Operating modes the dashboard can select; firmware may report SCHEDULE for SCHEMA.
CONTROL_MODES = ("ALWAYS_ON", "SCHEMA", "ALWAYS_OFF")
MODE_LABELS = {
    "ALWAYS_ON": "Available for charging",
    "ALWAYS_OFF": "Charging disabled",
    "SCHEMA": "Schedule enabled",
    "SCHEDULE": "Schedule enabled",
}


class ChargerError(Exception):
    pass


class ControlRejected(ChargerError):
    pass


def number(value):
    try:
        if type(value) in (int, float) and math.isfinite(value):
            return value
    except OverflowError:
        pass
    return None


def timestamp(value):
    value = number(value)
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def charger_identity(raw):
    if not isinstance(raw, dict) or type(raw.get("serialNumber")) is not int:
        raise ChargerError("Expected a GARO status object with an integer serialNumber.")
    connector = raw.get("connector")
    if not isinstance(connector, str) or not connector.strip():
        raise ChargerError("GARO status is missing its connector state.")
    return raw["serialNumber"], connector


def reported_controls(raw):
    mode = raw.get("mode")
    main = raw.get("mainCharger")
    lock_mode = main.get("cableLockMode") if isinstance(main, dict) and main.get("serialNumber") == raw["serialNumber"] else None
    mode = mode if isinstance(mode, str) else None
    return {
        "available": {"ALWAYS_ON": True, "ALWAYS_OFF": False}.get(mode),
        "mode": "SCHEMA" if mode == "SCHEDULE" else mode if mode in CONTROL_MODES else None,
        "locked": lock_mode != 0 if type(lock_mode) is int and lock_mode in (0, 1, 2) else None,
    }


def normalize(raw, source, now=None):
    """Keep reported measurements distinct from evidence of live charging."""
    now = now or datetime.now(timezone.utc)
    serial, connector = charger_identity(raw)
    mode = raw.get("mode")
    mode = mode if isinstance(mode, str) and mode.strip() else None
    main = raw.get("mainCharger")
    main = main if isinstance(main, dict) else {}
    online = main.get("online")
    online = online if isinstance(online, bool) else None
    contact = timestamp(main.get("lastContact"))
    age = (now - contact).total_seconds() if contact else None
    warnings = []
    freshness = "unknown"
    if age is None:
        warnings.append("The controller did not provide a usable timestamp.")
    elif age < -120:
        freshness = "clock_or_freshness_issue"
        warnings.append("Controller timestamp is in the future. Check the charger clock.")
    elif age > 300:
        freshness = "clock_or_freshness_issue"
        warnings.append("Controller timestamp is over five minutes old. Check its clock and measurement freshness.")
    else:
        freshness = "recent_controller_timestamp"
    if online is False:
        warnings.append("The controller reports its online flag as false. Live power is unverified.")
    current_ma = number(raw.get("currentChargingCurrent"))
    power = number(raw.get("currentChargingPower"))
    # The firmware uses a current of -1 to signal unavailable meter readings.
    if current_ma == -1:
        power = None
    if power is not None and power < 0:
        power = None
    if current_ma is not None and current_ma < 0:
        current_ma = None
    charging = connector == "CHARGING"
    if not charging and ((power or 0) > 0 or (current_ma or 0) > 0):
        warnings.append("The charger may be showing retained power or current from an earlier session.")
    sample = source["kind"] == "sample"
    if sample:
        warnings.insert(0, "SAMPLE DATA from a local JSON file. Live power is unverified.")

    def scaled(key, divisor):
        value = number(raw.get(key))
        return value / divisor if value is not None and value >= 0 else None

    return {
        "source": dict(source), "processed_at_utc": iso(now),
        "serial_number": serial,
        "connector": connector,
        "connector_label": CONNECTOR_ALERTS.get(connector) or CONNECTOR_LABELS.get(connector, "Unknown connector state"),
        "connector_alert": connector in CONNECTOR_ALERTS,
        "mode": mode,
        "mode_label": MODE_LABELS.get(mode),
        "controls": reported_controls(raw),
        "reported_charging": charging,
        "freshness": {
            "status": freshness, "controller_last_contact_utc": iso(contact),
            "controller_timestamp_age_seconds": round(age, 1) if age is not None else None,
            "controller_online_flag": online,
            "note": "Clock-dependent evidence; a fresh HTTP response alone does not prove fresh measurements.",
        },
        "reported": {
            "power_w": power, "current_a": current_ma / 1000 if current_ma is not None else None,
            "session_energy_kwh": scaled("accSessionEnergy", 1000),
            "total_energy_kwh": scaled("latestReading", 1000),
            "session_seconds": scaled("accSessionMillis", 1000),
            "session_started_at_utc": iso(timestamp(raw.get("sessionStartTime"))),
            "temperature_c": number(raw.get("currentTemperature")),
            "current_limit_a": number(raw.get("currentLimit")),
            "hardware_switch_limit_a": number(raw.get("switchCurrentLimit")),
            "factory_limit_a": number(raw.get("factoryCurrentLimit")),
            "pilot_current_a": number(raw.get("pilotLevel")),
            "phases": number(raw.get("nrOfPhases")),
        },
        "power_reading_usable_as_live": bool(
            not sample and freshness == "recent_controller_timestamp" and online is not False
            and charging and power is not None
        ),
        "warnings": warnings,
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ChargerError("Status endpoint redirected. Redirects are disabled; check the configured host/port.")


def invalid_constant(value):
    raise ValueError("Invalid JSON number: " + value)


def parse_json_number(value, convert):
    result = convert(value)
    if number(result) is None:
        raise ValueError("JSON number is outside the supported range.")
    return result


def decode_charger_json(body):
    if len(body) > MAX_BYTES:
        raise ChargerError("Charger response exceeds 1 MiB.")
    return json.loads(
        body,
        parse_constant=invalid_constant,
        parse_float=lambda value: parse_json_number(value, float),
        parse_int=lambda value: parse_json_number(value, int),
    )


def energy_query(query):
    try:
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
        if any(key not in {"year", "month", "timezone", "refresh"} or len(values) != 1
               for key, values in params.items()):
            raise ValueError
        year, month = int(params["year"][0]), int(params["month"][0])
        zone = ZoneInfo(params.get("timezone", ["UTC"])[0])
        refresh = params.get("refresh", ["0"])[0]
        if not FIRST_ENERGY_YEAR <= year <= datetime.now(zone).year or not 1 <= month <= 12:
            raise ValueError
        if refresh not in {"0", "1"}:
            raise ValueError
    except (KeyError, ValueError) as exc:
        raise ValueError("Provide a valid year, month, time zone, and optional refresh flag.") from exc
    return year, month, zone, refresh == "1"


def normalize_energy(raw, serial, year, month, zone):
    if not isinstance(raw, dict) or type(raw.get("chargeboxSerial")) is not int or raw["chargeboxSerial"] != serial:
        raise ChargerError("Energy history did not identify the configured charger.")
    stamps, values = raw.get("timestamps"), raw.get("values")
    if not isinstance(stamps, list) or not isinstance(values, list) or len(stamps) != len(values):
        raise ChargerError("Energy history contained mismatched daily readings.")
    readings = {}
    for stamp, value in zip(stamps, values):
        instant = timestamp(stamp)
        energy = number(value)
        if instant is None or energy is None or energy < 0:
            raise ChargerError("Energy history contained an invalid daily reading.")
        day = instant.astimezone(zone).date()
        key = day.isoformat()
        if (day.year, day.month) != (year, month) or key in readings:
            raise ChargerError("Energy history contained a duplicate or out-of-month reading.")
        readings[key] = energy
    meter = number(raw.get("stopValue"))
    return energy_month(year, month, readings, meter if meter is not None and meter >= 0 else None)


def local_host(value):
    if value == "localhost":
        return value
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Use a private IPv4 address or localhost.")
    if not any(addr in ipaddress.IPv4Network(net) for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")):
        raise argparse.ArgumentTypeError("Only private or loopback addresses are accepted.")
    return str(addr)


class ChargerClient:
    def __init__(self, host=None, port=8080, serial=None, sample=None, timeout=5):
        self.sample = Path(sample) if sample else None
        if not self.sample and (not host or serial is None):
            raise ChargerError("Set GARO_CHARGER_HOST and GARO_CHARGER_SERIAL in .env or pass --host and --serial.")
        self.base = None if self.sample else f"http://{local_host(host)}:{port}"
        self.serial, self.timeout = serial, timeout
        self.lock = threading.Lock()
        self.cached, self.last_attempt, self.last_error = None, None, None
        self.energy_lock = threading.Lock()
        self.energy_cache = OrderedDict()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def source(self, received_at=None):
        return {
            "kind": "sample" if self.sample else "charger_http",
            "endpoint": None if self.sample else self.base + STATUS_PATH,
            "original_interface": None if self.sample else self.base + "/serialweb/",
            "http_received_at_utc": iso(received_at),
        }

    def _fetch(self):
        if self.sample:
            with self.sample.open("rb") as handle:
                raw = decode_charger_json(handle.read(MAX_BYTES + 1))
            received_at = None
        else:
            raw, received_at = self._request_json(STATUS_PATH + "?_=" + str(int(time.time() * 1000)))
        serial, _ = charger_identity(raw)
        if self.serial is not None and serial != self.serial:
            raise ChargerError("Serial mismatch: expected {}, received {}.".format(self.serial, serial))
        return raw, self.source(received_at)

    def _request_json(self, path, payload=None, *, method=None, allow_empty=False):
        headers = {
            "Accept": "application/json", "Cache-Control": "no-cache, no-store", "Pragma": "no-cache",
            "User-Agent": "GARO-local-bridge/0.1.0",
        }
        body = None
        if payload is not None or method == "POST":
            headers["Content-Type"] = "application/json"
        if payload is not None:
            body = json.dumps(payload).encode()
        request = Request(self.base + path, data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=self.timeout) as response:
            body = response.read(MAX_BYTES + 1)
            if response.length and len(body) <= MAX_BYTES:
                raise ChargerError("The charger closed the connection before sending a complete response.")
            received_at = datetime.now(timezone.utc)
        return (None if allow_empty and not body else decode_charger_json(body)), received_at

    def set_availability(self, available):
        return self._set_control("available", available)

    def set_cable_lock(self, locked):
        return self._set_control("locked", locked)

    def set_mode(self, mode):
        return self._set_control("mode", mode)

    def _control_request(self, field, desired, raw):
        config, _ = self._request_json(CONFIG_PATH)
        if not isinstance(config, dict) or config.get("serialNumber") != self.serial:
            raise ControlRejected("The configuration did not identify the configured charger.")
        slaves = config.get("slaveList")
        if not isinstance(slaves, list):
            raise ControlRejected("The charger did not report its outlet configuration.")
        matching = [outlet for outlet in slaves if isinstance(outlet, dict) and outlet.get("serialNumber") == self.serial]
        if len(matching) != 1:
            raise ControlRejected("The configured outlet could not be uniquely identified.")
        if field in ("available", "mode"):
            if config.get("master") is not True or len(slaves) != 1 or config.get("twinSerial", 0) not in (-1, 0):
                raise ControlRejected("Availability control is supported for a single locally managed charger.")
            if raw.get("mode") not in ("ALWAYS_ON", "ALWAYS_OFF", "SCHEMA", "SCHEDULE"):
                raise ControlRejected("The charger did not report a supported availability mode.")
            if field == "mode":
                return MODE_PATH + desired, None
            return MODE_PATH + ("ALWAYS_ON" if desired else "ALWAYS_OFF"), None
        product = config.get("productId")
        version, revision = config.get("firmwareVersion"), config.get("firmwareRevision")
        if (type(product) is not int or product not in OUTLET_PRODUCTS
                or type(version) is not int or type(revision) is not int or (version, revision) < (7, 5)
                or reported_controls(raw)["locked"] is None):
            raise ControlRejected("Cable locking is unavailable for this charger or firmware.")
        # Match the firmware UI by changing the configured outlet's lock field in its original record.
        return CABLE_LOCK_PATH, dict(matching[0], cableLockMode=int(desired))

    def _set_control(self, field, desired):
        if self.sample:
            raise ControlRejected("Charger controls are unavailable in sample mode.")
        if field == "mode":
            if desired not in CONTROL_MODES:
                raise ControlRejected("The requested mode must be one of " + ", ".join(CONTROL_MODES) + ".")
        elif type(desired) is not bool:
            raise ControlRejected("The requested state must be a boolean.")
        with self.lock:
            sent = False
            try:
                raw, source = self._fetch()
                path, payload = self._control_request(field, desired, raw)
                changed = reported_controls(raw)[field] != desired
                if changed:
                    self.cached = self.last_attempt = self.last_error = None
                    sent = True
                    reply, _ = self._request_json(path, payload, method="POST", allow_empty=True)
                    if reply is False or (isinstance(reply, dict) and reply.get("success") is False):
                        raise ControlRejected("The charger refused the requested change.")
                    for delay in CONTROL_READBACK_DELAYS:
                        if delay:
                            time.sleep(delay)
                        raw, source = self._fetch()
                        if reported_controls(raw)[field] == desired:
                            break
                    else:
                        raise ChargerError("The charger did not confirm the requested state. Refresh status before retrying.")
                self.cached = (raw, source)
                self.last_attempt = time.monotonic()
                self.last_error = None
                return {"status": normalize(raw, source), "raw": raw, "changed": changed}
            except ControlRejected:
                raise
            except HTTPError as exc:
                if exc.code in (404, 405):
                    raise ControlRejected("The charger does not support this control.") from exc
                if sent and exc.code in (400, 403, 409, 422, 423):
                    raise ControlRejected("The charger refused the requested change.") from exc
                message = "The charger command could not be confirmed." if sent else "Could not read the charger before making a change."
                raise ChargerError(message) from exc
            except (OSError, ValueError, HTTPException, RecursionError) as exc:
                message = "The charger command could not be confirmed." if sent else "Could not read the charger before making a change."
                raise ChargerError(message) from exc

    def energy_history(self, year, month, zone, refresh=False, *, include_observation=False):
        if self.sample:
            return {"state": "unavailable", "message": "Energy history is unavailable in sample mode."}
        key = (year, month)
        max_age = CACHE_SECONDS if refresh else ENERGY_CACHE_SECONDS
        with self.energy_lock:
            entry = self.energy_cache.pop(key, None)
            try:
                if entry and time.monotonic() - entry[0] < max_age:
                    raw = entry[1]
                else:
                    # The firmware exposes this read-only query as a POST.
                    raw, received_at = self._request_json(ENERGY_PATH, {
                        "chargeboxSerial": str(self.serial), "year": str(year), "month": str(month),
                        "meterSerial": "DEFAULT", "resolution": "DAY",
                    })
                    entry = (time.monotonic(), raw, received_at)
                result = normalize_energy(raw, self.serial, year, month, zone)
            except HTTPError as exc:
                if exc.code in (404, 405):
                    return {"state": "unavailable", "message": "The charger does not expose energy history."}
                raise ChargerError(f"Cannot read energy history: {exc}") from exc
            except (OSError, ValueError, HTTPException, RecursionError, OverflowError, ChargerError) as exc:
                raise ChargerError(f"Cannot read energy history: {exc}") from exc
            self.energy_cache[key] = entry
            if len(self.energy_cache) > ENERGY_CACHE_MONTHS:
                self.energy_cache.popitem(last=False)
            if include_observation and result["state"] == "available":
                meter = raw.get("meterSerial")
                result["meter_serial"] = (meter.strip() or "DEFAULT") if isinstance(meter, str) else "DEFAULT"
                result["observed_at_utc"] = entry[2].isoformat(timespec="microseconds")
            return result

    def snapshot(self):
        with self.lock:
            if self.last_attempt is None or time.monotonic() - self.last_attempt >= CACHE_SECONDS:
                self.cached, self.last_error = None, None
                try:
                    self.cached = self._fetch()
                except (OSError, ValueError, HTTPException, RecursionError, ChargerError) as exc:
                    self.last_error = str(exc) or type(exc).__name__
                finally:
                    self.last_attempt = time.monotonic()
            if self.last_error:
                raise ChargerError("Cannot read charger status: " + self.last_error)
            raw, source = copy.deepcopy(self.cached)
        return normalize(raw, source), raw


def create_mcp(client):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import ToolAnnotations
        from pydantic import StrictBool
    except ImportError:
        raise ChargerError("MCP requires Python 3.10+ and mcp==1.29.0. Install requirements-mcp.txt in a virtual environment.")
    server = FastMCP("garo-local", instructions=(
        "Local GARO integration. Always present connector state alongside freshness warnings. "
        "Positive retained power readings do not prove active charging. Sample data is never live. "
        "Apply controls only when requested. Cable lock readback describes the configured setting."
    ), log_level="WARNING")
    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

    @server.tool(annotations=annotations, structured_output=True)
    async def get_charger_status() -> dict[str, Any]:
        """Read GARO status, power, current, session energy, limits and freshness warnings."""
        status, _ = await asyncio.to_thread(client.snapshot)
        return status

    @server.tool(annotations=annotations, structured_output=True)
    async def get_charger_diagnostics() -> dict[str, Any]:
        """Read normalized status and the original GARO JSON. Does not change settings or reset the charger."""
        status, raw = await asyncio.to_thread(client.snapshot)
        return {"status": status, "raw": raw}

    if not client.sample:
        writes = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)

        @server.tool(annotations=writes, structured_output=True)
        async def set_charger_availability(available: StrictBool) -> dict[str, Any]:
            """Set the charger available or unavailable and verify its reported operating mode."""
            result = await asyncio.to_thread(client.set_availability, available)
            return {"changed": result["changed"], "available": result["status"]["controls"]["available"]}

        @server.tool(annotations=writes, structured_output=True)
        async def set_cable_lock(locked: StrictBool) -> dict[str, Any]:
            """Set the outlet cable lock and verify the reported setting."""
            result = await asyncio.to_thread(client.set_cable_lock, locked)
            return {"changed": result["changed"], "locked": result["status"]["controls"]["locked"]}

    return server


class ArchivedEnergy:
    def __init__(self, client, archive):
        self.client = client
        self.archive = archive

    def collect_month(self, year, month, refresh):
        history = self.client.energy_history(
            year, month, self.archive.zone, refresh=refresh, include_observation=True
        )
        self.archive.record(history)
        return history

    def collect_recent(self, today=None):
        today = today or datetime.now(self.archive.zone).date()
        first = today - timedelta(days=ENERGY_LOOKBACK_DAYS)
        months = dict.fromkeys(((first.year, first.month), (today.year, today.month)))
        success = True
        for year, month in months:
            try:
                result = self.collect_month(year, month, refresh=True)
                if result["state"] == "unavailable":
                    raise ChargerError(result["message"])
            except (ChargerError, OSError, sqlite3.Error) as exc:
                print(f"Energy collection for {year:04}-{month:02} failed: {exc}", file=sys.stderr)
                success = False
        if success:
            self.archive.mark_collection()
        return success

    def run(self, stop, seconds):
        while not stop.is_set():
            try:
                success = self.collect_recent()
            except (OSError, sqlite3.Error) as exc:
                print(f"Energy collection could not update the archive: {exc}", file=sys.stderr)
                success = False
            stop.wait(seconds if success else min(seconds, ENERGY_CACHE_SECONDS))

    def history(self, year, month, refresh):
        saved = self.archive.history(year, month)
        if saved["state"] == "available" and not refresh:
            return saved
        try:
            result = self.collect_month(year, month, refresh)
        except ChargerError as exc:
            saved = self.archive.history(year, month)
            if saved["state"] == "available":
                saved["notice"] = "Could not refresh from the charger. Showing saved readings."
            else:
                saved.update(state="unavailable", message=str(exc))
            return saved
        saved = self.archive.history(year, month)
        if result["state"] == "unavailable":
            if saved["state"] == "available":
                saved["notice"] = "Charger history is unavailable. Showing saved readings."
            else:
                saved.update(result)
        return saved


def dashboard_handler(client, browser_port=None, refresh_seconds=DEFAULT_REFRESH_SECONDS, archived_energy=None):
    refresh_value = str(refresh_seconds).encode("ascii")
    actions = {
        "/api/controls/availability": ("available", client.set_availability),
        "/api/controls/cable-lock": ("locked", client.set_cable_lock),
        "/api/controls/mode": ("mode", client.set_mode),
    }

    class Handler(BaseHTTPRequestHandler):
        def local_origin(self, *, required=False):
            port = browser_port if browser_port is not None else self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if port == 80:
                hosts.update(("127.0.0.1", "localhost"))
            host = self.headers.get("Host")
            origins = self.headers.get_all("Origin", [])
            if host not in hosts or len(self.headers.get_all("Host", [])) != 1:
                return False
            authority = host.removesuffix(":80") if port == 80 else host
            return origins == [f"http://{authority}"] if origins or required else True

        def do_GET(self):
            if not self.local_origin():
                return self.respond(403, b"Host or Origin rejected.", "text/plain")
            path, _, query = self.path.partition("?")
            if path == "/healthz":
                return self.respond_json(200, {"status": "ok"})
            if path == "/api/status":
                try:
                    status, raw = client.snapshot()
                    return self.respond_json(200, {"status": status, "raw": raw})
                except ChargerError as exc:
                    return self.respond_json(502, {"error": str(exc), "source": client.source()})
            if path == "/api/energy":
                try:
                    year, month, zone, refresh = energy_query(query)
                except ValueError as exc:
                    return self.respond_json(400, {"error": str(exc)})
                try:
                    if archived_energy:
                        return self.respond_json(200, archived_energy.history(year, month, refresh))
                    return self.respond_json(200, client.energy_history(year, month, zone, refresh))
                except ChargerError as exc:
                    return self.respond_json(502, {"error": str(exc)})
                except sqlite3.Error:
                    return self.respond_json(502, {"error": "Could not access the local energy archive."})
            if path not in DASHBOARD_ASSETS:
                return self.respond(404, b"Not found", "text/plain")
            filename, mime = DASHBOARD_ASSETS[path]
            body = (ROOT / "dashboard" / filename).read_bytes()
            if path == "/":
                body = body.replace(b"__REFRESH_SECONDS__", refresh_value)
                body = body.replace(b"__FIRST_ENERGY_YEAR__", str(FIRST_ENERGY_YEAR).encode("ascii"))
                if archived_energy:
                    zone_attribute = f'data-energy-timezone="{escape(archived_energy.archive.zone.key)}"'.encode()
                    body = body.replace(b'data-energy-timezone=""', zone_attribute)
            self.respond(200, body, mime)

        do_HEAD = do_GET

        def do_POST(self):
            if self.path not in actions:
                return self.respond(405, b"Method not allowed.", "text/plain")
            if not self.local_origin(required=True):
                return self.respond_json(403, {"error": "Host or Origin rejected."})
            if len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json":
                return self.respond_json(415, {"error": "Expected application/json."})
            lengths = self.headers.get_all("Content-Length", [])
            try:
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding") or not 1 <= int(lengths[0]) <= 256:
                    raise ValueError
                length = int(lengths[0])
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError
                data = json.loads(body)
            except (ValueError, RecursionError):
                return self.respond_json(400, {"error": "Expected a small JSON request body."})
            field, action = actions[self.path]
            if not isinstance(data, dict) or set(data) != {field}:
                return self.respond_json(400, {"error": f"Provide only {field}."})
            if field == "mode" and data[field] not in CONTROL_MODES:
                return self.respond_json(400, {"error": "Provide mode as " + ", ".join(CONTROL_MODES) + "."})
            if field != "mode" and type(data[field]) is not bool:
                return self.respond_json(400, {"error": f"Provide a boolean {field}."})
            try:
                self.respond_json(200, action(data[field]))
            except ControlRejected as exc:
                self.respond_json(409, {"error": str(exc)})
            except ChargerError as exc:
                self.respond_json(502, {"error": str(exc)})

        def respond_json(self, code, data):
            self.respond(code, json.dumps(data, allow_nan=False).encode(), "application/json")

        def respond(self, code, body, mime):
            self.send_response(code)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

    return Handler


def device_settings(path):
    settings = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            key = key.strip()
            if separator and key in DEVICE_ENV:
                settings[key] = value.strip()
    except FileNotFoundError:
        pass
    settings.update({key: os.environ[key] for key in DEVICE_ENV if key in os.environ})
    return settings


def mcp_config(args):
    charger_flags = []
    for option in ("host", "port", "serial"):
        value = getattr(args, option)
        if value is not None:
            charger_flags.extend((f"--{option}", str(value)))
    if not args.docker:
        command = [str(ROOT / "garo_bridge.py"), "mcp", *charger_flags]
        if args.sample:
            command.extend(("--sample", str(args.sample.resolve())))
        return {"mcpServers": {"garo": {"command": sys.executable, "args": command}}}

    config = json.loads((ROOT / "mcp.docker.json").read_text(encoding="utf-8"))
    command = config["mcpServers"]["garo"]["args"]
    env_file = command.index("--env-file")
    command[env_file + 1] = str(ROOT / ".env")
    command.extend(charger_flags)
    if args.sample:
        command[env_file:env_file + 2] = [
            "--mount", f"type=bind,src={args.sample.resolve()},dst=/sample_status.json,readonly",
        ]
        command.extend(("--sample", "/sample_status.json"))
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dashboard", "mcp", "status", "config"))
    parser.add_argument("--host", type=local_host, help="Charger address")
    parser.add_argument("--port", type=int, help="Charger port (default 8080)")
    parser.add_argument("--serial", type=int, help="Expected charger serial")
    parser.add_argument("--docker", action="store_true", help="Generate a Docker MCP config with the config command")
    parser.add_argument("--sample", type=Path, help="Read sample JSON instead of connecting; prominently labelled sample")
    parser.add_argument("--listen-host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1", help="Dashboard bind address")
    parser.add_argument("--listen-port", type=int, default=8765, help="Local dashboard port")
    parser.add_argument("--browser-port", type=int, help="Loopback port used in the browser, if forwarded to the listen port")
    parser.add_argument(
        "--refresh-seconds", type=int, default=DEFAULT_REFRESH_SECONDS,
        help=f"Dashboard refresh interval in seconds (default {DEFAULT_REFRESH_SECONDS})",
    )
    parser.add_argument("--energy-archive", type=Path, help="SQLite path for the dashboard energy archive")
    parser.add_argument("--energy-timezone", default="UTC", help="Time zone for archived daily readings (default UTC)")
    parser.add_argument(
        "--energy-interval-seconds", type=int, default=DEFAULT_ENERGY_INTERVAL_SECONDS,
        help=f"Background energy collection interval (default {DEFAULT_ENERGY_INTERVAL_SECONDS})",
    )
    args = parser.parse_args()
    if args.docker and args.command != "config":
        parser.error("--docker is used with the config command.")
    ports = (args.port, args.listen_port, args.browser_port)
    if not all(1 <= port <= 65535 for port in ports if port is not None):
        parser.error("Ports must be between 1 and 65535.")
    if not CACHE_SECONDS <= args.refresh_seconds <= MAX_REFRESH_SECONDS:
        parser.error(f"--refresh-seconds must be between {CACHE_SECONDS} and {MAX_REFRESH_SECONDS}.")
    if not CACHE_SECONDS <= args.energy_interval_seconds <= threading.TIMEOUT_MAX:
        parser.error(f"--energy-interval-seconds must be between {CACHE_SECONDS} and {int(threading.TIMEOUT_MAX)}.")
    try:
        if args.command == "config":
            print(json.dumps(mcp_config(args), indent=2))
            return 0
        if args.sample:
            client = ChargerClient(serial=args.serial, sample=args.sample)
        else:
            settings = device_settings(ROOT / ".env")
            host = args.host or settings.get("GARO_CHARGER_HOST")
            serial = args.serial if args.serial is not None else settings.get("GARO_CHARGER_SERIAL")
            if not host or serial in (None, ""):
                parser.error("Set GARO_CHARGER_HOST and GARO_CHARGER_SERIAL in .env or pass --host and --serial.")
            try:
                port = args.port if args.port is not None else int(settings.get("GARO_CHARGER_PORT", "8080"))
                serial = int(serial)
            except ValueError:
                parser.error("Set an integer charger port and serial in .env or use --port and --serial.")
            if not 1 <= port <= 65535:
                parser.error("The charger port must be between 1 and 65535.")
            client = ChargerClient(host, port, serial)
        if args.command == "mcp":
            create_mcp(client).run(transport="stdio")
        elif args.command == "status":
            print(json.dumps(client.snapshot()[0], indent=2, allow_nan=False))
        else:
            archived_energy = None
            if args.energy_archive and not args.sample:
                try:
                    zone = ZoneInfo(args.energy_timezone)
                except (ZoneInfoNotFoundError, ValueError):
                    parser.error("--energy-timezone must be a valid IANA time zone.")
                archive = EnergyArchive(args.energy_archive, client.serial, zone)
                archived_energy = ArchivedEnergy(client, archive)
            server = ThreadingHTTPServer(
                (args.listen_host, args.listen_port),
                dashboard_handler(client, browser_port=args.browser_port, refresh_seconds=args.refresh_seconds,
                                  archived_energy=archived_energy),
            )
            stop = threading.Event()
            collector = None
            print("GARO dashboard: http://127.0.0.1:{}/".format(args.browser_port or args.listen_port), flush=True)
            mode = "sample data" if args.sample else "live charger"
            print(f"Dashboard using {mode}. Ctrl+C to stop.", flush=True)
            try:
                if archived_energy:
                    collector = threading.Thread(
                        target=archived_energy.run, args=(stop, args.energy_interval_seconds), daemon=True
                    )
                    collector.start()
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                stop.set()
                server.server_close()
                if collector:
                    collector.join(timeout=10)
    except (ChargerError, OSError, sqlite3.Error, ValueError, argparse.ArgumentTypeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
