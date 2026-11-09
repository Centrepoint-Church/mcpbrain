"""The console's shared shell: /static/ assets, vendored fonts, packaging,
and the Overview page's floor (no network, no colour literals, every
endpoint it acts through)."""
import http.client
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

from mcpbrain.control_api import ControlServer

ROOT = Path(__file__).resolve().parents[1]
WIZARD = ROOT / "mcpbrain" / "wizard"
DASH = (WIZARD / "dashboard.html").read_text(encoding="utf-8")


class _Daemon:
    def status(self):
        return {"paused": False}


@pytest.fixture
def srv(tmp_path):
    s = ControlServer(_Daemon(), str(tmp_path), store=None)
    s.start()
    try:
        yield s
    finally:
        s.stop()


def _raw(srv, path):
    """GET a path exactly as written (urllib would not, but a browser or curl
    can send `..` segments)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.getheader("Content-Type", ""), r.read()
    finally:
        c.close()


# ---- /static/ route ---------------------------------------------------------

@pytest.mark.parametrize("path, mime", [
    ("/static/console.css", "text/css"),
    ("/static/console.js", "text/javascript"),
    ("/static/fonts/HostGrotesk-400.woff2", "font/woff2"),
    ("/static/fonts/AtkinsonHyperlegibleMono-500.woff2", "font/woff2"),
    ("/static/fonts/OFL.txt", "text/plain"),
])
def test_static_serves_with_mime(srv, path, mime):
    code, ctype, body = _raw(srv, path)
    assert code == 200
    assert ctype.split(";")[0] == mime
    assert len(body) > 100


@pytest.mark.parametrize("path", [
    "/static/../store.py",
    "/static/x.py",
    "/static/../control_api.py",
    "/static/fonts/../console.css",
    "/static/fonts/../../store.py",
    "/static/nope.css",
    "/static/index.html",
    "/static/",
])
def test_static_refuses_anything_else(srv, path):
    code, _, _ = _raw(srv, path)
    assert code == 404


def test_static_is_served_without_a_token_and_never_injects_it(srv):
    code, _, body = _raw(srv, "/static/console.js")
    assert code == 200
    assert srv.token.encode() not in body


# ---- fonts and licence ------------------------------------------------------

def test_fonts_vendored_with_licence():
    fonts = WIZARD / "fonts"
    for name in ("HostGrotesk-400", "HostGrotesk-500",
                 "AtkinsonHyperlegibleMono-400", "AtkinsonHyperlegibleMono-500"):
        p = fonts / f"{name}.woff2"
        assert p.is_file() and p.read_bytes()[:4] == b"wOF2", name
    ofl = (fonts / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in ofl
    assert "Host Grotesk Project Authors" in ofl
    assert "Atkinson Hyperlegible Mono Project Authors" in ofl
    assert "fonts.gstatic.com" in ofl  # where each file came from


def test_stylesheet_carries_hallmark_stamp_and_no_amber():
    css = (WIZARD / "console.css").read_text(encoding="utf-8")
    assert css.startswith("/* Hallmark")
    assert "colourway: neutral" in css and "modes: light+dark" in css
    # No yellow, amber, orange or brown anywhere: every oklch hue is a
    # variable or a hue outside 35..115.
    for m in re.finditer(r"oklch\(\s*[\d.]+%\s+[\d.]+\s+([\d.]+)\s*\)", css):
        hue = float(m.group(1))
        assert not 35 <= hue <= 115, m.group(0)
    assert "fonts.googleapis" not in css and "http" not in css.replace(
        "https://openfontlicense.org", "")


# ---- packaging --------------------------------------------------------------

def test_package_data_declares_console_assets():
    import tomllib
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pd = cfg["tool"]["setuptools"]["package-data"]
    assert {"*.html", "*.css", "*.js"} <= set(pd["mcpbrain.wizard"])
    assert "wizard/fonts/*" in pd["mcpbrain"]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_built_wheel_ships_console_assets():
    with tempfile.TemporaryDirectory() as out:
        r = subprocess.run(["uv", "build", "--wheel", "--out-dir", out, str(ROOT)],
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        wheel = next(Path(out).glob("*.whl"))
        names = set(zipfile.ZipFile(wheel).namelist())
    for need in ("mcpbrain/wizard/console.css", "mcpbrain/wizard/console.js",
                 "mcpbrain/wizard/fonts/OFL.txt",
                 "mcpbrain/wizard/fonts/HostGrotesk-400.woff2"):
        assert need in names, need


# ---- the Overview page ------------------------------------------------------

def test_page_makes_no_network_calls_and_has_no_colour_literals():
    assert "fonts.googleapis" not in DASH
    # Nothing the page loads comes from the network: no remote script, link,
    # img or url(). The one remote address allowed is a navigation anchor to
    # Drive ("Open in Drive" for a skipped file), opened in a new tab.
    assert not re.search(r"<(script|img)[^>]*\bsrc=[\"']?(https?:)?//", DASH, re.I)
    assert not re.search(r"<link[^>]*\bhref=[\"']?(https?:)?//", DASH, re.I)
    assert not re.search(r"url\(\s*[\"']?(https?:)?//", DASH, re.I)
    for m in re.finditer(r"https?://[^\s\"'`]*", DASH):
        assert m.group(0).startswith("https://drive.google.com/"), m.group(0)
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", DASH)
    assert "oklch(" not in DASH
    assert "/static/console.css" in DASH and "/static/console.js" in DASH


def test_page_never_uses_innerhtml():
    """Every string on the page comes from the server (summaries are model or
    mail text): build the DOM with textContent only."""
    assert "innerHTML" not in DASH
    assert "insertAdjacentHTML" not in DASH
    assert "outerHTML" not in DASH
    js = (WIZARD / "console.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js


def _inline_scripts(html):
    return [m.group(1) for m in re.finditer(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scripts_parse(tmp_path):
    scripts = _inline_scripts(DASH) + [(WIZARD / "console.js").read_text(encoding="utf-8")]
    assert len(scripts) >= 2
    for i, js in enumerate(scripts):
        f = tmp_path / f"s{i}.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("endpoint", [
    "/api/dashboard/stats", "/api/changes?hours=", "/api/changes/", "/undo",
    "/api/changes/added", "/api/circles/", "/api/pending", "/api/findings",
    "/api/corrections/pending", "/api/corrections/", "/api/sync-now",
    "/api/pause", "/api/resume", "/api/backup/run", "/api/chunks/cold",
    "/api/chunks/waiting", "/api/chunks/reinclude", "/api/chunks/prioritise",
    "/api/chunks/reflow-gave-up", "/api/chunks/reflow-retry", "/api/settings",
])
def test_page_acts_through_each_endpoint(endpoint):
    assert endpoint in DASH


def test_page_layout_a_in_order():
    order = [DASH.index(m) for m in (
        'id="health-chip"', 'data-mode-toggle', 'id="reading"', 'id="needs"',
        'id="changes"', 'id="g-circles"', 'id="sources"', 'id="system"')]
    assert order == sorted(order)


def test_theme_keys_and_api_contract():
    js = (WIZARD / "console.js").read_text(encoding="utf-8")
    for key in ("mcpbrain-mode", "mcpbrain-accent", "window.mcpConsole",
                "applyTheme", "relTime", "api"):
        assert key in js


def test_open_in_drive_is_a_new_tab_anchor():
    assert 'const DRIVE_OPEN = "https://drive.google.com/open?id="' in DASH
    assert 'target: "_blank", rel: "noopener"' in DASH


def test_monospace_only_for_measured_values():
    """Placeholders and prose stay in the body face; .measured is for times,
    counts, versions and ids."""
    assert not re.search(r'class="[^"]*\bmeasured\b[^"]*"[^>]*>\s*(not yet|none yet|never|unknown)',
                         DASH)
    css = (WIZARD / "console.css").read_text(encoding="utf-8")
    rule = re.search(r"details\.more > summary::after \{[^}]*\}", css).group(0)
    assert "font-outlier" not in rule


def test_stale_cell_is_status_red():
    """.measured sets a colour, so the stale tone needs its own measured
    modifier declared after it (a generic .said--bad earlier would lose)."""
    css = (WIZARD / "console.css").read_text(encoding="utf-8")
    assert css.index(".measured--bad {") > css.index(".measured {")
    assert "var(--color-bad)" in re.search(r"\.measured--bad \{[^}]*\}", css).group(0)
    assert "measured--bad" in DASH


def test_sources_use_the_enabled_flag_and_keep_row_results():
    assert "s.enabled" in DASH
    assert "sourcesActedAt" in DASH and "renderSources(force)" in DASH


def test_restoring_is_said_in_words():
    js = (WIZARD / "console.js").read_text(encoding="utf-8")
    assert "mcpbrain is restoring a backup, try again in a moment." in js


SHARED = ("el", "fill", "said", "errText", "act", "reenable", "plural", "monthYear",
          "renderChip", "wireOffline", "orgRow", "collectOrgs", "populateTimezones")


def test_page_helpers_are_defined_once_in_console_js():
    js = (WIZARD / "console.js").read_text(encoding="utf-8")
    for name in SHARED:
        assert len(re.findall(rf"function {name}\(", js)) == 1, name
        assert f"{name}: {name}" in js, name
    for page in ("dashboard.html", "settings.html", "index.html", "graph.html"):
        html = (WIZARD / page).read_text(encoding="utf-8")
        for name in SHARED:
            assert not re.search(rf"function {name}\(", html), (page, name)
        assert "const CHIP" not in html and "apiText" not in html, page
        assert 'addEventListener("mcpconsole:offline"' not in html, page


def test_whatchanged_words_a_restore():
    assert 'entity_unsuppressed: ["entity restored to the graph"' in DASH
    ents = DASH[DASH.index("const ENTITY_TYPES"):].split(");", 1)[0]
    assert '"entity_unsuppressed"' in ents
