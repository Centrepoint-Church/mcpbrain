"""Tests for the daemon orchestration loop and single-writer lock.

Reuses the fake Gmail service shape from test_sync_cycle.py and a tiny
FakeEmbedder (like test_index.py) so a cycle indexes without the bge download.
No real network or hanging timers: cycles are driven directly via run_one(),
and run() is bounded by stop()/events with a tiny interval.
"""

import base64
import threading
import time

import pytest

import mcpbrain.daemon as daemon_module
from mcpbrain.daemon import (
    AlreadyRunningError,
    BackupConfig,
    Daemon,
    SingleWriterLock,
    run_cycle,
)
from mcpbrain.store import Store
from mcpbrain.backup import generate_escrow_key, decrypt_file

# Reuse the Drive fake shape from test_backup.py.
from tests.test_backup import FakeFiles, FakeService


# ---------------------------------------------------------------------------
# Tiny fakes
# ---------------------------------------------------------------------------

class FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] for _ in texts]


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def plain_msg(mid: str, subject: str, sender: str, body: str) -> dict:
    return {
        "id": mid,
        "threadId": "t-" + mid,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": b64(body)},
        },
    }


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _History:
    def __init__(self, pages):
        self._pages = pages

    def list(self, **kw):
        token = kw.get("pageToken")
        idx = 0 if token is None else int(token)
        return _Req(self._pages[idx])


class _Messages:
    def __init__(self, by_id):
        self._by_id = by_id

    def get(self, userId, id, format):
        result = self._by_id[id]
        if isinstance(result, Exception):
            raise result
        return _Req(result)


class _Users:
    def __init__(self, profile_hid, history, messages):
        self._p = profile_hid
        self._h = history
        self._m = messages

    def getProfile(self, userId):
        return _Req({"historyId": self._p, "emailAddress": "test@example.com"})

    def history(self):
        return self._h

    def messages(self):
        return self._m


class FakeGmailService:
    def __init__(self, profile_hid="1000", pages=None, messages=None):
        msgs = _Messages(messages or {})
        self._users = _Users(profile_hid, _History(pages or []), msgs)

    def users(self):
        return self._users


def _make_page(msg_ids, history_id, next_page_token=None):
    history = [
        {
            "id": f"h-{mid}",
            "messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX"]}}],
        }
        for mid in msg_ids
    ]
    page = {"history": history, "historyId": history_id}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path, name="b.sqlite3"):
    store = Store(tmp_path / name, dim=4)
    store.init()
    store.set_cursor("gmail", "1000")  # delta path, not bootstrap
    return store


def _gmail_fake_one_message():
    body = "Annual budget review and quarterly expenditure forecast for finance."
    msg = plain_msg("m1", "Finance Budget Forecast", "finance@example.com", body)
    pages = [_make_page(["m1"], history_id="1005")]
    return FakeGmailService(profile_hid="1000", pages=pages, messages={"m1": msg})


def _chunk_count(store) -> int:
    with store._connect() as db:
        return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


# ---------------------------------------------------------------------------
# run_cycle / run_one
# ---------------------------------------------------------------------------

