import json
import urllib.request

from mcpbrain import daemon as daemon_mod
from mcpbrain.control_api import ControlServer
from mcpbrain.daemon import Daemon
from mcpbrain.store import Store


class FakeEmbedder:
    dim = 4

    def embed(self, texts):
        return [[0.0] * self.dim for _ in texts]


class FakeDaemon:
    def __init__(self):
        self.paused = False
        self.cfg = None
        self.registered = False
        self.auth = False

    def status(self):
        return {"paused": self.paused, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": self.cfg is not None}

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def apply_config(self, body):
        self.cfg = body

    def register(self):
        self.registered = True
        return "/tmp/claude_desktop_config.json"

    def start_auth(self):
        self.auth = True


def _post(url, token, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req)


def _post_raw(url, token, raw: bytes):
    """POST raw bytes (no JSON encoding) so we can send a malformed body."""
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req)


def test_post_endpoints(tmp_path):
    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        _post(base + "/api/pause", srv.token, {})
        assert d.paused
        _post(base + "/api/resume", srv.token, {})
        assert not d.paused
        _post(base + "/api/config", srv.token, {"gemini_key": "k"})
        assert d.cfg == {"gemini_key": "k"}
        _post(base + "/api/auth/start", srv.token, {})
        # start_auth runs on a background thread; give it a beat to set the flag.
        for _ in range(50):
            if d.auth:
                break
            import time
            time.sleep(0.01)
        assert d.auth
        r = _post(base + "/api/register", srv.token, {})
        assert d.registered
        assert "config" in json.loads(r.read())["config_path"]
    finally:
        srv.stop()


def test_post_malformed_json_returns_400(tmp_path):
    """A present-but-invalid JSON body returns 400, not a connection reset."""
    import urllib.error

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        try:
            _post_raw(base + "/api/config", srv.token, b"not json")
            assert False, "expected HTTP 400 for malformed body"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        # The daemon never saw the malformed body.
        assert d.cfg is None
    finally:
        srv.stop()


def test_post_register_failure_returns_json_error(tmp_path):
    """A handler that raises surfaces as a JSON {'error': ...} 500 so the wizard
    can show the cause, not an opaque failure."""
    import urllib.error

    class FailingDaemon(FakeDaemon):
        def register(self):
            raise RuntimeError("could not find mcpbrain on PATH")

    d = FailingDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        try:
            _post(base + "/api/register", srv.token, {})
            assert False, "expected HTTP 500"
        except urllib.error.HTTPError as e:
            assert e.code == 500
            assert "could not find mcpbrain" in json.loads(e.read())["error"]
    finally:
        srv.stop()


def test_post_oversize_body_returns_413(tmp_path):
    """A POST DECLARING a Content-Length over the 1 MiB cap is rejected with 413
    before the body is read — so the daemon never sees it.

    The cap keys off the Content-Length HEADER and returns 413 *before* reading
    the body (control_api._handle_post): the threat it guards is "a client
    claiming a huge length" without making the server buffer it. So this sends
    the oversize Content-Length with NO body and reads the response.

    It deliberately does NOT transmit a real 1 MiB body. Doing that raced the
    server's early close against the client's in-flight write and intermittently
    surfaced a BrokenPipeError/ConnectionResetError on the client instead of the
    413 (the body write failed before the response could be read). Asserting on
    the header-only path tests the actual contract and is race-free. The client
    timeout also means a regression that moved the cap to AFTER the read (so the
    server waits for a body that never arrives) fails loudly instead of passing.
    """
    import http.client

    d = FakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        conn.putrequest("POST", "/api/config")
        conn.putheader("Authorization", f"Bearer {srv.token}")
        conn.putheader("Content-Length", str(1_048_576 + 1))  # claim > 1 MiB
        conn.putheader("Connection", "close")
        conn.endheaders()          # send headers only; no body bytes follow
        resp = conn.getresponse()
        assert resp.status == 413
        resp.read()
        conn.close()
        # The daemon never saw the oversize body.
        assert d.cfg is None
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Daemon-level hooks: apply_config re-wiring + register path
# ---------------------------------------------------------------------------

def _make_daemon(tmp_path):
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    return Daemon(store, FakeEmbedder(), services={})


