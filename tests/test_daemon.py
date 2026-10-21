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
    def __init__(self, result, executes=None):
        self._r = result
        self.executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self.executes.append(num_retries)
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

    assert res["discovered"]["gmail"] >= 1
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
    assert res["discovered"]["gmail"] >= 1
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
    assert res["discovered"]["gmail"] >= 1
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
# incremental backup in the loop: capture (maybe_backup) + upload step
# (upload_backup_step). Capture writes page deltas into the local outbox;
# the upload ships them to Drive outside the bulk lock. Every test here runs
# with MCPBRAIN_HOME = a tmp dir (the `bk` fixture), a real tiny Store, the
# real backup_outbox.capture, and the uploader's fake Drive + transport.
# ---------------------------------------------------------------------------

from dataclasses import replace  # noqa: E402
from pathlib import Path  # noqa: E402

from mcpbrain import backup_outbox as _bo  # noqa: E402
from mcpbrain import backup_uploader as _bu  # noqa: E402
from tests.test_backup_uploader import Server as _UpServer, Service as _UpService  # noqa: E402
from tests.test_drive_resumable import FakeTransport as _UpTransport  # noqa: E402

# The real capture, held before any test patches backup_outbox.capture
# (daemon_module.backup_outbox IS _bo, so a patch there replaces _bo.capture).
_REAL_CAPTURE = _bo.capture


SQLITE_MAGIC = b"SQLite format 3\x00"


class _Clock:
    """A list-controlled monotonic clock for deterministic 'due' checks."""

    def __init__(self, value=0.0):
        self._value = value

    def __call__(self):
        return self._value

    def advance(self, by):
        self._value += by


@pytest.fixture
def bk(tmp_path, monkeypatch):
    """Isolated home for backup tests: backup_state.json, the outbox and the
    journal all land under tmp_path/home, never the real app dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    from mcpbrain.config import write_config
    write_config(str(home), {
        "owner_email": "owner@example.org",
        "backup": {"shared_drive_id": "0Adrive", "user_id": "owner"},
        "fleet": {"escrow_folder_id": "ESC"},
    })
    # Never consult pmset from a test unless the test opts in.
    monkeypatch.setattr(daemon_module, "_mac_user_is_active", lambda: True)
    return home


def _store_with_chunk(tmp_path, name="backup.sqlite3"):
    store = Store(tmp_path / name, dim=4)
    store.init()
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    return store


def _backup_config(server=None, *, key=None, settings=None, transport="fake"):
    server = server or _UpServer()
    if transport == "fake":
        transport = _UpTransport(server)
    return BackupConfig(
        key=key or generate_escrow_key(),
        drive_service=_UpService(server),
        shared_drive_id="0Adrive",
        user_id="owner",
        settings=settings or _bo.Settings(),
        transport=transport,
    )


def _daemon(tmp_path, cfg, *, interval=3600.0, clock=None, wall=None, store=None, **kw):
    store = store or _store_with_chunk(tmp_path)
    extra = {"wall_clock": wall} if wall is not None else {}
    return Daemon(store, FakeEmbedder(), services={},
                  lock=SingleWriterLock(tmp_path / "d.lock"),
                  backup=cfg, backup_interval_s=interval,
                  clock=clock or _Clock(), **extra, **kw)


def _spy_capture(monkeypatch):
    """Wrap the real backup_outbox.capture, recording each call's kwargs."""
    calls = []
    real = _REAL_CAPTURE

    def spy(*a, **kw):
        calls.append(kw)
        return real(*a, **kw)
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", spy)
    return calls


class _FlakyCapture:
    """capture() stand-in that raises until heal(), then runs the real one."""

    def __init__(self, exc=None):
        self.exc = exc or OSError("simulated disk error")
        self.healed = False
        self.calls = 0

    def heal(self):
        self.healed = True

    def __call__(self, *a, **kw):
        self.calls += 1
        if not self.healed:
            raise self.exc
        return _REAL_CAPTURE(*a, **kw)


def _attempted(result):
    """A capture ran to completion (an item, or nothing changed)."""
    return (result is not None and "error" not in result
            and "skipped" not in result and "deferred" not in result)


def _backup_state(home):
    p = Path(home) / "backup_state.json"
    return json.loads(p.read_text()) if p.exists() else None


