import urllib.error
import urllib.request
from pathlib import Path

from mcpbrain.control_api import ControlServer


class _Daemon:
    def status(self):
        return {"paused": False}


def _raw(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def test_graph_page_injects_token(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        code, ctype, body = _raw(f"http://127.0.0.1:{srv.port}/graph")
        text = body.decode()
        assert code == 200 and "text/html" in ctype
        assert "__MCPBRAIN_TOKEN__" not in text
        assert srv.token in text
    finally:
        srv.stop()


def test_vendor_serves_js(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        code, ctype, body = _raw(f"http://127.0.0.1:{srv.port}/vendor/force-graph.min.js")
        assert code == 200 and "javascript" in ctype and len(body) > 1000
    finally:
        srv.stop()


def test_vendor_rejects_unknown(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        try:
            _raw(f"http://127.0.0.1:{srv.port}/vendor/../secrets.js")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.stop()


def test_graph_html_has_expected_hooks():
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "graph.html").read_text()
    for marker in ['/vendor/force-graph.min.js', '/vendor/d3.min.js',
                   '/api/graph/canvas', 'id="graph"', 'new ForceGraph',
                   'd3.forceManyBody', 'd3.forceRadial', 'linkDirectionalParticles',
                   'id="f-layout"', 'zoomToFit',
                   'id="legend"', '__MCPBRAIN_TOKEN__',
                   'id="drawer"', '/api/graph/entity/', '/api/graph/search',
                   '/api/graph/merge', 'openDrawer',
                   'id="f-hits"', 'id="f-type"', '/api/graph/ego',
                   'renderTypeChips', 'obsList',
                   '/api/graph/merge/preview', 'showMergePreview', 'setHitActive',
                   'strengthWidth', '/api/graph/types', '/api/graph/suppressed',
                   '/facts', 'readPalette', 'refreshColours']:
        assert marker in html, f"missing: {marker}"


def test_graph_html_js_syntax():
    import re
    import shutil
    import subprocess
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "graph.html").read_text()
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)  # inline only
    assert scripts, "expected an inline <script>"
    if shutil.which("node"):
        for i, js in enumerate(scripts):
            f = Path(f"/tmp/_graph_{i}.js"); f.write_text(js)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr


def test_dashboard_links_to_graph():
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "dashboard.html").read_text()
    assert "/graph" in html                       # the graph link targets the page
    assert "Open the graph" in html
    # the button is no longer a disabled "soon" teaser
    assert 'class="explore" disabled' not in html


# --- Task 8: shell, kinds from the server, URL state, colours ------------------

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

_WIZ = Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard"
GRAPH = (_WIZ / "graph.html").read_text(encoding="utf-8")


def test_graph_page_uses_the_shared_shell():
    for marker in ('href="/static/console.css"', 'src="/static/console.js"',
                   '<meta name="mcpbrain-token" content="__MCPBRAIN_TOKEN__">',
                   '<a class="tab" href="/graph" aria-current="page">Graph</a>',
                   'data-mode-toggle', 'id="health-chip"', 'id="offline"',
                   'C.wireOffline(', 'class="gshell"', 'class="skip"'):
        assert marker in GRAPH, marker
    # One way to call the API: the shared helper (offline banner, token, errors).
    assert "fetch(" not in GRAPH
    assert "const TOKEN" not in GRAPH


def test_graph_page_floor():
    assert "innerHTML" not in GRAPH and "insertAdjacentHTML" not in GRAPH
    assert "outerHTML" not in GRAPH
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", GRAPH), "colour literal"
    assert "oklch(" not in GRAPH and "rgba(" not in GRAPH
    assert 'style="' not in GRAPH
    assert "—" not in GRAPH, "em dash"
    assert "window.confirm" not in GRAPH and "confirm(" not in GRAPH
    assert "prefers-reduced-motion" in GRAPH


def test_kinds_come_from_the_server():
    assert "TYPE_ORDER" not in GRAPH
    assert "/api/graph/types" in GRAPH and "Unclassified" in GRAPH
    m = re.search(r"const OFF_BY_DEFAULT = new Set\(\[([^\]]*)\]\)", GRAPH)
    assert m and set(re.findall(r'"(\w+)"', m.group(1))) == {"document", "thread"}


def test_colours_are_read_again_on_a_mode_or_colour_change():
    assert 'addEventListener("mcpconsole:mode"' in GRAPH
    body = re.search(r"function refreshColours\(\)\{(.*?)\n\}", GRAPH, re.S)
    assert body, "refreshColours missing"
    for call in ("readPalette()", "fg.backgroundColor(", "fg.nodeColor(fg.nodeColor())",
                 "fg.linkColor(fg.linkColor())", "renderLegend("):
        assert call in body.group(1), call
    css = (_WIZ / "console.css").read_text(encoding="utf-8")
    for tok in ("--graph-ground", "--graph-link-idle", "--graph-link-hot", "--graph-ring"):
        assert tok + ":" in css, tok


