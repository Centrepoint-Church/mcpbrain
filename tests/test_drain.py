"""Tests for the daemon-side drain step (Task 4).

drain consumes enrich_inbox/*.json: validates each file against the contract,
applies each extraction through Phase 1's apply() (injected as a stub here),
marks that thread's chunks enriched, feeds merge-review answers into entity
resolution, and deletes the file on full success. Malformed files are
quarantined to enrich_inbox/bad/ rather than crashing the daemon.

apply() is graph_write.apply, injected as a parameter. These tests pass a
recording stub matching its real signature: apply(store, extraction, *,
doc_ids). drain accepts an embedder= but does not forward it to apply (the
structural apply takes no embedder), so the stub does not accept one. The
end-to-end round trip against the real apply lives in test_integration_spool.py.
"""

import json
from pathlib import Path

import pytest

from mcpbrain import drain
from mcpbrain.store import Store


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def home(tmp_path):
    """A spool root with enrich_inbox/ ready."""
    (tmp_path / "enrich_inbox").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    """A real Store on a temp db, initialised with the schema."""
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


def _envelope(thread_id, **overrides):
    """A minimal contract envelope that passes validate_batch_file."""
    env = {
        "thread_id": thread_id,
        "org": "Centrepoint",
        "content_type": "update",
        "summary": "A short summary.",
        "entities": [],
        "topics": [],
        "actions": [],
        "relations": [],
        "messages": [
            {"message_id": f"{thread_id}-m1", "sender": "Joel <joel@centrepoint.church>",
             "date": "2026-04-18", "labels": "INBOX", "subject": "Subject"}
        ],
        "resolved_action_ids": [],
        "updated_actions": [],
        "reply_needed": False,
        "reply_reason": "",
    }
    env.update(overrides)
    return env


def _batch(batch_id, extractions, merge_answers=None):
    return {
        "batch_id": batch_id,
        "extractions": extractions,
        "merge_answers": merge_answers or [],
    }


def _write_inbox(home, name, payload):
    """Write payload (dict or raw str) to enrich_inbox/<name>."""
    path = home / "enrich_inbox" / name
    if isinstance(payload, str):
        path.write_text(payload)
    else:
        path.write_text(json.dumps(payload))
    return path


def _seed_chunk(store, doc_id, thread_id, message_id=None):
    """Insert one un-enriched chunk carrying thread_id + message_id in metadata.

    message_id defaults to f"{thread_id}-m1" so it matches the default _envelope's
    single message — drain recovers a thread's chunks by message id, so the chunk
    provenance and the extraction's messages[] must line up (as they do in real
    Gmail data, where reassemble_thread copies the chunk's message_id forward).
    """
    meta = {"thread_id": thread_id, "message_id": message_id or f"{thread_id}-m1"}
    store.upsert_chunk(doc_id, "body text", f"hash-{doc_id}", meta)


def _enriched_count(store, doc_ids):
    with store._connect() as db:
        marks = db.execute(
            "SELECT doc_id, enriched FROM chunks WHERE doc_id IN (%s)"
            % ",".join("?" * len(doc_ids)),
            doc_ids,
        ).fetchall()
    return {r["doc_id"]: r["enriched"] for r in marks}


class RecordingApply:
    """Stub for Phase 1's graph_write.apply. Records calls; can raise per thread.

    Mirrors the real apply's return: a summary dict carrying entities/relations
    counts (entities counts those LINKED to the thread, so it is > 0 even when
    no new store rows are created). The default return is {"entities": 1,
    "relations": 1}. Pass per_thread={thread_id: {"entities": E, "relations": R}}
    to vary the return per call, or returns=<value> to force a fixed return
    (e.g. None) for robustness tests.
    """

    _SENTINEL = object()

    def __init__(self, fail_threads=(), per_thread=None, returns=_SENTINEL):
        self.calls = []
        self.fail_threads = set(fail_threads)
        self.per_thread = per_thread or {}
        self.returns = returns

    def __call__(self, store, extraction, *, doc_ids):
        thread_id = extraction["thread_id"]
        self.calls.append({
            "thread_id": thread_id,
            "extraction": extraction,
            "doc_ids": list(doc_ids),
        })
        if thread_id in self.fail_threads:
            raise RuntimeError(f"apply failed for {thread_id}")
        if self.returns is not RecordingApply._SENTINEL:
            return self.returns
        if thread_id in self.per_thread:
            return self.per_thread[thread_id]
        return {"entities": 1, "relations": 1, "topics": 0, "email_context": 0}


