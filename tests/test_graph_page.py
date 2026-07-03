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
                   '/api/graph/merge', 'openDrawer']:
        assert marker in html, f"missing: {marker}"


def test_graph_html_js_syntax():
    import re
    import shutil
    import subprocess
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "graph.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)  # inline only (no src)
    assert scripts, "expected an inline <script>"
    if shutil.which("node"):
        for i, js in enumerate(scripts):
            f = Path(f"/tmp/_graph_{i}.js"); f.write_text(js)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr


def test_dashboard_links_to_graph():
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "dashboard.html").read_text()
    assert "/graph" in html                       # explore button targets the page
    assert "Explore graph" in html
    # the button is no longer a disabled "soon" teaser
    assert 'class="explore" disabled' not in html
