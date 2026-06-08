# tests/test_meeting_pack_routes.py
import json
import threading
from http.server import HTTPServer
from pathlib import Path
import urllib.request

import pytest
from mcpbrain.store import Store
from mcpbrain.control_api import ControlServer


class FakeDaemon:
    def status(self): return {"google_connected": False, "granted_scopes": []}
    def pause(self): pass
    def resume(self): pass
    def apply_config(self, b): pass
    def start_auth(self): pass
    def register(self): return "/tmp/reg.json"


def _server(tmp_path):
    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    srv = ControlServer(FakeDaemon(), str(tmp_path), store=store)
    srv.start()
    return srv, store


def _post(port, token, path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Content-Length": str(len(data)),
                 "Host": "127.0.0.1"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class TestMeetingPackRoutes:
    def test_upsert_creates_pack(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            status, body = _post(srv.port, srv.token, "/api/meeting-packs/upsert", {
                "event_id": "evt1",
                "event_title": "Board Meeting",
                "event_date": "2026-06-06",
                "pack_text": "## Agenda\n- Intro",
                "attendees": ["Alice"],
            })
            assert status == 200
            assert body["ok"] is True
            pack = store.get_meeting_pack("evt1")
            assert pack is not None
            assert pack["event_title"] == "Board Meeting"
        finally:
            srv.stop()

    def test_upsert_missing_event_id_returns_400(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            status, body = _post(srv.port, srv.token, "/api/meeting-packs/upsert", {
                "event_title": "No ID",
            })
            assert status == 400
        finally:
            srv.stop()

    def test_get_pack_returns_pack_text(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            store.upsert_meeting_pack("evt2", "Standup", "2026-06-06", "## Notes\n- item")
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/api/meeting-packs/evt2",
                headers={"Authorization": f"Bearer {srv.token}", "Host": "127.0.0.1"})
            with urllib.request.urlopen(req) as r:
                body = json.loads(r.read())
            assert body["event_id"] == "evt2"
            assert "Notes" in body["pack_text"]
        finally:
            srv.stop()

    def test_get_pack_404_if_missing(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/api/meeting-packs/nope",
                headers={"Authorization": f"Bearer {srv.token}", "Host": "127.0.0.1"})
            try:
                urllib.request.urlopen(req)
                assert False, "expected 404"
            except urllib.error.HTTPError as e:
                assert e.code == 404
        finally:
            srv.stop()


class TestSessionIngestRoute:
    def test_ingest_writes_capture_file(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            status, body = _post(srv.port, srv.token, "/api/session/ingest", {
                "title": "Claude Code session abc123",
                "content": "Worked on dashboard uplift. Added inbox card.",
                "tags": "session,claude_code",
            })
            assert status == 200
            assert body.get("queued") is True
            captures = list((tmp_path / "capture_inbox").glob("*.json"))
            assert len(captures) == 1
            data = json.loads(captures[0].read_text())
            assert data["kind"] == "ingest"
            assert data["title"] == "Claude Code session abc123"
        finally:
            srv.stop()

    def test_ingest_missing_title_returns_400(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            status, body = _post(srv.port, srv.token, "/api/session/ingest", {
                "content": "Some content without title",
            })
            assert status == 400
        finally:
            srv.stop()

    def test_non_dict_body_returns_400(self, tmp_path):
        srv, store = _server(tmp_path)
        try:
            import urllib.request, urllib.error
            data = json.dumps([1, 2, 3]).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{srv.port}/api/session/ingest",
                data=data,
                headers={"Authorization": f"Bearer {srv.token}",
                         "Content-Type": "application/json",
                         "Content-Length": str(len(data)),
                         "Host": "127.0.0.1"},
                method="POST")
            try:
                urllib.request.urlopen(req)
                assert False, "expected 400"
            except urllib.error.HTTPError as e:
                assert e.code == 400
        finally:
            srv.stop()