def _url_state_js():
    m = re.search(r"/\* url-state:start \*/(.*?)/\* url-state:end \*/", GRAPH, re.S)
    assert m, "url-state block missing"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_url_state_round_trips(tmp_path):
    js = _url_state_js() + r"""
const out = {};
for (const q of ["?type=person", "?type=document&type=thread", "?show=unlinked",
                 "?entity=p-dana", "?view=circles", "?since=24h", "?entity=p-dana&rel=works_at",
                 "", "?since=bogus"]) {
  const s = readUrlState(q);
  out[q] = {state: s, back: writeUrlState(s), canvas: canvasQuery(s, 7)};
}
console.log(JSON.stringify(out));
"""
    p = tmp_path / "u.js"; p.write_text(js)
    r = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["?type=person"]["state"]["types"] == ["person"]
    assert o["?type=person"]["back"] == "?type=person"
    assert o["?type=document&type=thread"]["state"]["types"] == ["document", "thread"]
    assert "unlinked=1" in o["?show=unlinked"]["canvas"]
    assert o["?show=unlinked"]["back"] == "?show=unlinked"
    assert o["?entity=p-dana"]["state"]["entity"] == "p-dana"
    assert o["?view=circles"]["state"]["view"] == "circles"
    assert o["?since=24h"]["state"]["sinceHours"] == 24
    assert "since_hours=24" in o["?since=24h"]["canvas"]
    assert "min_conn=0" in o["?since=24h"]["canvas"]
    assert o["?entity=p-dana&rel=works_at"]["state"]["rel"] == "works_at"
    assert o["?entity=p-dana&rel=works_at"]["back"] == "?entity=p-dana&rel=works_at"
    assert o[""]["back"] == "" and "min_conn=7" in o[""]["canvas"]
    assert o["?since=bogus"]["state"]["sinceHours"] == 0


def test_unnamed_relations_travel_as_the_server_marker():
    from mcpbrain import graph_view
    assert f'const EMPTY_REL = "{graph_view.EMPTY_RELATION}"' in GRAPH
    assert "x.relation || EMPTY_REL" in GRAPH and '"related"' not in GRAPH


def test_unlinked_note_says_which_kinds_it_counts():
    assert "This counts the kinds switched on under Kinds" in GRAPH
    assert "out.hidden" in GRAPH   # the header total leaves hidden entities out


def _lch(css, name):
    m = re.search(rf"{re.escape(name)}: oklch\(([\d.]+)% ([\d.]+) ([\d.]+)\)", css)
    assert m, name
    return tuple(float(x) for x in m.groups())


def test_graph_colours_stay_off_the_meaning_colours():
    css = (_WIZ / "console.css").read_text(encoding="utf-8")
    for mode in ("lt", "dk"):
        for i in range(1, 7):
            hue = _lch(css, f"--{mode}-circle-{i}")[2]
            # measured teal 205, status red 30, accent 355
            for meaning in (205, 30, 355):
                d = min(abs(hue - meaning), 360 - abs(hue - meaning))
                assert d >= 25 or (meaning == 30 and hue == 20), (mode, i, hue, meaning)
        ev, mt = _lch(css, f"--{mode}-type-event"), _lch(css, f"--{mode}-type-meeting")
        assert abs(ev[0] - mt[0]) >= 15, (mode, ev, mt)


def _graph_colours(css):
    """Every graph kind and circle colour, both modes: name -> (L, C, H)."""
    out = {}
    for m in re.finditer(r"(--(?:lt|dk)-(?:type|circle)-[\w-]+): oklch\(([\d.]+)% ([\d.]+) ([\d.]+)\)", css):
        out[m.group(1)] = tuple(float(x) for x in m.groups()[1:])
    return out


def test_graph_colours_avoid_violet_yellow_and_brown():
    css = (_WIZ / "console.css").read_text(encoding="utf-8")
    cols = _graph_colours(css)
    assert len(cols) >= 2 * (10 + 6), sorted(cols)   # kinds and six circles, both modes
    for name, (L, C, H) in cols.items():
        if C <= 0.02:
            continue   # a neutral grey: its hue doesn't show
        assert not 265 <= H <= 330, f"{name} is violet"
        assert not 40 <= H <= 110, f"{name} is yellow, amber or orange"
        assert not (20 <= H <= 60 and C < 0.08 and L < 60), f"{name} reads brown"


def test_circles_close_in_hue_differ_in_lightness():
    css = (_WIZ / "console.css").read_text(encoding="utf-8")
    for mode in ("lt", "dk"):
        circles = [_lch(css, f"--{mode}-circle-{i}") for i in range(1, 7)]
        for i, a in enumerate(circles):
            for b in circles[i + 1:]:
                dh = min(abs(a[2] - b[2]), 360 - abs(a[2] - b[2]))
                if dh < 30:
                    assert abs(a[0] - b[0]) >= 12, (mode, a, b)


# --- Follow-up Task 2: unlinked entities as a reviewable list --------------------

