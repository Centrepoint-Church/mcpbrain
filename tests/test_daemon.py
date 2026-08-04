"""Tests for the daemon orchestration loop and single-writer lock.

Reuses the fake Gmail service shape from test_sync_cycle.py and a tiny
FakeEmbedder (like test_index.py) so a cycle indexes without the bge download.
No real network or hanging timers: cycles are driven directly via run_one(),
and run() is bounded by stop()/events with a tiny interval.
"""

import base64
import json
import os
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
from mcpbrain.backup import generate_escrow_key

# Reuse the Drive fake shape from test_backup.py.
from tests.test_backup import FakeFiles, FakeService


@pytest.fixture(autouse=True)
def _isolate_app_home(tmp_path, monkeypatch):
    """Point MCPBRAIN_HOME at a per-test temp dir so no daemon test touches the
    developer's real ~/Library/Application Support/mcpbrain.

    Without this, daemon.run() startup (maybe_restore_on_first_run ->
    _backup_from_config) reads the real config; on a machine with a configured
    backup it restores the real 384-dim snapshot OVER the dim=4 test store, and
    the next embed pass fails with a vec dimension mismatch (384 vs 4). A clean
    CI box has no backup configured, so the leak is invisible there. Tests that
    set their own MCPBRAIN_HOME still win — their monkeypatch runs after this.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    yield


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


def test_run_cycle_drains_captures_even_when_the_shared_budget_is_already_spent(
        tmp_path, monkeypatch):
    """drain_captures must not starve on run_sync_cycle's leftover budget.

    Adversarial review finding: drain_captures used to be called with the
    SAME `budget` object run_sync_cycle had just spent -- on any cycle with a
    sync backlog (the live-store NORMAL case per the CYCLE_BUDGET_S incident;
    see its docstring) that budget is already expired by the time
    drain_captures runs, so it applies zero queued MCP-write-tool envelopes
    (brain_note/brain_decision/brain_memory_write/brain_action_create) for as
    long as the backlog persists -- silently breaking the "queued...within
    ~a minute" contract even though nothing is technically lost (the spool
    is durable). This drives run_cycle with an ALREADY-EXPIRED shared budget
    (Budget(deadline_s=0.0), simulating a cycle that just used its whole
    slice on sync) and asserts a pre-spooled capture is still applied --
    proving drain_captures now runs on its own independent budget
    (CAPTURES_BUDGET_S), not the spent one.
    """
    import json

    from mcpbrain.budget import Budget

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()

    inbox = tmp_path / "capture_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "cap-1.json").write_text(json.dumps({
        "kind": "ingest", "captured_at": "2026-06-04T12:00:00Z",
        "source": "code", "title": "T", "content": "C",
        "tags": "", "observation_type": "memory", "org": "",
    }))

    already_expired = Budget(deadline_s=0.0)
    result = run_cycle(store, emb, budget=already_expired)

    assert result["budget_spent"] is True, "the shared budget must indeed read as expired"
    assert not list(inbox.glob("cap-*.json")), "the queued capture must have been drained and deleted"
    with store._connect() as db:
        row = db.execute(
            "SELECT doc_id FROM chunks WHERE doc_id LIKE 'note-%'"
        ).fetchone()
    assert row is not None, "capture must be applied even though the shared cycle budget was already spent"


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


def test_run_one_builds_budget_from_cycle_budget_s_and_wires_on_progress(tmp_path, monkeypatch):
    """run_one() must actually CONSTRUCT `Budget(self._cycle_budget_s, ...)`
    and pass `on_progress=self._note_progress` through to run_cycle -- not
    just resolve/store the tuning value on the instance.
    test_daemon_cli_applies_tuning_config_overrides already pins that
    `_cycle_budget_s` LANDS on a constructed Daemon; nothing pins that
    run_one() actually USES it when building the cycle's Budget."""
    from mcpbrain.budget import Budget

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    daemon._cycle_budget_s = 12.5

    captured = {}

    def _fake_run_cycle(store_arg, embedder_arg, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(daemon_module, "run_cycle", _fake_run_cycle)

    daemon.run_one()

    budget = captured.get("budget")
    assert isinstance(budget, Budget), "run_one must pass a real Budget instance"
    assert budget._deadline_s == 12.5, "the Budget must be built from self._cycle_budget_s"
    assert captured.get("on_progress") == daemon._note_progress, (
        "on_progress must be wired to self._note_progress so run_cycle's "
        "on_progress('sync') call actually stamps the watchdog's clock")


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
    assert store.get_meta("enrich_mode") is None  # enrichment never called


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


def test_lock_acquire_retries_briefly_to_cover_a_handover(tmp_path):
    """The watchdog's unsupervised-Windows path spawns the successor BEFORE the
    parent has finished exiting, so a strictly non-blocking acquire kills the
    successor and leaves nothing running. acquire(timeout_s=...) waits out the
    handover; the default is still non-blocking."""
    lock_path = tmp_path / "daemon.lock"
    parent = SingleWriterLock(lock_path)
    parent.acquire()
    # The "parent" finishes exiting shortly after the successor starts.
    threading.Timer(0.3, parent.release).start()

    successor = SingleWriterLock(lock_path)
    started = time.monotonic()
    successor.acquire(timeout_s=3.0, interval_s=0.05)   # must NOT raise
    try:
        waited = time.monotonic() - started
        assert waited >= 0.2, "acquire returned before the parent released"
        assert waited < 3.0
    finally:
        successor.release()


def test_lock_acquire_still_raises_once_the_retry_window_expires(tmp_path):
    """Bounded, not a queue: a genuinely-running second daemon must still lose."""
    lock_path = tmp_path / "daemon.lock"
    held = SingleWriterLock(lock_path)
    held.acquire()
    try:
        other = SingleWriterLock(lock_path)
        started = time.monotonic()
        with pytest.raises(AlreadyRunningError):
            other.acquire(timeout_s=0.3, interval_s=0.05)
        assert time.monotonic() - started < 3.0
    finally:
        held.release()


def test_lock_acquire_is_non_blocking_by_default(tmp_path):
    lock_path = tmp_path / "daemon.lock"
    held = SingleWriterLock(lock_path)
    held.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(AlreadyRunningError):
            SingleWriterLock(lock_path).acquire()
        assert time.monotonic() - started < 0.2
    finally:
        held.release()


def test_lock_acquire_retry_loop_calls_acquire_once_repeatedly_until_success(
        tmp_path, monkeypatch):
    """Unit-level test of acquire()'s OWN retry loop (the while/deadline/sleep
    mechanics), independent of which platform backend `_acquire_once` uses --
    the real-thread tests above (test_lock_acquire_retries_briefly_to_cover_a_
    handover etc.) only exercise the retry loop indirectly through actual
    concurrent fcntl file locking on this (POSIX) box; nothing pins the loop's
    own attempt-count/backoff mechanics deterministically, independent of
    fcntl vs. msvcrt vs. any future backend.
    """
    lock = SingleWriterLock(tmp_path / "d.lock")
    attempts = []

    def _fake_once():
        attempts.append(1)
        if len(attempts) < 3:
            raise AlreadyRunningError("still held")
        # third attempt succeeds -- no exception, no return value needed

    monkeypatch.setattr(lock, "_acquire_once", _fake_once)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))

    lock.acquire(timeout_s=10.0, interval_s=0.05)  # must NOT raise

    assert len(attempts) == 3, "acquire must retry _acquire_once until it succeeds"
    assert sleeps == [0.05, 0.05], "must sleep interval_s between failed attempts"


