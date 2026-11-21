"use strict";

(() => {
  const panel = document.getElementById("energy-history");
  const month = document.getElementById("energy-month");
  const year = document.getElementById("energy-year");
  const reload = document.getElementById("energy-reload");
  const message = document.getElementById("energy-message");
  const results = document.getElementById("energy-results");
  const scale = document.getElementById("energy-scale");
  const bars = document.getElementById("energy-bars");
  const table = document.getElementById("energy-days");
  const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" });
  const monthName = new Intl.DateTimeFormat(undefined, { month: "long", timeZone: "UTC" });
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const now = new Date();
  let pending;

  for (let index = 0; index < 12; index++) {
    month.add(new Option(monthName.format(Date.UTC(2024, index)), index + 1));
  }
  for (let value = now.getFullYear(); value >= Number(year.dataset.firstYear); value--) {
    year.add(new Option(value, value));
  }
  month.value = now.getMonth() + 1;
  year.value = now.getFullYear();

  function formatEnergy(value) {
    return value > 0 && value < 0.001 ? "<" + number.format(0.001) : number.format(value);
  }

  function render({ days, total_kwh, meter_kwh }) {
    const known = days.filter((day) => day.kwh !== null);
    document.getElementById("energy-total").textContent = formatEnergy(total_kwh) + " kWh";
    document.getElementById("energy-meter").textContent = meter_kwh === null
      ? "Unavailable" : formatEnergy(meter_kwh) + " kWh";
    document.getElementById("energy-legend").hidden = known.length === days.length;
    message.textContent = known.length + " of " + days.length + " days with readings.";

    const peak = Math.max(...known.map((day) => day.kwh));
    const unit = 10 ** Math.floor(Math.log10(peak || 1));
    const max = Math.ceil(peak / unit) * unit || 1;
    scale.replaceChildren(...[1, 0.75, 0.5, 0.25, 0].map((fraction) => {
      const tick = document.createElement("span");
      tick.textContent = number.format(max * fraction);
      return tick;
    }));

    const columns = document.createDocumentFragment();
    const rows = document.createDocumentFragment();
    for (const day of days) {
      const dayNumber = Number(day.date.slice(-2));
      const label = date.format(new Date(day.date + "T00:00:00Z"));
      const value = day.kwh === null ? "No reading" : formatEnergy(day.kwh);

      const column = document.createElement("div");
      column.className = "energy-column";
      column.title = label + " · " + value + (day.kwh === null ? "" : " kWh");
      const track = document.createElement("div");
      track.className = "energy-track";
      const bar = document.createElement("div");
      bar.className = "energy-bar";
      if (day.kwh === null) bar.classList.add("missing");
      else if (day.kwh === 0) bar.classList.add("zero");
      else bar.style.height = (day.kwh / max * 100) + "%";
      track.append(bar);
      const tick = document.createElement("span");
      if (dayNumber === 1 || dayNumber % 5 === 0) tick.textContent = dayNumber;
      column.append(track, tick);
      columns.append(column);

      const row = document.createElement("tr");
      const dayCell = document.createElement("th");
      dayCell.scope = "row";
      dayCell.textContent = label;
      const energyCell = document.createElement("td");
      energyCell.textContent = value;
      row.append(dayCell, energyCell);
      rows.append(row);
    }
    bars.replaceChildren(columns);
    table.replaceChildren(rows);
    results.hidden = false;
  }

  async function load(refresh = false) {
    if (!panel.open) return;
    pending?.abort();
    const request = new AbortController();
    pending = request;
    reload.disabled = true;
    results.hidden = true;
    message.textContent = "Loading energy history…";
    const query = new URLSearchParams({ year: year.value, month: month.value, timezone: timeZone });
    if (refresh) query.set("refresh", "1");
    const timeout = setTimeout(() => request.abort(), 8000);
    try {
      const response = await fetch("/api/energy?" + query, { cache: "no-store", signal: request.signal });
      const data = await response.json();
      if (pending !== request) return;
      if (!response.ok) message.textContent = data.error || "Could not load energy history.";
      else if (data.state === "available") render(data);
      else if (data.state === "empty") message.textContent = "No daily energy readings for this month.";
      else message.textContent = data.message || "Energy history is unavailable.";
    } catch (error) {
      if (pending !== request) return;
      message.textContent = error.name === "AbortError"
        ? "The energy history request timed out." : "Could not load energy history.";
    } finally {
      clearTimeout(timeout);
      if (pending === request) {
        pending = undefined;
        reload.disabled = false;
      }
    }
  }

  panel.addEventListener("toggle", () => {
    if (panel.open) load();
    else {
      pending?.abort();
      pending = undefined;
    }
  });
  month.addEventListener("change", () => load());
  year.addEventListener("change", () => load());
  reload.addEventListener("click", () => load(true));
  if (panel.open) load();
})();
