"""Unit tests for mcpbrain.fleet — Drive mocked at the drive_service boundary."""
import json
from datetime import datetime, timedelta, timezone

from mcpbrain import fleet


def _beacon(email, *, ver="0.6.0", reported_at=None, daemon_heartbeat=None, probes=None):
    if reported_at is None:
        reported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # A healthy install's daemon heartbeat tracks the beacon time; default to it.
    if daemon_heartbeat is None:
        daemon_heartbeat = reported_at
    return {
        "user_email": email,
        "mcpbrain_version": ver,
        "reported_at": reported_at,
        "daemon_heartbeat": daemon_heartbeat,
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


def test_write_beacon_includes_daemon_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, probes
    config.write_config(str(tmp_path), {"owner_email": "j@x.com",
                                        "fleet": {"folder_id": "F"}})
    monkeypatch.setattr(probes, "all_connections", lambda home, store=None: {})
    (tmp_path / "daemon_heartbeat.json").write_text(
        json.dumps({"last_cycle": "2026-06-16T05:00:00Z"}))
    store = {"listed": []}
    fleet.write_beacon(str(tmp_path), _FakeDrive(store))
    payload = _read_uploaded_json(store)
    assert payload["daemon_heartbeat"] == "2026-06-16T05:00:00Z"


def test_write_beacon_daemon_heartbeat_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, probes
    config.write_config(str(tmp_path), {"owner_email": "j@x.com",
                                        "fleet": {"folder_id": "F"}})
    monkeypatch.setattr(probes, "all_connections", lambda home, store=None: {})
    fleet.write_beacon(str(tmp_path), _FakeDrive({"listed": []}))


def test_generate_report_flags_dead_daemon_even_when_beacon_fresh():
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
    b = _beacon("zoe@x.com", reported_at=fresh)
    b["daemon_heartbeat"] = old  # beacon job alive, daemon dead 3 days
    html = fleet.generate_report([b])
    assert "<th>Daemon</th>" in html
    # the daemon cell must carry the stale badge even though Last seen is fresh
    assert html.count("stale") >= 1


def test_daemon_write_heartbeat_roundtrips(tmp_path):
    from mcpbrain import daemon
    daemon.write_daemon_heartbeat(str(tmp_path))
    data = json.loads((tmp_path / "daemon_heartbeat.json").read_text())
    assert data["last_cycle"].endswith("Z")


class _PagedFiles(_FakeFiles):
    """`list` returns two pages; `get_media` serves bytes from a per-id map."""
    def list(self, **kw):
        page_token = kw.get("pageToken")
        pages = self._store["pages"]
        self._store.setdefault("list_calls", []).append(page_token)
        if page_token is None:
            return _Exec({"files": pages[0], "nextPageToken": "PAGE2"})
        return _Exec({"files": pages[1]})  # no nextPageToken -> loop ends

    def get_media(self, **kw):
        return _Exec(self._store["media_by_id"][kw["fileId"]])


class _PagedDrive(_FakeDrive):
    def files(self):
        return _PagedFiles(self._store)


def test_list_beacon_files_paginates_and_dedupes_by_newest(tmp_path):
    # Page 1: alice + a stale duplicate of bob. Page 2: the fresh bob + org-config.
    store = {
        "pages": [
            [{"id": "A", "name": "alice@x.com.json", "modifiedTime": "2026-06-16T01:00:00Z"},
             {"id": "B_OLD", "name": "bob@x.com.json", "modifiedTime": "2026-06-16T01:00:00Z"}],
            [{"id": "B_NEW", "name": "bob@x.com.json", "modifiedTime": "2026-06-16T05:00:00Z"},
             {"id": "OC", "name": "org-config.json", "modifiedTime": "2026-06-16T05:00:00Z"}],
        ],
    }
    files = fleet._list_beacon_files(_PagedDrive(store), "FLEET1")
    # Both pages consulted (second call carried the page token).
    assert store["list_calls"] == [None, "PAGE2"]
    by_name = {f["name"]: f["id"] for f in files}
    assert by_name == {"alice@x.com.json": "A", "bob@x.com.json": "B_NEW"}  # newest bob, no org-config


def test_write_report_dedupes_duplicate_beacons_across_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    store = {
        "pages": [
            [{"id": "B_OLD", "name": "bob@x.com.json", "modifiedTime": "2026-06-16T01:00:00Z"}],
            [{"id": "B_NEW", "name": "bob@x.com.json", "modifiedTime": "2026-06-16T05:00:00Z"}],
        ],
        "media_by_id": {
            "B_OLD": json.dumps(_beacon("bob@x.com")).encode(),
            "B_NEW": json.dumps(_beacon("bob@x.com")).encode(),
        },
    }
    fleet.write_report(str(tmp_path), _PagedDrive(store))
    # status.html uploaded once; bob appears a single time (deduped to newest).
    rec = (store.get("created") or store.get("updated"))[0]
    html = rec["media_body"].getbytes(0, rec["media_body"].size()).decode()
    assert html.count("bob@x.com") == 1


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


def test_merge_org_config_stages_allowlisted_into_overlay_and_drops_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {
        "owner_email": "real@me.com",
        "clickup_api_key": "MYKEY",
        "clickup_list_id": "MYLIST",
        "records_dir": "/home/me/records",
        "enrich_mode": "auto",
        "cadences": {"lint": 100},                 # user's own cadence
        "fleet": {"folder_id": "FLEET1"},
        "backup": {"escrow_key": "SECRET", "shared_drive_id": "ESC"},
    })
    store = {
        "listed": [{"id": "OCID", "name": "org-config.json"}],
        "media_bytes": json.dumps({
            "cadences": {"lint": 900},                # allowlisted
            "owner_email": "attacker@evil.com",       # denied (identity)
            "clickup_api_key": "STOLEN",              # denied (secret)
            "clickup_list_id": "HIJACK_LIST",         # denied (misdirection)
            "records_dir": "/tmp/evil",               # denied (write-anywhere)
            "enrich_mode": "off",                     # denied
            "fleet": {"folder_id": "HIJACK"},         # denied (binding)
            "backup": {"shared_drive_id": "HIJACK"},  # denied (binding)
            "google_token": {"refresh_token": "x"},   # denied (oauth)
        }).encode(),
    }
    out = fleet.merge_org_config(str(tmp_path), _FakeDrive(store))
    assert out == {"cadences": {"lint": 900}}          # only allowlisted returned
    cfg = config.read_config(str(tmp_path))
    # Allowlisted org value is staged into the managed overlay block, NOT the
    # user's own top-level keys.
    assert cfg["org_config"] == {"cadences": {"lint": 900}}
    assert cfg["cadences"] == {"lint": 100}            # user's own cadence untouched
    # Everything dangerous is left exactly as the user set it.
    assert cfg["owner_email"] == "real@me.com"
    assert cfg["clickup_api_key"] == "MYKEY"
    assert cfg["clickup_list_id"] == "MYLIST"
    assert cfg["records_dir"] == "/home/me/records"
    assert cfg["enrich_mode"] == "auto"
    assert cfg["fleet"]["folder_id"] == "FLEET1"
    assert cfg["backup"]["escrow_key"] == "SECRET"
    assert "google_token" not in cfg


