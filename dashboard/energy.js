"use strict";

(() => {
  const panel = document.getElementById("energy-history");
  const period = document.getElementById("energy-period");
  const prev = document.getElementById("energy-prev");
  const next = document.getElementById("energy-next");
  const reload = document.getElementById("energy-reload");
  const total = document.getElementById("energy-total");
  const summary = document.getElementById("energy-summary");
  const message = document.getElementById("energy-message");
  const archiveNote = document.getElementById("energy-archive-note");
  const refreshWarning = document.getElementById("energy-refresh-warning");
  const results = document.getElementById("energy-results");
  const legend = document.getElementById("energy-legend");
  const scale = document.getElementById("energy-scale");
  const bars = document.getElementById("energy-bars");
  const table = document.getElementById("energy-days");
  const number = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
  const precise = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
  const date = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeZone: "UTC" });
  const monthYear = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
  const timeZone = panel.dataset.energyTimezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const collectionTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone });
  const firstYear = Number(panel.dataset.firstYear) || 2017;
  const RELOAD_CURRENT_MS = 10 * 60_000;
  let view;
  let pending;

  function today() {
    const parts = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone })
      .formatToParts(new Date());
    const get = (type) => parts.find((part) => part.type === type).value;
    return { year: Number(get("year")), month: Number(get("month")), iso: get("year") + "-" + get("month") + "-" + get("day") };
  }

  function isCurrent() {
    const now = today();
    return view.year === now.year && view.month === now.month;
  }

  function updateNav() {
    period.textContent = monthYear.format(Date.UTC(view.year, view.month - 1));
    prev.disabled = view.year === firstYear && view.month === 1;
    next.disabled = isCurrent();
  }

  function renderArchive(archive, notice) {
    archiveNote.hidden = !archive;
    if (archive) {
      const last = archive.last_collection_at_utc;
      archiveNote.textContent = last
        ? "Saved readings, last collected " + collectionTime.format(new Date(last)) + " (" + archive.timezone + ")."
        : "Waiting for the first background collection.";
    }
    refreshWarning.hidden = !notice;
    refreshWarning.textContent = notice || "";
  }

  function formatEnergy(value, format = number) {
    return value > 0 && value < 0.1 ? "<" + number.format(0.1) : format.format(value);
  }

  function render({ days, total_kwh, meter_kwh }) {
    const todayIso = today().iso;
    const past = days.filter((day) => day.date <= todayIso);
    const known = past.filter((day) => day.kwh !== null);
    const charged = known.filter((day) => day.kwh > 0);
    const missing = past.length - known.length;
    total.textContent = formatEnergy(total_kwh);
    summary.textContent = (isCurrent() ? "this month, " : "") + "on " + charged.length
      + (charged.length === 1 ? " charging day" : " charging days");
    message.textContent = "";
    const notes = [];
    if (missing) notes.push("Dashed marks show days with no reading.");
    if (meter_kwh !== null) notes.push("Meter reading at period end: " + formatEnergy(meter_kwh) + " kWh.");
    legend.textContent = notes.join(" ");
    legend.hidden = notes.length === 0;

    const peak = Math.max(0, ...known.map((day) => day.kwh));
    const unit = 10 ** Math.floor(Math.log10(peak || 1));
    const max = Math.ceil(peak / unit) * unit || 1;
    scale.replaceChildren(...[1, 0.5, 0].map((fraction) => {
      const tick = document.createElement("span");
      tick.textContent = number.format(max * fraction);
      return tick;
    }));

    const columns = document.createDocumentFragment();
    const rows = document.createDocumentFragment();
    for (const day of days) {
      const dayNumber = Number(day.date.slice(-2));
      const future = day.date > todayIso;
      const label = date.format(new Date(day.date + "T00:00:00Z"));
      const value = day.kwh === null ? "No reading" : formatEnergy(day.kwh, precise);

      const column = document.createElement("div");
      column.className = "energy-column";
      if (!future) column.title = label + ": " + value + (day.kwh === null ? "" : " kWh");
      const track = document.createElement("div");
      track.className = "energy-track";
      const bar = document.createElement("div");
      bar.className = "energy-bar";
      if (day.date === todayIso) bar.classList.add("today");
      if (future) bar.classList.add("future");
      else if (day.kwh === null) bar.classList.add("missing");
      else if (day.kwh === 0) bar.classList.add("zero");
      else bar.style.height = (day.kwh / max * 100) + "%";
      track.append(bar);
      const tick = document.createElement("span");
      if (dayNumber === 1 || dayNumber % 5 === 0) tick.textContent = dayNumber;
      column.append(track, tick);
      columns.append(column);

      if (future) continue;
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

  function showEmpty(text) {
    total.textContent = "—";
    summary.textContent = "";
    results.hidden = true;
    message.textContent = text;
  }

  async function load(refresh = false) {
    pending?.abort();
    const request = new AbortController();
    pending = request;
    updateNav();
    reload.disabled = true;
    reload.setAttribute("aria-busy", "true");
    refreshWarning.hidden = true;
    message.textContent = results.hidden ? "Loading energy history…" : "";
    const query = new URLSearchParams({ year: view.year, month: view.month, timezone: timeZone });
    if (refresh) query.set("refresh", "1");
    const timeout = setTimeout(() => request.abort(), 8000);
    try {
      const response = await fetch("/api/energy?" + query, { cache: "no-store", signal: request.signal });
      const data = await response.json();
      if (pending !== request) return;
      if (response.ok) renderArchive(data.archive, data.notice);
      if (!response.ok) showEmpty(data.error || "Could not load energy history.");
      else if (data.state === "available") render(data);
      else if (data.state === "empty") showEmpty("No charging recorded this month.");
      else showEmpty(data.message || "Energy history is unavailable.");
    } catch (error) {
      if (pending !== request) return;
      showEmpty(error.name === "AbortError" ? "The energy history request timed out." : "Could not load energy history.");
    } finally {
      clearTimeout(timeout);
      if (pending === request) {
        pending = undefined;
        reload.disabled = false;
        reload.removeAttribute("aria-busy");
      }
    }
  }

  function step(delta) {
    const index = view.year * 12 + (view.month - 1) + delta;
    view = { year: Math.floor(index / 12), month: (index % 12) + 1 };
    showEmpty("Loading energy history…");
    load();
  }

  const now = today();
  view = { year: now.year, month: now.month };
  prev.addEventListener("click", () => { if (!prev.disabled) step(-1); });
  next.addEventListener("click", () => { if (!next.disabled) step(1); });
  reload.addEventListener("click", () => load(true));
  setInterval(() => { if (!document.hidden && !pending && isCurrent()) load(); }, RELOAD_CURRENT_MS);
  load();
})();