def test_unconfigured_daemon_never_backs_up(tmp_path, bk):
    store = _store_with_chunk(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    assert daemon.maybe_backup() is None
    assert daemon.upload_backup_step() is None
    assert _bo.pending_items(bk) == []


def test_first_capture_is_a_base_item_in_the_outbox_not_an_upload(tmp_path, bk):
    server = _UpServer()
    cfg = _backup_config(server)
    daemon = _daemon(tmp_path, cfg)

    summary = daemon.maybe_backup()

    assert summary["captured"] is True and summary["item"]
    [item] = _bo.pending_items(bk)
    assert item.kind == "base" and item.name == summary["item"]
    # Capture is local: nothing went to Drive, no snapshot.enc was written.
    assert server.files == {} and server.sessions == {}
    assert not (bk / "snapshot.enc").exists()
    # The outbox is ciphertext, not a plaintext store.
    for f in item.files():
        assert not (_bo.outbox_dir(bk) / f).read_bytes().startswith(SQLITE_MAGIC)
    # A capture alone is not a backup that landed.
    assert _backup_state(bk) is None


def test_capture_then_upload_step_commits_and_records_restore_point(tmp_path, bk):
    server = _UpServer()
    cfg = _backup_config(server)
    daemon = _daemon(tmp_path, cfg, wall=_Clock(1_790_000_000.0))

    item = daemon.maybe_backup()["item"]
    step = daemon.upload_backup_step()

    assert step["error"] is None and step["committed"] == [item]
    assert step["pending"] == 0
    state = _backup_state(bk)
    assert state["consecutive_failures"] == 0 and state["last_success"]
    assert state["restore_point"] == 1_790_000_000.0
    assert any(n.startswith("manifest-") for n in
               (m["name"] for m in cfg.drive_service._all().values()))
    assert daemon.status()["backup"]["restore_point"] == 1_790_000_000.0


def test_unchanged_store_captures_nothing(tmp_path, bk):
    cfg = _backup_config()
    daemon = _daemon(tmp_path, cfg, interval=0.0)
    assert daemon.maybe_backup()["captured"] is True
    again = daemon.maybe_backup()
    assert _attempted(again) and again == {"captured": False, "item": None}
    assert len(_bo.pending_items(bk)) == 1




def test_mac_user_is_active_parses_pmset_assertions(monkeypatch):
    """_mac_user_is_active reads the 'UserIsActive' line from pmset -g
    assertions' system-wide summary block -- 1 during a genuine wake, 0
    during a dark-wake maintenance cycle. This is the Apple-Silicon-compatible
    signal: IODisplayWrangler (the older technique) does not exist on this
    hardware's IORegistry at all."""
    import subprocess

    class _Result:
        def __init__(self, stdout): self.returncode = 0; self.stdout = stdout

    active_output = (
        "2026-09-08 11:05:48 +0800 \n"
        "Assertion status system-wide:\n"
        "   BackgroundTask                 0\n"
        "   UserIsActive                   1\n"
        "   PreventUserIdleSystemSleep     1\n"
        "Listed by owning process:\n")
    dark_output = active_output.replace("UserIsActive                   1",
                                        "UserIsActive                   0")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(active_output))
    assert daemon_module._mac_user_is_active() is True

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Result(dark_output))
    assert daemon_module._mac_user_is_active() is False


def test_mac_user_is_active_fails_open_on_error(monkeypatch):
    """A broken check must never PERMANENTLY block backups -- fail open
    (report active) on any subprocess error, missing pmset, or unparseable
    output, rather than fail closed (which could mean silently never backing
    up again on some future macOS whose pmset output shape changed)."""
    import subprocess

    def _raise(*a, **k):
        raise FileNotFoundError("no such file: pmset")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert daemon_module._mac_user_is_active() is True

    class _BadResult:
        returncode = 0
        stdout = "nothing resembling the expected format\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _BadResult())
    assert daemon_module._mac_user_is_active() is True



def test_maybe_backup_defers_without_consuming_cadence_when_dark_waking(
        tmp_path, bk, monkeypatch):
    """A DUE capture on a dark-waking Mac defers rather than attempts, and
    does NOT stamp _last_backup, so the very next check (not the next full
    interval) retries once the lid is really open."""
    calls = _spy_capture(monkeypatch)
    daemon = _daemon(tmp_path, _backup_config(), interval=100.0)
    monkeypatch.setattr(daemon_module.sys, "platform", "darwin")
    monkeypatch.setattr(daemon_module, "_mac_user_is_active", lambda: False)

    result = daemon.maybe_backup()

    assert result == {"captured": False, "deferred": True}
    assert calls == [], "a dark-waking Mac must never attempt the capture"

    monkeypatch.setattr(daemon_module, "_mac_user_is_active", lambda: True)
    assert _attempted(daemon.maybe_backup())
    assert len(calls) == 1


def test_maybe_backup_ignores_the_check_on_non_mac(tmp_path, bk, monkeypatch):
    daemon = _daemon(tmp_path, _backup_config())
    monkeypatch.setattr(daemon_module.sys, "platform", "win32")
    monkeypatch.setattr(daemon_module, "_mac_user_is_active", lambda: False)
    assert daemon.maybe_backup()["captured"] is True


def test_not_due_skips_second_capture_then_due_captures_again(tmp_path, bk, monkeypatch):
    calls = _spy_capture(monkeypatch)
    clock = _Clock()
    daemon = _daemon(tmp_path, _backup_config(), interval=100.0, clock=clock,
                     wall=_Clock(1_790_000_000.0))

    assert _attempted(daemon.maybe_backup())
    assert len(calls) == 1

    clock.advance(50.0)
    assert daemon.maybe_backup() is None
    assert len(calls) == 1

    clock.advance(60.0)  # total 110 >= 100
    assert _attempted(daemon.maybe_backup())
    assert len(calls) == 2


def test_capture_interval_comes_from_settings(tmp_path, bk, monkeypatch):
    """`interval_s` no longer drives capture; `capture_interval_s` does."""
    from mcpbrain.config import write_config
    write_config(str(bk), {"backup": {
        "escrow_key": "k" * 44, "shared_drive_id": "0Adrive", "user_id": "owner",
        "interval_s": 86400, "capture_interval_s": 1800}})
    monkeypatch.setattr(daemon_module.auth, "build_google_services",
                        lambda **kw: {"drive_service": "DRIVE"})
    bc, interval = daemon_module._backup_from_config(str(bk))
    assert interval == 1800.0 and bc.settings.capture_interval_s == 1800.0


def test_capture_failure_does_not_crash_and_loop_continues(tmp_path, bk, monkeypatch):
    flaky = _FlakyCapture()
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", flaky)
    daemon = _daemon(tmp_path, _backup_config(), interval=0.0)

    failed = daemon.maybe_backup()
    assert failed["captured"] is False and "simulated disk error" in failed["error"]

    flaky.heal()
    ok = daemon.maybe_backup()  # interval 0 -> always due
    assert ok["captured"] is True


