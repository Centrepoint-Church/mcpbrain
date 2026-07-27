"""A busy cycle must not starve the four gated passes indefinitely.

Live evidence from the reviewed build: 183 consecutive
"bulk lock held for more than 5.0s" warnings and not one gated pass run. The
cycle held _bulk_lock for all of run_one() and re-acquired 1s later, so with
non-FIFO locks the maintenance thread lost essentially every race.

test_cycle_yields_when_maintenance_wants_the_lock and
test_bulk_section_releases_between_phases exercise the fairness HAND-OFF
mechanism (_bulk_lock_intent / BULK_LOCK_YIELD_S) in isolation and are kept as
mechanism documentation, but a soak test run against the REAL section
granularity the daemon originally used (one section per whole run_sync_cycle/
drain.drain call, each budget-bounded to CYCLE_BUDGET_S=60s) found the
hand-off makes NO measurable difference at that granularity: 1/32 gated-pass
runs at a 60s lock hold, 0/32 at 300s -- this exactly reproduces the live
8m39s/183-skip failure. What actually restored throughput was splitting the
SAME work into many smaller sections (20x3s sections -> 36 runs, 0 skips),
independent of the hand-off. So this is a lock DUTY-CYCLE problem (held too
long per acquisition), not only a fairness problem -- the real fix is the
finer-grained sectioning now threaded through run_sync_cycle/index_pending/
drain/sync_gmail/sync_calendar/sync_drive/prepare_units (one section per
message/event/file/embed-batch/inbox-file, not per whole call).
test_gated_pass_runs_within_bounded_ticks_under_sustained_backlog below is the
soak-style regression test for THAT: it drives the real _cycle_bulk_section
and _run_periodic_passes at a small-section granularity under a cycle that
never idles, and asserts the gated pass actually gets a turn.
"""
import threading
import time
from contextlib import contextmanager

from mcpbrain import daemon as d
from mcpbrain.daemon import Daemon


def _dm():
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._bulk_lock_wait_s = 5.0
    dm._stop = threading.Event()
    return dm


def test_cycle_yields_when_maintenance_wants_the_lock():
    dm = _dm()
    got = []

    def maintenance():
        with dm._bulk_lock_intent():
            acquired = dm._bulk_lock.acquire(timeout=dm._bulk_lock_wait_s)
        got.append(acquired)
        if acquired:
            dm._bulk_lock.release()

    # Cycle does 20 short "phases", entering the bulk section each time.
    def cycle():
        for _ in range(20):
            with dm._cycle_bulk_section():
                time.sleep(0.02)

    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    time.sleep(0.05)
    m = threading.Thread(target=maintenance, daemon=True)
    m.start()
    m.join(timeout=10)
    c.join(timeout=10)

    assert got == [True], "maintenance never got the bulk lock while the cycle ran"


def test_bulk_section_releases_between_phases():
    """The lock must not be held across the whole cycle."""
    dm = _dm()
    seen_free = []

    def watcher():
        for _ in range(50):
            if dm._bulk_lock.acquire(blocking=False):
                dm._bulk_lock.release()
                seen_free.append(1)
                return
            time.sleep(0.01)

    def cycle():
        for _ in range(10):
            with dm._cycle_bulk_section():
                time.sleep(0.01)

    w = threading.Thread(target=watcher, daemon=True)
    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    w.start()
    w.join(timeout=5)
    c.join(timeout=5)
    assert seen_free, "bulk lock was never observably free during the cycle"


