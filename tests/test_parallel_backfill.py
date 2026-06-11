import json
import threading
from mcpbrain import parallel_backfill


def test_run_parallel_backfill_refuses_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    res = parallel_backfill.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {})
    assert res["status"] == "not_configured"
    assert res["batches"] == 0


def test_helpers_are_importable():
    # The drain_backlog helpers AND claude_runner must be reachable from the module.
    for name in ("extract_answer", "parse_extractor_json", "patch_extractions",
                 "atomic_write_inbox", "quarantine", "daemon_status",
                 "claude_runner"):
        assert hasattr(parallel_backfill, name), name


def test_partition_splits_into_disjoint_sub_batches():
    from mcpbrain import parallel_backfill
    items = list(range(45))           # 45 threads
    parts = parallel_backfill._partition(items, batch_size=20)
    assert [len(p) for p in parts] == [20, 20, 5]
    # disjoint + complete
    flat = [x for p in parts for x in p]
    assert flat == items and len(set(flat)) == 45


def test_partition_empty_returns_empty():
    from mcpbrain import parallel_backfill
    assert parallel_backfill._partition([], batch_size=20) == []


import subprocess


def _pending(batch_id="fastbf-0-0"):
    return {"batch_id": batch_id, "prepared_at": "2026-06-11T09:00:00Z",
            "context": {}, "threads": [{"thread_id": "t1",
            "messages": [{"message_id": "m1", "date": "2026-01-01",
                          "sender": "a@x.org", "subject": "Hi", "text": "hello"}]}],
            "merge_review": []}


def test_worker_writes_inbox_on_valid_answer(tmp_path):
    from mcpbrain import parallel_backfill
    # Extraction must satisfy the full contract: org, summary, topics required.
    answer = {"batch_id": "fastbf-0-0", "extractions": [
        {"thread_id": "t1", "org": "external", "content_type": "fyi",
         "summary": "greeting", "topics": [],
         "messages": [{"message_id": "m1", "date": "2026-01-01", "sender": "a@x.org"}],
         "entities": [], "relations": [], "actions": []}]}
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=lambda prompt, **k: json.dumps(answer),
        model="sonnet", timeout=600, max_retries=3, backoff_base=0.0)
    assert ok is True
    assert (tmp_path / "enrich_inbox" / "fastbf-0-0.json").exists()


def test_worker_retries_then_succeeds_on_rate_limit(tmp_path):
    from mcpbrain import parallel_backfill
    answer = {"batch_id": "fastbf-0-0", "extractions": []}
    calls = {"n": 0}
    def flaky(prompt, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise subprocess.CalledProcessError(1, "claude", stderr="overloaded_error: 429")
        return json.dumps(answer)
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=flaky, model="sonnet", timeout=600,
        max_retries=5, backoff_base=0.0)
    assert ok is True and calls["n"] == 3


def test_worker_quarantines_on_persistent_timeout(tmp_path):
    from mcpbrain import parallel_backfill
    def always_timeout(prompt, **k):
        raise subprocess.TimeoutExpired("claude", 600)
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=always_timeout, model="sonnet", timeout=600,
        max_retries=2, backoff_base=0.0)
    assert ok is False
    assert list((tmp_path / "enrich_inbox" / "bad").glob("*.txt"))


def test_worker_quarantines_unparseable_answer(tmp_path):
    from mcpbrain import parallel_backfill
    ok, reason = parallel_backfill._process_batch_worker(
        home=tmp_path, pending=_pending(), prompt_prefix="EXTRACT",
        run_claude=lambda prompt, **k: "not json at all",
        model="sonnet", timeout=600, max_retries=2, backoff_base=0.0)
    assert ok is False
    assert list((tmp_path / "enrich_inbox" / "bad").glob("*.txt"))


def _configure(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "Sam", "owner_email": "s@x.org", "orgs": [{"name": "Org"}]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))


class _Batch:
    def __init__(self, tid):
        self.thread_id = tid; self.doc_ids = [f"d-{tid}"]; self.chunks = []


# ---------------------------------------------------------------------------
# Guard tests — unchanged
# ---------------------------------------------------------------------------

def test_guard_refuses_when_daemon_running_and_not_paused():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status={"paused": False}, force=False)
    assert ok is False and "pause" in msg.lower()


def test_guard_proceeds_when_daemon_unreachable():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status=None, force=False)
    assert ok is True