def test_lock_acquire_retry_loop_reraises_once_the_deadline_is_exceeded(
        tmp_path, monkeypatch):
    """The other side of the same unit-level retry loop: once the fake clock
    (advanced only by the loop's own sleep calls, so this needs no real
    timing/threads) passes the deadline, the loop must give up and re-raise
    rather than retry forever."""
    lock = SingleWriterLock(tmp_path / "d.lock")
    calls = {"n": 0}
    now = [1000.0]

    def _fake_once():
        calls["n"] += 1
        raise AlreadyRunningError("still held")

    def _fake_sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(lock, "_acquire_once", _fake_once)
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", _fake_sleep)

    with pytest.raises(AlreadyRunningError):
        lock.acquire(timeout_s=0.2, interval_s=0.1)

    assert calls["n"] >= 2, "must have retried at least once before giving up"


def test_lock_acquire_retries_through_the_actual_msvcrt_branch(tmp_path, monkeypatch):
    """The Windows-specific `_acquire_once` branch (msvcrt) is `# pragma: no
    cover` on this POSIX box, but acquire()'s bounded retry (timeout_s=...)
    exists SPECIFICALLY for the Windows unsupervised-handover case (see
    acquire()'s own docstring) -- the two retry-loop tests just above mock
    `_acquire_once` entirely and so never actually enter this branch. Fakes
    just enough of msvcrt to drive the REAL branch end-to-end: forces fcntl
    off and msvcrt on, and makes `locking()` fail (as a real Windows
    LK_NBLCK byte-range-lock conflict would) for the first two attempts
    before succeeding on the third -- exercising both the w+b-create (no
    pre-existing lockfile, first attempt) and the r+b-reopen (lockfile
    already has the sentinel byte, later attempts) fallback paths, plus the
    real retry loop bridging the gap on this platform-specific code path.
    """
    monkeypatch.setattr(daemon_module, "fcntl", None)

    lock_calls = {"n": 0}
    seen_opens: list = []

    class _FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        def locking(self, fd, mode, nbytes):
            if mode == self.LK_NBLCK:
                lock_calls["n"] += 1
                if lock_calls["n"] < 3:
                    raise OSError("lock violation")
                return
            # LK_UNLCK (release): no-op for this fake.

    monkeypatch.setattr(daemon_module, "msvcrt", _FakeMsvcrt())

    lock_path = tmp_path / "d.lock"
    orig_open = open

    def _spy_open(path, mode="r", *a, **kw):
        if path == lock_path:
            seen_opens.append(mode)
        return orig_open(path, mode, *a, **kw)

    monkeypatch.setattr("builtins.open", _spy_open)

    lock = SingleWriterLock(lock_path)
    lock.acquire(timeout_s=5.0, interval_s=0.05)   # must NOT raise
    try:
        assert lock_calls["n"] == 3, (
            "expected the msvcrt branch's retry loop to keep calling "
            "locking() until it succeeded"
        )
        assert "w+b" in seen_opens, (
            "first attempt (no pre-existing lockfile) must create one and "
            "write the sentinel byte"
        )
        assert "r+b" in seen_opens, (
            "later attempts must reopen the now-existing lockfile rather "
            "than recreating it"
        )
    finally:
        lock.release()


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


