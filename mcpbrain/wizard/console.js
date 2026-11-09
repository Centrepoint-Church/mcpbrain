/* mcpbrain console: shared theme and API helper for every console page.
 *
 * Loaded with a plain <script src> in <head>, before the stylesheet paints, so
 * the saved mode and main colour apply with no flash. Exposes
 * window.mcpConsole = {api, applyTheme, relTime, time, dayKey, tz, ...} plus the
 * page helpers every console page shares (el, fill, said, worded, act, renderChip,
 * wireOffline, orgRow, collectOrgs, populateTimezones, ...), defined here once.
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

  /* api(path, {method, body, text}) resolves with the parsed JSON body of a
   * 2xx reply, or with the raw text when text is true (logs, reports). Anything else rejects with an Error carrying .status and .data
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
        if (res.ok && opts.text) return text;
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (e) { data = {error: text}; }
        if (res.ok) return data;
        var msg = (data && (data.reason || data.error)) || ("Request failed (" + res.status + ")");
        // A restored snapshot is being swapped in: every route answers 503.
        if (res.status === 503 && data && data.error === "restoring")
          msg = "mcpbrain is restoring a backup, try again in a moment.";
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

  // ---- page helpers --------------------------------------------------------

  /* Build an element. Every string goes in through textContent: names,
     summaries and folders come from mail, documents, config and the model. */
  function el(tag, attrs) {
    var n = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === "text") n.textContent = v;
      else if (k === "class") n.className = v;
      else if (k.indexOf("on") === 0) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : String(v));
    });
    var kids = Array.prototype.slice.call(arguments, 2);
    (function add(list) {
      list.forEach(function (kid) {
        if (Array.isArray(kid)) { add(kid); return; }
        if (kid == null || kid === false) return;
        n.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
      });
    })(kids);
    return n;
  }
  function flat(list, out) {
    list.forEach(function (k) { if (Array.isArray(k)) flat(k, out); else if (k != null && k !== false) out.push(k); });
    return out;
  }
  function fill(node) { node.replaceChildren.apply(node, flat(Array.prototype.slice.call(arguments, 1), [])); }
  function said(node, text, kind) {
    node.textContent = text || "";
    node.classList.remove("said--ok", "said--bad");
    if (kind) node.classList.add("said--" + kind);
    node.hidden = !text;
  }
  function errText(e) {
    return e && e.offline ? "mcpbrain is not responding." : (e && e.message) || "Something went wrong.";
  }
  // Server detail strings are written for a terminal: dashes become commas.
  function words(s) { return String(s || "").replace(/\s+—\s+/g, ", ").replace(/—/g, ", "); }
  /* worded:start */
  // A code from the server in words: the map's entry, or `fallback` for a code
  // the page doesn't know yet. A raw code never reaches the page.
  function worded(map, code, fallback) {
    if (map && code != null && code !== "" && Object.prototype.hasOwnProperty.call(map, code)) {
      var v = map[code];
      if (v != null && v !== "") return v;
    }
    return fallback;
  }
  /* worded:end */
  function fmtNum(n) { return (typeof n === "number" ? n : 0).toLocaleString("en-AU"); }
  function plural(n, one, many) { return fmtNum(n) + " " + (n === 1 ? one : (many || one + "s")); }
  function monthYear(v) {
    var d = parse(/^\d{4}-\d{2}$/.test(String(v)) ? v + "-01T00:00:00" : v);
    return d ? d.toLocaleDateString("en-AU", {month: "short", year: "numeric"}) : String(v || "");
  }

  /* Run an action from a button: disable it while the request runs, report
     the result (or the server's reason, such as a 409's) next to it, and
     never assume success. On failure the button comes back; on success it
     stays disabled unless opts.again. Resolves true when it went through. */
  function act(btn, out, run, opts) {
    opts = opts || {};
    btn.disabled = true; btn.setAttribute("aria-busy", "true");
    return Promise.resolve().then(run).then(function (msg) {
      if (out && msg) said(out, msg, "ok");
      if (opts.again) btn.disabled = false;
      btn.removeAttribute("aria-busy");
      return true;
    }, function (e) {
      if (out) said(out, errText(e), "bad");
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      return false;
    });
  }
  function reenable(btn, ms) { setTimeout(function () { btn.disabled = false; }, ms); }

  // Health chip in the top bar: the Overview's verdict, in words.
  var CHIP = {
    healthy: ["chip--ok", "Healthy"], needs_you: ["chip--person", "Needs you"],
    behind: ["chip--person", "Behind"], stalled: ["chip--bad", "Stalled"],
    paused: ["chip--quiet", "Paused"], offline: ["chip--bad", "Offline"]
  };
  function renderChip(state, reasons) {
    var chip = document.getElementById("health-chip");
    if (!chip) return;
    var c = CHIP[state] || ["chip--quiet", "Unknown"];
    chip.className = "chip " + c[0];
    chip.textContent = c[1];
    chip.title = (reasons || []).join(". ");
  }
  /* Show #offline (and the Offline chip) while mcpbrain isn't answering;
     #offline-retry, when the page has one, calls retry. */
  function wireOffline(retry) {
    document.addEventListener("mcpconsole:offline", function () {
      var b = document.getElementById("offline");
      if (b) b.hidden = false;
      renderChip("offline", ["mcpbrain is not responding"]);
    });
    document.addEventListener("mcpconsole:online", function () {
      var b = document.getElementById("offline");
      if (b) b.hidden = true;
    });
    var r = document.getElementById("offline-retry");
    if (r && retry) r.addEventListener("click", function () { retry(); });
  }

  /* One organisation row: name, email domains, Remove. Removing the last
     row clears it instead, so there is always one to type into. */
  function orgRow(name, domains, onRemove) {
    var n = el("input", {class: "input org-name", type: "text", "aria-label": "Organisation",
      placeholder: "Organisation name", autocomplete: "off", spellcheck: "false"});
    n.value = name || "";
    var d = el("input", {class: "input org-domains", type: "text", placeholder: "Email domains, comma-separated",
      autocomplete: "off", spellcheck: "false"});
    d.value = (domains || []).join(", ");
    function label() {
      var who = n.value.trim() || "this organisation";
      d.setAttribute("aria-label", "Email domains for " + who);
      x.setAttribute("aria-label", "Remove " + who);
    }
    var row = el("div", {class: "orgs__row org-row"}, n, d);
    var x = el("button", {class: "btn", type: "button", text: "Remove", onclick: function () {
      var box = row.parentNode;
      if (box && box.querySelectorAll(".org-row").length > 1) row.remove();
      else { n.value = ""; d.value = ""; label(); }
      if (onRemove) onRemove();
    }});
    row.append(x);
    n.addEventListener("input", label);
    label();
    return row;
  }
  // Rows with a name become {name, domains: [...]}; blank rows are skipped.
  function collectOrgs(box) {
    var out = [];
    box.querySelectorAll(".org-row").forEach(function (row) {
      var name = row.querySelector(".org-name").value.trim();
      if (!name) return;
      out.push({name: name, domains: row.querySelector(".org-domains").value.split(",")
        .map(function (x) { return x.trim().toLowerCase(); }).filter(Boolean)});
    });
    return out;
  }
  /* Fill a timezone <select> from /api/timezones, choosing the saved zone,
     else the Mac's own. */
  function populateTimezones(sel, selected) {
    var detected = "";
    try { detected = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* none */ }
    return api("/api/timezones").then(function (j) {
      fill(sel, (j.zones || []).map(function (z) {
        var o = el("option", {value: z.value, text: z.label});
        if (z.value === (selected || detected)) o.selected = true;
        return o;
      }));
    }, function () {
      var v = selected || detected;
      fill(sel, el("option", {value: v, text: v || "Couldn’t load timezones"}));
    });
  }

  var exported = {
    api: api, applyTheme: applyTheme, relTime: relTime, time: time, dayKey: dayKey,
    parse: parse, currentMode: currentMode, isOffline: function () { return offline; },
    el: el, fill: fill, said: said, errText: errText, words: words, worded: worded, fmt: fmtNum, plural: plural,
    monthYear: monthYear, act: act, reenable: reenable, renderChip: renderChip,
    wireOffline: wireOffline, orgRow: orgRow, collectOrgs: collectOrgs,
    populateTimezones: populateTimezones
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
