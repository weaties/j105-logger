// Race start UI — #644.
//
// Server returns a snapshot { phase, kind, t0_utc, sync_offset_s, flags, ... }.
// The client computes the live countdown locally from t0_utc and reconciles
// every 30 s. Each user action POSTs to a mutation endpoint and refreshes
// from the response.

(function () {
  "use strict";

  const grid = document.querySelector(".rs-grid");
  const isWriter = grid && grid.dataset.isWriter === "true";
  const errorEl = document.getElementById("rs-error");
  const clockEl = document.getElementById("rs-clock");
  const phaseEl = document.getElementById("rs-phase");
  const syncStatusEl = document.getElementById("rs-sync-status");
  const classFlagEl = document.getElementById("rs-class-flag");
  const prepFlagEl = document.getElementById("rs-prep-flag");
  const specialFlagEl = document.getElementById("rs-special-flag");

  let snapshot = null;

  function showError(msg) {
    errorEl.textContent = msg || "";
  }

  function fmtClock(seconds) {
    const sign = seconds < 0 ? "-" : "+";
    const abs = Math.abs(Math.floor(seconds));
    const mm = Math.floor(abs / 60);
    const ss = abs % 60;
    return sign + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");
  }

  function renderClock() {
    if (!snapshot || !snapshot.t0_utc) {
      clockEl.textContent = "--:--";
      clockEl.classList.remove("warn", "go");
      return;
    }
    const t0 = new Date(snapshot.t0_utc).getTime();
    const now = Date.now();
    const remaining = (t0 - now) / 1000;  // seconds; negative after t0

    // Display countdown as a positive number until t0, then count up.
    const displaySec = remaining >= 0 ? remaining : -remaining;
    const sign = remaining >= 0 ? "" : "+";
    const mm = Math.floor(displaySec / 60);
    const ss = Math.floor(displaySec % 60);
    clockEl.textContent = sign + String(mm).padStart(2, "0") + ":" + String(ss).padStart(2, "0");

    clockEl.classList.toggle("warn", remaining > 0 && remaining <= 60);
    clockEl.classList.toggle("go", remaining <= 0);
  }

  function renderFlags() {
    if (!snapshot || !snapshot.flags) return;
    classFlagEl.textContent = snapshot.flags.class_flag_up || "—";
    prepFlagEl.textContent = snapshot.flags.prep_flag_up || "—";
    specialFlagEl.textContent = snapshot.flags.special_flag_up || "—";
  }

  function renderPhase() {
    if (!snapshot) return;
    phaseEl.textContent = snapshot.phase;
    if (snapshot.last_sync_at_utc) {
      const last = new Date(snapshot.last_sync_at_utc);
      const ageS = (Date.now() - last.getTime()) / 1000;
      if (ageS > 300) {
        syncStatusEl.textContent = " · sync stale (" + Math.round(ageS) + "s)";
        syncStatusEl.style.color = "var(--warning)";
      } else {
        syncStatusEl.textContent = " · synced";
        syncStatusEl.style.color = "var(--text-muted)";
      }
    } else {
      syncStatusEl.textContent = " · drift unverified";
      syncStatusEl.style.color = "var(--warning)";
    }
  }

  function renderLineMetrics(metrics) {
    function set(id, value) { document.getElementById(id).textContent = value; }
    if (!metrics) {
      set("rs-line-bearing", "—");
      set("rs-line-length", "—");
      set("rs-line-bias", "—");
      set("rs-line-dist", "—");
      set("rs-line-time", "—");
      return;
    }
    set("rs-line-bearing", metrics.line_bearing_deg.toFixed(0) + "°");
    set("rs-line-length", metrics.line_length_m.toFixed(0) + " m");
    if (metrics.line_bias_deg === null || metrics.line_bias_deg === undefined) {
      set("rs-line-bias", "TWD needed");
    } else {
      const sign = metrics.line_bias_deg >= 0 ? "+" : "";
      const fav = metrics.favoured_end ? " " + metrics.favoured_end : "";
      set("rs-line-bias", sign + metrics.line_bias_deg.toFixed(0) + "°" + fav);
    }
    set("rs-line-dist",
      metrics.distance_to_line_m === null ? "—"
        : metrics.distance_to_line_m.toFixed(0) + " m");
    set("rs-line-time",
      metrics.time_to_line_s === null ? "—"
        : metrics.time_to_line_s.toFixed(0) + " s");
  }

  async function refreshState() {
    try {
      const r = await fetch("/api/race-start/state");
      if (!r.ok) throw new Error("HTTP " + r.status);
      snapshot = await r.json();
      renderPhase();
      renderFlags();
      renderClock();
      showError("");
    } catch (e) {
      showError("could not load state: " + e.message);
    }
  }

  async function postJSON(url, body) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await r.json();
    if (!r.ok) {
      throw new Error(data.detail || ("HTTP " + r.status));
    }
    return data;
  }

  async function action(url, body) {
    if (!isWriter) return;
    showError("");
    try {
      snapshot = await postJSON(url, body);
      renderPhase();
      renderFlags();
      renderClock();
    } catch (e) {
      showError(e.message);
    }
  }

  function defaultT0Utc() {
    // Default arm: 5 minutes from now, rounded up to next 30s.
    const ms = Date.now() + 5 * 60 * 1000;
    const rounded = Math.ceil(ms / 30000) * 30000;
    return new Date(rounded).toISOString();
  }

  function bind(id, fn) {
    const btn = document.getElementById(id);
    if (btn && !btn.disabled) btn.addEventListener("click", fn);
  }

  bind("rs-arm", () => action("/api/race-start/arm",
        { kind: "5-4-1-0", t0_utc: defaultT0Utc() }));
  bind("rs-sync", () => {
    if (!snapshot || !snapshot.t0_utc) return showError("arm a sequence first");
    // Sync at t0 — the user is tapping at the start gun.
    action("/api/race-start/sync", { expected_signal_offset_s: 0 });
  });
  bind("rs-plus-min", () => action("/api/race-start/nudge", { delta_s: 60 }));
  bind("rs-minus-min", () => action("/api/race-start/nudge", { delta_s: -60 }));
  bind("rs-postpone", () => action("/api/race-start/postpone"));
  bind("rs-recall", () => action("/api/race-start/recall"));
  bind("rs-abandon", () => {
    if (confirm("Abandon the race?")) action("/api/race-start/abandon");
  });
  bind("rs-reset", () => {
    if (confirm("Reset the sequence?")) action("/api/race-start/reset");
  });

  function geolocateAndPing(end) {
    if (!isWriter) return;
    if (!navigator.geolocation) return showError("geolocation unavailable");
    navigator.geolocation.getCurrentPosition(
      (pos) => action("/api/race-start/ping/" + end, {
        latitude_deg: pos.coords.latitude,
        longitude_deg: pos.coords.longitude,
      }),
      (err) => showError("geolocation: " + err.message),
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 1000 }
    );
  }
  bind("rs-ping-boat", () => geolocateAndPing("boat"));
  bind("rs-ping-pin", () => geolocateAndPing("pin"));

  // Live tick at 4 Hz; reconcile from server every 30 s.
  setInterval(renderClock, 250);
  setInterval(refreshState, 30000);

  refreshState();
})();