def test_failed_backup_backs_off_for_the_full_interval(tmp_path):
    """A FAILED backup must wait interval_s before retrying, not retry at once.

    Regression (live incident 2026-08-04): the cadence clock advanced only on
    SUCCESS, so a backup that could never succeed was "due" on every cycle. The
    daemon re-snapshotted the 11.9GB store every ~60s -- filling /var/folders
    until ENOSPC, exhausting swap, and wedging the cycle thread so no heartbeat
    or maintenance pass ran for an hour. Backing off on ATTEMPT bounds the cost
    of a persistently broken backup to one attempt per interval.
    """
    store = _store_with_chunk(tmp_path)
    files = _RaisingFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    clock = _Clock()
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=100.0, clock=clock)

    first = daemon.maybe_backup()
    assert first is not None and first["backed_up"] is False

    # Well inside the interval: the failed attempt must NOT be retried.
    clock.advance(50.0)
    assert daemon.maybe_backup() is None, "failed backup hot-looped instead of backing off"

    # Past the interval: it is due again.
    clock.advance(60.0)  # total 110 >= 100
    files.heal()
    retried = daemon.maybe_backup()
    assert retried is not None and retried["backed_up"] is True


def test_slow_successful_backup_is_spaced_from_completion(tmp_path, monkeypatch):
    """A backup slower than the interval must NOT restart the instant it ends.

    The cadence clock was stamped only at the START of a run, so effective
    spacing was max(interval, duration) measured from start — i.e. `interval`
    from START, not from COMPLETION. DEFAULT_BACKUP_INTERVAL_S is 3600 and
    backup_setup writes interval_s: 3600 for new installs, while the live store
    is ~11.9GB (~4.2GB artifact). Any install whose snapshot+upload exceeds an
    hour saw elapsed > interval the moment it finished and immediately started
    again: back-to-back multi-GB uploads, continuously holding the bulk lock.
    The start stamp still has to stay (it is what bounds a FAILING backup to one
    attempt per interval) — success must ADDITIONALLY re-stamp.
    """
    from mcpbrain import daemon as dm

    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    clock = _Clock()
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=100.0, clock=clock)

    real_upload = dm.upload_snapshot

    def _slow_upload(*a, **kw):
        clock.advance(250.0)  # the upload alone outlasts the interval 2.5x
        return real_upload(*a, **kw)

    monkeypatch.setattr(dm, "upload_snapshot", _slow_upload)

    first = daemon.maybe_backup()
    assert first is not None and first["backed_up"] is True

    def _file_create_count():
        return len([c for c in files.create_calls
                    if c["body"].get("mimeType") != FakeFiles.FOLDER_MIME])

    assert _file_create_count() == 1

    # No time has passed since the backup COMPLETED, so it is not due.
    assert daemon.maybe_backup() is None, (
        "a slow backup restarted immediately — spacing measured from start, "
        "not completion")
    assert _file_create_count() == 1

    # A full interval after completion, it is due again.
    clock.advance(100.0)
    again = daemon.maybe_backup()
    assert again is not None and again["backed_up"] is True