def test_two_independent_waiters_dont_erase_each_others_intent():
    """Regression for a single-Event _bulk_lock_wanted bug caught in review.

    _backup_under_bulk_lock runs on the CYCLE thread itself (called from
    run()'s loop, right after run_one()) and ALSO marks intent around its own
    bounded acquire -- so TWO independent call sites can be "waiting" at once
    (the maintenance thread's _run_periodic_passes, and the cycle thread's own
    backup call). With a single threading.Event, one waiter's cleanup
    (.clear() in its own finally) could erase the OTHER waiter's still-pending
    signal: maintenance sets the flag and blocks; the cycle thread's own
    backup call sets it too (already set, no-op), wins its own race, runs,
    and clears the flag in ITS finally -- even though maintenance is STILL
    genuinely waiting. The next _cycle_bulk_section release then sees no
    intent and skips the yield pause, right when it's needed. The counter-
    based _bulk_lock_intent/_bulk_lock_wanted design fixes this: cleanup only
    decrements, so the signal stays true as long as ANY waiter is pending.
    """
    dm = _dm()
    dm._bulk_lock.acquire()  # simulate a chunk-mutating phase in progress

    a_marked = threading.Event()
    a_done = threading.Event()

    def waiter_a():
        with dm._bulk_lock_intent():
            a_marked.set()
            dm._bulk_lock.acquire(timeout=2.0)  # never actually gets it in this test
        a_done.set()

    t = threading.Thread(target=waiter_a, daemon=True)
    t.start()
    assert a_marked.wait(timeout=2.0)

    # Waiter B marks intent and immediately clears it (simulating
    # _backup_under_bulk_lock's own short-lived intent window), while A is
    # STILL outstanding.
    with dm._bulk_lock_intent():
        assert dm._bulk_lock_wanted() is True, "B's own marking should also read True"
    # B's cleanup must NOT have erased A's still-pending signal.
    assert dm._bulk_lock_wanted() is True, (
        "a waiter's cleanup erased another waiter's still-pending intent "
        "(the single-Event bug this counter design fixes)"
    )

    dm._bulk_lock.release()
    assert a_done.wait(timeout=3.0)
    t.join(timeout=1.0)
    assert dm._bulk_lock_wanted() is False, "counter should be back to zero once both waiters finish"


def test_gated_pass_runs_within_bounded_ticks_under_sustained_backlog(monkeypatch):
    """Soak-style regression test at the granularity the daemon ACTUALLY uses.

    Drives the real _cycle_bulk_section and the real _run_periodic_passes
    (over one fake always-due gated pass) with a cycle thread that NEVER
    idles -- back-to-back small sections (~20ms each), simulating a
    sustained "more_work" backlog where the cycle keeps finding fresh work
    every tick, same as run_sync_cycle/drain.drain do when threaded a
    bulk_section per message/event/file/batch instead of per whole call.

    The 2s deadline is deliberately TIGHT, not generous: at this
    (~20ms-section) granularity the gated pass gets through almost
    immediately (measured: ~90ms, first tick) every time, so 2s is a huge
    margin for THIS design. But it is tight enough to genuinely discriminate
    against a regression to coarse sectioning -- verified directly: replacing
    the 20ms per-section sleep with a single hold of just 5s (still far
    smaller than a real ~60s CYCLE_BUDGET_S section) makes the gated pass
    time out on every attempt and never run within this same 2s window,
    because one coarse hold already outlasts the whole test. This is the
    test that should have caught the original 8m39s/183-skip failure (which
    was really "one section per ~minutes of work") and must keep catching a
    regression back to that coarse a granularity.
    """
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._bulk_lock_wait_s = 0.3
    dm._stop = threading.Event()
    dm._clock = time.monotonic

    gated_calls: list = []
    fake_pass = d.CadencePass(
        "fake_gated", "_fake_gated_interval_s", "_fake_gated_last",
        "_run_fake_gated", needs_configured=False, needs_bulk_lock=True)
    monkeypatch.setattr(d, "_CADENCE_PASSES", (fake_pass,))
    dm._run_fake_gated = lambda: gated_calls.append(1)
    dm._fake_gated_interval_s = 0.0   # always due
    dm._fake_gated_last = None

    stop_cycle = threading.Event()

    def cycle():
        # Never idles: one small section right after another, mirroring a
        # continuous backlog where run_sync_cycle/drain.drain always find
        # more work (each of THEIR own per-item bulk_section calls is this
        # short, not the whole ~60s call).
        while not stop_cycle.is_set():
            with dm._cycle_bulk_section():
                time.sleep(0.02)

    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    try:
        deadline = time.monotonic() + 2.0
        ticks = 0
        while not gated_calls and time.monotonic() < deadline:
            dm._run_periodic_passes()
            ticks += 1
            time.sleep(0.05)
        assert gated_calls, (
            f"gated pass never ran within {ticks} maintenance ticks under a "
            "sustained (never-idle) backlog at small-section granularity"
        )
    finally:
        stop_cycle.set()
        c.join(timeout=2)