def test_failed_capture_backs_off_for_the_full_interval(tmp_path, bk, monkeypatch):
    """A FAILED capture waits before retrying, not every cycle (2026-08-04:
    a backup that could never succeed was "due" on every cycle and filled the
    disk). The interval here is under BACKUP_RETRY_BASE_S, so it binds."""
    flaky = _FlakyCapture()
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", flaky)
    clock = _Clock()
    daemon = _daemon(tmp_path, _backup_config(), interval=100.0, clock=clock,
                     wall=_Clock(1_790_000_000.0))

    assert daemon.maybe_backup()["captured"] is False
    clock.advance(50.0)
    assert daemon.maybe_backup() is None, "failed capture hot-looped instead of backing off"
    clock.advance(60.0)
    flaky.heal()
    assert daemon.maybe_backup()["captured"] is True


def test_slow_capture_is_spaced_from_completion(tmp_path, bk, monkeypatch):
    clock = _Clock()

    def slow(*a, **kw):
        clock.advance(250.0)
        return _REAL_CAPTURE(*a, **kw)
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", slow)
    daemon = _daemon(tmp_path, _backup_config(), interval=100.0, clock=clock,
                     wall=_Clock(1_790_000_000.0))

    assert _attempted(daemon.maybe_backup())
    assert daemon.maybe_backup() is None, "spacing measured from start, not completion"
    clock.advance(100.0)
    assert _attempted(daemon.maybe_backup())


# ---------------------------------------------------------------------------
# the capture cadence survives a restart and counts sleep
# ---------------------------------------------------------------------------


def test_recent_recorded_attempt_is_not_due_after_a_restart(tmp_path, bk):
    daemon = _daemon(tmp_path, _backup_config(),
                     last_backup_attempt_epoch=time.time() - 60)
    assert daemon.maybe_backup() is None, "a restart re-armed a capture"


def test_stale_recorded_attempt_is_still_due_after_a_restart(tmp_path, bk):
    daemon = _daemon(tmp_path, _backup_config(),
                     last_backup_attempt_epoch=time.time() - 7200)
    assert daemon.maybe_backup()["captured"] is True


def test_no_recorded_attempt_still_captures_immediately(tmp_path, bk):
    daemon = _daemon(tmp_path, _backup_config(), last_backup_attempt_epoch=None)
    assert daemon.maybe_backup()["captured"] is True


def test_recorded_attempt_in_the_future_does_not_wedge_the_cadence(tmp_path, bk):
    daemon = _daemon(tmp_path, _backup_config(),
                     last_backup_attempt_epoch=time.time() + 86400)
    assert daemon.maybe_backup() is None


def test_backup_due_after_sleep_when_monotonic_clock_is_frozen(tmp_path, bk):
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, _backup_config(), interval=86400.0, clock=mono,
                     wall=wall)
    assert _attempted(daemon.maybe_backup())
    assert daemon.maybe_backup() is None
    wall.advance(25 * 3600)  # asleep: monotonic frozen
    assert _attempted(daemon.maybe_backup()), "a slept day did not count"


def test_backup_due_after_sleep_when_seeded_from_persisted_attempt(tmp_path, bk):
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, _backup_config(), interval=86400.0, clock=mono,
                     wall=wall, last_backup_attempt_epoch=wall() - 3600)
    assert daemon.maybe_backup() is None
    wall.advance(24 * 3600)
    assert _attempted(daemon.maybe_backup())


def test_backwards_wall_clock_jump_does_not_suppress_a_due_backup(tmp_path, bk):
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, _backup_config(), interval=86400.0, clock=mono,
                     wall=wall)
    assert _attempted(daemon.maybe_backup())
    mono.advance(25 * 3600)
    wall.advance(25 * 3600 - 2 * 86400)
    assert _attempted(daemon.maybe_backup()), (
        "a backwards wall-clock jump suppressed a capture monotonic says is due")


def test_failed_attempt_retries_after_the_failure_backoff_not_the_interval(
        tmp_path, bk, monkeypatch):
    """A failure backs off BACKUP_RETRY_BASE_S, doubling per consecutive
    failure, never past the interval."""
    from mcpbrain.daemon import BACKUP_RETRY_BASE_S
    flaky = _FlakyCapture()
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", flaky)
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, _backup_config(), interval=86400.0, clock=mono,
                     wall=wall)

    def tick(s):
        mono.advance(s)
        wall.advance(s)
    assert daemon.maybe_backup()["captured"] is False
    tick(BACKUP_RETRY_BASE_S - 1)
    assert daemon.maybe_backup() is None, "still backing off"
    tick(1)
    assert daemon.maybe_backup()["captured"] is False, "retried after the backoff"
    tick(BACKUP_RETRY_BASE_S)
    assert daemon.maybe_backup() is None   # second failure: doubled
    tick(BACKUP_RETRY_BASE_S)
    flaky.heal()
    assert daemon.maybe_backup()["captured"] is True
    tick(BACKUP_RETRY_BASE_S * 4)
    assert daemon.maybe_backup() is None   # success restores the full interval


def test_failure_backoff_survives_a_restart(tmp_path, bk):
    from mcpbrain.daemon import BACKUP_RETRY_BASE_S, backup_failures
    assert backup_failures(str(bk)) == 0
    (bk / "backup_state.json").write_text(json.dumps({
        "last_attempt": 1_790_000_000.0, "last_success": 1_789_000_000.0,
        "consecutive_failures": 1, "last_error": "timed out"}))
    assert backup_failures(str(bk)) == 1
    daemon = _daemon(tmp_path, _backup_config(), interval=86400.0,
                     clock=_Clock(5.0),
                     wall=_Clock(1_790_000_000.0 + BACKUP_RETRY_BASE_S),
                     last_backup_attempt_epoch=1_790_000_000.0,
                     backup_failures=backup_failures(str(bk)))
    assert daemon.maybe_backup()["captured"] is True