def _backup_state(home):
    from pathlib import Path
    p = Path(home) / "backup_state.json"
    return json.loads(p.read_text()) if p.exists() else None


# ---------------------------------------------------------------------------
# the backup cadence must survive a restart
#
# _last_backup lived only in memory, so every daemon start looked like a first
# run and triggered a full snapshot+upload regardless of interval_s. On
# 2026-08-04 that meant 9 starts and 12.75GB uploaded where the configured
# daily cadence calls for 4.25GB -- plus three more attempts that copied and
# encrypted the whole 11.9GB store before failing at the upload.
# ---------------------------------------------------------------------------


def test_recent_recorded_attempt_is_not_due_after_a_restart(tmp_path):
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=3600.0, clock=_Clock(),
                    last_backup_attempt_epoch=time.time() - 60)  # 1 min ago

    assert daemon.maybe_backup() is None, "a restart re-armed a full backup"


def test_stale_recorded_attempt_is_still_due_after_a_restart(tmp_path):
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=3600.0, clock=_Clock(),
                    last_backup_attempt_epoch=time.time() - 7200)  # 2h ago

    summary = daemon.maybe_backup()
    assert summary is not None and summary["backed_up"] is True


def test_no_recorded_attempt_still_backs_up_immediately(tmp_path):
    """A fresh install must not wait a whole interval for its first backup."""
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=3600.0, clock=_Clock(),
                    last_backup_attempt_epoch=None)

    summary = daemon.maybe_backup()
    assert summary is not None and summary["backed_up"] is True


def test_recorded_attempt_in_the_future_does_not_wedge_the_cadence(tmp_path):
    """Clock skew must not park backups forever.

    The persisted stamp is wall-clock while _last_backup is monotonic; a
    backwards system-clock adjustment can make the recorded attempt look like
    it is in the future. Clamp rather than compute a negative elapsed.
    """
    store = _store_with_chunk(tmp_path)
    files = FakeFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=3600.0, clock=_Clock(),
                    last_backup_attempt_epoch=time.time() + 86400)

    # Treated as "just attempted": not due now, and never further than one
    # interval away.
    assert daemon.maybe_backup() is None


