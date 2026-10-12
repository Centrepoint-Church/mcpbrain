"""A TRANSIENT failure to reach Google at daemon start must not disable sync,
backup or the org-config merge for the whole run.

Live 2026-10-09: the daemon was bootstrapped while oauth2.googleapis.com was
briefly unreachable (Errno 49, local ephemeral ports exhausted by a drain).
Services, backup and the org-config merge each resolved "permanently" to
nothing on that one failure, and stayed off for three days until a manual
restart. Now a transient failure retries on a backoff (60 s, doubling, capped
at 900 s); a permanent one (no token, revoked grant) behaves as before.

Fully offline: auth.build_google_services and _build_drive_service are
monkeypatched, and the clocks are injected.
"""
import errno
import logging

import pytest
from google.auth import exceptions as gae

from mcpbrain import auth
from mcpbrain import daemon as dmod
from mcpbrain.daemon import Daemon
from mcpbrain.store import Store


class FakeEmbedder:
    dim = 4

    def embed(self, texts):
        return [[0.0] * 4 for _ in texts]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _transient():
    # The live shape: google-auth wraps the requests ConnectionError.
    return gae.TransportError(OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"))


class FlakyBuild:
    """build_google_services stand-in: raises each queued exception in turn,
    then returns `result`. Counts calls."""

    def __init__(self, *errors, result=None):
        self.errors = list(errors)
        self.result = result if result is not None else {}
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    return tmp_path


def _daemon(home, clock, wall=None):
    store = Store(home / "b.sqlite3", dim=4)
    store.init()
    return Daemon(store, FakeEmbedder(), clock=clock,
                  wall_clock=wall or (lambda: 1_789_000_000.0))


# -- services -----------------------------------------------------------------

def test_transient_failure_retries_on_backoff_then_restores(home, monkeypatch, caplog):
    clock = Clock()
    gmail, drive = object(), object()
    build = FlakyBuild(_transient(), result={"gmail_service": gmail, "drive_service": drive})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock)

    with caplog.at_level(logging.INFO, logger="mcpbrain.daemon"):
        assert d.ensure_services() == {}
        assert build.calls == 1
        assert d._services_resolved is False
        assert d._google_services_pending()

        # Before retry_at: cheap {} and auth is NOT called again.
        clock.t += 59
        assert d.ensure_services() == {}
        assert build.calls == 1

        # After retry_at: rebuilt, resolved, recovery logged once.
        clock.t += 1
        assert d.ensure_services() == {"gmail_service": gmail, "drive_service": drive}
        assert build.calls == 2
        assert d._services_resolved is True
        d.ensure_services()
        assert build.calls == 2

    restored = [r for r in caplog.records if "Google services restored" in r.getMessage()]
    assert len(restored) == 1
    assert "after 2 attempt(s)" in restored[0].getMessage()
    assert any("transient" in r.getMessage() and "will retry" in r.getMessage()
               for r in caplog.records)
    assert d.google_unavailable() is None


@pytest.mark.parametrize("exc", [
    FileNotFoundError("google_token.json"),
    gae.RefreshError("invalid_grant: Token has been expired or revoked."),
    RuntimeError("No valid credentials found"),
])
def test_permanent_failure_resolves_empty_without_retry(home, monkeypatch, exc):
    clock = Clock()
    build = FlakyBuild(exc)
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock)

    assert d.ensure_services() == {}
    assert d._services_resolved is True
    clock.t += 10_000
    assert d.ensure_services() == {}
    assert build.calls == 1
    assert d.google_unavailable() is None
    assert not d._google_services_pending()


def test_injected_services_never_call_auth(home, monkeypatch):
    build = FlakyBuild(_transient())
    monkeypatch.setattr(auth, "build_google_services", build)
    store = Store(home / "b.sqlite3", dim=4)
    store.init()
    d = Daemon(store, FakeEmbedder(), services={})
    assert d.ensure_services() == {}
    assert build.calls == 0


def test_first_time_success_is_unchanged(home, monkeypatch, caplog):
    gmail = object()
    build = FlakyBuild(result={"gmail_service": gmail})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, Clock())
    with caplog.at_level(logging.INFO, logger="mcpbrain.daemon"):
        assert d.ensure_services() == {"gmail_service": gmail}
        d.ensure_services()
    assert build.calls == 1
    assert not any("restored" in r.getMessage() for r in caplog.records)


def test_org_pass_stays_due_while_services_await_retry(home, monkeypatch):
    """A daily org pass that ran with no Drive service during the outage must
    not stamp itself done for a day: it stays due for the tick after recovery."""
    clock = Clock()
    monkeypatch.setattr(auth, "build_google_services", FlakyBuild(_transient()))
    monkeypatch.setattr(dmod.config, "org_import_enabled", lambda home: True)
    d = _daemon(home, clock)
    d._org_import_interval_s = 86400.0
    d._last_org_import = None
    d.ensure_services()
    assert d._run_org_import() == {"skipped": "no_fleet_storage"}
    assert d._last_org_import is None


# -- backoff ------------------------------------------------------------------

def test_backoff_doubles_and_caps_at_900s():
    clock = Clock(0.0)
    bo = dmod._GoogleBackoff("x", clock, lambda: 0.0)
    delays = []
    for _ in range(7):
        bo.failed(_transient())
        delays.append(bo.retry_at - clock.t)
    assert delays == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0]
    assert dmod.GOOGLE_RETRY_MAX_S == 900.0
    assert not bo.due()
    clock.t += 900
    assert bo.due()


