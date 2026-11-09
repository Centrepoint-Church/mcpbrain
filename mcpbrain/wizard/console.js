/* mcpbrain console: shared theme and API helper for every console page.
 *
 * Loaded with a plain <script src> in <head>, before the stylesheet paints, so
 * the saved mode and main colour apply with no flash. Exposes
 * window.mcpConsole = {api, applyTheme, relTime, time, dayKey, tz, ...}.
 *
 * Storage keys (localStorage, per browser):
 *   mcpbrain-mode    "light" | "dark" (absent: follow macOS appearance)
 *   mcpbrain-accent  {"hex", "h", "c", "coolH", "badH"} (absent: raspberry)
 */
(function () {
  "use strict";
  var root = document.documentElement;
  var MODE_KEY = "mcpbrain-mode";
  var ACCENT_KEY = "mcpbrain-accent";

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  function applyTheme() {
    var m = read(MODE_KEY);
    if (m === "light" || m === "dark") root.dataset.mode = m;
    else delete root.dataset.mode;
    var a = null;
    try { a = JSON.parse(read(ACCENT_KEY) || "null"); } catch (e) { a = null; }
    var props = [["--accent-h", "h"], ["--accent-c", "c"], ["--cool-h", "coolH"], ["--bad-h", "badH"]];
    props.forEach(function (p) {
      var v = a && a[p[1]];
      if (typeof v === "number" && isFinite(v)) root.style.setProperty(p[0], String(v));
      else root.style.removeProperty(p[0]);
    });
  }
  applyTheme();

  function currentMode() {
    if (root.dataset.mode) return root.dataset.mode;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function wireModeToggles() {
    document.querySelectorAll("[data-mode-toggle]").forEach(function (b) {
      function label() {
        b.setAttribute("aria-label", currentMode() === "dark" ? "Switch to light mode" : "Switch to dark mode");
      }
      label();
      b.addEventListener("click", function () {
        var next = currentMode() === "dark" ? "light" : "dark";
        root.dataset.mode = next;
        try { localStorage.setItem(MODE_KEY, next); } catch (e) { /* still applies this visit */ }
        label();
        document.dispatchEvent(new CustomEvent("mcpconsole:mode", {detail: {mode: next}}));
      });
      if (window.matchMedia) {
        var mq = matchMedia("(prefers-color-scheme: dark)");
        if (mq.addEventListener) mq.addEventListener("change", label);
      }
    });
  }

  // Another tab changed the mode or colour: follow it.
  window.addEventListener("storage", function (e) {
    if (e.key === MODE_KEY || e.key === ACCENT_KEY) {
      applyTheme();
      document.dispatchEvent(new CustomEvent("mcpconsole:mode", {detail: {mode: currentMode()}}));
    }
  });

  // ---- API ---------------------------------------------------------------

  function token() {
    var m = document.querySelector('meta[name="mcpbrain-token"]');
    return m ? m.getAttribute("content") : "";
  }

  var offline = false;
  function setOffline(on) {
    if (offline === on) return;
    offline = on;
    if (on) root.dataset.offline = "true";
    else delete root.dataset.offline;
    document.dispatchEvent(new CustomEvent(on ? "mcpconsole:offline" : "mcpconsole:online"));
  }

  /* api(path, {method, body}) resolves with the parsed JSON body of a 2xx
   * reply. Anything else rejects with an Error carrying .status and .data
   * (the parsed body, so a 409's {reason} or {error} can be shown in place).
   * A failed connection marks the page offline (status 0); the next reply
   * of any kind marks it back online. */
  function api(path, opts) {
    opts = opts || {};
    var init = {method: opts.method || "GET",
                headers: {Authorization: "Bearer " + token()}};
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    } else if (init.method !== "GET") {
      init.headers["Content-Type"] = "application/json";
      init.body = "{}";
    }
    return fetch(path, init).then(function (res) {
      setOffline(false);
      return res.text().then(function (text) {
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = {error: text}; }
        if (res.ok) return data;
        var msg = (data && (data.reason || data.error)) || ("Request failed (" + res.status + ")");
        var err = new Error(msg);
        err.status = res.status;
        err.data = data;
        throw err;
      });
    }, function () {
      setOffline(true);
      var err = new Error("mcpbrain is not responding");
      err.status = 0;
      err.offline = true;
      throw err;
    });
  }

  // ---- time --------------------------------------------------------------
  // Times show in the configured timezone (the page sets mcpConsole.tz from
  // /api/config); until it is known, the Mac's own zone.

  function parse(iso) {
    if (!iso) return null;
    var s = String(iso);
    // "2026-11-09 01:02:03" (SQLite) is UTC with no marker.
    if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}/.test(s)) s = s.replace(" ", "T");
    if (!/[zZ]|[+-]\d{2}:?\d{2}$/.test(s) && /T\d{2}:\d{2}/.test(s)) s += "Z";
    var d = new Date(s);
    return isNaN(d.getTime()) ? null : d;
  }

  function fmt(d, o) {
    var opts = Object.assign({}, o);
    if (api.tz) opts.timeZone = api.tz;
    try { return new Intl.DateTimeFormat("en-AU", opts).format(d); }
    catch (e) { delete opts.timeZone; return new Intl.DateTimeFormat("en-AU", opts).format(d); }
  }

  function time(iso) {
    var d = parse(iso);
    return d ? fmt(d, {hour: "2-digit", minute: "2-digit", hour12: false}) : "";
  }

  function dayKey(iso) {
    var d = parse(iso);
    return d ? fmt(d, {year: "numeric", month: "2-digit", day: "2-digit"}) : "";
  }

  function relTime(iso, now) {
    var d = parse(iso);
    if (!d) return "never";
    now = now || new Date();
    var mins = Math.round((now - d) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return mins + " min ago";
    var today = dayKey(now.toISOString());
    var yest = dayKey(new Date(now.getTime() - 86400000).toISOString());
    var k = dayKey(d.toISOString());
    if (k === today) return time(d.toISOString());
    if (k === yest) return "yesterday " + time(d.toISOString());
    return fmt(d, {day: "numeric", month: "short"});
  }

  var exported = {
    api: api, applyTheme: applyTheme, relTime: relTime, time: time, dayKey: dayKey,
    parse: parse, currentMode: currentMode, isOffline: function () { return offline; }
  };
  // tz lives on the api function too so fmt() reads one place.
  Object.defineProperty(exported, "tz", {
    get: function () { return api.tz || ""; },
    set: function (v) { api.tz = v || ""; }
  });
  window.mcpConsole = exported;

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wireModeToggles);
  else wireModeToggles();
})();