def _block(name):
    m = re.search(rf"/\* {name}:start \*/(.*?)/\* {name}:end \*/", GRAPH, re.S)
    assert m, f"{name} block missing"
    return m.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_unlinked_list_and_map_views_round_trip_in_the_address(tmp_path):
    js = _url_state_js() + r"""
const out = {};
for (const q of ["?show=unlinked", "?show=unlinked&as=map", "?show=unlinked&page=3&q=Dana",
                 "?show=unlinked&page=0", "?as=map", "?page=4"]) {
  const s = readUrlState(q);
  out[q] = {state: s, back: writeUrlState(s), list: unlinkedQuery({...s, types: ["person", "org"]}, 50),
            canvas: canvasQuery(s, 7), listed: listView(s)};
}
console.log(JSON.stringify(out));
"""
    p = tmp_path / "u.js"; p.write_text(js)
    r = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["?show=unlinked"]["listed"] is True
    assert o["?show=unlinked"]["back"] == "?show=unlinked"
    assert o["?show=unlinked"]["list"] == "page=1&page_size=50&type=person&type=org"
    # "Show on the map" keeps the filter and draws the capped canvas.
    assert o["?show=unlinked&as=map"]["listed"] is False
    assert o["?show=unlinked&as=map"]["back"] == "?show=unlinked&as=map"
    assert "unlinked=1" in o["?show=unlinked&as=map"]["canvas"]
    s = o["?show=unlinked&page=3&q=Dana"]
    assert s["state"]["page"] == 3 and s["state"]["q"] == "Dana"
    assert s["back"] == "?show=unlinked&page=3&q=Dana"
    assert s["list"] == "page=3&page_size=50&q=Dana&type=person&type=org"
    assert o["?show=unlinked&page=0"]["state"]["page"] == 1
    # Outside the unlinked view the list's own state means nothing.
    assert o["?as=map"]["back"] == "" and o["?page=4"]["back"] == ""
    assert o["?as=map"]["listed"] is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_pager_words(tmp_path):
    js = ("const fmt = (n) => n.toLocaleString(\"en-AU\");\n"
          "const plural = (n, one, many) => fmt(n) + \" \" + (n === 1 ? one : (many || one + \"s\"));\n"
          + _block("unlinked-words") + r"""
console.log(JSON.stringify({
  mid: pagerWords(2, 9205, 50), one: pagerWords(1, 1, 50), empty: pagerWords(1, 0, 50),
  pages: pageCount(9205, 50), none: pageCount(0, 50), back: settlePage(4, 0, 151, 50),
  stay: settlePage(2, 3, 151, 50), first: settlePage(1, 0, 0, 50),
  plain: emptyWords("", false), named: emptyWords("Dana", false), kinds: emptyWords("", true)
}));
""")
    p = tmp_path / "w.js"; p.write_text(js)
    r = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["mid"] == "Page 2 of 185, 9,205 entities"
    assert o["one"] == "Page 1 of 1, 1 entity"
    assert o["pages"] == 185 and o["none"] == 1
    # A page emptied by a hide or a merge steps back; page 1 stays put.
    assert o["back"] == 3 and o["stay"] == 2 and o["first"] == 1
    assert o["plain"] == "No unlinked entities. Everything has at least one link."
    assert "Dana" in o["named"] and o["named"] != o["plain"]
    assert o["kinds"] != o["plain"]
    for v in o.values():
        if isinstance(v, str):
            assert "—" not in v


def test_unlinked_list_markup():
    for marker in ('id="ulist"', 'id="ul-body"', 'id="ul-q"', 'id="ul-prev"', 'id="ul-next"',
                   'id="ul-page"', 'id="ul-map"', 'id="ul-said"', 'id="view-list"',
                   "/api/graph/unlinked?"):
        assert marker in GRAPH, marker
    # Columns: name, kind, organisation, last seen, actions.
    head = re.search(r'<table class="table table--action ulist__table".*?</thead>', GRAPH, re.S)
    assert head, "list table missing"
    for col in ("Name", "Kind", "Organisation", "Last seen"):
        assert f">{col}<" in head.group(0), col


def test_unlinked_rows_use_the_existing_flows():
    rows = _block("unlinked-rows")
    # Open: the entity drawer.
    assert "openEntity(r.id)" in rows
    # Merge with: the drawer's merge search, preview and merge.
    assert "Merge with…" in rows and "renderMerge()" in rows
    # Hide: the existing suppress route, with Undo in place.
    assert '"/api/graph/entity/" + encodeURIComponent(r.id), {method: "DELETE"}' in rows
    assert "offerUndo(" in rows
    # Kinds in words, last seen measured.
    assert "kindWord(r.type)" in rows and 'class: "measured"' in rows
    # No new write endpoints for the list.
    assert "/api/graph/unlinked/" not in GRAPH


def test_merge_and_hide_refresh_the_list_page():
    js = GRAPH
    assert "listView(url)" in js
    # Ticking "Only entities with no links" opens the list, not the map.
    m = re.search(r'\$\("f-unlinked"\)\.addEventListener\("change", \(e\) => \{(.*?)\}\);', js, re.S)
    assert m and "url.asMap = false" in m.group(1)


def test_dashboard_review_link_opens_the_list():
    dash = (_WIZ / "dashboard.html").read_text(encoding="utf-8")
    assert '<a class="link" href="/graph?show=unlinked">Review unlinked entities</a>' in dash