def test_guard_proceeds_when_daemon_paused():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status={"paused": True}, force=False)
    assert ok is True


def test_guard_force_overrides():
    from mcpbrain import parallel_backfill as pb
    ok, msg = pb.check_daemon_guard(status={"paused": False}, force=True)
    assert ok is True and "force" in msg.lower()


# ---------------------------------------------------------------------------
# Continuous-pipeline tests (replace the old wave-loop tests)
# ---------------------------------------------------------------------------

def _fake_build_pending(store, batches, **k):
    """Minimal build_pending stub that returns a valid pending dict."""
    return {"batch_id": k["batch_id"],
            "threads": [{"thread_id": b.thread_id} for b in batches]}


def test_pipeline_drains_backlog_dry(tmp_path, monkeypatch):
    """group_unenriched_threads returns the remaining un-dispatched threads on
    every call; once all are dispatched and drained (fake drain removes them) it
    returns []. Pipeline must finish with status=='done', threads_dispatched==3,
    worker invoked once per sub-batch (2 batches for 3 threads at batch_size=2),
    and drain_fn called at least once."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    # Remaining threads; fake drain removes them after each worker call.
    remaining = {"t1", "t2", "t3"}

    def fake_group(store, **k):
        return [_Batch(tid) for tid in sorted(remaining)]

    monkeypatch.setattr(pb, "group_unenriched_threads", fake_group)
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)

    worker_calls = []
    monkeypatch.setattr(pb, "_process_batch_worker",
                        lambda **kw: (worker_calls.append(kw["pending"]["batch_id"]), (True, ""))[1])

    drain_count = {"n": 0}

    def fake_drain(**k):
        # Simulate drain: mark all dispatched threads enriched so they stop returning.
        remaining.clear()
        drain_count["n"] += 1

    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=8, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=fake_drain)

    assert res["status"] == "done"
    assert res["threads_dispatched"] == 3
    # 3 threads / batch_size 2 => 2 sub-batches => 2 worker calls
    assert len(worker_calls) == 2
    assert drain_count["n"] >= 1


def test_pipeline_no_double_processing(tmp_path, monkeypatch):
    """The in_flight exclusion must prevent the same thread_id being dispatched
    to two workers concurrently.

    Strategy: group_unenriched_threads always returns the same 3 _Batch objects
    (simulating enriched=0 not yet flushed). A fake drain_fn marks the threads
    enriched so the fake store stops returning them. Worker calls are recorded;
    we assert each thread_id appears in exactly one worker's batch.
    """
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    # Tracks which tids the fake store considers still-unenriched.
    unenriched = {"t1", "t2", "t3"}

    def fake_group(store, **k):
        return [_Batch(tid) for tid in sorted(unenriched)]

    monkeypatch.setattr(pb, "group_unenriched_threads", fake_group)
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)

    # Record every (batch_id -> set of thread_ids) the worker sees.
    worker_batches: list[set] = []

    def fake_worker(**kw):
        tids = {t["thread_id"] for t in kw["pending"]["threads"]}
        worker_batches.append(tids)
        return True, ""

    monkeypatch.setattr(pb, "_process_batch_worker", fake_worker)

    # drain_fn marks the dispatched threads enriched so fake_group stops
    # returning them after the first drain cycle.
    def fake_drain(**k):
        # All threads ever dispatched get marked enriched (removed from unenriched).
        dispatched = set().union(*worker_batches) if worker_batches else set()
        unenriched.difference_update(dispatched)

    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=3, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=fake_drain)

    assert res["status"] == "done"
    assert res["threads_dispatched"] == 3

    # Flatten all dispatched thread_ids across all worker calls.
    all_dispatched = [tid for batch in worker_batches for tid in batch]
    # Each thread_id must appear exactly once — no double-processing.
    from collections import Counter
    counts = Counter(all_dispatched)
    for tid, cnt in counts.items():
        assert cnt == 1, f"thread {tid!r} dispatched {cnt} times (expected 1)"


def test_pipeline_max_batches_honoured(tmp_path, monkeypatch):
    """When max_batches is set the pipeline stops submitting new batches once
    batches_done reaches max_batches. status must be 'max_batches'."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    # Never dries up — always returns work.
    call_n = {"n": 0}
    def fake_group(store, **k):
        call_n["n"] += 1
        return [_Batch(f"t{call_n['n']}-{i}") for i in range(4)]

    monkeypatch.setattr(pb, "group_unenriched_threads", fake_group)
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))

    max_b = 3
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=2, batch_size=2, max_batches=max_b,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: {})

    assert res["status"] == "max_batches"
    # batches_done must not exceed max_batches (workers=2 means at most 2 extra
    # in-flight at the cutoff moment, but with fake instant workers it's exact).
    assert res["batches"] == max_b