def test_apply_config_writes_and_rewires(tmp_path, monkeypatch):
    """apply_config persists the config and re-wires the enrich client from it."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    sentinel = object()
    monkeypatch.setattr(daemon_mod, "_enrich_client_from_config", lambda home: sentinel)
    monkeypatch.setattr(daemon_mod, "_backup_from_config", lambda home: (None, None))

    d = _make_daemon(tmp_path)
    d.apply_config({"gemini_key": "k"})

    assert d._enrich_client is sentinel
    assert d._backup is None
    assert d._backup_interval_s is None
    # Key landed on disk.
    written = json.loads((tmp_path / "config.json").read_text())
    assert written["gemini_key"] == "k"


def test_apply_config_rewires_backup_pair_together(tmp_path, monkeypatch):
    """apply_config sets _backup and _backup_interval_s as a consistent pair.

    The two fields are read together by the loop thread's maybe_backup; this
    pins that apply_config updates both from the freshly-written config (under
    the backup lock) rather than leaving a stale interval.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    sentinel_backup = object()
    monkeypatch.setattr(daemon_mod, "_enrich_client_from_config", lambda home: None)
    monkeypatch.setattr(
        daemon_mod, "_backup_from_config", lambda home: (sentinel_backup, 1800.0)
    )

    d = _make_daemon(tmp_path)
    assert d._backup is None and d._backup_interval_s is None

    d.apply_config({"backup": {"escrow_key": "k", "shared_drive_id": "D", "user_id": "u"}})

    assert d._backup is sentinel_backup
    assert d._backup_interval_s == 1800.0


def test_start_auth_is_single_flight(tmp_path, monkeypatch):
    """A second start_auth while one is in progress does not run the consent
    flow twice. The non-blocking lock no-ops the duplicate and releases cleanly
    so a later flow can run."""
    import threading
    import time

    calls = {"n": 0}
    gate = threading.Event()

    def fake_consent_flow():
        calls["n"] += 1
        gate.wait(timeout=5)

    monkeypatch.setattr(daemon_mod.auth, "run_consent_flow", fake_consent_flow)

    d = _make_daemon(tmp_path)

    # First flow runs on a background thread; it acquires the lock and blocks
    # inside fake_consent_flow waiting on the gate.
    t = threading.Thread(target=d.start_auth, daemon=True)
    t.start()
    for _ in range(500):
        if calls["n"] == 1:
            break
        time.sleep(0.001)
    assert calls["n"] == 1, "first start_auth did not begin"

    # Second call on the main thread must return immediately as a no-op.
    d.start_auth()
    assert calls["n"] == 1, "duplicate start_auth ran a second consent flow"

    # Release the first flow; once it finishes the lock is free again.
    gate.set()
    t.join(timeout=5)
    assert not t.is_alive()

    # A later flow can run (lock was released in finally).
    gate.clear()
    gate.set()  # don't block this one
    d.start_auth()
    assert calls["n"] == 2


def test_register_returns_path(tmp_path, monkeypatch):
    """register() returns the str of the path register_mcpbrain produces."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from pathlib import Path
    target = Path("/tmp/claude_desktop_config.json")
    import mcpbrain.wizard.register as reg_mod
    monkeypatch.setattr(reg_mod, "register_mcpbrain", lambda **kw: target)

    d = _make_daemon(tmp_path)
    assert d.register() == str(target)


def test_apply_config_rewires_enrich_mode(tmp_path, monkeypatch):
    """apply_config re-reads enrich_mode from the config and writes _enrich_mode.

    Mirrors the setup of test_apply_config_writes_and_rewires: same _make_daemon
    helper, same MCPBRAIN_HOME env-var, same monkeypatches for the two module-
    level config builders that apply_config calls. The new assertion is that
    _enrich_mode (not _enrich_client) reflects the value config.enrich_mode
    returns after the write.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(daemon_mod, "_enrich_client_from_config", lambda home: None)
    monkeypatch.setattr(daemon_mod, "_backup_from_config", lambda home: (None, None))
    # Patch config.enrich_mode so it returns "spool" regardless of what is on disk.
    monkeypatch.setattr(daemon_mod.config, "enrich_mode", lambda home: "spool")

    d = _make_daemon(tmp_path)
    assert d._enrich_mode == "off"   # constructor default

    d.apply_config({"enrich_mode": "spool"})

    assert d._enrich_mode == "spool"