def test_run_cycle_runs_one_cycle_against_fixtures(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()

    res = run_cycle(store, emb, gmail_service=fake)

    assert res["gmail"] >= 1
    assert res["embedded"] >= 1
    assert store.get_chunk("gmail-m1-body-0") is not None


def test_run_cycle_surfaces_agent_err_as_finding(tmp_path, monkeypatch):
    """A cycle with a records .err file in the home records an open finding."""
    from mcpbrain.agent_errs import FINDING_TYPE

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    (tmp_path / "com.mcpbrain.records.prune.err").write_text(
        "Traceback (most recent call last): boom\n")

    run_cycle(store, emb, gmail_service=fake)

    findings = store.open_findings(FINDING_TYPE)
    assert len(findings) == 1
    assert "records" in findings[0]["summary"]


def test_run_one_runs_one_cycle_against_fixtures(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    res = daemon.run_one()

    assert res is not None
    assert res["gmail"] >= 1
    assert res["embedded"] >= 1
    assert store.get_chunk("gmail-m1-body-0") is not None


def test_paused_cycle_writes_nothing_including_no_enrich(tmp_path):
    """Paused run_one returns None and writes nothing — no sync, no enrich."""
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    before_chunks = _chunk_count(store)
    daemon.pause()

    res = daemon.run_one()

    assert res is None
    assert _chunk_count(store) == before_chunks   # no sync
    assert store.list_entities() == []            # no enrichment
    assert store.get_meta("enrich_mode") is None  # run_enrichment never called


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------

def test_pause_skips_the_cycle_and_writes_nothing(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    before = _chunk_count(store)
    daemon.pause()
    assert daemon.is_paused() is True

    res = daemon.run_one()

    assert res is None
    assert _chunk_count(store) == before, "Paused cycle must not write to the store"


def test_resume_re_enables_the_cycle(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    daemon.pause()
    assert daemon.run_one() is None

    daemon.resume()
    assert daemon.is_paused() is False

    res = daemon.run_one()
    assert res is not None
    assert res["gmail"] >= 1
    assert store.get_chunk("gmail-m1-body-0") is not None


# ---------------------------------------------------------------------------
# single-writer lock
# ---------------------------------------------------------------------------

def test_single_writer_lock_excludes_second_acquirer(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    first = SingleWriterLock(lock_path)
    first.acquire()
    try:
        second = SingleWriterLock(lock_path)
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    # Once released, a fresh acquirer succeeds.
    third = SingleWriterLock(lock_path)
    third.acquire()
    third.release()


def test_single_writer_lock_context_manager(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    with SingleWriterLock(lock_path):
        blocked = SingleWriterLock(lock_path)
        with pytest.raises(AlreadyRunningError):
            blocked.acquire()
    # After the with-block exits the lock is released.
    after = SingleWriterLock(lock_path)
    after.acquire()
    after.release()


def test_lock_defaults_to_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    lock = SingleWriterLock()
    assert lock.lock_path == tmp_path / "daemon.lock"


def test_single_writer_lock_accepts_string_path(tmp_path):
    """Fix 1 regression test: SingleWriterLock accepts a plain str lock_path.

    The Windows acquire() branch calls self.lock_path.exists(), which raises
    AttributeError if lock_path is a str.  __init__ now coerces to Path so both
    the POSIX and Windows branches are robust.  This test exercises the POSIX
    path (runs on Linux) but validates the coercion contract end-to-end:
    acquire/release on a string path, and AlreadyRunningError on a second
    acquirer given the same string path.
    """
    str_path = str(tmp_path / "d.lock")

    first = SingleWriterLock(lock_path=str_path)
    first.acquire()
    try:
        second = SingleWriterLock(lock_path=str_path)
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()

    # After release, a fresh lock on the same string path succeeds.
    third = SingleWriterLock(lock_path=str_path)
    third.acquire()
    third.release()


def test_daemon_module_imports_and_exposes_locking_backend():
    """daemon.py must import cleanly on any platform and expose at least one
    locking backend (fcntl on POSIX, msvcrt on Windows).

    On this Linux box fcntl is available and msvcrt is not. The test asserts:
    - SingleWriterLock is exported (public API intact).
    - At least one backend module is non-None (the platform-guard imports work).
    - On POSIX, fcntl is not None (the known Linux/macOS condition).
    """
    assert SingleWriterLock is not None
    assert daemon_module.fcntl is not None or daemon_module.msvcrt is not None, (
        "neither fcntl nor msvcrt is available — daemon has no locking backend"
    )
    # On this Linux CI box we expect the POSIX backend.
    assert daemon_module.fcntl is not None, (
        "expected fcntl to be available on this POSIX platform"
    )


# ---------------------------------------------------------------------------
# run() is bounded by stop()
# ---------------------------------------------------------------------------

def test_stop_bounds_run_and_at_least_one_cycle_runs(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    interval_s=0.01, lock=SingleWriterLock(tmp_path / "d.lock"))

    t = threading.Thread(target=daemon.run)
    t.start()
    # Poll until the first chunk appears, then stop.  poll is never set; it is
    # used only as a 10ms sleep so we can yield the GIL between checks without
    # calling time.sleep() directly.
    poll = threading.Event()  # never set; used only as a bounded sleep
    while store.get_chunk("gmail-m1-body-0") is None and not poll.wait(0.01):
        if not t.is_alive():
            break
    daemon.stop()
    t.join(timeout=5.0)

    assert not t.is_alive(), "run() did not return promptly after stop()"
    assert store.get_chunk("gmail-m1-body-0") is not None, "at least one cycle should have run"


def test_run_exits_when_stop_preset(tmp_path):
    """If _stop is set before run(), run() acquires the lock, may run cycles, exits."""
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    interval_s=0.01, lock=SingleWriterLock(tmp_path / "d.lock"))
    daemon.stop()

    t = threading.Thread(target=daemon.run)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "run() should return promptly when stop is preset"


class _FlakyGmailService:
    """Raises a network-ish error on the first sync, then serves one message."""

    def __init__(self):
        self.calls = 0
        self._ok = _gmail_fake_one_message()

    def users(self):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("The read operation timed out")
        return self._ok.users()


def test_run_survives_transient_sync_error(tmp_path):
    """A sync exception (e.g. a Gmail read timeout) must NOT kill the loop.

    Live 2026-06-05 failure: an uncaught sync_gmail timeout crashed the
    process; launchd restarted it, resetting cadence anchors and dropping
    stashed block requests. The loop must log the cycle failure and try
    again on the next interval.
    """
    store = _make_store(tmp_path)
    flaky = _FlakyGmailService()
    daemon = Daemon(store, FakeEmbedder(), services={"gmail_service": flaky},
                    interval_s=0.01, lock=SingleWriterLock(tmp_path / "d.lock"))

    t = threading.Thread(target=daemon.run, daemon=True)
    t.start()
    poll = threading.Event()  # never set; used only as a bounded sleep
    for _ in range(500):      # up to ~5s
        if store.get_chunk("gmail-m1-body-0") is not None or not t.is_alive():
            break
        poll.wait(0.01)
    daemon.stop()
    t.join(timeout=5.0)

    assert flaky.calls >= 2, "loop died on the first (failing) sync cycle"
    assert store.get_chunk("gmail-m1-body-0") is not None, \
        "a later cycle should sync successfully after the transient error"


# ---------------------------------------------------------------------------
# sync_now() during an in-flight cycle triggers an immediate extra cycle
# ---------------------------------------------------------------------------

class _TwoCycleFakeGmailService:
    """Returns a different message on each of the first two sync cycles.

    Cycle 1 → message m1; cycle 2 → message m2 (after advance() is called);
    subsequent cycles → empty history.

    The Gmail API calls users() several times per cycle (getProfile,
    history().list(), messages().get()).  Rather than trying to detect cycle
    boundaries automatically, the test calls advance() explicitly after the
    first chunk appears.  All users() calls until advance() share the same
    underlying fake; after advance() they share the second fake.  This keeps
    the fake stateless between API calls within a single cycle.
    """

    def __init__(self):
        self._idx = 0
        self._fakes = [
            self._build(0),
            self._build(1),
        ]

    @staticmethod
    def _build(n: int) -> FakeGmailService:
        if n == 0:
            mid, hid = "m1", "1005"
        else:
            mid, hid = "m2", "1010"
        body = f"Message {mid} body text for embedding purposes."
        msg = plain_msg(mid, f"Subject {mid}", "sender@example.com", body)
        pages = [_make_page([mid], history_id=hid)]
        return FakeGmailService(profile_hid="1000", pages=pages, messages={mid: msg})

    def advance(self) -> None:
        """Switch to the second fake; call this once the first cycle has landed."""
        self._idx = min(self._idx + 1, len(self._fakes) - 1)

    def users(self):
        return self._fakes[self._idx].users()


def test_sync_now_during_cycle_triggers_immediate_extra_cycle(tmp_path):
    """sync_now() called while a cycle is running must trigger an additional
    cycle promptly, not waiting the full interval.

    Mechanism: _wake is cleared BEFORE run_one() so a sync_now() that arrives
    during the cycle re-sets _wake, and the subsequent _wake.wait() returns
    immediately.  Under the old clear-AFTER-wait placement, _wake was cleared
    at the end of the wait, so a sync_now() fired during a cycle was silently
    dropped and the loop sat for the full interval before running again.

    The test uses interval_s=3600 so the only way the second cycle can finish
    within the test budget is if sync_now() actually wakes the loop immediately.
    A two-cycle fake Gmail service yields m1 on the first cycle and m2 on the
    second (after advance() is called), so the appearance of the m2 chunk is
    definitive proof a second cycle ran.
    """
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    svc = _TwoCycleFakeGmailService()
    daemon = Daemon(
        store, emb,
        services={"gmail_service": svc},
        interval_s=3600.0,  # huge interval: second cycle must be wake-driven
        lock=SingleWriterLock(tmp_path / "d.lock"),
    )

    t = threading.Thread(target=daemon.run, daemon=True)
    t.start()

    # Wait for the first cycle to deposit its chunk.
    poll = threading.Event()  # never set; used only as a bounded sleep
    deadline = time.monotonic() + 10.0
    while store.get_chunk("gmail-m1-body-0") is None:
        assert time.monotonic() < deadline, "timed out waiting for first-cycle chunk (m1)"
        poll.wait(0.02)

    # Switch the fake to m2, then fire sync_now() to wake the loop immediately.
    svc.advance()
    daemon.sync_now()

    # The second chunk must appear well within 5 s — not 3600 s.
    deadline2 = time.monotonic() + 5.0
    while store.get_chunk("gmail-m2-body-0") is None:
        assert time.monotonic() < deadline2, (
            "second-cycle chunk (m2) did not appear within 5 s — "
            "sync_now() did not trigger a prompt extra cycle (lost-wakeup bug)"
        )
        poll.wait(0.02)

    daemon.stop()
    t.join(timeout=5.0)
    assert not t.is_alive(), "run() did not return promptly after stop()"


# ---------------------------------------------------------------------------
# is_stopped()
# ---------------------------------------------------------------------------

def test_is_stopped_false_initially(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    assert daemon.is_stopped() is False


def test_is_stopped_true_after_stop(tmp_path):
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    daemon.stop()

    assert daemon.is_stopped() is True


# ---------------------------------------------------------------------------
# periodic encrypted backup in the loop (Task H2)
# ---------------------------------------------------------------------------

SQLITE_MAGIC = b"SQLite format 3\x00"


class _RaisingFiles(FakeFiles):
    """A Drive files() fake whose create() raises, so maybe_backup hits the
    upload failure path. After heal() is called, create() behaves normally."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raise = True

    def heal(self):
        self._raise = False

    def create(self, **kw):
        if self._raise:
            raise RuntimeError("simulated Drive error")
        return super().create(**kw)


class _Clock:
    """A list-controlled monotonic clock for deterministic 'due' checks."""

    def __init__(self, value=0.0):
        self._value = value

    def __call__(self):
        return self._value

    def advance(self, by):
        self._value += by


def _store_with_chunk(tmp_path, name="backup.sqlite3"):
    store = Store(tmp_path / name, dim=4)
    store.init()
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    return store


def _backup_config(tmp_path, files, *, out_name="snapshot.enc", key=None):
    return BackupConfig(
        key=key or generate_escrow_key(),
        drive_service=FakeService(files),
        shared_drive_id="drive-XYZ",
        user_id="sam",
        out_path=tmp_path / out_name,
    )


def test_unconfigured_daemon_never_backs_up(tmp_path):
    store = _store_with_chunk(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    assert daemon.maybe_backup() is None


def test_configured_first_call_snapshots_and_uploads(tmp_path):
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=3600.0, clock=_Clock())

    summary = daemon.maybe_backup()

    assert summary is not None
    assert summary["backed_up"] is True
    assert summary["file_id"] == "file-123"

    # The Drive fake received a file-upload create.
    file_creates = [
        c for c in files.create_calls
        if c["body"].get("mimeType") != FakeFiles.FOLDER_MIME
    ]
    assert len(file_creates) == 1

    # The local encrypted artifact exists and is NOT plaintext sqlite.
    out = cfg.out_path
    assert out.exists()
    head = out.read_bytes()[: len(SQLITE_MAGIC)]
    assert head != SQLITE_MAGIC, "artifact looks like plaintext sqlite — mail in clear"


def test_not_due_skips_second_backup_then_due_backs_up_again(tmp_path):
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    clock = _Clock()
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=100.0, clock=clock)

    first = daemon.maybe_backup()
    assert first is not None and first["backed_up"] is True

    def _file_create_count():
        return len([
            c for c in files.create_calls
            if c["body"].get("mimeType") != FakeFiles.FOLDER_MIME
        ])

    assert _file_create_count() == 1

    # Advance less than the interval: not due, no new upload.
    clock.advance(50.0)
    assert daemon.maybe_backup() is None
    assert _file_create_count() == 1

    # Advance past the interval: due again, a second upload happens.
    clock.advance(60.0)  # total 110 >= 100
    second = daemon.maybe_backup()
    assert second is not None and second["backed_up"] is True
    assert _file_create_count() == 2


def test_backup_failure_does_not_crash_and_loop_continues(tmp_path):
    store = _store_with_chunk(tmp_path)
    files = _RaisingFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=0.0, clock=_Clock())

    failed = daemon.maybe_backup()
    assert failed is not None
    assert failed["backed_up"] is False
    assert "error" in failed

    # The daemon is still usable: heal the Drive fake and back up successfully.
    files.heal()
    ok = daemon.maybe_backup()  # interval 0 -> always due
    assert ok is not None and ok["backed_up"] is True
    assert ok["file_id"] == "file-123"


def test_backup_artifact_decrypts_to_a_valid_store(tmp_path):
    store = _store_with_chunk(tmp_path)
    store.upsert_entity("taryn-hamilton", "Taryn Hamilton", "person", org="Acme")
    store.set_cursor("gmail", "cursor-42")

    files = FakeFiles(list_response={"files": []})
    key = generate_escrow_key()
    cfg = _backup_config(tmp_path, files, key=key)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=3600.0, clock=_Clock())

    summary = daemon.maybe_backup()
    assert summary["backed_up"] is True

    dec = decrypt_file(cfg.out_path, tmp_path / "restored.sqlite3", key)
    loaded = Store(dec, dim=4)
    assert loaded.get_chunk("d-budget") is not None
    assert loaded.get_entity("taryn-hamilton") is not None
    assert loaded.get_cursor("gmail") == "cursor-42"


def test_run_loop_runs_a_backup_within_the_loop(tmp_path):
    """run() should call maybe_backup() each iteration; with interval 0 the first
    loop pass backs up. Proves the loop wiring, not just maybe_backup in isolation."""
    store = _make_store(tmp_path)
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=0.0, clock=_Clock())

    t = threading.Thread(target=daemon.run)
    t.start()
    poll = threading.Event()  # never set; bounded sleep
    deadline = time.monotonic() + 5.0
    while not cfg.out_path.exists():
        if time.monotonic() >= deadline or not t.is_alive():
            break
        poll.wait(0.01)
    daemon.stop()
    t.join(timeout=5.0)

    assert not t.is_alive(), "run() did not return promptly after stop()"
    assert cfg.out_path.exists(), "loop did not produce a backup artifact"


# ---------------------------------------------------------------------------
# construction-time validation: backup_interval_s is required when backup is on
# ---------------------------------------------------------------------------

def test_daemon_raises_if_backup_configured_without_interval(tmp_path):
    """Daemon(backup=<cfg>, backup_interval_s=None) must raise ValueError immediately.

    Without this guard the first maybe_backup() call succeeds (self._last_backup
    is None), but the second call does ``elapsed < None`` which raises TypeError
    and is swallowed by the broad except, causing a silent perpetual-failure loop.
    Fail loud at construction time instead.
    """
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)

    with pytest.raises(ValueError, match="backup_interval_s"):
        Daemon(store, FakeEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"),
               backup=cfg, backup_interval_s=None)


def test_daemon_constructs_fine_when_backup_is_not_configured(tmp_path):
    """backup=None, backup_interval_s=None is the normal unconfigured case and
    must not raise."""
    store = _store_with_chunk(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    assert daemon.maybe_backup() is None


# ---------------------------------------------------------------------------
# periodic entity resolution in the loop (Task R8)
# ---------------------------------------------------------------------------

def _seed_duplicate_entities(store):
    """Upsert two id-distinct, same-type entities that share a canonical key
    ('Ps Joel' and 'Joel' both -> 'joel', type person). The deterministic
    resolver folds them into one survivor; without resolution both remain."""
    store.upsert_entity("ps-joel", "Ps Joel", "person", org="Acme")
    store.upsert_entity("joel", "Joel", "person", org="Acme")
    return ["ps-joel", "joel"]


def test_unconfigured_daemon_never_resolves(tmp_path):
    """resolve_interval_s=None -> maybe_resolve() returns None and no merge runs;
    the two key-identical duplicates both survive."""
    store = _make_store(tmp_path)
    _seed_duplicate_entities(store)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    assert daemon.maybe_resolve() is None
    assert len(store.list_entities()) == 2, "no resolution should have run"


def test_configured_first_call_resolves_duplicates(tmp_path):
    """resolve_interval_s set + due (first call) -> deterministic merge folds the
    two duplicates into one. enrich_client is None: deterministic-only tier."""
    store = _make_store(tmp_path)
    _seed_duplicate_entities(store)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    resolve_interval_s=3600.0, clock=_Clock())

    summary = daemon.maybe_resolve()

    assert summary is not None
    assert summary["mode"] == "deterministic"
    assert summary["auto_merges"] == 1
    assert len(store.list_entities()) == 1, "duplicates should be merged"


def test_resolve_not_due_skips_then_due_runs_again(tmp_path):
    """After a due resolve, advancing < interval -> None (no second run);
    advancing >= interval -> runs again (a fresh dup is merged)."""
    store = _make_store(tmp_path)
    _seed_duplicate_entities(store)
    clock = _Clock()
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    resolve_interval_s=100.0, clock=clock)

    first = daemon.maybe_resolve()
    assert first is not None and first["auto_merges"] == 1
    assert len(store.list_entities()) == 1

    # Seed a fresh duplicate pair so a re-run would have work to do.
    store.upsert_entity("ps-taryn", "Ps Taryn", "person", org="Acme")
    store.upsert_entity("taryn", "Taryn", "person", org="Acme")

    # Not due yet: maybe_resolve returns None and the new dup is NOT merged.
    clock.advance(50.0)
    assert daemon.maybe_resolve() is None
    assert len(store.list_entities()) == 3  # 1 survivor + 2 fresh dups

    # Due again: runs and merges the fresh dup.
    clock.advance(60.0)  # total 110 >= 100
    second = daemon.maybe_resolve()
    assert second is not None and second["auto_merges"] == 1
    assert len(store.list_entities()) == 2  # both survivors


def test_resolve_failure_does_not_crash_and_loop_continues(tmp_path, monkeypatch):
    """A resolve_entities exception is logged and swallowed: maybe_resolve()
    returns {"resolved": False, "error": ...} and never propagates."""
    store = _make_store(tmp_path)
    _seed_duplicate_entities(store)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    resolve_interval_s=0.0, clock=_Clock())

    import mcpbrain.resolve as resolve_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated resolve error")

    monkeypatch.setattr(resolve_module, "resolve_entities", _boom)

    failed = daemon.maybe_resolve()
    assert failed is not None
    assert failed["resolved"] is False
    assert "error" in failed
    # No merge happened; both duplicates still present.
    assert len(store.list_entities()) == 2

    # The daemon is still usable: undo the monkeypatch and resolve cleanly.
    monkeypatch.undo()
    ok = daemon.maybe_resolve()  # interval 0 -> always due
    assert ok is not None and ok["auto_merges"] == 1
    assert len(store.list_entities()) == 1


def test_run_loop_runs_a_resolve_within_the_loop(tmp_path, monkeypatch):
    """run() should call maybe_resolve() each iteration; with interval 0 the
    first loop pass resolves. Proves the loop wiring, not maybe_resolve alone."""
    import json
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "S", "owner_email": "s@x.com", "orgs": [{"name": "O"}]}
    ))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    _seed_duplicate_entities(store)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    resolve_interval_s=0.0, clock=_Clock())

    t = threading.Thread(target=daemon.run)
    t.start()
    poll = threading.Event()  # never set; bounded sleep
    deadline = time.monotonic() + 5.0
    while len(store.list_entities()) > 1:
        if time.monotonic() >= deadline or not t.is_alive():
            break
        poll.wait(0.01)
    daemon.stop()
    t.join(timeout=5.0)

    assert not t.is_alive(), "run() did not return promptly after stop()"
    assert len(store.list_entities()) == 1, "loop did not run a resolve"


# ---------------------------------------------------------------------------
# G3 — daemon self-wires real services when not injected
# ---------------------------------------------------------------------------

def test_injected_services_used_as_is_no_auth_call(tmp_path, monkeypatch):
    """An explicitly injected services dict is used as-is: run() must NOT call
    auth.build_google_services (the explicit-injection contract)."""
    import mcpbrain.auth as auth_module

    def boom():
        raise AssertionError("build_google_services must not be called when services injected")

    monkeypatch.setattr(auth_module, "build_google_services", boom)

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    interval_s=0.01, lock=SingleWriterLock(tmp_path / "d.lock"))

    t = threading.Thread(target=daemon.run)
    t.start()
    poll = threading.Event()
    deadline = time.monotonic() + 5.0
    while store.get_chunk("gmail-m1-body-0") is None:
        if time.monotonic() >= deadline or not t.is_alive():
            break
        poll.wait(0.01)
    daemon.stop()
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert store.get_chunk("gmail-m1-body-0") is not None, "injected service should have synced"


def test_services_none_auto_builds_from_token(tmp_path, monkeypatch):
    """services=None (default): run() builds services via auth.build_google_services
    and the built gmail service drives a sync."""
    import mcpbrain.auth as auth_module

    fake = _gmail_fake_one_message()
    build_calls = []

    def fake_build():
        build_calls.append(True)
        return {"gmail_service": fake}

    monkeypatch.setattr(auth_module, "build_google_services", fake_build)

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    t = threading.Thread(target=daemon.run)
    t.start()
    poll = threading.Event()
    deadline = time.monotonic() + 5.0
    while store.get_chunk("gmail-m1-body-0") is None:
        if time.monotonic() >= deadline or not t.is_alive():
            break
        poll.wait(0.01)
    daemon.stop()
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert build_calls, "build_google_services should have been called when services=None"
    assert store.get_chunk("gmail-m1-body-0") is not None, "auto-built service should have synced"


def test_services_none_auth_raises_runs_with_empty_services(tmp_path, monkeypatch):
    """services=None + auth.build_google_services raising (no/invalid token):
    the daemon logs, runs with empty services (no crash, no sync), and a bounded
    run() completes."""
    import mcpbrain.auth as auth_module

    def boom():
        raise RuntimeError("no valid token")

    monkeypatch.setattr(auth_module, "build_google_services", boom)

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    daemon.stop()  # bound the loop to (at most) one pass

    t = threading.Thread(target=daemon.run)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), "run() should complete even when auth raises"
    # No sync occurred (empty services) -> no gmail chunk written.
    assert store.get_chunk("gmail-m1-body-0") is None


def test_run_resolves_services_at_startup_not_deferred(tmp_path, monkeypatch):
    """Fix 1 regression: run() must call ensure_services() once at startup,
    before the first loop iteration, so services are resolved even if the daemon
    starts paused or the first cycle is skipped.

    Mechanism: build_google_services is monkeypatched to a spy that records calls
    and returns {}. The daemon is constructed with services=None and stop() preset
    so run() executes exactly one bounded pass. After run() returns, the spy must
    have been called exactly once (at startup) and the loop must not have crashed.
    """
    import mcpbrain.auth as auth_module

    build_calls = []

    def spy_build():
        build_calls.append(True)
        return {}

    monkeypatch.setattr(auth_module, "build_google_services", spy_build)

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    daemon.stop()  # bound: run() acquires lock, calls ensure_services, then exits

    t = threading.Thread(target=daemon.run)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), "run() did not return promptly after stop()"
    assert len(build_calls) == 1, (
        "build_google_services must be called exactly once at run() startup "
        "(not deferred to first unpaused cycle)"
    )


# ---------------------------------------------------------------------------
# G4 — daemon CLI entry point (offline, tmp home, fake embedder)
# ---------------------------------------------------------------------------

def test_daemon_cli_once_runs_one_offline_cycle(tmp_path, monkeypatch, capsys):
    """daemon.main(["--once"]) wires a real embedder+store+enrich client and runs
    one cycle. Fully offline: get_embedder is a FakeEmbedder, the store is under a
    tmp MCPBRAIN_HOME, and auth.build_google_services returns {} (no token) so no
    sync happens. Must not crash and must complete a cycle."""
    import mcpbrain.embed as embed_module
    import mcpbrain.auth as auth_module

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(embed_module, "get_embedder", lambda kind=None: FakeEmbedder())
    monkeypatch.setattr(auth_module, "build_google_services", lambda: {})

    daemon_module.main(["--once"])

    out = capsys.readouterr().out
    assert "cycle:" in out, "the --once CLI should print the cycle result"


def test_status_includes_is_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    d = Daemon(store, emb, enrich_mode="off")
    s = d.status()
    assert "is_configured" in s
    assert isinstance(s["is_configured"], bool)


def test_status_includes_connections_block(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # Hermetic: probe_claude reads the real claude_desktop_config.json otherwise.
    from mcpbrain import probes
    monkeypatch.setattr(probes, "_claude_registered", lambda: False)
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    d = Daemon(store, emb, enrich_mode="off")
    st = d.status()
    assert "connections" in st
    assert set(st["connections"]) == {
        "google", "claude", "clickup", "backup", "records",
        "enrichment", "memory-hooks",
    }
    assert st["connections"]["claude"]["state"] == "not_started"  # no heartbeat yet