# --- 4.1: validate + quarantine -------------------------------------------


def test_drain_empty_inbox_noop(store, home):
    summary = drain.drain(store, home=home, apply=RecordingApply())
    assert summary["files"] == 0
    assert summary["quarantined"] == 0


def test_drain_quarantines_malformed(store, home):
    # An invalid-JSON file plus a valid file: the valid one still processes.
    _write_inbox(home, "broken.json", "{not valid json")
    _seed_chunk(store, "d-ok", "t-ok")
    _write_inbox(home, "good.json", _batch("batch-ok", [_envelope("t-ok")]))

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    bad = home / "enrich_inbox" / "bad" / "broken.json"
    assert bad.exists(), "malformed file should be quarantined"
    assert not (home / "enrich_inbox" / "broken.json").exists()
    # the valid file was not quarantined; it still processed
    assert not (home / "enrich_inbox" / "bad" / "good.json").exists()
    assert summary["quarantined"] == 1


def test_drain_quarantines_contract_violation(store, home):
    # Valid JSON, but an extraction violates the contract structurally
    # (org must be a string; an unconfigured org STRING is coerced, not
    # quarantined — see test_org_taxonomy.TestDrainOrgDrift).
    bad_env = _envelope("t-bad", org=None)
    _write_inbox(home, "violation.json", _batch("batch-bad", [bad_env]))

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    assert (home / "enrich_inbox" / "bad" / "violation.json").exists()
    assert app.calls == [], "a contract-violating file must not be applied"
    assert summary["quarantined"] == 1


# --- 4.2: apply per thread + mark chunks -----------------------------------


def test_drain_applies_each_extraction(store, home):
    _seed_chunk(store, "d-a", "t-a")
    _seed_chunk(store, "d-b", "t-b")
    _write_inbox(home, "batch.json",
                 _batch("batch-1", [_envelope("t-a"), _envelope("t-b")]))

    # An embedder may be passed to drain (the daemon does), but drain does not
    # forward it to the structural apply, which takes no embedder.
    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app, embedder="EMB")

    by_thread = {c["thread_id"]: c for c in app.calls}
    assert set(by_thread) == {"t-a", "t-b"}
    assert by_thread["t-a"]["doc_ids"] == ["d-a"]
    assert by_thread["t-b"]["doc_ids"] == ["d-b"]
    assert summary["applied"] == 2


def test_drain_marks_chunks_enriched(store, home):
    _seed_chunk(store, "d-a", "t-a")
    _write_inbox(home, "batch.json", _batch("batch-1", [_envelope("t-a")]))

    drain.drain(store, home=home, apply=RecordingApply())

    assert _enriched_count(store, ["d-a"]) == {"d-a": 1}


def test_drain_apply_failure_isolated_no_mark(store, home):
    # Thread A applies + marks; thread B's apply raises -> B not marked, file kept.
    _seed_chunk(store, "d-a", "t-a")
    _seed_chunk(store, "d-b", "t-b")
    path = _write_inbox(home, "batch.json",
                        _batch("batch-1", [_envelope("t-a"), _envelope("t-b")]))

    app = RecordingApply(fail_threads={"t-b"})
    summary = drain.drain(store, home=home, apply=app)

    assert _enriched_count(store, ["d-a"]) == {"d-a": 1}
    assert _enriched_count(store, ["d-b"]) == {"d-b": 0}
    assert path.exists(), "a file with a failed extraction must be kept for retry"
    assert summary["applied"] == 1


