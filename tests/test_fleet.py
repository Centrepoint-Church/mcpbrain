"""Unit tests for mcpbrain.fleet — Drive mocked at the drive_service boundary."""
import json
from datetime import datetime, timedelta, timezone

from mcpbrain import fleet


def _beacon(email, *, ver="0.6.0", reported_at=None, probes=None):
    if reported_at is None:
        reported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "user_email": email,
        "mcpbrain_version": ver,
        "reported_at": reported_at,
        "probes": probes or {
            "google": {"state": "ok", "detail": "Connected"},
            "claude": {"state": "ok", "detail": ""},
            "clickup": {"state": "needs_action", "detail": "API key missing"},
            "backup": {"state": "ok", "detail": ""},
            "records": {"state": "not_started", "detail": ""},
            "enrichment": {"state": "ok", "detail": ""},
        },
    }


def test_generate_report_renders_one_row_per_user_with_colour_classes():
    html = fleet.generate_report([_beacon("john@centrepoint.church")])
    assert "john@centrepoint.church" in html
    assert "0.6.0" in html
    # colour-coded probe cells: green=ok, amber=needs_action, grey=not_started
    assert "probe-ok" in html
    assert "probe-needs_action" in html
    assert "probe-not_started" in html
    assert "Last generated" in html


def test_generate_report_flags_stale_rows_over_48h():
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = fleet.generate_report([_beacon("mike@centrepoint.church", reported_at=old)])
    assert "stale" in html  # ⚠️ stale badge present on the row


def test_generate_report_fresh_row_not_stale():
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = fleet.generate_report([_beacon("sarah@centrepoint.church", reported_at=fresh)])
    # the fresh row must not carry the stale badge
    assert 'class="stale"' not in html


class _FakeFiles:
    def __init__(self, store):
        self._store = store

    def list(self, **kw):
        self._store["last_list_q"] = kw.get("q", "")
        return _Exec({"files": self._store.get("listed", [])})

    def create(self, **kw):
        self._store.setdefault("created", []).append(kw)
        return _Exec({"id": "NEWID"})

    def update(self, **kw):
        self._store.setdefault("updated", []).append(kw)
        return _Exec({"id": kw.get("fileId", "")})

    def get_media(self, **kw):
        self._store["get_media"] = kw
        return _Exec(self._store.get("media_bytes", b"{}"))


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeDrive:
    def __init__(self, store):
        self._store = store

    def files(self):
        return _FakeFiles(self._store)


def _read_uploaded_json(store):
    """Decode the MediaInMemoryUpload bytes the fake captured on create/update."""
    from googleapiclient.http import MediaInMemoryUpload  # noqa: F401
    rec = (store.get("created") or store.get("updated"))[0]
    media = rec["media_body"]
    raw = media.getbytes(0, media.size())
    return json.loads(raw.decode())


def test_write_beacon_uploads_user_email_json_with_required_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, probes
    config.write_config(str(tmp_path), {"owner_email": "john@centrepoint.church",
                                        "fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr(probes, "all_connections",
                        lambda home, store=None: {"google": {"state": "ok", "detail": "Connected"}})
    store = {"listed": []}  # file does not yet exist -> create path
    fleet.write_beacon(str(tmp_path), _FakeDrive(store))
    rec = store["created"][0]
    assert rec["body"]["name"] == "john@centrepoint.church.json"
    assert rec["body"]["parents"] == ["FLEET1"]
    payload = _read_uploaded_json(store)
    assert payload["user_email"] == "john@centrepoint.church"
    assert payload["mcpbrain_version"]
    assert payload["reported_at"].endswith("Z")
    assert payload["probes"]["google"]["state"] == "ok"


def test_write_beacon_swallows_drive_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, probes
    config.write_config(str(tmp_path), {"owner_email": "x@y.com",
                                        "fleet": {"folder_id": "F"}})
    monkeypatch.setattr(probes, "all_connections", lambda home, store=None: {})

    class _Boom:
        def files(self):
            raise RuntimeError("drive down")

    # Must not raise — beacon failure never affects the daemon.
    fleet.write_beacon(str(tmp_path), _Boom())


def test_read_org_config_missing_returns_empty(tmp_path):
    store = {"listed": []}  # org-config.json not present
    assert fleet.read_org_config("FLEET1", _FakeDrive(store)) == {}


def test_read_org_config_present_returns_parsed_dict(tmp_path):
    store = {
        "listed": [{"id": "OCID", "name": "org-config.json"}],
        "media_bytes": b'{"cadences": {"lint": 900}}',
    }
    out = fleet.read_org_config("FLEET1", _FakeDrive(store))
    assert out == {"cadences": {"lint": 900}}


def test_read_org_config_download_failure_returns_empty():
    class _Boom:
        def files(self):
            raise RuntimeError("drive down")
    assert fleet.read_org_config("FLEET1", _Boom()) == {}


def test_merge_org_config_applies_allowed_keys_and_drops_blocklisted(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {
        "owner_email": "real@me.com",
        "clickup_api_key": "MYKEY",
        "fleet": {"folder_id": "FLEET1"},
        "backup": {"escrow_key": "SECRET", "shared_drive_id": "ESC"},
    })
    store = {
        "listed": [{"id": "OCID", "name": "org-config.json"}],
        "media_bytes": json.dumps({
            "cadences": {"lint": 900},          # allowed
            "owner_email": "attacker@evil.com",  # blocklisted
            "owner_name": "Evil",                # blocklisted
            "clickup_api_key": "STOLEN",         # blocklisted
            "fleet": {"folder_id": "HIJACK"},    # blocklisted
            "backup": {"shared_drive_id": "HIJACK"},  # blocklisted
            "google_token": {"refresh_token": "x"},   # blocklisted (oauth)
        }).encode(),
    }
    fleet.merge_org_config(str(tmp_path), _FakeDrive(store))
    cfg = config.read_config(str(tmp_path))
    assert cfg["cadences"] == {"lint": 900}        # allowed key applied
    assert cfg["owner_email"] == "real@me.com"     # identity untouched
    assert cfg["clickup_api_key"] == "MYKEY"       # secret untouched
    assert cfg["fleet"]["folder_id"] == "FLEET1"   # fleet binding untouched
    assert cfg["backup"]["escrow_key"] == "SECRET"  # escrow untouched
    assert "google_token" not in cfg               # oauth field dropped