def test_last_backup_attempt_epoch_reads_the_state_file(tmp_path):
    """The wiring helper the daemon entry point uses to seed the cadence."""
    from mcpbrain.daemon import last_backup_attempt_epoch

    assert last_backup_attempt_epoch(str(tmp_path)) is None  # no file yet

    (tmp_path / "backup_state.json").write_text(json.dumps({
        "last_attempt": 1785817648.95, "last_success": 1785817648.95,
        "consecutive_failures": 0, "last_error": None}))
    assert last_backup_attempt_epoch(str(tmp_path)) == 1785817648.95

    (tmp_path / "backup_state.json").write_text("{not json")
    assert last_backup_attempt_epoch(str(tmp_path)) is None


def test_maybe_backup_records_a_failed_upload(tmp_path, monkeypatch):
    """A failing backup must leave a durable, countable trace.

    Nothing recorded upload outcomes, so repeated failure was invisible to
    doctor/status (probe_backup only saw a freshly-written local snapshot.enc,
    which a FAILED run leaves behind too). The consecutive count is what makes
    a failure storm legible -- 57 uploads failed on 2026-08-03 and the status
    line read "Backup: On" throughout.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _store_with_chunk(tmp_path)
    cfg = _backup_config(tmp_path, _RaisingFiles(list_response={"files": []}))
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=0.0, clock=_Clock())

    daemon.maybe_backup()
    daemon.maybe_backup()

    state = _backup_state(tmp_path)
    assert state is not None, "no backup_state.json written"
    assert state["consecutive_failures"] == 2
    assert "simulated Drive error" in state["last_error"]


def test_maybe_backup_records_success_and_clears_the_failure_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _store_with_chunk(tmp_path)
    files = _RaisingFiles(list_response={"files": []})
    cfg = _backup_config(tmp_path, files)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=0.0, clock=_Clock())

    daemon.maybe_backup()
    assert _backup_state(tmp_path)["consecutive_failures"] == 1

    files.heal()
    daemon.maybe_backup()

    state = _backup_state(tmp_path)
    assert state["consecutive_failures"] == 0
    assert state["last_success"] is not None


def test_backup_artifact_decrypts_to_a_valid_store(tmp_path, monkeypatch):
    # Isolate the home so the bundle reflects this test's data, not the dev box.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
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

    # Recover via the real restore path — handles either a bare-store or a
    # bundled (store + records + config) artifact transparently.
    from mcpbrain import backup as _bk
    _bk.restore(cfg.out_path, tmp_path / "restored.sqlite3", key)
    loaded = Store(tmp_path / "restored.sqlite3", dim=4)
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


def test_run_loop_holds_the_bulk_lock_across_the_backup(tmp_path):
    """backup.snapshot() runs PRAGMA wal_checkpoint(TRUNCATE) and aborts with
    RuntimeError on a busy checkpoint — its docstring rests on a single-writer
    invariant that the maintenance thread removed. A racing chunk-writing pass
    either silently stops backups advancing (_last_backup never moves; only
    discovered during a restore) or writes enough during the subsequent copy2 to
    trip wal_autocheckpoint and tear the snapshot. maybe_backup must therefore be
    held under _bulk_lock, exactly as run_one() already is."""
    store = _make_store(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={}, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    locked_during_backup = []
    daemon.maybe_backup = lambda: locked_during_backup.append(daemon._bulk_lock.locked())

    t = threading.Thread(target=daemon.run)
    t.start()
    deadline = time.monotonic() + 5.0
    while not locked_during_backup and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    daemon.stop()
    t.join(timeout=5.0)

    assert locked_during_backup, "maybe_backup was never called from the loop"
    assert all(locked_during_backup), "maybe_backup ran without holding _bulk_lock"


def test_backup_skips_rather_than_blocking_when_bulk_lock_is_held(tmp_path):
    """The other side of test_run_loop_holds_the_bulk_lock_across_the_backup: the
    cycle thread's acquire of _bulk_lock around maybe_backup() must be BOUNDED,
    the same shape (BULK_LOCK_ACQUIRE_S) as the four gated maintenance passes'
    own acquire on the other side of this lock (see
    test_maintenance_scheduler.test_dispatch_skips_a_lock_gated_pass_rather_than_blocking_forever).

    A gated pass's own execution time is not bounded by that plan -- only its
    acquire is -- so a pass like _run_salience_score's `while rounds < 500` loop
    or stale_reextract's network-touching sweep can legitimately hold
    _bulk_lock past one maintenance tick. Before this fix, run()'s backup call
    did a plain `with self._bulk_lock:`, which would park the cycle thread for
    that pass's whole duration -- a new, narrower echo of the same
    unbounded-lock-blocks-the-other-side problem the bounded gated-pass acquire
    already solved. Real thread, real lock -- no mocked locks.
    """
    store = _make_store(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={}, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    daemon._bulk_lock_wait_s = 0.15
    backup_calls = []
    daemon.maybe_backup = lambda: backup_calls.append(1)
    daemon._last_backup = None  # sentinel: a skip must not falsely advance this

    release_lock = threading.Event()
    acquired = threading.Event()

    def _hold():
        with daemon._bulk_lock:
            acquired.set()
            release_lock.wait(timeout=5.0)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert acquired.wait(timeout=2.0), "lock holder never acquired _bulk_lock"

    try:
        started = time.monotonic()
        daemon._backup_under_bulk_lock()  # must RETURN promptly, lock still held
        elapsed = time.monotonic() - started
    finally:
        release_lock.set()
        holder.join(timeout=2.0)

    assert not holder.is_alive()
    assert elapsed < 2.0, f"backup blocked {elapsed:.1f}s on a held bulk lock"
    assert backup_calls == [], "maybe_backup ran while the bulk lock was held elsewhere"
    assert daemon._last_backup is None, "a skipped backup must not advance _last_backup"


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


def test_status_includes_org_block(tmp_path, monkeypatch):
    """status() surfaces cache hit/miss counts + curator queue depth (spec
    Task 5, observability) so /api/status exposes them without needing direct
    store access. Must degrade gracefully but here we seed real data and
    expect real counts."""
    from mcpbrain import org_curate

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    store.set_meta("org_curator_version", "3")
    with store._connect() as db:
        db.execute(
            "INSERT INTO org_contrib_staging(contributor_email, source_ref, claim) "
            "VALUES ('a@x.org', 'ref1', 'claim1')")
    org_curate._suppress_pair(store, "a|b")

    emb = FakeEmbedder()
    d = Daemon(store, emb, enrich_mode="off")
    st = d.status()
    assert st["org"]["curator_version"] == 3
    assert st["org"]["contrib_staged"] == 1
    assert st["org"]["merge_suppressed"] == 1
    assert "cache_hits" in st["org"] and "cache_misses" in st["org"]


def test_status_cache_counts_reset_when_shared_drive_block_absent(tmp_path, monkeypatch):
    """A cycle whose result has no "shared_drive_cache" key (fleet unpinned,
    a Drive-API outage caught by the block's own try/except, or
    drive_service/home not both present) must reset the counters to 0/0, not
    leave the previous cycle's numbers stale in status() -- stale non-zero
    counts would mask an ongoing shared-drive sync failure as healthy."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, enrich_mode="off",
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    # First cycle: shared-drive cache block ran and reported activity.
    monkeypatch.setattr(
        daemon_module, "run_cycle",
        lambda *a, **k: {"shared_drive_cache": {"hits": 7, "misses": 3}})
    daemon.run_one()
    st = daemon.status()
    assert st["org"]["cache_hits"] == 7
    assert st["org"]["cache_misses"] == 3

    # Second cycle: shared-drive cache block did not run this time (key
    # absent). status() must report 0/0, not the stale 7/3 from before.
    monkeypatch.setattr(daemon_module, "run_cycle", lambda *a, **k: {})
    daemon.run_one()
    st = daemon.status()
    assert st["org"]["cache_hits"] == 0
    assert st["org"]["cache_misses"] == 0


