"use strict";

const el = (id) => document.getElementById(id);
const blank = "—";
const formats = {
  date: new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }),
  clock: new Intl.DateTimeFormat(undefined, { timeStyle: "short" }),
  time: new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }),
  number: new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }),
};
const freshnessLabels = {
  recent_controller_timestamp: "Recent timestamp",
  clock_or_freshness_issue: "Timestamp outside window",
  unknown: "Unverified",
};
// Connector codes that mean a car is plugged in but not drawing power.
const WAITING = new Set(["CONNECTED", "CHARGING_PAUSED", "UNAVAILABLE", "CHANGING", "INITIALIZATION"]);
const FINISHED = new Set(["CHARGING_FINISHED", "CHARGING_CANCELLED"]);
const DISABLED = new Set(["DISABLED", "REMOTE_DISABLED"]);
const IDLE_DETAIL = {
  ALWAYS_ON: "Plug in a car to start charging.",
  SCHEMA: "Charging starts at the next scheduled time.",
  ALWAYS_OFF: "Charging is off. Choose On to allow charging.",
};

function text(id, value) {
  const node = el(id);
  if (node.textContent !== value) node.textContent = value;
}

function quantity(value, unit = "") {
  if (!Number.isFinite(value)) return blank;
  return formats.number.format(value) + (unit ? " " + unit : "");
}

function withUnit(id, value, unit, extra = "") {
  if (!Number.isFinite(value)) {
    text(id, blank);
    return;
  }
  const suffix = document.createElement("span");
  suffix.className = "unit";
  suffix.textContent = unit + extra;
  el(id).replaceChildren(formats.number.format(value), " ", suffix);
}

function formatDate(value, format = formats.date) {
  return value ? format.format(new Date(value)) : blank;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "";
  if (seconds < 60) return "under a minute";
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours ? hours + " h " + (minutes % 60) + " min" : minutes + " min";
}

function normalMode(mode) {
  return mode === "SCHEDULE" ? "SCHEMA" : mode;
}

function sessionLine(reported, includeStart) {
  const energy = reported.session_energy_kwh;
  if (!Number.isFinite(energy) || energy <= 0) return "";
  let line = quantity(energy, "kWh") + " added";
  const duration = formatDuration(reported.session_seconds);
  if (duration) line += " in " + duration;
  if (includeStart && reported.session_started_at_utc) {
    line += ", since " + formats.clock.format(new Date(reported.session_started_at_utc));
  }
  return line + ".";
}

function overviewState(status) {
  const connector = status.connector;
  if (status.connector_alert) return ["attention", status.connector_label];
  if (status.reported_charging) return ["charging", "Charging"];
  if (WAITING.has(connector)) return ["waiting", "Waiting"];
  if (FINISHED.has(connector)) return ["finished", "Finished"];
  if (DISABLED.has(connector) || normalMode(status.mode) === "ALWAYS_OFF") return ["off", "Off"];
  return ["ready", "Ready"];
}

