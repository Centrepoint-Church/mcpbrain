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


def test_run_sync_cycle_propagates_the_embed_cap(tmp_path, monkeypatch):
    """The signal has to survive the six index_pending call sites."""
    from mcpbrain import sync as sync_mod

    s = _store_with_pending(tmp_path, 50)
    monkeypatch.setattr("mcpbrain.sync.gmail.sync_gmail", lambda svc, store: 0)
    monkeypatch.setattr(sync_mod, "progressive_backfill_step",
                        lambda store, **kw: {"gmail": 0, "drive": 0, "calendar": 0})
    res = sync_mod.run_sync_cycle(s, _FakeEmbedder(), gmail_service=object(),
                                  home=str(tmp_path), embed_max_items=10)
    assert res["embedded"] == 10
    assert res["embed_capped"] is True


def test_run_sync_cycle_leaves_embed_capped_unset_when_the_backlog_fits(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod

    s = _store_with_pending(tmp_path, 5)
    monkeypatch.setattr("mcpbrain.sync.gmail.sync_gmail", lambda svc, store: 0)
    monkeypatch.setattr(sync_mod, "progressive_backfill_step",
                        lambda store, **kw: {"gmail": 0, "drive": 0, "calendar": 0})
    res = sync_mod.run_sync_cycle(s, _FakeEmbedder(), gmail_service=object(),
                                  home=str(tmp_path), embed_max_items=1000)
    assert res.get("embed_capped") is None


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