# ---------------------------------------------------------------------------
# POST /api/dashboard/actions/<id>/snooze
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

_PERTH = timezone(timedelta(hours=8))


def _perth_date(offset_days: int = 0) -> str:
    return (datetime.now(_PERTH).date() + timedelta(days=offset_days)).isoformat()


def _snooze_srv(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=s)
    srv.start()
    return s, srv


def test_snooze_success_returns_200(tmp_path):
    s, srv = _snooze_srv(tmp_path)
    try:
        aid = s.add_unified_action(text="Snooze via API", status="open")
        r = _post(f"http://127.0.0.1:{srv.port}/api/dashboard/actions/{aid}/snooze",
                  srv.token, {"until": _perth_date(2)})
        assert json.loads(r.read()) == {"snoozed": True}
    finally:
        srv.stop()


def test_snooze_unknown_id_returns_404(tmp_path):
    import urllib.error
    s, srv = _snooze_srv(tmp_path)
    try:
        url = f"http://127.0.0.1:{srv.port}/api/dashboard/actions/99999/snooze"
        try:
            _post(url, srv.token, {"until": _perth_date(1)})
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.stop()


def test_snooze_closed_action_returns_404(tmp_path):
    import urllib.error
    s, srv = _snooze_srv(tmp_path)
    try:
        aid = s.add_unified_action(text="Closed", status="done")
        url = f"http://127.0.0.1:{srv.port}/api/dashboard/actions/{aid}/snooze"
        try:
            _post(url, srv.token, {"until": _perth_date(1)})
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.stop()


def test_snooze_bad_date_returns_400_and_no_mutation(tmp_path):
    import urllib.error
    s, srv = _snooze_srv(tmp_path)
    try:
        aid = s.add_unified_action(text="Untouched", status="open")
        url = f"http://127.0.0.1:{srv.port}/api/dashboard/actions/{aid}/snooze"
        try:
            _post(url, srv.token, {"until": "garbage"})
            assert False, "expected 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
        row = s.list_unified_actions()[0]
        assert (row["snoozed_until"] or "") == ""
    finally:
        srv.stop()


def test_status_degrades_without_token(tmp_path, monkeypatch):
    """status() with no token file: google_connected False, no scopes, no raise.

    Guards the degradation path. With no token there is no credential to read,
    so status() must not attempt a network refresh — it returns the five
    expected keys and never crashes.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    assert not (tmp_path / "google_token.json").exists()

    d = _make_daemon(tmp_path)
    st = d.status()

    assert st["google_connected"] is False
    assert st["granted_scopes"] == []
    assert st["google_account"] == ""
    assert st["spool"] == {"pending": 0, "inbox": 0}
    assert st["enriched_count"] == 0
    assert set(st) >= {
        "paused", "chunk_count", "enriched_count", "google_connected",
        "granted_scopes", "google_account", "enrich_enabled", "spool",
        "open_findings", "is_configured", "connections",
    }


def test_enrich_backfill_start_cancel_endpoints(tmp_path):
    import time

    class _ExtFakeDaemon(FakeDaemon):
        def __init__(self):
            super().__init__()
            self.backfill_started = False
            self.backfill_cancelled = False

        def start_enrich_backfill(self):
            self.backfill_started = True

        def cancel_enrich_backfill(self):
            self.backfill_cancelled = True

    d = _ExtFakeDaemon()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        base = f"http://127.0.0.1:{srv.port}"
        r = _post(base + "/api/enrich-backfill/start", srv.token, {})
        assert json.loads(r.read())["started"] is True
        # start runs on a daemon thread — give it a beat
        for _ in range(50):
            if d.backfill_started:
                break
            time.sleep(0.01)
        assert d.backfill_started

        r = _post(base + "/api/enrich-backfill/cancel", srv.token, {})
        assert json.loads(r.read())["cancelled"] is True
        assert d.backfill_cancelled
    finally:
        srv.stop()