# ---------------------------------------------------------------------------
# request_backup: capture now and upload until the outbox is empty
# ---------------------------------------------------------------------------


def test_request_backup_captures_now_and_uploads_until_committed(tmp_path, bk, monkeypatch):
    """`mcpbrain backup`: the capture is due immediately (mid-interval, past
    the dark-wake deferral), and uploads keep running on a dark-waking Mac
    until the outbox is empty; `backup_requested` stays true until the
    requested capture has been taken."""
    monkeypatch.setattr(daemon_module.sys, "platform", "darwin")
    monkeypatch.setattr(daemon_module, "_mac_user_is_active", lambda: False)
    server = _UpServer()
    cfg = _backup_config(server)
    wall = _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, cfg, interval=86400.0, clock=_Clock(1000.0),
                     wall=wall, last_backup_attempt_epoch=wall() - 60)
    assert daemon.maybe_backup() is None  # not due

    assert daemon.request_backup() is True
    assert daemon.status()["backup_requested"] is True
    out = daemon.maybe_backup()
    assert out["captured"] is True
    assert daemon.status()["backup_requested"] is False
    assert daemon.status()["backup_running"] is None
    assert daemon.status()["backup"]["pending"] == 1

    step = daemon.upload_backup_step()
    assert step["committed"] == [out["item"]] and step["pending"] == 0
    assert daemon.status()["backup"]["pending"] == 0
    assert _backup_state(bk)["restore_point"] == 1_790_000_000.0
    assert daemon.maybe_backup() is None, "one request, one capture"

    # The drain request is answered: a later dark-wake upload is deferred.
    daemon._store.upsert_chunk("d-2", "another", "h2", {})
    daemon.request_backup()
    daemon.maybe_backup()
    daemon._backup_drain_requested.clear()
    assert daemon.upload_backup_step() is None, "dark-wake upload ran with no request"
    assert daemon.status()["backup"]["pending"] == 1


def test_request_backup_kept_while_the_lock_is_busy(tmp_path, bk):
    daemon = _daemon(tmp_path, _backup_config(), interval=86400.0,
                     last_backup_attempt_epoch=time.time() - 60)
    daemon._bulk_lock_wait_s = 0.05
    daemon.request_backup()
    with daemon._bulk_lock:
        r = threading.Thread(target=lambda: setattr(daemon, "_r", daemon.maybe_backup()))
        r.start()
        r.join(5.0)
    assert daemon._r == {"captured": False, "skipped": "lock_busy"}
    assert daemon.status()["backup_requested"] is True
    # The empty-outbox upload step must not drop the drain request while the
    # requested capture is still outstanding.
    daemon._last_prune_wall = daemon._wall_clock()
    daemon.upload_backup_step()
    assert daemon._backup_drain_requested.is_set()
    assert daemon.maybe_backup()["captured"] is True
    assert daemon.status()["backup_requested"] is False


