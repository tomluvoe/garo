"use strict";

(() => {
  const panel = document.getElementById("charger-controls");
  const availability = document.getElementById("control-availability");
  const cableLock = document.getElementById("control-cable-lock");
  const message = document.getElementById("control-result");
  const buttons = [...panel.querySelectorAll("button[data-field]")];
  const paths = { available: "/api/controls/availability", locked: "/api/controls/cable-lock" };
  let current;
  let busy = false;

  function update() {
    const controls = current?.controls;
    const knownMode = ["ALWAYS_ON", "ALWAYS_OFF", "SCHEMA", "SCHEDULE"].includes(current?.mode);
    availability.textContent = current?.mode_label || "Unknown";
    cableLock.textContent = controls?.locked === true ? "Lock enabled"
      : controls?.locked === false ? "Lock disabled" : "Unknown";
    for (const button of buttons) {
      const field = button.dataset.field;
      const reported = controls?.[field];
      const known = field === "available" ? knownMode : typeof reported === "boolean";
      button.disabled = busy || !known || reported === (button.dataset.value === "true");
    }
  }

  window.addEventListener("garo-status", (event) => {
    const status = event.detail;
    current = status?.source.kind === "charger_http" ? status : null;
    if (status) panel.hidden = !current;
    update();
  });

  for (const button of buttons) {
    button.addEventListener("click", async () => {
      if (busy || button.disabled) return;
      const field = button.dataset.field;
      const state = button.dataset.value === "true";
      busy = true;
      update();
      message.hidden = false;
      message.textContent = "Sending request…";
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), 30_000);
      try {
        const response = await fetch(paths[field], {
          method: "POST", headers: { "Content-Type": "application/json" },
          referrerPolicy: "same-origin", body: JSON.stringify({ [field]: state }), signal: abort.signal,
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "The charger command failed.");
        render(data);
        message.textContent = data.changed ? "The charger confirmed the setting."
          : "The charger already reports the requested setting.";
      } catch (error) {
        message.textContent = error.name === "AbortError" ? "The command could not be confirmed." : error.message;
        refresh();
      } finally {
        clearTimeout(timer);
        busy = false;
        update();
      }
    });
  }
})();