def test_drain_long_thread_subparts_regrouped(store, home):
    # Two sub-parts share one thread_id; drain recombines and applies once.
    _seed_chunk(store, "d-x1", "t-long", message_id="t-long-m1")
    _seed_chunk(store, "d-x2", "t-long", message_id="t-long-m2")
    part1 = _envelope("t-long", part=1, of=2,
                      messages=[{"message_id": "t-long-m1", "sender": "A <a@x.com>",
                                 "date": "2026-04-18", "labels": "INBOX", "subject": "S"}])
    part2 = _envelope("t-long", part=2, of=2,
                      messages=[{"message_id": "t-long-m2", "sender": "B <b@x.com>",
                                 "date": "2026-04-19", "labels": "INBOX", "subject": "S"}])
    _write_inbox(home, "batch.json", _batch("batch-1", [part1, part2]))

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    long_calls = [c for c in app.calls if c["thread_id"] == "t-long"]
    assert len(long_calls) == 1, "split long thread must be applied once"
    # messages recombined in part order
    msg_ids = [m["message_id"] for m in long_calls[0]["extraction"]["messages"]]
    assert msg_ids == ["t-long-m1", "t-long-m2"]
    # part/of stripped before apply
    assert "part" not in long_calls[0]["extraction"]
    assert "of" not in long_calls[0]["extraction"]
    # all the thread's chunks marked
    assert _enriched_count(store, ["d-x1", "d-x2"]) == {"d-x1": 1, "d-x2": 1}
    assert summary["applied"] == 1


def test_drain_truncated_multipart_marks_only_delivered_parts(store, home, caplog):
    # Extractor delivered only part 1 of a 3-part thread. drain applies what it
    # has (apply is idempotent, so a real partial converges next cycle) and warns,
    # but it must mark ONLY the chunks whose messages were actually extracted. The
    # dropped parts' chunks stay enriched=0 so they re-queue rather than being
    # silently lost.
    _seed_chunk(store, "d-x1", "t-long", message_id="t-long-m1")  # delivered
    _seed_chunk(store, "d-x2", "t-long", message_id="t-long-m2")  # dropped
    part1 = _envelope("t-long", part=1, of=3,
                      messages=[{"message_id": "t-long-m1", "sender": "A <a@x.com>",
                                 "date": "2026-04-18", "labels": "INBOX", "subject": "S"}])
    _write_inbox(home, "batch.json", _batch("batch-1", [part1]))

    app = RecordingApply()
    with caplog.at_level("WARNING", logger="mcpbrain.drain"):
        summary = drain.drain(store, home=home, apply=app)

    long_calls = [c for c in app.calls if c["thread_id"] == "t-long"]
    assert len(long_calls) == 1, "the incomplete thread is still applied"
    assert "part" not in long_calls[0]["extraction"]
    assert summary["applied"] == 1
    # Only the delivered message's chunk is marked; the dropped part re-queues.
    assert _enriched_count(store, ["d-x1", "d-x2"]) == {"d-x1": 1, "d-x2": 0}
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("t-long" in m and "of=3" in m for m in warnings), \
        "a truncated multi-part thread must emit a warning naming the thread"


def test_drain_marks_only_extracted_messages(store, home):
    # A message that arrived in the thread after prepare ran (so it was never in
    # the extraction the session saw) must NOT be marked enriched — otherwise it
    # would never re-queue and would be silently lost.
    _seed_chunk(store, "d-a", "t-a", message_id="t-a-m1")     # in the extraction
    _seed_chunk(store, "d-late", "t-a", message_id="t-a-late")  # arrived after
    _write_inbox(home, "batch.json", _batch("batch-1", [_envelope("t-a")]))

    app = RecordingApply()
    drain.drain(store, home=home, apply=app)

    assert _enriched_count(store, ["d-a", "d-late"]) == {"d-a": 1, "d-late": 0}
    assert app.calls[0]["doc_ids"] == ["d-a"]


def test_drain_marks_chunk_with_no_thread_id(store, home):
    # A chunk that carries no thread_id is grouped under its message_id (or
    # doc_id) by thread_enrich, so the extraction's thread_id IS that fallback
    # key. thread_chunks(thread_id) would find nothing; message-id recovery still
    # marks the right chunk.
    store.upsert_chunk("d-solo", "body", "hash-d-solo", {"message_id": "msg-solo"})
    env = _envelope("msg-solo",
                    messages=[{"message_id": "msg-solo", "sender": "A <a@x.com>",
                               "date": "2026-04-18", "labels": "INBOX", "subject": "S"}])
    _write_inbox(home, "batch.json", _batch("batch-1", [env]))

    app = RecordingApply()
    drain.drain(store, home=home, apply=app)

    assert _enriched_count(store, ["d-solo"]) == {"d-solo": 1}
    assert app.calls[0]["doc_ids"] == ["d-solo"]