# -- backup -------------------------------------------------------------------

def _configure_backup(home):
    from mcpbrain import config
    config.write_config(str(home), {"backup": {
        "escrow_key": "k" * 44, "shared_drive_id": "SD", "user_id": "u1"}})


def test_backup_transient_build_failure_rebuilds_later_and_backs_up(home, monkeypatch):
    _configure_backup(home)
    clock = Clock()
    drive = object()
    build = FlakyBuild(_transient(), result={"drive_service": drive})
    monkeypatch.setattr(auth, "build_google_services", build)

    # Start-up, as main() does it.
    res = dmod._backup_from_config(str(home))
    assert tuple(res) == (None, None)
    assert res.transient is not None
    d = _daemon(home, clock)
    d._backup_backoff.failed(res.transient)
    assert d.google_unavailable()["affects"] == ["backup"]

    d._retry_backup_build()                 # not due yet
    assert d._backup is None and build.calls == 1

    clock.t += 60
    d._retry_backup_build()
    assert build.calls == 2
    assert d._backup is not None and d._backup.drive_service is drive
    assert d._backup_interval_s == dmod.DEFAULT_BACKUP_INTERVAL_S
    assert d.google_unavailable() is None

    # ... and a backup is now actually possible.
    uploaded = []
    monkeypatch.setattr(dmod, "_mac_user_is_active", lambda: True)
    monkeypatch.setattr(dmod, "make_encrypted_snapshot", lambda *a, **k: home / "snap.enc")
    monkeypatch.setattr(dmod, "upload_snapshot",
                        lambda svc, path, sd, uid: uploaded.append(svc) or "FILE1")
    monkeypatch.setattr("mcpbrain.backup.prune_snapshots", lambda *a, **k: None)
    out = d.maybe_backup()
    assert out["backed_up"] is True
    assert uploaded == [drive]


def test_backup_permanent_build_failure_stays_disabled(home, monkeypatch):
    _configure_backup(home)
    clock = Clock()
    build = FlakyBuild(FileNotFoundError("google_token.json"))
    monkeypatch.setattr(auth, "build_google_services", build)

    res = dmod._backup_from_config(str(home))
    assert tuple(res) == (None, None)
    assert res.transient is None
    d = _daemon(home, clock)
    clock.t += 10_000
    d._retry_backup_build()
    assert d._backup is None
    assert build.calls == 1
    assert d.maybe_backup() is None


def test_apply_config_transient_backup_failure_schedules_retry(home, monkeypatch):
    clock = Clock()
    drive = object()
    build = FlakyBuild(_transient(), result={"drive_service": drive})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock)
    d.apply_config({"backup": {"escrow_key": "k" * 44, "shared_drive_id": "SD",
                               "user_id": "u1"}})
    assert d._backup is None
    assert d._backup_backoff.pending
    clock.t += 60
    d._retry_google_builds()
    assert d._backup is not None and d._backup.drive_service is drive


# -- org-config merge ---------------------------------------------------------

def test_org_config_merge_transient_retries(home, monkeypatch):
    (home / "google_token.json").write_text("{}")
    monkeypatch.setattr(auth, "token_path", lambda: home / "google_token.json")
    clock = Clock()
    merges = []
    build = FlakyBuild(_transient(), result="SVC")
    monkeypatch.setattr(dmod, "_build_drive_service", build)
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: merges.append(svc))

    exc = dmod._maybe_merge_org_config(str(home))
    assert exc is not None and merges == []
    d = _daemon(home, clock)
    d._org_config_backoff.failed(exc)

    d._retry_google_builds()                # not due
    assert merges == []
    clock.t += 60
    d._retry_google_builds()
    assert merges == ["SVC"]
    assert not d._org_config_backoff.pending


def test_org_config_merge_permanent_is_skipped_not_retried(home, monkeypatch):
    (home / "google_token.json").write_text("{}")
    monkeypatch.setattr(auth, "token_path", lambda: home / "google_token.json")
    build = FlakyBuild(RuntimeError("No valid credentials found"))
    monkeypatch.setattr(dmod, "_build_drive_service", build)
    assert dmod._maybe_merge_org_config(str(home)) is None
    assert build.calls == 1


# -- visibility ---------------------------------------------------------------

def test_status_reports_google_unavailable_then_null_after_recovery(home, monkeypatch):
    clock = Clock()
    build = FlakyBuild(_transient(), _transient(), result={"gmail_service": object()})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock, wall=lambda: 1_789_000_000.0)

    d.ensure_services()
    clock.t += 60
    d.ensure_services()
    gu = d.status()["google_unavailable"]
    assert gu["attempts"] == 2
    assert gu["since"] == "2026-09-10T00:26:40Z"
    assert "Can't assign requested address" in gu["last_error"]
    assert len(gu["last_error"]) <= 200
    assert gu["affects"] == ["sync services"]

    clock.t += 120
    d.ensure_services()
    assert d.status()["google_unavailable"] is None


def test_doctor_line_appears_while_google_unavailable():
    from mcpbrain import doctor
    line = doctor.google_unavailable_line({"google_unavailable": {
        "since": "2026-10-09T08:00:00Z", "attempts": 5,
        "last_error": "Max retries exceeded", "affects": ["sync services", "backup"]}})
    assert line.startswith("⚠️")
    assert "unreachable since 2026-10-09T08:00:00Z" in line
    assert "retrying (5 attempts)" in line
    assert doctor.google_unavailable_line({"google_unavailable": None}) is None
    assert doctor.google_unavailable_line(None) is None
