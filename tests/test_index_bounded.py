"""index_pending must respect a limit and a budget, and resume cleanly — and
report an item-cap stop all the way up to the cycle's `more_work`."""
import datetime as _datetime
import json

from mcpbrain import prepare
from mcpbrain.budget import Budget
from mcpbrain.index import index_pending
from mcpbrain.store import Store

_NOW = _datetime.datetime(2026, 6, 2, 9, 30, 0, tzinfo=_datetime.timezone.utc)


class _FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2, 0.3, 0.4]


def _store_with_pending(tmp_path, n):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        for i in range(n):
            db.execute(
                "INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded,enriched) "
                "VALUES (?,?,?,?,0,0)",
                (f"d-{i}", f"text {i}", f"h{i}", json.dumps({})),
            )
    return s


def test_unembedded_chunks_respects_limit(tmp_path):
    s = _store_with_pending(tmp_path, 50)
    assert len(s.unembedded_chunks(limit=10)) == 10
    assert len(s.unembedded_chunks()) == 50


def test_index_pending_stops_at_expired_budget(tmp_path):
    s = _store_with_pending(tmp_path, 200)
    spent = Budget(deadline_s=0.0)          # already expired
    done = index_pending(s, _FakeEmbedder(), batch_size=32, home=str(tmp_path),
                         budget=spent)
    assert done == 0, "an expired budget must embed nothing"


def test_index_pending_resumes_and_processes_every_item_exactly_once(tmp_path):
    """N bounded slices over a K-item backlog process exactly K items."""
    s = _store_with_pending(tmp_path, 100)
    total = 0
    for _ in range(20):                      # generous slice count
        done = index_pending(s, _FakeEmbedder(), batch_size=10,
                             home=str(tmp_path), max_items=10)
        total += done
        if done == 0:
            break
    assert total == 100
    assert s.unembedded_chunks() == []


def test_stats_report_hitting_the_item_cap(tmp_path):
    """A cap hit means there is (almost certainly) more work RIGHT NOW. Without
    this signal run_cycle reported more_work=False and the loop slept the full
    300s interval between 2000-chunk slices of a live backlog."""
    s = _store_with_pending(tmp_path, 50)
    stats: dict = {}
    done = index_pending(s, _FakeEmbedder(), batch_size=10, home=str(tmp_path),
                         max_items=10, stats=stats)
    assert done == 10
    assert stats["capped"] is True


def test_stats_report_no_cap_when_the_pending_set_is_exhausted(tmp_path):
    s = _store_with_pending(tmp_path, 5)
    stats: dict = {}
    done = index_pending(s, _FakeEmbedder(), batch_size=10, home=str(tmp_path),
                         max_items=100, stats=stats)
    assert done == 5
    assert stats["capped"] is False


def test_stats_report_no_cap_on_a_budget_cut(tmp_path):
    """A budget cut is already covered by more_work; it must not ALSO look like a
    cap hit (done < max_items, so the two signals stay distinguishable)."""
    s = _store_with_pending(tmp_path, 200)
    stats: dict = {}
    index_pending(s, _FakeEmbedder(), batch_size=32, home=str(tmp_path),
                  max_items=100, budget=Budget(deadline_s=0.0), stats=stats)
    assert stats["capped"] is False


def test_stats_omitted_is_still_supported(tmp_path):
    """Every existing caller passes no stats and reads the int return."""
    s = _store_with_pending(tmp_path, 3)
    assert index_pending(s, _FakeEmbedder(), home=str(tmp_path)) == 3


def test_index_pending_stops_mid_batch_when_budget_expires(tmp_path):
    """The between-batches `break` (index.py's `for i in range(...): if
    budget.expired(): break`) must actually execute. Both existing budget
    tests above (test_index_pending_stops_at_expired_budget,
    test_stats_report_no_cap_on_a_budget_cut) use an ALREADY-expired
    Budget(deadline_s=0.0), which trips the ENTRY check (before the `for`
    loop even starts) -- the loop's own per-batch check never runs in either
    of them. This uses a clock that is NOT expired at construction or before
    the first batch, but IS expired before the second, so embedding must
    stop after exactly one batch, leaving the rest resumable.
    """
    s = _store_with_pending(tmp_path, 4)

    class _Clock:
        """Not-expired through the first batch, expired from the second on."""
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            # 1st call: Budget.__init__'s self._start.
            # 2nd call: the entry check (index.py, before `if pending:`).
            # 3rd call: the loop's check before batch 0 (i=0).
            # 4th call: the loop's check before batch 1 (i=2) -> expired.
            return {1: 0.0, 2: 0.0, 3: 0.0}.get(self.calls, 10.0)

    budget = Budget(deadline_s=1.0, clock=_Clock())
    done = index_pending(s, _FakeEmbedder(), batch_size=2, home=str(tmp_path),
                         budget=budget)

    assert done == 2, "must embed exactly the first batch, then stop"
    assert len(s.unembedded_chunks()) == 2, (
        "the second batch must remain pending, resumable next cycle")


