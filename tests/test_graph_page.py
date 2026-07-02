import urllib.error
import urllib.request
from unittest import mock
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
        code, ctype, body = _raw(f"http://127.0.0.1:{srv.port}/vendor/sigma.min.js")
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