def test_merge_org_config_overlay_reverts_when_org_config_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    # First startup: org-config sets a cadence.
    store = {
        "listed": [{"id": "OCID", "name": "org-config.json"}],
        "media_bytes": json.dumps({"cadences": {"lint": 900}}).encode(),
    }
    fleet.merge_org_config(str(tmp_path), _FakeDrive(store))
    assert config.read_config(str(tmp_path))["org_config"] == {"cadences": {"lint": 900}}
    # Later startup: org-config.json removed → overlay reverts to empty.
    empty = {"listed": [], "media_bytes": b"{}"}
    fleet.merge_org_config(str(tmp_path), _FakeDrive(empty))
    assert config.read_config(str(tmp_path))["org_config"] == {}


def test_org_cadences_overlay_wins_in_daemon_cadence_read(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, daemon
    config.write_config(str(tmp_path), {
        "cadences": {"lint_interval_s": 100},
        "org_config": {"cadences": {"lint_interval_s": 900}},
    })
    cadences = daemon._cadences_from_config(str(tmp_path))
    assert cadences["lint_interval_s"] == 900.0  # org overlay wins over the user's 100


class _FilesMulti(_FakeFiles):
    """List returns beacon files; get_media returns per-file bytes from a map."""
    def list(self, **kw):
        import re
        q = kw.get("q", "")
        # Simple parsing: if "name='X'" is in the query, filter by that
        match = re.search(r"name='([^']*)'", q)
        if match:
            target_name = match.group(1)
            files = [f for f in self._store.get("listed", []) if f.get("name") == target_name]
        else:
            files = self._store.get("listed", [])
        self._store["last_list_q"] = q
        return _Exec({"files": files})

    def get_media(self, **kw):
        fid = kw.get("fileId")
        return _Exec(self._store["media_by_id"][fid])


class _DriveMulti(_FakeDrive):
    def files(self):
        return _FilesMulti(self._store)


def test_write_report_uploads_status_html_excluding_org_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    store = {
        "listed": [
            {"id": "A", "name": "john@centrepoint.church.json"},
            {"id": "B", "name": "org-config.json"},       # must be excluded
            {"id": "C", "name": "bad.json"},               # malformed -> skipped
        ],
        "media_by_id": {
            "A": json.dumps(_beacon("john@centrepoint.church")).encode(),
            "C": b"{not json",
        },
    }
    fleet.write_report(str(tmp_path), _DriveMulti(store))
    rec = (store.get("created") or store.get("updated"))[0]
    body = rec.get("body", {})
    assert body.get("name") == "status.html"
    # the uploaded HTML contains the valid beacon's user row
    media = rec["media_body"]
    html = media.getbytes(0, media.size()).decode()
    assert "john@centrepoint.church" in html
    assert "org-config" not in html  # org-config.json never parsed as a beacon


def test_write_report_empty_folder_prints_no_beacons(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    store = {"listed": []}
    fleet.write_report(str(tmp_path), _DriveMulti(store))
    assert "No beacons found" in capsys.readouterr().out
    assert "created" not in store and "updated" not in store  # nothing uploaded