def test_request_backup_without_backup_configured_is_refused(tmp_path, bk):
    store = _store_with_chunk(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    assert daemon.request_backup() is False


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


def test_write_backup_state_is_atomic(tmp_path, monkeypatch):
    """backup_state.json is the ONLY durable record of whether a backup
    actually landed (probes.probe_backup / last_backup_success key off it).
    A crash mid-write must never leave a truncated or half-written file for a
    reader to trip over -- write via a temp file in the same dir + os.replace,
    never in place."""
    from mcpbrain.daemon import write_backup_state
    home = str(tmp_path)
    good = {"last_attempt": 1.0, "last_success": 1.0,
            "consecutive_failures": 0, "last_error": None}
    (tmp_path / "backup_state.json").write_text(json.dumps(good))

    real_replace = os.replace

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    write_backup_state(home, ok=False, error="simulated")
    monkeypatch.setattr(os, "replace", real_replace)

    # The pre-existing valid file must survive an interrupted write untouched
    # -- never truncated, never left holding a partial temp write.
    assert json.loads((tmp_path / "backup_state.json").read_text()) == good
    # No leaked temp file.
    assert not list(tmp_path.glob(".backup_state.*.tmp"))


def test_write_backup_state_leaves_no_temp_file_on_success(tmp_path):
    from mcpbrain.daemon import write_backup_state
    write_backup_state(str(tmp_path), ok=True)
    assert json.loads((tmp_path / "backup_state.json").read_text())["last_success"]
    assert not list(tmp_path.glob(".backup_state.*.tmp"))



# ---------------------------------------------------------------------------
# capture skips that are NOT failures: busy lock (R3), empty store (R28);
# a full outbox is recorded but never advances the cadence
# ---------------------------------------------------------------------------


def test_lock_busy_skips_capture_without_failure_or_cadence(tmp_path, bk):
    """R3: capture's bulk-lock acquire is bounded; on timeout the capture is
    skipped this cycle -- not a failure, cadence untouched -- and nothing is
    written to the outbox."""
    daemon = _daemon(tmp_path, _backup_config())
    daemon._bulk_lock_wait_s = 0.1
    acquired, release = threading.Event(), threading.Event()

    def hold():
        with daemon._bulk_lock:
            acquired.set()
            release.wait(5.0)
    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert acquired.wait(2.0)
    try:
        started = time.monotonic()
        out = daemon.maybe_backup()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(2.0)

    assert out == {"captured": False, "skipped": "lock_busy"}
    assert elapsed < 2.0, "capture blocked on a held bulk lock"
    assert daemon._last_backup is None, "a skipped capture advanced the cadence"
    assert daemon._backup_failures == 0
    assert _backup_state(bk) is None
    assert _bo.pending_items(bk) == []
    assert not daemon._backup_in_progress.is_set()
    assert daemon.maybe_backup()["captured"] is True, "retried next cycle"


def test_empty_store_is_never_captured(tmp_path, bk, monkeypatch):
    """R28: a store with 0 chunks must never become the newest restore point
    (a reinstall that has not restored yet). Not a failure; cadence untouched."""
    calls = _spy_capture(monkeypatch)
    store = Store(tmp_path / "empty.sqlite3", dim=4)
    store.init()
    daemon = _daemon(tmp_path, _backup_config(), store=store)

    assert daemon.maybe_backup() == {"captured": False, "skipped": "empty_store"}
    assert calls == []
    assert daemon._last_backup is None and daemon._backup_failures == 0
    assert _backup_state(bk) is None

    store.upsert_chunk("d-1", "now there is content", "h1", {})
    assert daemon.maybe_backup()["captured"] is True


def test_outbox_full_records_a_clear_error_and_keeps_uploading(tmp_path, bk, monkeypatch):
    """A full outbox pauses capture with a clear backup_state error, never
    advances the cadence past it, and the upload step keeps draining; once
    drained the next capture runs."""
    server = _UpServer()
    cfg = _backup_config(server)
    daemon = _daemon(tmp_path, cfg, interval=86400.0)
    first = daemon.maybe_backup()["item"]
    daemon._last_backup = None   # due again
    daemon._backup = replace(cfg, settings=_bo.Settings(outbox_max_bytes=1))

    out = daemon.maybe_backup()

    assert out["captured"] is False and out["skipped"] == "outbox_full"
    state = _backup_state(bk)
    assert state["consecutive_failures"] == 1
    assert state["last_error"].startswith("outbox full: ")
    assert "MB waiting to upload" in state["last_error"]
    assert daemon._last_backup is None, "a full outbox advanced the cadence"

    step = daemon.upload_backup_step()
    assert step["committed"] == [first]
    daemon._store.upsert_chunk("d-2", "more", "h2", {})
    assert daemon.maybe_backup()["captured"] is True


def test_outbox_full_is_recorded_once_and_rechecked_cheaply(tmp_path, bk, monkeypatch):
    """Two (or many) consecutive full iterations record backup_state ONCE and
    leave consecutive_failures alone; while full, capture neither sweeps nor
    copies -- only a cheap outbox_bytes re-check, at most every
    BACKUP_RETRY_BASE_S, or after an upload step. Draining resumes capture."""
    from mcpbrain.daemon import BACKUP_RETRY_BASE_S
    cfg = _backup_config()
    clock = _Clock()
    daemon = _daemon(tmp_path, cfg, interval=86400.0, clock=clock)
    daemon.maybe_backup()
    daemon._last_backup = None
    daemon._backup = replace(cfg, settings=_bo.Settings(outbox_max_bytes=1))

    assert daemon.maybe_backup()["skipped"] == "outbox_full"
    first = _backup_state(bk)
    assert first["consecutive_failures"] == 1

    calls = _spy_capture(monkeypatch)
    sizes = []
    real_size = _bo.outbox_bytes
    monkeypatch.setattr(daemon_module.backup_outbox, "outbox_bytes",
                        lambda h: sizes.append(1) or real_size(h))
    for _ in range(5):
        clock.advance(1.0)
        assert daemon.maybe_backup() == {"captured": False, "skipped": "outbox_full"}
    assert calls == [], "a full outbox re-ran capture (sweep + copy)"
    assert sizes == [], "re-checked before BACKUP_RETRY_BASE_S"
    assert _backup_state(bk) == first, "a repeat full iteration re-recorded state"

    clock.advance(BACKUP_RETRY_BASE_S)
    assert daemon.maybe_backup()["skipped"] == "outbox_full"
    assert sizes == [1] and calls == []
    assert _backup_state(bk) == first

    # The upload step drains it and lifts the pause; capture resumes.
    daemon.upload_backup_step()
    assert daemon._outbox_full_since is None
    daemon._store.upsert_chunk("d-2", "more", "h2", {})
    assert daemon.maybe_backup()["captured"] is True
    assert len(calls) == 1


def test_outbox_full_handler_never_raises(tmp_path, bk, monkeypatch):
    daemon = _daemon(tmp_path, _backup_config())

    def full(*a, **k):
        raise _bo.OutboxFull("full")

    def unreadable(home):
        raise OSError("outbox gone")
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", full)
    monkeypatch.setattr(daemon_module.backup_outbox, "outbox_bytes", unreadable)
    out = daemon.maybe_backup()
    assert out["skipped"] == "outbox_full"
    assert "outbox full: ? MB" in _backup_state(bk)["last_error"]


def test_upload_discard_is_recorded_as_last_discard(tmp_path, bk):
    """R14: an item the uploader throws away (here: a local file that no
    longer matches its sidecar) is recorded in backup_state.json, so a person
    can see that a restore point was discarded."""
    cfg = _backup_config()
    wall = _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, cfg, wall=wall)
    item_name = daemon.maybe_backup()["item"]
    [item] = _bo.pending_items(bk)
    seg = _bo.outbox_dir(bk) / item.segments[0]["file"]
    data = bytearray(seg.read_bytes())
    data[100] ^= 0xFF
    seg.write_bytes(bytes(data))

    daemon.upload_backup_step()

    disc = _backup_state(bk)["last_discard"]
    assert disc["item"] == item_name and disc["at"] == 1_790_000_000.0
    assert "sha256" in disc["reason"]
    assert daemon.status()["backup"]["last_discard"]["item"] == item_name
    # Carried through later attempts.
    from mcpbrain.daemon import write_backup_state
    write_backup_state(str(bk), ok=False, error="x")
    assert _backup_state(bk)["last_discard"]["item"] == item_name