def test_run_one_actually_holds_bulk_lock_during_the_cycle(tmp_path, monkeypatch):
    """Regression for a silent-revert class Task 1 exists to prevent.

    `bulk_section` defaults to `contextlib.nullcontext` throughout this
    module's call chain (run_cycle -> run_sync_cycle -> sync_gmail, etc.), so
    deleting `bulk_section=self._cycle_bulk_section` from run_one's call to
    run_cycle would make the daemon run its whole cycle with NO bulk lock at
    all -- and the rest of the suite would very likely stay green, since
    almost no other test checks locking (they check the RESULT of a cycle,
    not whether _bulk_lock was held while producing it).

    This drives the REAL Daemon.run_one() -> run_cycle() -> run_sync_cycle()
    -> sync_gmail() path against real fixtures (not a direct call to
    _cycle_bulk_section in isolation) and observes, via a thin spy on
    store.upsert_chunk (the actual chunk-mutating call inside sync_gmail's
    bulk_section), that the daemon's own _bulk_lock is genuinely held at the
    moment a chunk is written.
    """
    from mcpbrain.daemon import SingleWriterLock
    from tests.test_daemon import FakeEmbedder, _gmail_fake_one_message, _make_store

    # Not autouse across modules -- test_daemon.py's own _isolate_app_home
    # fixture doesn't apply here, so this must set MCPBRAIN_HOME itself
    # (same reason that fixture exists: an unconfigured real backup would
    # otherwise get pulled in by run_one()'s startup path).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    seen_locked: list = []
    orig_upsert = store.upsert_chunk

    def spy_upsert(*a, **kw):
        seen_locked.append(daemon._bulk_lock.locked())
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy_upsert

    res = daemon.run_one()

    assert res is not None
    assert seen_locked, "fixture produced no chunk writes to observe -- test setup is broken"
    assert all(seen_locked), (
        "run_one()'s real path wrote a chunk WITHOUT holding _bulk_lock -- the "
        "bulk_section=self._cycle_bulk_section wiring from run_one into "
        "run_cycle (and down into run_sync_cycle/sync_gmail) is missing or "
        "broken. bulk_section silently defaults to a no-op nullcontext, so "
        "this class of regression produces no other test failure."
    )


# ---------------------------------------------------------------------------
# Silent-revert coverage for the remaining bulk_section threading points.
#
# test_run_one_actually_holds_bulk_lock_during_the_cycle above only pins ONE
# of the eight `bulk_section` threading call sites this task added (gmail,
# via the full Daemon.run_one() -> run_cycle() chain). Adversarial review
# (round 3) found deleting `bulk_section=bulk_section` from any of the other
# SEVEN would still leave the whole suite green. These tests close the
# remaining seven. Six of them (calendar, my-drive, shared-drive,
# index_pending, drain_captures — plus gmail above) drive the real
# `run_cycle` free function (the actual choke point that threads
# `bulk_section` to each source/step; a full Daemon isn't needed to test
# THIS link since run_cycle takes bulk_section as a plain parameter) with a
# standalone real-Lock-based section, and observe the lock is GENUINELY held
# at the moment of the relevant write — not merely that an argument was
# forwarded. The last two (drain.drain, prepare.prepare_units) are complex to
# drive end-to-end with real contract-valid content, so they instead verify
# run_cycle's own wiring directly: the exact `bulk_section` object passed to
# run_cycle is forwarded, unmodified, into both calls — weaker than a held-
# lock observation, but still catches exactly the "deleted
# bulk_section=bulk_section" regression class this task is about.
# ---------------------------------------------------------------------------

