"""The console's Settings page (/settings) and the setup flow's routing.

/settings serves settings.html with the token injected, like /dashboard. `/`
serves the setup page until the install is configured, then redirects to the
Overview. The page itself is checked as source: every section, every endpoint
it acts through, one filled primary, no colour literals, no innerHTML, and the
accent maths run under node.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcpbrain import config, tenant
from mcpbrain.control_api import ControlServer

_ROOT = Path(__file__).parent.parent
_WIZ = _ROOT / "mcpbrain" / "wizard"
PAGE = (_WIZ / "settings.html").read_text(encoding="utf-8") if (_WIZ / "settings.html").exists() else ""


class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _get_raw(url):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        r = opener.open(url, timeout=5)
        return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


@pytest.fixture
def srv(tmp_path):
    s = ControlServer(FakeDaemon(), home=str(tmp_path))
    s.start()
    yield s
    s.stop()


# --- routes ------------------------------------------------------------------

def test_settings_serves_with_token(srv):
    code, headers, html = _get_raw(f"http://127.0.0.1:{srv.port}/settings")
    assert code == 200
    assert headers["Content-Type"].startswith("text/html")
    assert srv.token in html and "__MCPBRAIN_TOKEN__" not in html


def test_root_serves_setup_until_configured(srv):
    code, _, html = _get_raw(f"http://127.0.0.1:{srv.port}/")
    assert code == 200
    assert srv.token in html and 'id="step-google"' in html


def test_root_redirects_to_overview_once_configured(srv):
    config.write_config(str(srv.home), {
        "owner_name": "Dana", "owner_full_name": "Dana Okafor",
        "owner_email": "dana@example.org",
        "orgs": [{"name": "Harbour Trust", "domains": ["example.org"]}]})
    assert config.is_configured(str(srv.home))
    code, headers, body = _get_raw(f"http://127.0.0.1:{srv.port}/")
    assert code == 302
    assert headers["Location"] == "/dashboard"
    # The token is never handed out on the redirect.
    assert srv.token not in body


def test_settings_500s_when_template_missing(tmp_path, monkeypatch):
    from mcpbrain import control_api
    monkeypatch.setattr(control_api.Path, "exists", lambda self: False)
    s = ControlServer(FakeDaemon(), home=str(tmp_path)); s.start()
    try:
        code, _, body = _get_raw(f"http://127.0.0.1:{s.port}/settings")
        assert code == 500 and "not found" in body
    finally:
        s.stop()


# --- page source ---------------------------------------------------------------

SECTIONS = ("profile", "appearance", "sources", "automatic", "connections",
            "hidden", "system", "advanced")


def test_every_section_in_order_with_a_jump_list():
    pos = [PAGE.index(f'<section class="section" id="{s}"') for s in SECTIONS]
    assert pos == sorted(pos)
    jump = PAGE[PAGE.index('class="jump"'):PAGE.index('id="profile"')]
    for s in SECTIONS:
        assert f'href="#{s}"' in jump


ENDPOINTS = (
    "/api/config", "/api/settings", "/api/pending", "/api/system", "/api/system/login",
    "/api/restart", "/api/update/check", "/api/update/apply", "/api/diagnostics/run",
    "/api/diagnostics", "/api/logs/", "/api/support/report", "/api/model/status",
    "/api/model/ensure", "/api/sync-now", "/api/backup/run", "/api/graph/suppressed",
    "/restore", "/api/auth/start", "/api/connect-desktop", "/api/dashboard/stats",
    "/api/status",
)


def test_timezones_through_the_shared_helper():
    assert 'C.populateTimezones($("timezone")' in PAGE
    assert "/api/timezones" in (_WIZ / "console.js").read_text(encoding="utf-8")


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_page_acts_through_each_endpoint(endpoint):
    assert endpoint in PAGE


def test_no_catch_up_enrichment():
    assert "enrich-backfill" not in PAGE
    assert "Catch up" not in PAGE


def test_exactly_one_filled_primary_and_it_saves_the_profile():
    assert PAGE.count("btn--primary") == 1
    m = re.search(r'<button[^>]*btn--primary[^>]*>([^<]*)</button>', PAGE)
    assert m and m.group(1).strip() == "Save profile"


def test_no_colour_literals_and_no_network():
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", PAGE)
    assert "oklch(" not in PAGE and "rgb(" not in PAGE
    assert "fonts.googleapis" not in PAGE
    assert not re.search(r"""(?:src|href)=["']https?://""", PAGE)


def test_page_never_uses_innerhtml():
    assert "innerHTML" not in PAGE and "insertAdjacentHTML" not in PAGE


def test_shared_shell_and_theme():
    assert '<link rel="stylesheet" href="/static/console.css">' in PAGE
    assert '<script src="/static/console.js"></script>' in PAGE
    assert '<meta name="mcpbrain-token" content="__MCPBRAIN_TOKEN__">' in PAGE
    assert 'aria-current="page">Settings' in PAGE
    assert "data-mode-toggle" in PAGE
    assert "mcpbrain-accent" in PAGE and "mcpbrain-mode" in PAGE


