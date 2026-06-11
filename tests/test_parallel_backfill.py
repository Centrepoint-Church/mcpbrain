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