# --- 4.3: merge-review answers --------------------------------------------


def _pair_id(a_id, b_id):
    return "|".join(sorted((a_id, b_id)))


def test_drain_applies_merge_answers(store, home):
    # winner is the higher-mentions entity; loser folds into it.
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", "Centrepoint", "2026-04-01")
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", "Centrepoint", "2026-04-02")  # mentions=2
    store.upsert_entity("j-chelliah", "J Chelliah", "person", "Centrepoint", "2026-04-01")  # mentions=1

    ans = {"pair_id": _pair_id("joel-chelliah", "j-chelliah"),
           "same": True, "canonical": "Joel Chelliah"}
    _write_inbox(home, "batch.json", _batch("batch-1", [], merge_answers=[ans]))

    summary = drain.drain(store, home=home, apply=RecordingApply())

    # loser j-chelliah is gone; winner joel-chelliah survives
    assert store.get_entity("j-chelliah") is None
    assert store.get_entity("joel-chelliah") is not None
    merges = store.list_entity_merges()
    assert len(merges) == 1
    assert merges[0]["winner_id"] == "joel-chelliah"
    assert merges[0]["loser_id"] == "j-chelliah"
    assert merges[0]["method"] == "llm"
    assert summary["merges"] == 1


def test_drain_merge_answer_same_false_noop(store, home):
    store.upsert_entity("ent-a", "Alpha", "person", "Centrepoint", "2026-04-01")
    store.upsert_entity("ent-b", "Beta", "person", "Centrepoint", "2026-04-01")
    ans = {"pair_id": _pair_id("ent-a", "ent-b"), "same": False, "canonical": ""}
    _write_inbox(home, "batch.json", _batch("batch-1", [], merge_answers=[ans]))

    summary = drain.drain(store, home=home, apply=RecordingApply())

    assert store.get_entity("ent-a") is not None
    assert store.get_entity("ent-b") is not None
    assert store.list_entity_merges() == []
    assert summary["merges"] == 0


def test_drain_merge_answer_unknown_pair_skipped(store, home):
    # Neither id exists (a prior cycle already merged them away).
    ans = {"pair_id": _pair_id("ghost-1", "ghost-2"), "same": True, "canonical": ""}
    _write_inbox(home, "batch.json", _batch("batch-1", [], merge_answers=[ans]))

    summary = drain.drain(store, home=home, apply=RecordingApply())  # no crash

    assert store.list_entity_merges() == []
    assert summary["merges"] == 0


# --- 4.4: delete on full success + idempotency -----------------------------


def test_drain_deletes_file_on_success(store, home):
    _seed_chunk(store, "d-a", "t-a")
    path = _write_inbox(home, "batch.json", _batch("batch-1", [_envelope("t-a")]))

    drain.drain(store, home=home, apply=RecordingApply())

    assert not path.exists(), "a fully-applied file should be deleted"
    assert not (home / "enrich_inbox" / "bad" / "batch.json").exists()


def test_drain_idempotent_rerun(store, home):
    # First run consumes and deletes the file. A second run with the file gone
    # is a no-op. Re-applying the same extraction is safe because apply() upserts
    # (Phase 1's dedup); the stub records calls but performs no double-write here.
    _seed_chunk(store, "d-a", "t-a")
    _write_inbox(home, "batch.json", _batch("batch-1", [_envelope("t-a")]))

    app1 = RecordingApply()
    s1 = drain.drain(store, home=home, apply=app1)
    assert s1["files"] == 1
    assert len(app1.calls) == 1

    app2 = RecordingApply()
    s2 = drain.drain(store, home=home, apply=app2)
    assert s2 == {"files": 0, "applied": 0, "marked": 0, "merges": 0,
                  "quarantined": 0, "entities": 0, "relations": 0}
    assert app2.calls == []


