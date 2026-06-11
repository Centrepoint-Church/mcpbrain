import json
from mcpbrain import parallel_backfill


def test_run_parallel_backfill_refuses_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    res = parallel_backfill.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {})
    assert res["status"] == "not_configured"
    assert res["waves"] == 0


def test_helpers_are_importable():
    # The drain_backlog helpers must be reachable from the module.
    for name in ("extract_answer", "parse_extractor_json", "patch_extractions",
                 "atomic_write_inbox", "quarantine", "daemon_status"):
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


def test_wave_loop_runs_until_backlog_dry(tmp_path, monkeypatch):
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)
    # Wave 1: 3 threads; wave 2: empty (dry).
    waves = iter([[_Batch("t1"), _Batch("t2"), _Batch("t3")], []])
    monkeypatch.setattr(pb, "group_unenriched_threads", lambda store, **k: next(waves))
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"],
                            "threads": [{"thread_id": b.thread_id} for b in batches]})
    workered = []
    monkeypatch.setattr(pb, "_process_batch_worker",
                        lambda **kw: (workered.append(kw["pending"]["batch_id"]), (True, ""))[1])
    drained = {"n": 0}
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=8, batch_size=2,
        run_claude=lambda *a, **k: "{}",
        apply=lambda *a, **k: {},
        drain_fn=lambda **k: drained.__setitem__("n", drained["n"] + 1) or {})
    assert res["status"] == "done"
    assert res["waves"] == 1                 # one productive wave, then dry
    assert res["threads_dispatched"] == 3
    # 3 threads / batch_size 2 => 2 sub-batches => 2 worker calls
    assert len(workered) == 2
    assert drained["n"] == 1                 # drain barrier ran once for the wave


def test_wave_loop_honours_max_waves(tmp_path, monkeypatch):
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: [_Batch("t1")])   # never goes dry
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"], "threads": []})
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=20, max_waves=3,
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {},
        drain_fn=lambda **k: {})
    assert res["status"] == "max_waves" and res["waves"] == 3


def test_wave_loop_cancels_after_current_wave(tmp_path, monkeypatch):
    from mcpbrain import parallel_backfill as pb
    import threading
    _configure(tmp_path, monkeypatch)
    monkeypatch.setattr(pb, "group_unenriched_threads",
                        lambda store, **k: [_Batch("t1")])
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"], "threads": []})
    monkeypatch.setattr(pb, "_process_batch_worker", lambda **kw: (True, ""))
    cancel = threading.Event()
    drained = {"n": 0}
    def drain_then_cancel(**k):
        drained["n"] += 1
        cancel.set()                          # cancel during the first wave's drain
        return {}
    res = pb.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        workers=1, batch_size=20, cancel_event=cancel,
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {},
        drain_fn=drain_then_cancel)
    assert res["status"] == "cancelled"
    assert drained["n"] == 1                  # the in-flight wave's drain completed


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


def test_wave_loop_partial_wave_quarantine(tmp_path, monkeypatch):
    """A wave where one sub-batch fails (quarantine) must still complete:
    status=="done", quarantined==1, and the drain barrier still ran once."""
    from mcpbrain import parallel_backfill as pb
    _configure(tmp_path, monkeypatch)
    # Wave 1: 3 threads; wave 2: empty (dry).
    waves = iter([[_Batch("t1"), _Batch("t2"), _Batch("t3")], []])
    monkeypatch.setattr(pb, "group_unenriched_threads", lambda store, **k: next(waves))
    monkeypatch.setattr(pb.prepare, "_filter_noise", lambda store, batches: batches)
    monkeypatch.setattr(pb.prepare, "build_pending",
                        lambda store, batches, **k: {"batch_id": k["batch_id"],
                            "threads": [{"thread_id": b.thread_id} for b in batches]})

    # batch_size=2 with 3 threads -> 2 sub-batches: "fastbf-0-0-..." and "fastbf-0-1-..."
    # Return True for first sub-batch, False for second.
    call_counter = {"n": 0}
    def mixed_worker(**kw):
        n = call_counter["n"]
        call_counter["n"] += 1
        if n == 0:
            return True, ""
        return False, "contract"
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
    assert drained["n"] == 1             # drain barrier ran exactly once for the wave