def _standalone_bulk_section():
    """A real, minimal _cycle_bulk_section-shaped CM, not tied to a Daemon —
    run_cycle is a free function and the actual choke point under test here.
    Returns (lock, section_factory)."""
    lock = threading.Lock()

    @contextmanager
    def section():
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

    return lock, section


def test_bulk_section_threaded_to_calendar_sync(tmp_path, monkeypatch):
    from mcpbrain.daemon import run_cycle
    from mcpbrain.store import Store
    from tests.test_calendar_sync import FakeCalService, _event, _resp
    from tests.test_daemon import FakeEmbedder

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    ev = _event("evt1", "Team meeting")
    svc = FakeCalService(on_full=_resp([ev], next_sync_token="tok1"))

    lock, section = _standalone_bulk_section()
    seen_locked: list = []
    orig_upsert = store.upsert_chunk

    def spy(*a, **kw):
        seen_locked.append(lock.locked())
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy
    run_cycle(store, FakeEmbedder(), calendar_service=svc, bulk_section=section)

    assert seen_locked, "fixture produced no chunk writes to observe"
    assert all(seen_locked), (
        "calendar sync wrote a chunk WITHOUT holding bulk_section's lock -- "
        "the bulk_section threading from run_cycle into run_sync_cycle/"
        "sync_calendar is missing or broken"
    )


def test_bulk_section_threaded_to_my_drive_sync(tmp_path, monkeypatch):
    from mcpbrain.daemon import run_cycle
    from mcpbrain.store import Store
    from tests.test_daemon import FakeEmbedder
    from tests.test_drive_sync import FakeDriveService, _gdoc_change, _page

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.set_cursor("drive", "100")  # skip the no-op bootstrap path
    pages = [_page([_gdoc_change("f1", "Doc One")], new_start_page_token="105")]
    svc = FakeDriveService(pages=pages,
                          exports={"f1": b"document body content, long enough to matter"})

    lock, section = _standalone_bulk_section()
    seen_locked: list = []
    orig_upsert = store.upsert_chunk

    def spy(*a, **kw):
        seen_locked.append(lock.locked())
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy
    run_cycle(store, FakeEmbedder(), drive_service=svc, bulk_section=section)

    assert seen_locked, "fixture produced no chunk writes to observe"
    assert all(seen_locked), (
        "My-Drive sync wrote a chunk WITHOUT holding bulk_section's lock -- "
        "the bulk_section threading from run_cycle into run_sync_cycle/"
        "sync_drive is missing or broken"
    )


def test_bulk_section_threaded_to_shared_drive_sync(tmp_path, monkeypatch):
    from mcpbrain import config
    from mcpbrain.daemon import run_cycle
    from mcpbrain.store import Store
    from tests.test_daemon import FakeEmbedder
    from tests.test_drive_sync import FakeDriveService, _gdoc_change
    from tests.helpers.org_fleet import LocalDirFleetStorage

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    home = str(tmp_path)
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.set_cursor("drive:D1", "100")

    def _fake_factory(_home, _svc):
        return lambda drive_id: LocalDirFleetStorage(tmp_path / drive_id)

    monkeypatch.setattr("mcpbrain.fleet_storage.cache_storage_factory", _fake_factory)
    svc = FakeDriveService(
        shared_drives=[{"id": "D1", "name": "Ops"}],
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"shared drive body content, long enough to matter"})

    lock, section = _standalone_bulk_section()
    seen_locked: list = []
    orig_upsert = store.upsert_chunk

    def spy(*a, **kw):
        seen_locked.append(lock.locked())
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy
    run_cycle(store, FakeEmbedder(), drive_service=svc, bulk_section=section)

    assert seen_locked, "fixture produced no chunk writes to observe"
    assert all(seen_locked), (
        "shared-drive sync wrote a chunk WITHOUT holding bulk_section's lock -- "
        "the bulk_section threading from run_cycle into run_sync_cycle/"
        "sync_shared_drives/sync_shared_drive/_cache_first_extract_one is "
        "missing or broken"
    )


