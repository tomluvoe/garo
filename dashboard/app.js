"use strict";

const el = (id) => document.getElementById(id);
const blank = "—";
const formats = {
  date: new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }),
  time: new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }),
  number: new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }),
};
const freshnessLabels = {
  recent_controller_timestamp: "Recent timestamp",
  clock_or_freshness_issue: "Timestamp outside window",
  unknown: "Unverified",
};

function text(id, value) {
  const node = el(id);
  if (node.textContent !== value) node.textContent = value;
}

function quantity(value, unit = "") {
  if (!Number.isFinite(value)) return blank;
  return formats.number.format(value) + (unit ? " " + unit : "");
}

function metric(id, value, unit) {
  if (!Number.isFinite(value)) {
    text(id, blank);
    return;
  }
  const suffix = document.createElement("span");
  suffix.className = "unit";
  suffix.textContent = unit;
  el(id).replaceChildren(quantity(value), " ", suffix);
}

function formatDate(value, format = formats.date) {
  return value ? format.format(new Date(value)) : blank;
}

function formatDuration(seconds) {
  if (!Number.isFinite(seconds)) return blank;
  if (seconds === 0) return "0 min";
  if (seconds < 60) return "<1 min";
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours ? quantity(hours, "h") + " " + quantity(minutes % 60, "min") : quantity(minutes, "min");
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

function renderNotes(messages, title = "Reading notes") {
  el("notice").hidden = messages.length === 0;
  text("notice-title", title);
  el("warnings").replaceChildren(...messages.map((message) => {
    const item = document.createElement("li");
    item.textContent = message;
    return item;
  }));
}

function renderReadings(readings = {}, live = false) {
  const power = readings.power_w;
  metric("power", Number.isFinite(power) ? power / 1000 : null, "kW");
  metric("energy", readings.session_energy_kwh, "kWh");
  metric("limit", readings.current_limit_a, "A");
  metric("temperature", readings.temperature_c, "°C");
  text("pilot", Number.isFinite(readings.pilot_current_a)
    ? "Pilot advertises " + quantity(readings.pilot_current_a, "A")
    : "Pilot current unavailable");
  text("power-note", live ? "Reported during active charging"
    : Number.isFinite(power) ? "Live power unverified" : "Power reading unavailable");
  text("current", quantity(readings.current_a, "A"));
  text("phases", quantity(readings.phases));
  text("total", quantity(readings.total_energy_kwh, "kWh"));
  text("session-start", formatDate(readings.session_started_at_utc));
  text("duration", formatDuration(readings.session_seconds));
  text("switch-limit", quantity(readings.hardware_switch_limit_a, "A"));
  text("factory-limit", quantity(readings.factory_limit_a, "A"));
}

let newestStatusTime = -Infinity;

function render({ status, raw }) {
  const receivedAt = Date.parse(status.source.http_received_at_utc);
  if (receivedAt < newestStatusTime) return;
  if (Number.isFinite(receivedAt)) newestStatusTime = receivedAt;
  const sample = status.source.kind === "sample";
  const live = status.power_reading_usable_as_live;
  const meterStatus = raw?.mainCharger?.meterStatus;
  el("energy-meter-warning").hidden = sample || !Number.isInteger(meterStatus) || meterStatus === 0;
  renderSource(status.source);
  el("charger-name").hidden = false;
  text("charger-name", "Serial " + status.serial_number);
  document.querySelector(".overview").dataset.tone = status.connector_alert ? "attention" : "normal";
  text("state", status.connector_label);
  const detail = status.reported_charging && !live && !sample ? "Live power is unverified." : "";
  el("state-detail").hidden = !detail;
  text("state-detail", detail);
  el("mode").hidden = !status.mode;
  text("mode", status.mode ? "Operating mode · " + (status.mode_label || status.mode) : "");
  text("connection-state", sample ? "Sample file" : "Charger API reachable");
  text("updated", formatDate(status.source.http_received_at_utc, formats.time));
  text("freshness", sample ? "Sample data" : freshnessLabels[status.freshness.status] || "Unverified");
  text("last-contact", formatDate(status.freshness.controller_last_contact_utc));
  const online = status.freshness.controller_online_flag;
  text("online-flag", typeof online === "boolean" ? String(online) : blank);
  text("connector-code", status.connector);

  renderReadings(status.reported, live);
  renderNotes(status.warnings);
  text("raw", JSON.stringify(raw, null, 2));
  window.dispatchEvent(new CustomEvent("garo-status", { detail: status }));
}

function renderError(message, source) {
  const sample = source?.kind === "sample";
  el("energy-meter-warning").hidden = true;
  renderSource(source);
  el("charger-name").hidden = true;
  el("mode").hidden = true;
  document.querySelector(".overview").dataset.tone = "unavailable";
  text("state", sample ? "Sample unavailable" : source ? "Charger status unavailable" : "Dashboard unavailable");
  el("state-detail").hidden = false;
  text("state-detail", sample ? "The bridge could not load the sample file."
    : source ? "The bridge could not read a valid response from the charger."
    : "This tab could not get a response from the local dashboard server.");
  text("connection-state", sample ? "Sample file error" : source ? "Charger read failed" : "Dashboard request failed");
  text("updated", blank);
  text("freshness", "Unverified");
  for (const id of ["last-contact", "online-flag", "connector-code"]) text(id, blank);

  renderReadings();
  renderNotes([message], "Status request failed");
  text("raw", "No current response available.");
  window.dispatchEvent(new CustomEvent("garo-status", { detail: null }));
}

const configuredInterval = Number(document.body.dataset.refreshSeconds) * 1000;
const refreshInterval = configuredInterval >= 5_000 && configuredInterval <= 2_147_483_647
  ? configuredInterval : 60_000;
let refreshing = false;
let refreshTimer;
let nextRefreshAt = 0;

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
    nextRefreshAt = performance.now() + refreshInterval;
    refreshing = false;
    el("refresh").removeAttribute("aria-busy");
    scheduleRefresh();
  }
}

el("refresh").addEventListener("click", refresh);
document.addEventListener("visibilitychange", scheduleRefresh);
scheduleRefresh();