# ---------------------------------------------------------------------------
# the bulk lock: held for the copy only; upload never holds it
# ---------------------------------------------------------------------------


def test_capture_takes_bulk_lock_only_for_copy(tmp_path, bk, monkeypatch):
    """Capture holds _bulk_lock for the backup() copy and nothing else; a
    second thread can take _bulk_lock while upload_backup_step runs."""
    held_during_copy = []
    real_copy = _bo._copy_store

    def copy(src, dst):
        held_during_copy.append(daemon._bulk_lock.locked())
        return real_copy(src, dst)
    monkeypatch.setattr(_bo, "_copy_store", copy)
    real_pages = _bo._write_pages

    def write_pages(*a, **kw):
        held_during_copy.append(("write", daemon._bulk_lock.locked()))
        return real_pages(*a, **kw)
    monkeypatch.setattr(_bo, "_write_pages", write_pages)

    server = _UpServer()
    cfg = _backup_config(server)
    daemon = _daemon(tmp_path, cfg)
    assert daemon.maybe_backup()["captured"] is True
    assert held_during_copy[0] is True, "the copy ran without the bulk lock"
    assert ("write", False) in held_during_copy, "page writing held the bulk lock"
    assert not daemon._bulk_lock.locked()

    other_got_lock = []
    real_upload = _bu.upload_step

    def upload(*a, **kw):
        def grab():
            ok = daemon._bulk_lock.acquire(timeout=2.0)
            other_got_lock.append(ok)
            if ok:
                daemon._bulk_lock.release()
        t = threading.Thread(target=grab)
        t.start()
        t.join(5.0)
        assert not daemon._backup_in_progress.is_set()
        return real_upload(*a, **kw)
    monkeypatch.setattr(daemon_module.backup_uploader, "upload_step", upload)

    assert daemon.upload_backup_step()["committed"]
    assert other_got_lock == [True], "upload_backup_step held the bulk lock"


def test_backup_in_progress_is_set_only_around_the_capture(tmp_path, bk, monkeypatch):
    seen = []
    real = _REAL_CAPTURE

    def spy(*a, **kw):
        seen.append(daemon._backup_in_progress.is_set())
        seen.append(dict(daemon._backup_progress))
        return real(*a, **kw)
    monkeypatch.setattr(daemon_module.backup_outbox, "capture", spy)
    daemon = _daemon(tmp_path, _backup_config())
    daemon.maybe_backup()
    assert seen[0] is True and seen[1]["phase"] == "capture"
    assert not daemon._backup_in_progress.is_set()
    assert daemon._backup_progress is None


def test_upload_step_runs_every_iteration_even_when_capture_not_due(tmp_path, bk, monkeypatch):
    """The loop's backup step uploads every iteration while items wait, with
    the configured budget, whether or not a capture was due; progress is
    stamped for the watchdog and surfaced as backup_running."""
    server = _UpServer()
    cfg = _backup_config(server, settings=_bo.Settings(upload_budget_s=42))
    clock = _Clock()
    daemon = _daemon(tmp_path, cfg, interval=3600.0, clock=clock,
                     wall=_Clock(1_790_000_000.0))
    assert daemon.maybe_backup()["captured"] is True

    steps, running, stamped = [], [], []
    real_upload = _bu.upload_step

    def upload(home, **kw):
        steps.append(kw["budget_s"])
        if len(steps) == 1:
            # First iteration: Drive unreachable, nothing moves.
            return _bu.StepResult(committed=[], progress=None, error="offline",
                                  pending=1, pending_bytes=1)

        def on_progress(p):
            kw_on(p)
            running.append(daemon.status()["backup_running"])
            stamped.append(daemon._progress.get("backup"))
        kw_on = kw["on_progress"]
        kw["on_progress"] = on_progress
        return real_upload(home, **kw)
    monkeypatch.setattr(daemon_module.backup_uploader, "upload_step", upload)

    capture_calls = _spy_capture(monkeypatch)
    for _ in range(2):
        clock.advance(10.0)   # capture not due
        daemon._backup_under_bulk_lock()
    assert capture_calls == [], "capture ran although it was not due"
    assert steps == [42.0, 42.0], "upload did not run every iteration"
    assert running and running[0]["phase"] == "upload"
    assert {"sent", "total", "item"} <= set(running[0])
    assert stamped and all(s is not None for s in stamped)
    assert daemon.status()["backup"]["pending"] == 0
    assert daemon.status()["backup_running"] is None

    # Outbox empty: the next iteration makes no Drive call at all.
    daemon._backup_under_bulk_lock()
    assert len(steps) == 2


def test_upload_and_capture_skipped_while_restore_pending(tmp_path, bk, monkeypatch):
    cfg = _backup_config()
    daemon = _daemon(tmp_path, cfg)
    daemon.maybe_backup()
    daemon._restore_pending = True
    called = []
    monkeypatch.setattr(daemon_module.backup_uploader, "upload_step",
                        lambda *a, **k: called.append(1))
    assert daemon.maybe_backup() == {"captured": False, "skipped": "restore_pending"}
    assert daemon.upload_backup_step() is None
    assert called == []