def test_no_em_dashes_or_middle_dots_in_copy():
    assert "—" not in PAGE
    assert "·" not in PAGE and "&middot;" not in PAGE


def _scripts(html):
    return re.findall(r"<script>(.*?)</script>", html, flags=re.S)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scripts_parse(tmp_path):
    scripts = _scripts(PAGE)
    assert scripts
    for i, src in enumerate(scripts):
        p = tmp_path / f"s{i}.js"
        p.write_text(src)
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def _accent_maths():
    m = re.search(r"/\* accent-maths:start \*/(.*?)/\* accent-maths:end \*/", PAGE, flags=re.S)
    assert m, "accent maths block missing"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_accent_maths_strict_thresholds(tmp_path):
    p = tmp_path / "accent.js"
    p.write_text(_accent_maths() + """
const out = {
  raspberry: accentFor(355, 0.19), teal40: accentFor(165, 0.1), teal39: accentFor(166, 0.1),
  red35a: accentFor(65, 0.1), red34a: accentFor(64, 0.1), red34b: accentFor(356, 0.1),
  red35b: accentFor(355, 0.1), strong: accentFor(300, 0.5), weak: accentFor(300, 0.01),
  teal: deriveAccent("#12808a"), crimson: deriveAccent("#b8253f"),
};
console.log(JSON.stringify(out));
""")
    r = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    # The default raspberry keeps measured teal and status red at 30.
    assert (o["raspberry"]["coolH"], o["raspberry"]["badH"]) == (205, 30)
    # Strictly less than 40 degrees from 205 moves measured values to 255.
    assert o["teal40"]["coolH"] == 205 and o["teal39"]["coolH"] == 255
    # Strictly less than 35 from 30 moves the red to 5 or 55, the farther one.
    assert o["red35a"]["badH"] == 30 and o["red35b"]["badH"] == 30
    assert o["red34a"]["badH"] == 5 and o["red34b"]["badH"] == 55
    # Strength clamped to 0.06..0.2.
    assert o["strong"]["c"] == 0.2 and o["weak"]["c"] == 0.06
    assert o["teal"]["coolH"] == 255 and o["teal"]["hex"] == "#12808a"
    assert o["crimson"]["badH"] == 55
    assert set(o["teal"]) >= {"hex", "h", "c", "coolH", "badH"}


def test_presets_are_tokens_in_the_stylesheet():
    css = (_WIZ / "console.css").read_text(encoding="utf-8")
    for name in ("raspberry", "crimson", "magenta", "cobalt", "teal", "slate"):
        assert f"--preset-{name}:" in css
        assert f'data-preset="{name}"' in PAGE
    assert 'type="color"' in PAGE and "Any colour" in PAGE


def test_start_at_login_only_through_its_own_route_with_the_agreed_help():
    assert "Takes effect next time you log in. While it's off, mcpbrain won't restart " \
           "itself if it stops; start it from the menu bar." in PAGE.replace("’", "'")
    assert "login_supported" in PAGE
    # Never written through /api/settings.
    assert "start_at_login:" not in PAGE.replace(" ", "").replace('"start_at_login":', "")


def test_restart_names_the_reconnect():
    assert "menu bar" in PAGE[PAGE.index('id="restart-confirm"'):][:600]


def test_keep_out_lists_say_which_is_which():
    assert "Searchable, kept out of the graph" in PAGE
    assert "Not indexed" in PAGE
    for key in ("generated_gmail_subject_prefixes", "generated_drive_folders",
                "exclude_senders", "exclude_labels"):
        assert key in PAGE


def test_automatic_changes_has_seven_rows_and_the_tidy_cap():
    # Rows are drawn from one table, in this order, each tagged data-kind.
    kinds = ("profiles", "circles", "reference", "voice", "consolidation", "tidy", "draft_style")
    pos = [PAGE.index(f'["{k}", ') for k in kinds]
    assert pos == sorted(pos)
    assert '"data-kind": kind' in PAGE
    # The per-pass cap sits under Graph tidy-up, not as a general cap.
    assert 'if (kind !== "tidy") return [row];' in PAGE
    assert "return [row, capRow()];" in PAGE
    assert "review_max_apply_per_run" in PAGE and 'id: "review-max"' in PAGE
    assert "Advise only" in PAGE and "Ask me first" in PAGE


def test_drive_row_reads_shared_drives_from_stats():
    assert "shared drive" in PAGE and "not syncing" in PAGE
    assert ".shared" in PAGE


def test_diagnostics_replaces_the_no_repair_phrase():
    assert "no repair registered" in PAGE   # the phrase it looks for
    assert "Fix this from the menu bar or ask for help" in PAGE


# --- fleet folders (moved from the setup page) ----------------------------------

def test_fleet_ids_read_only_until_edit_and_saved_together():
    assert re.search(r'id="fleet_folder_id"[^>]*\breadonly\b', PAGE)
    assert re.search(r'id="fleet_escrow_folder_id"[^>]*\breadonly\b', PAGE)
    assert len(re.findall(r"function saveFleet\(", PAGE)) == 1
    body = PAGE[PAGE.index("function saveFleet("):][:900]
    assert "folder_id:" in body and "escrow_folder_id:" in body