def test_run_sync_cycle_propagates_the_embed_cap(tmp_path, monkeypatch):
    """The signal has to survive the six index_pending call sites."""
    from mcpbrain import sync as sync_mod

    s = _store_with_pending(tmp_path, 50)
    monkeypatch.setattr("mcpbrain.sync.gmail.sync_gmail", lambda svc, store, **kw: 0)
    monkeypatch.setattr(sync_mod, "progressive_backfill_step",
                        lambda store, **kw: {"gmail": 0, "drive": 0, "calendar": 0})
    res = sync_mod.run_sync_cycle(s, _FakeEmbedder(), gmail_service=object(),
                                  home=str(tmp_path), embed_max_items=10)
    assert res["embedded"] == 10
    assert res["embed_capped"] is True


def test_run_sync_cycle_leaves_embed_capped_unset_when_the_backlog_fits(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod

    s = _store_with_pending(tmp_path, 5)
    monkeypatch.setattr("mcpbrain.sync.gmail.sync_gmail", lambda svc, store, **kw: 0)
    monkeypatch.setattr(sync_mod, "progressive_backfill_step",
                        lambda store, **kw: {"gmail": 0, "drive": 0, "calendar": 0})
    res = sync_mod.run_sync_cycle(s, _FakeEmbedder(), gmail_service=object(),
                                  home=str(tmp_path), embed_max_items=1000)
    assert res.get("embed_capped") is None


def test_run_sync_cycle_stops_between_sources_once_the_budget_expires(tmp_path, monkeypatch):
    """The inter-source `budget_spent` early return: once the budget expires
    after gmail's own block finishes, run_sync_cycle must return immediately
    -- calendar/drive must NEVER be reached this cycle, not just be bounded
    internally. Nothing in the suite drove all three services through
    run_sync_cycle with a budget that flips mid-cycle; the existing budget
    tests here only check embed_capped propagation with gmail alone.

    Each source function is patched at its SOURCE module (mcpbrain.sync.gmail /
    .calendar / .drive / mcpbrain.index), matching run_sync_cycle's own
    `from mcpbrain.sync.gmail import sync_gmail`-style local imports, which
    re-resolve the attribute on that module at call time -- patching an
    attribute on the `mcpbrain.sync` package itself (which doesn't define
    these names) would silently not take effect.
    """
    from mcpbrain import sync as sync_mod

    calendar_calls = []
    drive_calls = []

    class _FlipBudget:
        def __init__(self):
            self._expired = False

        def expired(self):
            return self._expired

    budget = _FlipBudget()

    def _fake_sync_gmail(svc, store, **kw):
        # Simulate the budget running out during/after gmail's own work.
        budget._expired = True
        return 5

    monkeypatch.setattr("mcpbrain.sync.gmail.sync_gmail", _fake_sync_gmail)
    monkeypatch.setattr("mcpbrain.index.index_pending", lambda *a, **kw: 0)
    monkeypatch.setattr(
        "mcpbrain.sync.calendar.sync_calendar",
        lambda *a, **kw: calendar_calls.append(1) or 0)
    monkeypatch.setattr(
        "mcpbrain.sync.drive.sync_drive",
        lambda *a, **kw: drive_calls.append(1) or 0)
    monkeypatch.setattr(sync_mod, "progressive_backfill_step",
                        lambda store, **kw: {"gmail": 0, "drive": 0, "calendar": 0})

    res = sync_mod.run_sync_cycle(
        object(), _FakeEmbedder(),
        gmail_service=object(), calendar_service=object(), drive_service=object(),
        budget=budget)

    assert res["gmail"] == 5
    assert res["budget_spent"] is True
    assert calendar_calls == [], "calendar must not run once the budget expired after gmail"
    assert drive_calls == [], "drive must not run once the budget expired after gmail"
    assert "calendar" not in res or res["calendar"] == 0, (
        "calendar's result slot must stay untouched -- proves the source never ran")


def test_run_cycle_more_work_on_an_item_cap_not_only_budget_expiry(tmp_path, monkeypatch):
    """The bug: more_work was `budget.expired()` alone. index_pending caps each
    call at embed_max_items (2000 by default), so a big backlog embedded 2000
    chunks well inside the 60s budget, reported more_work False, and run()'s loop
    then slept the full 300s interval with thousands of chunks still pending."""
    from mcpbrain import daemon as dmod

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store_with_pending(tmp_path, 1)
    monkeypatch.setattr(dmod, "run_sync_cycle",
                        lambda *a, **kw: {"gmail": 0, "calendar": 0, "drive": 0,
                                          "embedded": 2000, "embed_capped": True})
    res = dmod.run_cycle(s, _FakeEmbedder(), enrich_mode="off", budget=None)
    assert res["more_work"] is True


def test_run_cycle_more_work_false_when_nothing_was_capped(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store_with_pending(tmp_path, 1)
    monkeypatch.setattr(dmod, "run_sync_cycle",
                        lambda *a, **kw: {"gmail": 0, "calendar": 0, "drive": 0,
                                          "embedded": 3})
    res = dmod.run_cycle(s, _FakeEmbedder(), enrich_mode="off", budget=None)
    assert res["more_work"] is False


# --- Task 2: prepare_units must actually receive and respect a budget ------
#
# The core live defect: run_cycle called prepare.prepare_units(...) without a
# budget at all, so it ran unbounded while run()'s cycle held _bulk_lock for
# the whole of run_one() -- measured live at >8 minutes with zero heartbeat
# advance. These tests exercise prepare_units/build_pending directly (not
# through the daemon) to pin the budget contract at its source.

class _PrepFakeBatch:
    def __init__(self, thread_id, doc_ids, chunks):
        self.thread_id = thread_id
        self.doc_ids = doc_ids
        self.chunks = chunks


class _PrepFakeStore:
    def mark_enriched(self, doc_ids):
        pass

    def thread_context(self, thread_id):
        return ""

    def unified_actions(self, thread_id=None, status="open"):
        return []

    def entities_for_resolution(self):
        return []


def _prep_msg(message_id, text):
    return {
        "message_id": message_id, "sender": "a@b.com", "date": "2026-06-01",
        "labels": "INBOX", "subject": "x", "text": text,
    }


def _stub_prepare_seams(monkeypatch):
    monkeypatch.setattr(prepare, "_reassemble_thread",
                        lambda chunks: sorted(chunks, key=lambda c: c["date"]))
    monkeypatch.setattr(prepare, "_build_known_people",
                        lambda store, batch_thread_ids: [])
    monkeypatch.setattr(prepare, "_build_candidate_people", lambda store: [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])


def test_prepare_units_embeds_nothing_on_an_already_expired_budget(tmp_path, monkeypatch):
    """The bug, reproduced directly: prepare_units used to have no `budget`
    parameter at all, so an expired budget could never stop it. With the
    parameter wired through to build_pending's per-thread loop, an
    already-expired budget must produce zero thread units -- exactly the
    `Budget(deadline_s=0.0)` contract index_pending already honours above."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"salience_gate": false}')
    batches = [
        _PrepFakeBatch(f"t-{i}", [f"d-{i}"],
                       [_prep_msg(f"m{i}", "Can you confirm the Hall B booking for Sunday?")])
        for i in range(5)
    ]
    store = _PrepFakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: batches)
    _stub_prepare_seams(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path),
                                    budget=Budget(deadline_s=0.0))
    assert summary["threads"] == 0


def test_build_pending_stops_mid_batch_when_budget_expires(tmp_path, monkeypatch):
    """budget.expired() is checked once per batch, BETWEEN threads, not just
    once for the whole call -- so a large kept-thread set yields partway
    through instead of all-or-nothing."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    fake_batches = [
        _PrepFakeBatch(f"t-{i}", [f"d-{i}"], [_prep_msg(f"m{i}", "hello")])
        for i in range(3)
    ]
    _stub_prepare_seams(monkeypatch)

    class _Clock:
        """Not-expired for the first batch, expired from the second batch on."""
        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            # 1st call: Budget.__init__'s self._start.
            # 2nd call: expired() check before batch 0 -> False.
            # 3rd call: expired() check before batch 1 -> True.
            return {1: 0.0, 2: 0.0}.get(self.calls, 10.0)

    budget = Budget(deadline_s=1.0, clock=_Clock())
    data = prepare.build_pending(_PrepFakeStore(), fake_batches, char_budget=100000,
                                 now=_NOW, resolution_due=False, budget=budget)

    assert len(data["threads"]) == 1
    assert data["threads"][0]["thread_id"] == "t-0"


# --- the three per-batch WRITE loops that run BEFORE build_pending ---------
#
# Budgeting build_pending alone left the real stall in place. _apply_salience_
# gate, _filter_noise and _apply_trivial_threads all run first, all loop over
# every batch, and all do per-batch store I/O inside their own bulk_section --
# so with a waiter present each batch additionally pays BULK_LOCK_YIELD_S
# (0.25s) on section exit. At a few hundred batches that is minutes of
# unbudgeted work before build_pending's check is ever reached.

class _SpyStore(_PrepFakeStore):
    def __init__(self):
        self.marked: list = []
        self.cold: list = []

    def mark_enriched(self, doc_ids):
        self.marked.append(list(doc_ids))

    def set_enrich_state(self, doc_ids, state):
        self.cold.append((list(doc_ids), state))


def _spy_batches(n):
    """Chunks carry a doc_id as well as the message fields: _apply_salience_gate
    reads chunk["doc_id"], so without it that helper raises KeyError before
    reaching the assertion these tests are actually making."""
    return [_PrepFakeBatch(f"t-{i}", [f"d-{i}"],
                           [dict(_prep_msg(f"m{i}", "hello"), doc_id=f"d-{i}")])
            for i in range(n)]


def test_noise_filter_stops_writing_when_the_budget_expires(monkeypatch):
    """Every batch here is noise, so every batch would take the section and
    call mark_enriched. An already-expired budget must stop that at zero."""
    _stub_prepare_seams(monkeypatch)
    monkeypatch.setattr(prepare, "thread_is_noise", lambda messages: True)
    store = _SpyStore()

    kept = prepare._filter_noise(store, _spy_batches(3), budget=Budget(deadline_s=0.0))

    assert kept == []
    assert store.marked == [], (
        "_filter_noise kept marking batches after its budget expired — the loop "
        "is unbounded, which is the stall this budget exists to prevent"
    )


def test_noise_filter_still_processes_everything_without_a_budget(monkeypatch):
    """Discriminator for the test above: with no budget the loop must still run
    to completion, so a passing budget test can't be an always-empty fixture."""
    _stub_prepare_seams(monkeypatch)
    monkeypatch.setattr(prepare, "thread_is_noise", lambda messages: True)
    store = _SpyStore()

    prepare._filter_noise(store, _spy_batches(3), budget=None)

    assert len(store.marked) == 3


def test_salience_gate_stops_writing_when_the_budget_expires(monkeypatch):
    """Nothing enriches here, so every batch would cold-mark. An expired budget
    must stop before the first set_enrich_state, and drop the unreached tail
    rather than passing it through ungated."""
    monkeypatch.setattr(prepare, "should_enrich", lambda chunk: False)
    store = _SpyStore()

    kept, summary = prepare._apply_salience_gate(
        store, _spy_batches(3), budget=Budget(deadline_s=0.0))

    assert kept == []
    assert summary == {"gated": 0, "kept": 0}
    assert store.cold == [], (
        "_apply_salience_gate kept cold-marking batches after its budget expired"
    )


def test_trivial_threads_stops_writing_when_the_budget_expires(monkeypatch):
    """Every thread here is trivial, so every batch would graph_write.apply +
    mark_enriched. An expired budget must stop at zero."""
    from mcpbrain import graph_write

    _stub_prepare_seams(monkeypatch)
    monkeypatch.setattr(prepare, "is_trivial_thread", lambda messages: True)
    monkeypatch.setattr(prepare.config, "enrich_trivial_thread_summary", lambda home: True)
    applied: list = []
    monkeypatch.setattr(graph_write, "apply",
                        lambda *a, **kw: applied.append(1))
    store = _SpyStore()

    kept = prepare._apply_trivial_threads(store, _spy_batches(3),
                                          budget=Budget(deadline_s=0.0))

    assert kept == []
    assert applied == [], (
        "_apply_trivial_threads kept extracting after its budget expired"
    )
    assert store.marked == []


def test_prepare_units_does_no_per_batch_work_at_all_on_an_expired_budget(
        tmp_path, monkeypatch):
    """The whole-function contract, and the test that actually pins the defect.

    An already-expired budget must mean prepare_units touches NO batch. Before
    the fix this passed its `threads == 0` assertion (build_pending was
    budgeted) while still walking all 200 batches through the noise filter
    first — so the per-batch counter, not the thread count, is what
    discriminates here.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # Trivial-thread summarising is off so the third loop can't drag real
    # graph_write.apply() into this test; the salience gate and noise filter
    # are enough to count, and each is separately pinned above.
    (tmp_path / "config.json").write_text(
        '{"salience_gate": true, "enrich_trivial_thread_summary": false}')
    batches = _spy_batches(200)
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: batches)
    monkeypatch.setattr(prepare, "_build_known_people", lambda store, batch_thread_ids: [])
    monkeypatch.setattr(prepare, "_build_candidate_people", lambda store: [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: [])

    touched: list = []

    def _counting_reassemble(chunks):
        touched.append(1)
        return sorted(chunks, key=lambda c: c["date"])

    monkeypatch.setattr(prepare, "_reassemble_thread", _counting_reassemble)
    monkeypatch.setattr(prepare, "should_enrich",
                        lambda chunk: touched.append(1) or True)

    summary = prepare.prepare_units(_SpyStore(), thread_cap=500, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path),
                                    budget=Budget(deadline_s=0.0))

    assert summary["threads"] == 0
    assert touched == [], (
        f"prepare_units walked {len(touched)} batches through its pre-build_pending "
        "loops on an ALREADY-EXPIRED budget — those loops are unbounded"
    )