def test_failed_upload_steps_count_and_rebuild_clients_after_two(tmp_path, bk, monkeypatch):
    """Each failed upload step counts as a failure; at two in a row the Drive
    service and the upload transport are rebuilt (a fresh BackupConfig)."""
    cfg = _backup_config()
    daemon = _daemon(tmp_path, cfg)
    daemon.maybe_backup()
    monkeypatch.setattr(daemon_module.backup_uploader, "upload_step",
                        lambda home, **kw: _bu.StepResult(
                            [], None, "boom", 1, 1))
    write = daemon_module.write_backup_state
    write(str(bk), ok=False, error="boom")   # what upload_step records itself
    rebuilt = {"drive": 0, "transport": 0}

    def build_drive():
        rebuilt["drive"] += 1
        return object()

    def build_transport(*a, **k):
        rebuilt["transport"] += 1
        return object()
    monkeypatch.setattr(daemon_module, "_build_drive_service", build_drive)
    monkeypatch.setattr(daemon_module, "_build_backup_transport", build_transport)

    daemon.upload_backup_step()
    assert rebuilt == {"drive": 0, "transport": 0}
    assert daemon._backup_failures == 1
    daemon.upload_backup_step()
    assert rebuilt == {"drive": 1, "transport": 1}
    assert daemon._backup.drive_service is not cfg.drive_service
    assert daemon._backup.transport is not cfg.transport
    assert cfg.transport is not None, "the caller's BackupConfig was mutated"


def test_transport_built_lazily_when_missing(tmp_path, bk, monkeypatch):
    server = _UpServer()
    cfg = _backup_config(server, transport=None)
    daemon = _daemon(tmp_path, cfg)
    daemon.maybe_backup()
    monkeypatch.setattr(daemon_module, "_build_backup_transport",
                        lambda *a, **k: _UpTransport(server))
    assert daemon.upload_backup_step()["committed"]
    assert daemon._backup.transport is not None


def test_transport_unavailable_is_a_recorded_failure(tmp_path, bk, monkeypatch):
    cfg = _backup_config(transport=None)
    daemon = _daemon(tmp_path, cfg)
    daemon.maybe_backup()

    def no_creds(*a, **k):
        raise FileNotFoundError("google_token.json")
    monkeypatch.setattr(daemon_module, "_build_backup_transport", no_creds)
    out = daemon.upload_backup_step()
    assert "transport unavailable" in out["error"]
    assert "transport unavailable" in _backup_state(bk)["last_error"]
    assert out["pending"] == 1


def test_prune_runs_after_a_commit_and_surfaces_hold(tmp_path, bk, monkeypatch):
    pruned = []

    def prune(home, **kw):
        pruned.append(kw)
        return {"deleted": [], "held": {"newest": {"chain": "c2", "base_bytes": 10},
                                        "previous": {"chain": "c1", "base_bytes": 1000}}}
    monkeypatch.setattr(daemon_module.backup_prune, "prune_step", prune)
    cfg = _backup_config(settings=_bo.Settings(retain_days=3))
    wall = _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, cfg, wall=wall)
    daemon._last_prune_wall = wall()        # the daily idle prune just ran
    daemon.upload_backup_step()
    assert pruned == [], "an idle iteration pruned before its daily slot"
    daemon.maybe_backup()
    daemon.upload_backup_step()
    assert len(pruned) == 1 and pruned[0]["retain_days"] == 3
    assert 0 < pruned[0]["budget_s"] <= 60
    held = daemon.status()["prune_held"]
    assert held["newest_bytes"] == 10 and held["largest_kept_bytes"] == 1000


def test_idle_install_prunes_once_a_day(tmp_path, bk, monkeypatch):
    """Nothing pending, nothing committing: expired chains must still go.
    One budgeted prune pass per IDLE_PRUNE_INTERVAL_S of wall clock, and
    none in between (an idle iteration otherwise makes no Drive call)."""
    from mcpbrain.daemon import IDLE_PRUNE_INTERVAL_S
    pruned = []
    monkeypatch.setattr(daemon_module.backup_prune, "prune_step",
                        lambda home, **kw: pruned.append(kw) or {"deleted": [], "held": None})
    uploads = []
    monkeypatch.setattr(daemon_module.backup_uploader, "upload_step",
                        lambda *a, **k: uploads.append(1))
    wall = _Clock(1_790_000_000.0)
    daemon = _daemon(tmp_path, _backup_config(settings=_bo.Settings(retain_days=7)),
                     wall=wall)

    daemon.upload_backup_step()                  # first idle pass: prunes
    assert len(pruned) == 1
    assert pruned[0]["retain_days"] == 7 and 0 < pruned[0]["budget_s"] <= 60
    assert pruned[0]["now"] == wall()
    wall.advance(IDLE_PRUNE_INTERVAL_S - 1)
    daemon.upload_backup_step()
    assert len(pruned) == 1, "pruned again inside the day"
    wall.advance(1)
    daemon.upload_backup_step()
    assert len(pruned) == 2, "the daily idle prune did not run"
    assert uploads == [], "an idle prune started an upload"


def test_status_backup_block_shape(tmp_path, bk):
    cfg = _backup_config()
    daemon = _daemon(tmp_path, cfg)
    want = {"restore_point", "pending", "pending_bytes", "uploading", "chain",
            "delta_ratio", "last_error", "last_discard"}
    empty = daemon.status()["backup"]
    assert set(empty) == want
    assert empty["pending"] == 0 and empty["chain"] is None

    item = daemon.maybe_backup()["item"]
    blk = daemon.status()["backup"]
    assert blk["pending"] == 1 and blk["pending_bytes"] > 0
    assert blk["chain"] == item.rsplit("-", 1)[0]
    assert blk["delta_ratio"] == 0.0
    assert blk["uploading"] is None and blk["restore_point"] is None

    daemon._backup_progress = {"phase": "upload", "started": 1.0, "sent": 5,
                               "total": 10, "item": item}
    assert daemon.status()["backup"]["uploading"]["sent"] == 5
    daemon._backup_progress = None
    daemon.upload_backup_step()
    blk = daemon.status()["backup"]
    assert blk["pending"] == 0 and blk["restore_point"] is not None
    assert blk["last_error"] is None