def test_fleet_save_disabled_until_prefill_completes():
    assert re.search(r'id="fleet-save-btn"[^>]*\bdisabled\b', PAGE)
    assert 'fleetLoaded = true' in PAGE


def test_settings_does_not_hardcode_tenant_or_folder_ids():
    prof = tenant.load(_ROOT / "mcpbrain" / "tenant.json")
    assert prof.display_name not in PAGE
    assert prof.fleet_folder_id not in PAGE
    assert prof.escrow_folder_id not in PAGE
    assert "fleet-tenant-name" in PAGE


# --- fix round 1 -----------------------------------------------------------------

def _run_block(tmp_path, marker, tail):
    m = re.search(rf"/\* {marker}:start \*/(.*?)/\* {marker}:end \*/", PAGE, flags=re.S)
    assert m, f"{marker} block missing"
    p = tmp_path / f"{marker}.js"
    # words() comes from console.js on the page; the same function, inlined.
    p.write_text('const words = (s) => String(s || "").replace(/\\s+\\u2014\\s+/g, ", ")'
                 '.replace(/\\u2014/g, ", ");\n' + m.group(1) + tail)
    r = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hidden_reasons_are_words(tmp_path):
    o = _run_block(tmp_path, "reason-words", """
console.log(JSON.stringify([reasonWords("graph-ui"), reasonWords("user"), reasonWords("orphan_check"),
  reasonWords("No links \\u2014 nothing points here"), reasonWords("")]));""")
    assert o[0] == "you hid it in the graph"
    assert o[1] == "you hid it through Claude"
    assert o[2] == "hidden by an automatic check"
    assert o[3] == "reason given: No links, nothing points here"
    assert o[4] == ""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_save_profile_keeps_a_distinct_short_name(tmp_path):
    o = _run_block(tmp_path, "owner-name", """
const saved = {owner_full_name: "Dana Okafor", owner_name: "Dee"};
console.log(JSON.stringify([ownerNameFor("Dana Okafor", saved), ownerNameFor("Dana Reyes", saved),
  ownerNameFor("Dana Okafor", {owner_full_name: "Dana Okafor", owner_name: ""}),
  ownerNameFor("Marcus Reyes", {})]));""")
    assert o == ["Dee", "Dana", "Dana", "Marcus"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_doctor_lines_are_worded(tmp_path):
    o = _run_block(tmp_path, "diag-words", r"""
const L = [
  {status: "bad", text: "❌ Google           Sign-in expired — reconnect → repair failed: HttpError 401 boom"},
  {status: "bad", text: "❌ Claude           Not seen → repair did not fix it; run mcpbrain setup"},
  {status: "bad", text: "❌ Records          not created → creating... ✅ fixed"},
  {status: "bad", text: "❌ Daemon           no repair registered for 'daemon'"},
  {status: "bad", text: "❌ Records          records repo create failed: OSError(13, 'denied')"},
  {status: "info", text: "➖ Baseline         skipped (RuntimeError: no store)"},
  {status: "bad", text: "❌ Embedder         weights missing → re-download failed: x (needs network; rerun mcpbrain doctor when online)"},
  {status: "ok", text: "✅ Watchdog         no stall restarts"},
];
console.log(JSON.stringify(L.map(diagLine)));""")
    texts = [d["text"] for d in o]
    for t in texts:
        for raw in ("HttpError", "OSError", "RuntimeError", "mcpbrain setup", "mcpbrain doctor",
                    "→", "✅", "❌", "➖", "—", "repair registered", "  "):
            assert raw not in t, (raw, t)
    assert texts[0].startswith("Google Sign-in expired, reconnect. The automatic fix failed.")
    assert "Details are in the mcpbrain log." in texts[0]
    assert "Fix this from the menu bar or ask for help." in texts[1]
    assert o[2] == {"status": "ok", "text": "Records not created, fixed automatically."}
    assert texts[3] == "Daemon Fix this from the menu bar or ask for help."
    assert "Details are in the mcpbrain log." in texts[4] and "Details" in texts[5]
    assert "run Diagnostics again when you’re online" in texts[6]
    assert o[7] == {"status": "ok", "text": "Watchdog no stall restarts"}


def test_logs_and_report_go_through_api_text():
    assert "apiText" not in PAGE
    assert 'api("/api/logs/" + which, {text: true})' in PAGE
    assert 'api("/api/support/report", {text: true})' in PAGE
    js = (_WIZ / "console.js").read_text(encoding="utf-8")
    assert "if (res.ok && opts.text) return text;" in js


def test_restore_redraws_the_list():
    body = PAGE[PAGE.index("async function loadHidden("):PAGE.index("// ---- system")]
    assert "await loadHidden();" in body
    assert "setTimeout" not in body


def test_no_inline_styles():
    for name in ("settings.html", "index.html", "dashboard.html", "graph.html"):
        assert 'style="' not in (_WIZ / name).read_text(encoding="utf-8"), name