def test_pipeline_cancellation(tmp_path, monkeypatch):
    """Cancellation: once cancel_event is set, no NEW batches are submitted.
    In-flight batches are drained. status must be 'cancelled'."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    cancel = threading.Event()

    # Never dries up.
    call_n = {"n": 0}
    def fake_group(store, **k):
        call_n["n"] += 1
        return [_Batch(f"t{call_n['n']}")]

    monkeypatch.setattr(pb, "group_unenriched_threads", fake_group)
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))

    drained = {"n": 0}
    def drain_then_cancel(**k):
        drained["n"] += 1
        cancel.set()   # cancel after the first drain cycle

    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=1, cancel_event=cancel,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=drain_then_cancel)

    assert res["status"] == "cancelled"
    assert drained["n"] >= 1   # the in-flight batch was drained before we stopped


def test_pipeline_partial_failure(tmp_path, monkeypatch):
    """One batch's worker returns (False, 'contract'). quarantined==1, other
    batches succeed, status=='done', drain_fn still ran."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    batches_iter = iter([
        [_Batch("t1"), _Batch("t2"), _Batch("t3")],
        [],
    ])
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: next(batches_iter, []))
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)

    call_counter = {"n": 0}
    def mixed_worker(**kw):
        n = call_counter["n"]
        call_counter["n"] += 1
        if n == 0:
            return False, "contract"
        return True, ""

    monkeypatch.setattr(pb, "_process_batch_worker", mixed_worker)

    drained = {"n": 0}
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=8, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: drained.__setitem__("n", drained["n"] + 1) or {})

    assert res["status"] == "done"
    assert res["quarantined"] == 1
    assert drained["n"] >= 1


# ---------------------------------------------------------------------------
# Backlog/progress helpers (store with chunk_count / enriched_count)
# ---------------------------------------------------------------------------

class _CountingStore:
    """Minimal store stub that supports chunk_count/enriched_count."""
    def __init__(self, total, enriched):
        self._total = total
        self._enriched = enriched

    def chunk_count(self):
        return self._total

    def enriched_count(self):
        return self._enriched


def test_pipeline_progress_with_counting_store(tmp_path, monkeypatch):
    """When the store implements chunk_count/enriched_count, run_parallel_backfill
    completes and the progress path is exercised without crashing."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    counting_store = _CountingStore(total=10, enriched=7)

    batches_iter = iter([[_Batch("t1"), _Batch("t2")], []])
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: next(batches_iter, []))
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))

    res = pb.run_parallel_backfill(
        store=counting_store, embedder=object(), home=str(tmp_path),
        workers=2, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: {})

    assert res["status"] == "done"
    assert res["threads_dispatched"] == 2


def test_pipeline_progress_graceful_without_counts(tmp_path, monkeypatch):
    """store=object() (no chunk_count/enriched_count) must not crash the loop."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    batches_iter = iter([[_Batch("t1")], []])
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: next(batches_iter, []))
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))

    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: {})

    assert res["status"] == "done"


def test_pre_pool_cancel_stops_before_any_submission(tmp_path, monkeypatch):
    """If cancel_event is set during _filter_noise (before any future is submitted),
    the pipeline must exit with status='cancelled', worker never called, drain
    never called."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)

    cancel = threading.Event()

    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: [_Batch("t1")])

    def filter_then_cancel(store, batches):
        cancel.set()
        return batches

    monkeypatch.setattr(pb.prepare, "_filter_noise", filter_then_cancel)
    monkeypatch.setattr(pb.prepare, "build_pending", _fake_build_pending)

    worker_calls = {"n": 0}

    def counting_worker(**kw):
        worker_calls["n"] += 1
        return True, ""

    monkeypatch.setattr(pb, "_process_batch_worker", counting_worker)

    drain_calls = {"n": 0}

    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=20, cancel_event=cancel,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: drain_calls.__setitem__("n", drain_calls["n"] + 1) or {})

    assert res["status"] == "cancelled"
    assert worker_calls["n"] == 0, "worker must not run when cancel set before submission"
    assert drain_calls["n"] == 0, "drain must not run when cancel set before any future"