# ---------------------------------------------------------------------------
# the run loop
# ---------------------------------------------------------------------------


def test_run_loop_captures_and_uploads_within_the_loop(tmp_path, bk):
    """run() calls the backup step each iteration: with interval 0 the first
    pass captures and the upload commits it."""
    store = _make_store(tmp_path)
    store.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    server = _UpServer()
    cfg = _backup_config(server)
    daemon = Daemon(store, FakeEmbedder(), services={}, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=0.0, clock=_Clock())

    t = threading.Thread(target=daemon.run)
    t.start()
    deadline = time.monotonic() + 10.0
    while (_backup_state(bk) or {}).get("restore_point") is None:
        if time.monotonic() >= deadline or not t.is_alive():
            break
        threading.Event().wait(0.02)
    daemon.stop()
    t.join(timeout=5.0)

    assert not t.is_alive(), "run() did not return promptly after stop()"
    assert (_backup_state(bk) or {}).get("restore_point"), "loop did not commit a backup"


def test_backup_step_does_not_hold_the_bulk_lock(tmp_path):
    """The loop's backup step no longer runs under _bulk_lock: capture takes
    it inside, for the copy only, and the upload never holds it."""
    store = _make_store(tmp_path)
    daemon = Daemon(store, FakeEmbedder(), services={}, interval_s=0.01,
                    lock=SingleWriterLock(tmp_path / "d.lock"))
    seen = []
    daemon.maybe_backup = lambda: seen.append(("capture", daemon._bulk_lock.locked()))
    daemon.upload_backup_step = lambda: seen.append(("upload", daemon._bulk_lock.locked()))

    daemon._backup_under_bulk_lock()

    assert seen == [("capture", False), ("upload", False)]


def test_daemon_raises_if_backup_configured_without_interval(tmp_path, bk):
    with pytest.raises(ValueError, match="backup_interval_s"):
        Daemon(_store_with_chunk(tmp_path), FakeEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"),
               backup=_backup_config(), backup_interval_s=None)


def test_daemon_constructs_fine_when_backup_is_not_configured(tmp_path, bk):
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
    """status() surfaces curator queue depth (spec Task 5, observability) so
    /api/status exposes them without needing direct store access. Must
    degrade gracefully but here we seed real data and expect real counts.

    Note (final-review fix): the org block used to also carry
    `cache_hits`/`cache_misses` (shared-drive ingest-cache counts), but
    run_sync_cycle stopped producing the `"shared_drive_cache"` result key
    once this plan's Task 5 landed -- that hit/miss distinction is now
    internal to handle_shared_drive_item, not surfaced back to the caller --
    so those two fields were left silently pinned at 0 forever (worse than
    absent: it read as "the cache is healthy"). They were removed from
    status() entirely; do not resurrect an assertion on them here."""
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
    assert "cache_hits" not in st["org"] and "cache_misses" not in st["org"]


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


def test_enrich_payload_migration_is_registered_as_a_cadence_pass():
    from mcpbrain.daemon import _CADENCE_PASSES
    names = {p.name for p in _CADENCE_PASSES}
    assert "enrich_payload_migration" in names
    p = next(p for p in _CADENCE_PASSES if p.name == "enrich_payload_migration")
    # Draining our own cache table needs no Google identity, and it must not
    # run against the store while a backfill holds it.
    assert p.needs_configured is False
    assert p.needs_bulk_lock is True


def test_resolve_google_account_get_profile_passes_num_retries(tmp_path):
    """The last-resort getProfile in _resolve_google_account had no retry.

    It is the only network call on the /api/status path, so an Errno-49-class
    blip left `google_account` empty and re-issued the same uncached call on
    every subsequent poll. Same rationale as mcpbrain.backup._NUM_RETRIES.
    """
    from mcpbrain import daemon as daemon_mod

    calls: list = []

    class _Profile:
        def execute(self, num_retries=0):
            calls.append(num_retries)
            return {"emailAddress": "sam@example.com"}

    class _ProfileUsers:
        def getProfile(self, userId):
            return _Profile()

    class _ProfileGmail:
        def users(self):
            return _ProfileUsers()

    store = _make_store(tmp_path)
    daemon = Daemon(store, FakeEmbedder(),
                    services={"gmail_service": _ProfileGmail()},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    email = daemon._resolve_google_account(tmp_path / "no-such-token.json")

    assert email == "sam@example.com"
    assert calls == [daemon_mod._NUM_RETRIES]


def test_maybe_backup_without_wall_twin_falls_back_to_monotonic():
    """A Daemon built via __new__ (or any path that sets _last_backup alone)
    has no _last_backup_wall; maybe_backup must decide on monotonic only."""
    import threading

    d = Daemon.__new__(Daemon)
    d._backfill_active = threading.Event()
    d._config_lock = threading.Lock()
    d._backup = object()  # configured; never reached when not due
    d._backup_interval_s = 3600.0
    clock = _Clock(10_000.0)
    d._clock = clock
    d._last_backup = clock() - 60  # 1 min ago by monotonic
    assert not hasattr(d, "_last_backup_wall")

    assert d.maybe_backup() is None  # not due, and no AttributeError