function renderOverview(status) {
  const reported = status.reported;
  const [state, chip] = overviewState(status);
  const live = state === "charging" && status.power_reading_usable_as_live;
  document.querySelector(".overview").dataset.state = state;
  text("state", chip);

  const headline = el("headline");
  const power = reported.power_w;
  if (live && Number.isFinite(power)) {
    const unit = document.createElement("span");
    unit.className = "unit";
    unit.textContent = "kW";
    headline.replaceChildren(formats.number.format(power / 1000), " ", unit);
  } else {
    text("headline", state === "charging" ? "Charging reported" : status.connector_label);
  }

  let detail;
  if (state === "charging") detail = sessionLine(reported, true) || (live ? "The session has just started." : "");
  else if (state === "finished") detail = sessionLine(reported, false) || "The car has stopped charging.";
  else if (state === "waiting") detail = "The car is connected and not drawing power.";
  else if (state === "attention") detail = "Check the charger before charging.";
  else detail = IDLE_DETAIL[normalMode(status.mode)] || "";
  el("state-detail").hidden = !detail;
  text("state-detail", detail);

  const current = reported.current_a;
  const limit = reported.current_limit_a;
  const drawing = live && Number.isFinite(current) && Number.isFinite(limit) && limit > 0;
  el("draw").hidden = !drawing;
  if (drawing) el("draw-fill").style.width = Math.min(100, current / limit * 100) + "%";

  const notes = [];
  if (drawing) {
    const phases = reported.phases;
    notes.push("Drawing " + quantity(current, "A") + " of the " + quantity(limit, "A") + " limit"
      + (Number.isFinite(phases) ? " on " + phases + (phases === 1 ? " phase." : " phases.") : "."));
  }
  if (state === "charging" && !live && status.source.kind !== "sample") notes.push("Live power is unverified.");
  el("state-note").hidden = notes.length === 0;
  text("state-note", notes.join(" "));

  // Shown only when the mode buttons are not, for example in sample mode.
  const showMode = Boolean(status.mode) && status.source.kind !== "charger_http";
  el("mode").hidden = !showMode;
  text("mode", showMode ? "Operating mode: " + (status.mode_label || status.mode) : "");
}

function renderSource(source) {
  const sample = source?.kind === "sample";
  el("sample-banner").hidden = !sample;
  el("endpoint-row").hidden = !source?.endpoint;
  text("endpoint", source?.endpoint ? new URL(source.endpoint).host : "");
  const original = el("original");
  original.hidden = !source?.original_interface;
  if (source?.original_interface) original.href = source.original_interface;
  else original.removeAttribute("href");
}

function renderDiagnostics(warnings, attention) {
  const list = el("warnings");
  list.hidden = warnings.length === 0;
  list.replaceChildren(...warnings.map((message) => {
    const item = document.createElement("li");
    item.textContent = message;
    return item;
  }));
  el("diagnostics").dataset.tone = attention ? "attention" : "normal";
  text("diagnostics-summary", warnings.length === 0 ? "No reading notes"
    : warnings.length === 1 ? "1 reading note" : warnings.length + " reading notes");
}

function renderReadings(reported = {}) {
  withUnit("total", reported.total_energy_kwh, "kWh");
  const factory = reported.factory_limit_a;
  withUnit("limit", reported.current_limit_a, "A", Number.isFinite(factory) ? " of " + quantity(factory, "A") : "");
  withUnit("switch-limit", reported.hardware_switch_limit_a, "A");
  text("phases", quantity(reported.phases));
  withUnit("temperature", reported.temperature_c, "°C");
  text("pilot", quantity(reported.pilot_current_a, "A"));
  text("power", Number.isFinite(reported.power_w) ? quantity(reported.power_w / 1000, "kW") : blank);
  text("current", quantity(reported.current_a, "A"));
  text("factory-limit", quantity(factory, "A"));
}

function renderLive(state, label) {
  el("live").dataset.state = state;
  text("updated", label);
}

let newestStatusTime = -Infinity;
let charging = false;

function render({ status, raw }) {
  const receivedAt = Date.parse(status.source.http_received_at_utc);
  if (receivedAt < newestStatusTime) return;
  if (Number.isFinite(receivedAt)) newestStatusTime = receivedAt;
  const sample = status.source.kind === "sample";
  const meterStatus = raw?.mainCharger?.meterStatus;
  el("energy-meter-warning").hidden = sample || !Number.isInteger(meterStatus) || meterStatus === 0;
  el("alert").hidden = true;
  renderSource(status.source);
  el("charger-name").hidden = false;
  text("charger-name", "Charger " + status.serial_number);
  renderOverview(status);
  renderLive(sample ? "sample" : "live", sample ? "Sample data"
    : "Updated " + formatDate(status.source.http_received_at_utc, formats.time));

  text("connection-state", sample ? "Sample file" : "Charger API reachable");
  text("received", formatDate(status.source.http_received_at_utc, formats.time));
  text("freshness", sample ? "Sample data" : freshnessLabels[status.freshness.status] || "Unverified");
  text("last-contact", formatDate(status.freshness.controller_last_contact_utc));
  const online = status.freshness.controller_online_flag;
  text("online-flag", typeof online === "boolean" ? String(online) : blank);
  text("connector-code", status.connector);
  renderReadings(status.reported);
  renderDiagnostics(status.warnings, status.freshness.status === "clock_or_freshness_issue");
  text("raw", JSON.stringify(raw, null, 2));
  charging = status.reported_charging && !sample;
  window.dispatchEvent(new CustomEvent("garo-status", { detail: status }));
}