def test_maybe_resolve_does_not_exist():
    import inspect
    from mcpbrain.daemon import Daemon
    assert not hasattr(Daemon, "maybe_resolve")
    assert not hasattr(Daemon, "_resolve_due")
    sig = inspect.signature(Daemon.__init__)
    assert "resolve_interval_s" not in sig.parameters


def test_status_includes_connections_block(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    d = Daemon(store, emb, enrich_mode="off")
    st = d.status()
    assert "connections" in st
    assert set(st["connections"]) == {
        "google", "claude", "backup", "records", "enrichment",
    }
    assert st["connections"]["claude"]["state"] == "not_started"  # no heartbeat yet


# ---------------------------------------------------------------------------
# Task 7: config-overridable tuning constants + startup snapshot-orphan sweep
# ---------------------------------------------------------------------------

def test_tuning_from_config_overrides_and_falls_back(tmp_path):
    """config['tuning'] overrides daemon.py's own module-constant defaults;
    an absent key keeps its default and an invalid value (wrong type, or
    <= 0) is dropped back to the default rather than applied or disabling
    anything -- unlike cadences, none of these has an OFF meaning."""
    (tmp_path / "config.json").write_text(json.dumps({
        "tuning": {
            "cycle_budget_s": 12.5,
            "watchdog_max_exits": 7,
            "stall_s": -5,       # invalid: must be positive -> falls back
            "bulk_lock_yield_s": "not-a-number",  # invalid -> falls back
        }
    }))
    tuning = daemon_module._tuning_from_config(str(tmp_path))
    assert tuning["cycle_budget_s"] == 12.5
    assert tuning["watchdog_max_exits"] == 7
    assert isinstance(tuning["watchdog_max_exits"], int)
    # Invalid overrides fall back to the real module constants, not None.
    assert tuning["stall_s"] == daemon_module.STALL_S
    assert tuning["bulk_lock_yield_s"] == daemon_module.BULK_LOCK_YIELD_S
    # Absent keys keep their module-constant default.
    assert tuning["maintenance_tick_s"] == daemon_module.MAINTENANCE_TICK_S
    assert tuning["embed_max_items"] == 2000


def test_tuning_from_config_defaults_match_module_constants_when_unset(tmp_path):
    """A fresh install (no config.json at all) must behave exactly as before
    Task 7 -- every tuning value resolves to its pre-existing module constant."""
    tuning = daemon_module._tuning_from_config(str(tmp_path))
    assert tuning == {
        "cycle_budget_s": daemon_module.CYCLE_BUDGET_S,
        "maintenance_tick_s": daemon_module.MAINTENANCE_TICK_S,
        "stall_s": daemon_module.STALL_S,
        "bulk_lock_acquire_s": daemon_module.BULK_LOCK_ACQUIRE_S,
        "bulk_lock_yield_s": daemon_module.BULK_LOCK_YIELD_S,
        "watchdog_max_exits": daemon_module.WATCHDOG_MAX_EXITS,
        "watchdog_window_s": daemon_module.WATCHDOG_WINDOW_S,
        "handover_lock_wait_s": daemon_module.HANDOVER_LOCK_WAIT_S,
        "embed_max_items": 2000,
    }


def test_daemon_cli_applies_tuning_config_overrides(tmp_path, monkeypatch):
    """main() must not just COMPUTE the tuning dict and discard it -- the
    values must actually land on the constructed Daemon instance. Regression
    guard for the known failure shape this plan's reviews keep finding: a
    config read that looks wired but only reaches some (or none) of the call
    sites."""
    import mcpbrain.embed as embed_module
    import mcpbrain.auth as auth_module

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "tuning": {
            "cycle_budget_s": 12.5,
            "watchdog_max_exits": 7,
            "bulk_lock_acquire_s": 9.0,
            "bulk_lock_yield_s": 0.75,
            "maintenance_tick_s": 45.0,
            "stall_s": 900.0,
            "watchdog_window_s": 1800.0,
            "embed_max_items": 42,
        }
    }))
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(embed_module, "get_embedder", lambda kind=None: FakeEmbedder())
    monkeypatch.setattr(auth_module, "build_google_services", lambda: {})

    captured = {}
    real_daemon_cls = daemon_module.Daemon

    class SpyDaemon(real_daemon_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["instance"] = self

    monkeypatch.setattr(daemon_module, "Daemon", SpyDaemon)
    daemon_module.main(["--once"])

    d = captured["instance"]
    assert d._cycle_budget_s == 12.5
    assert d._watchdog_max_exits == 7
    assert d._bulk_lock_wait_s == 9.0
    assert d._bulk_lock_yield_s == 0.75
    assert d._maintenance_interval_s == 45.0
    assert d._stall_s == 900.0
    assert d._watchdog_window_s == 1800.0
    assert d._embed_max_items == 42


def test_run_sweeps_orphan_snapshot_dirs_at_startup(tmp_path, monkeypatch):
    """run() must sweep stale mcpbrain-snap-* work dirs from the OS temp dir
    (tempfile.gettempdir(), NOT $HOME -- /var/folders/... on macOS, which is
    where the live ~24GB was actually found) before entering the loop.
    make_encrypted_snapshot's own cleanup runs in a `finally` that cannot fire
    when the watchdog os._exits mid-snapshot, so these orphans never clean up
    on their own."""
    fake_tmp = tmp_path / "os_tmp"
    fake_tmp.mkdir()
    orphan = fake_tmp / "mcpbrain-snap-old"
    orphan.mkdir()
    (orphan / "part.bin").write_bytes(b"x" * 16)
    old = time.time() - 999_999
    os.utime(orphan, (old, old))
    monkeypatch.setattr(daemon_module.tempfile, "gettempdir", lambda: str(fake_tmp))

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    daemon = Daemon(store, emb, lock=SingleWriterLock(tmp_path / "d.lock"))
    daemon.stop()  # preset so run() does startup work, then exits promptly

    daemon.run()

    assert not orphan.exists(), "orphaned snapshot temp dir should be swept at startup"


def test_backup_under_bulk_lock_resweeps_orphan_snapshots_every_cycle(tmp_path, monkeypatch):
    """Review fix: the startup-only sweep can't clean the very orphan it exists
    for -- a watchdog os._exit mid-snapshot triggers an IMMEDIATE restart, so
    the fresh orphan is only minutes old (younger than SNAPSHOT_ORPHAN_MAX_AGE_S)
    when the successor's startup sweep runs, and would otherwise survive that
    successor's entire lifetime since the startup sweep never runs again.
    _backup_under_bulk_lock must re-run the same sweep every loop iteration
    (still cycle-thread, still under _bulk_lock, so no snapshot can be
    in-flight) so an old-enough orphan is caught well within a day."""
    fake_tmp = tmp_path / "os_tmp"
    fake_tmp.mkdir()
    orphan = fake_tmp / "mcpbrain-snap-old"
    orphan.mkdir()
    (orphan / "part.bin").write_bytes(b"x" * 16)
    old = time.time() - 999_999
    os.utime(orphan, (old, old))
    monkeypatch.setattr(daemon_module.tempfile, "gettempdir", lambda: str(fake_tmp))

    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    # No backup configured (backup=None): maybe_backup() is a no-op, proving
    # the sweep runs regardless of whether a backup actually happened this
    # cycle, not only as a side effect of a successful snapshot.
    daemon = Daemon(store, emb, lock=SingleWriterLock(tmp_path / "d.lock"))

    daemon._backup_under_bulk_lock()

    assert not orphan.exists(), (
        "orphaned snapshot temp dir should be re-swept every "
        "_backup_under_bulk_lock call, not just once at startup"
    )