def test_drain_merge_failure_is_nonfatal(store, home, monkeypatch):
    # A per-answer merge failure inside _apply_merge_answers is swallowed
    # (try/continue), so file_ok stays True: the file is still deleted and the
    # successful extraction's chunks stay marked. Merges are idempotent, so a
    # dropped one is re-adjudicated next cycle rather than blocking the file.
    store.upsert_entity("ent-a", "Alpha Longname", "person", "Centrepoint", "2026-04-01")
    store.upsert_entity("ent-b", "Beta", "person", "Centrepoint", "2026-04-01")

    def _boom(*args, **kwargs):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(store, "merge_entities", _boom)

    _seed_chunk(store, "d-a", "t-a")
    ans = {"pair_id": _pair_id("ent-a", "ent-b"), "same": True, "canonical": "Alpha Longname"}
    path = _write_inbox(home, "batch.json",
                        _batch("batch-1", [_envelope("t-a")], merge_answers=[ans]))

    summary = drain.drain(store, home=home, apply=RecordingApply())  # no crash

    assert not path.exists(), "a merge failure is non-fatal; the file is still deleted"
    assert _enriched_count(store, ["d-a"]) == {"d-a": 1}, "the extraction stays marked"
    assert summary["merges"] == 0, "the failed merge is not counted"
    assert summary["applied"] == 1


def test_drain_summary_counts(store, home):
    store.upsert_entity("ent-a", "Alpha Longname", "person", "Centrepoint", "2026-04-01")
    store.upsert_entity("ent-a", "Alpha Longname", "person", "Centrepoint", "2026-04-02")  # mentions=2
    store.upsert_entity("ent-b", "Beta", "person", "Centrepoint", "2026-04-01")
    _seed_chunk(store, "d-a", "t-a")
    _seed_chunk(store, "d-b", "t-b")
    ans = {"pair_id": _pair_id("ent-a", "ent-b"), "same": True, "canonical": "Alpha Longname"}
    _write_inbox(home, "batch.json",
                 _batch("batch-1", [_envelope("t-a"), _envelope("t-b")], merge_answers=[ans]))

    summary = drain.drain(store, home=home, apply=RecordingApply())

    assert summary == {"files": 1, "applied": 2, "marked": 2, "merges": 1,
                       "quarantined": 0, "entities": 2, "relations": 2}


# --- 4.5: surfacing apply's entity/relation counts -------------------------


def test_drain_sums_apply_entity_relation_counts(store, home):
    # drain accumulates apply()'s own entity/relation counts across every
    # extraction. The two threads' applies return entities 2 and 3 -> summed
    # entities == 5; relations 1 and 4 -> summed relations == 5.
    _seed_chunk(store, "d-a", "t-a")
    _seed_chunk(store, "d-b", "t-b")
    _write_inbox(home, "batch.json",
                 _batch("batch-1", [_envelope("t-a"), _envelope("t-b")]))

    app = RecordingApply(per_thread={
        "t-a": {"entities": 2, "relations": 1},
        "t-b": {"entities": 3, "relations": 4},
    })
    summary = drain.drain(store, home=home, apply=app)

    assert summary["entities"] == 5
    assert summary["relations"] == 5
    assert summary["applied"] == 2


def test_drain_handles_apply_returning_none(store, home):
    # A minimal apply that returns None (or a dict without the count keys) must
    # not crash drain; the counts simply stay at 0.
    _seed_chunk(store, "d-a", "t-a")
    _write_inbox(home, "batch.json", _batch("batch-1", [_envelope("t-a")]))

    summary = drain.drain(store, home=home, apply=RecordingApply(returns=None))

    assert summary["applied"] == 1
    assert summary["entities"] == 0


def test_consumed_pending_json_deleted(store, home):
    import json as _json
    queue = home / "enrich_queue"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "pending.json").write_text(_json.dumps({"batch_id": "batch-1"}))
    _write_inbox(home, "b1.json", _batch("batch-1", [_envelope("t-1")]))
    app = RecordingApply()
    drain.drain(store, home=home, apply=app)
    assert not (queue / "pending.json").exists()


def test_unrelated_pending_json_kept(store, home):
    import json as _json
    queue = home / "enrich_queue"
    queue.mkdir(parents=True, exist_ok=True)
    (queue / "pending.json").write_text(_json.dumps({"batch_id": "batch-OTHER"}))
    _write_inbox(home, "b1.json", _batch("batch-1", [_envelope("t-1")]))
    drain.drain(store, home=home, apply=RecordingApply())
    assert (queue / "pending.json").exists()