def test_bulk_section_threaded_to_index_pending(tmp_path, monkeypatch):
    """Reuses the gmail fixture purely to produce a chunk for index_pending
    to embed; the assertion is on write_embedding (index_pending's own
    write), not upsert_chunk."""
    from mcpbrain.daemon import run_cycle
    from tests.test_daemon import FakeEmbedder, _gmail_fake_one_message, _make_store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    fake = _gmail_fake_one_message()

    lock, section = _standalone_bulk_section()
    seen_locked: list = []
    orig_write_embedding = store.write_embedding

    def spy(*a, **kw):
        seen_locked.append(lock.locked())
        return orig_write_embedding(*a, **kw)

    store.write_embedding = spy
    run_cycle(store, FakeEmbedder(), gmail_service=fake, bulk_section=section)

    assert seen_locked, "fixture produced no embeddings to observe"
    assert all(seen_locked), (
        "index_pending wrote an embedding WITHOUT holding bulk_section's lock -- "
        "the bulk_section threading from run_cycle into run_sync_cycle/"
        "index_pending is missing or broken"
    )


def test_bulk_section_threaded_to_drain_captures(tmp_path, monkeypatch):
    import json

    from mcpbrain.daemon import run_cycle
    from tests.test_daemon import FakeEmbedder, _make_store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    inbox = tmp_path / "capture_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "cap-1.json").write_text(json.dumps({
        "kind": "ingest", "captured_at": "2026-06-04T12:00:00Z",
        "source": "code", "title": "T", "content": "C",
        "tags": "", "observation_type": "memory", "org": "",
    }))

    lock, section = _standalone_bulk_section()
    seen_locked: list = []
    orig_upsert = store.upsert_chunk

    def spy(*a, **kw):
        seen_locked.append(lock.locked())
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy
    run_cycle(store, FakeEmbedder(), bulk_section=section)

    assert seen_locked, "fixture produced no chunk writes to observe"
    assert all(seen_locked), (
        "drain_captures wrote a chunk WITHOUT holding bulk_section's lock -- "
        "the bulk_section threading from run_cycle into drain.drain_captures "
        "is missing or broken"
    )


def test_bulk_section_argument_reaches_drain_and_prepare_units(monkeypatch):
    """prepare_units and drain.drain are costly to drive end-to-end with real
    contract-valid content (unit files, valid extractions, graph_write
    plumbing), so — unlike the five tests above — this verifies run_cycle's
    OWN wiring directly: the exact `bulk_section` object passed into
    run_cycle is forwarded, unmodified, into both calls. Weaker than a
    genuinely-held-lock observation, but still catches exactly the "deleted
    bulk_section=bulk_section" regression class this task is about."""
    import mcpbrain.daemon as daemon_module
    from mcpbrain.daemon import run_cycle

    calls: dict = {}

    def _prepare_spy(store, **kwargs):
        calls["prepare_units"] = kwargs.get("bulk_section")
        return {}

    def _drain_spy(store, **kwargs):
        calls["drain"] = kwargs.get("bulk_section")
        return {}

    monkeypatch.setattr(daemon_module.prepare, "prepare_units", _prepare_spy)
    monkeypatch.setattr(daemon_module.drain, "drain", _drain_spy)
    monkeypatch.setattr(daemon_module, "_graph_apply", lambda: object())

    class _FakeStore:
        def unenriched_chunks(self, limit=None):
            return []

    class _FakeEmbedder:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0, 0, 0] for _ in texts]

    def my_section():
        return _standalone_bulk_section()[1]()

    run_cycle(_FakeStore(), _FakeEmbedder(), enrich_mode="spool", bulk_section=my_section)

    assert calls.get("prepare_units") is my_section, (
        "run_cycle no longer forwards its bulk_section into prepare.prepare_units"
    )
    assert calls.get("drain") is my_section, (
        "run_cycle no longer forwards its bulk_section into drain.drain"
    )
