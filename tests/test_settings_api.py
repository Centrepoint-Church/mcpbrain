"""The allowlisted /api/settings endpoint: deep-merges one level, validates
types, refuses unknown keys without touching config."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcpbrain import config, settings_api


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({
        "owner_name": "Dana",
        "anarlog": {"enabled": False, "db_path": "/tmp/acme.db"},
        "ingest": {"generated_drive_folders": ["Acme/Reports"],
                   "generated_gmail_subject_prefixes": ["[Acme digest]"]},
        "gardener_auto_apply": False,
    }, indent=2))
    return str(h)


def _raw(home) -> bytes:
    return (Path(home) / "config.json").read_bytes()


def test_nested_post_keeps_sibling_keys(home):
    out = settings_api.apply_settings(home, {"ingest": {"exclude_senders": ["news@example.org"]}})
    assert out["ok"] is True
    ingest = config.read_config(home)["ingest"]
    assert ingest["exclude_senders"] == ["news@example.org"]
    assert ingest["generated_drive_folders"] == ["Acme/Reports"]
    assert ingest["generated_gmail_subject_prefixes"] == ["[Acme digest]"]


def test_anarlog_enable_keeps_db_path(home):
    settings_api.apply_settings(home, {"anarlog": {"enabled": True}})
    assert config.read_config(home)["anarlog"] == {"enabled": True, "db_path": "/tmp/acme.db"}


def test_auto_changes_merge(home):
    settings_api.apply_settings(home, {"auto_changes": {"profiles": "ask"}})
    settings_api.apply_settings(home, {"auto_changes": {"tidy": "off"}})
    assert config.read_config(home)["auto_changes"] == {"profiles": "ask", "tidy": "off"}
    assert config.auto_change_mode(home, "profiles") == "ask"


@pytest.mark.parametrize("body, key", [
    ({"enrich_mode": "spool"}, "enrich_mode"),
    ({"start_at_login": True}, "start_at_login"),
    ({"ingest": {"bogus": []}}, "ingest.bogus"),
    ({"auto_changes": {"nonsense": "apply"}}, "auto_changes.nonsense"),
    ({"sources": {"slack": True}}, "sources.slack"),
    ({"anarlog": {"db_path": "/x"}}, "anarlog.db_path"),
    ({"owner_aliases": ["dee"], "api_key": "x"}, "api_key"),
])
def test_unknown_key_refused_and_config_byte_identical(home, body, key):
    before = _raw(home)
    out = settings_api.apply_settings(home, body)
    assert out == {"ok": False, "error": f"unknown key: {key}"}
    assert _raw(home) == before


@pytest.mark.parametrize("body", [
    {"review_max_apply_per_run": "50"},
    {"review_max_apply_per_run": 0},
    {"review_max_apply_per_run": 501},
    {"review_max_apply_per_run": True},
    {"sources": {"gmail": "yes"}},
    {"sources": "off"},
    {"anarlog": {"enabled": 1}},
    {"ingest": {"exclude_labels": "Promotions"}},
    {"ingest": {"exclude_labels": ["ok", 3]}},
    {"owner_aliases": "dee"},
    {"auto_changes": {"profiles": "sometimes"}},
    {"auto_changes": {"draft_style": "ask"}},
    {"auto_changes": {"tidy": "advise"}},
])
def test_wrong_type_refused(home, body):
    before = _raw(home)
    out = settings_api.apply_settings(home, body)
    assert out["ok"] is False and out["error"]
    assert _raw(home) == before


def test_get_returns_every_allowlisted_key_with_effective_value(home):
    got = settings_api.get_settings(home)
    assert got["auto_changes"] == {
        "profiles": "apply", "circles": "apply", "reference": "ask", "voice": "apply",
        "consolidation": "apply", "tidy": "apply", "draft_style": "advise"}
    assert got["review_max_apply_per_run"] == 50
    assert got["sources"] == {"gmail": True, "drive": True, "calendar": True}
    assert got["anarlog"] == {"enabled": False}
    assert got["ingest"] == {
        "generated_gmail_subject_prefixes": ["[Acme digest]"],
        "generated_drive_folders": ["Acme/Reports"],
        "exclude_senders": [], "exclude_labels": []}
    assert got["owner_aliases"] == []
    assert "start_at_login" not in got
    assert set(got) == {"auto_changes", "review_max_apply_per_run", "sources", "anarlog",
                        "ingest", "owner_aliases"}


def test_post_returns_the_new_settings(home):
    out = settings_api.apply_settings(home, {"review_max_apply_per_run": 20,
                                             "sources": {"drive": False},
                                             "owner_aliases": ["dee", "d.okafor"]})
    assert out["ok"] is True
    assert out["settings"]["review_max_apply_per_run"] == 20
    assert out["settings"]["sources"] == {"gmail": True, "drive": False, "calendar": True}
    assert config.read_config(home)["owner_aliases"] == ["dee", "d.okafor"]
    assert config.read_config(home)["owner_name"] == "Dana"


# --- routes ------------------------------------------------------------------------

class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}

    def apply_config(self, body):  # must NOT be called by /api/settings
        raise AssertionError("settings must not go through apply_config")


def _req(srv, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {srv.token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_settings_routes(home):
    from mcpbrain.control_api import ControlServer
    srv = ControlServer(FakeDaemon(), home=home)
    srv.start()
    try:
        code, out = _req(srv, "GET", "/api/settings")
        assert code == 200 and out == settings_api.get_settings(home)
        code, out = _req(srv, "POST", "/api/settings",
                         {"ingest": {"exclude_senders": ["news@example.org"]}})
        assert code == 200 and out["ok"] is True
        assert out["settings"]["ingest"]["generated_drive_folders"] == ["Acme/Reports"]
        before = _raw(home)
        code, out = _req(srv, "POST", "/api/settings", {"x": 1})
        assert (code, out) == (400, {"error": "unknown key: x"})
        assert _raw(home) == before
        code, out = _req(srv, "POST", "/api/settings", {"review_max_apply_per_run": "lots"})
        assert code == 400 and out["error"]
    finally:
        srv.stop()