function renderError(message, source) {
  const sample = source?.kind === "sample";
  el("energy-meter-warning").hidden = true;
  renderSource(source);
  el("charger-name").hidden = true;
  el("mode").hidden = true;
  el("draw").hidden = true;
  el("state-note").hidden = true;
  document.querySelector(".overview").dataset.state = "unavailable";
  text("state", "Not connected");
  text("headline", sample ? "Sample unavailable" : source ? "Charger not responding" : "Dashboard unavailable");
  el("state-detail").hidden = false;
  text("state-detail", sample ? "The bridge could not load the sample file."
    : source ? "Check that the charger is powered and on the network. The dashboard keeps trying."
    : "This tab could not reach the local dashboard server. Check that it is running.");
  el("alert").hidden = false;
  text("alert", message);
  renderLive("error", "Not connected");
  text("connection-state", sample ? "Sample file error" : source ? "Charger read failed" : "Dashboard request failed");
  text("received", blank);
  text("freshness", "Unverified");
  for (const id of ["last-contact", "online-flag", "connector-code"]) text(id, blank);

  renderReadings();
  renderDiagnostics([message], true);
  text("raw", "No current response available.");
  charging = false;
  window.dispatchEvent(new CustomEvent("garo-status", { detail: null }));
}

const configuredInterval = Number(document.body.dataset.refreshSeconds) * 1000;
const refreshInterval = configuredInterval >= 5_000 && configuredInterval <= 2_147_483_647
  ? configuredInterval : 60_000;
// Poll faster while a car charges so power and session energy stay current.
const chargingInterval = Math.min(refreshInterval, 30_000);
let refreshing = false;
let refreshTimer;
let nextRefreshAt = 0;

text("refresh-note", chargingInterval < refreshInterval
  ? "Refreshes every " + chargingInterval / 1000 + " seconds while charging and every "
    + refreshInterval / 1000 + " seconds otherwise, while this tab is visible."
  : "Refreshes every " + refreshInterval / 1000 + " seconds while this tab is visible.");

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  if (document.hidden || refreshing) return;
  refreshTimer = setTimeout(refresh, Math.max(0, Math.ceil(nextRefreshAt - performance.now())));
}

async function refresh() {
  if (refreshing || document.hidden) return;
  clearTimeout(refreshTimer);
  refreshing = true;
  el("refresh").setAttribute("aria-busy", "true");
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("/api/status", { cache: "no-store", signal: controller.signal });
    const data = await response.json();
    if (response.ok) render(data);
    else renderError(data.error || "Status unavailable.", data.source);
  } catch (error) {
    const message = error.name === "AbortError" ? "The dashboard request timed out." : "Could not load charger status from the local bridge.";
    renderError(message);
  } finally {
    clearTimeout(timeout);
    nextRefreshAt = performance.now() + (charging ? chargingInterval : refreshInterval);
    refreshing = false;
    el("refresh").removeAttribute("aria-busy");
    scheduleRefresh();
  }
}

el("refresh").addEventListener("click", refresh);
document.addEventListener("visibilitychange", scheduleRefresh);
scheduleRefresh();
