"use strict";

(() => {
  const panel = document.getElementById("charger-controls");
  const modeButtons = [...panel.querySelectorAll("button[data-mode]")];
  const modeText = document.getElementById("mode-text");
  const lockState = document.getElementById("control-cable-lock");
  const lockButton = document.getElementById("cable-lock-toggle");
  const message = document.getElementById("control-result");
  const MODE_TEXT = {
    ALWAYS_ON: "Charging is always allowed.",
    SCHEMA: "Charging follows the weekly schedule saved on the charger.",
    ALWAYS_OFF: "Charging is blocked.",
  };
  let current;
  let busy = false;
  let hideTimer;

  function update() {
    const controls = current?.controls;
    const mode = controls?.mode ?? null;
    for (const button of modeButtons) {
      const selected = button.dataset.mode === mode;
      button.setAttribute("aria-pressed", String(selected));
      button.disabled = busy || !mode || selected;
    }
    modeText.textContent = MODE_TEXT[mode] || (current?.mode_label ?? "Mode unknown");

    const lockEnabled = controls?.locked;
    lockState.textContent = lockEnabled === true ? "Lock enabled" : lockEnabled === false ? "Lock disabled" : "Cable lock unknown";
    lockButton.textContent = lockEnabled === true ? "Disable lock" : "Enable lock";
    lockButton.disabled = busy || typeof lockEnabled !== "boolean";
  }

  function say(text, linger) {
    clearTimeout(hideTimer);
    message.hidden = false;
    message.textContent = text;
    if (linger) hideTimer = setTimeout(() => { message.hidden = true; }, 6000);
  }

  async function send(path, body) {
    if (busy) return;
    busy = true;
    update();
    say("Sending to the charger…");
    const abort = new AbortController();
    const timer = setTimeout(() => abort.abort(), 30_000);
    try {
      const response = await fetch(path, {
        method: "POST", headers: { "Content-Type": "application/json" },
        referrerPolicy: "same-origin", body: JSON.stringify(body), signal: abort.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "The charger command failed.");
      render(data);
      say(data.changed ? "The charger confirmed the change." : "The charger already had this setting.", true);
    } catch (error) {
      say(error.name === "AbortError" ? "The charger did not confirm the change. Check the status before trying again." : error.message, false);
      refresh();
    } finally {
      clearTimeout(timer);
      busy = false;
      update();
    }
  }

  window.addEventListener("garo-status", (event) => {
    const status = event.detail;
    current = status?.source.kind === "charger_http" ? status : null;
    if (status) panel.hidden = !current;
    update();
  });

  for (const button of modeButtons) {
    button.addEventListener("click", () => {
      if (!button.disabled) send("/api/controls/mode", { mode: button.dataset.mode });
    });
  }
  lockButton.addEventListener("click", () => {
    const locked = current?.controls?.locked;
    if (!lockButton.disabled && typeof locked === "boolean") send("/api/controls/cable-lock", { locked: !locked });
  });
})();
