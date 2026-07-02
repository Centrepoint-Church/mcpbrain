"""Tests for the daemon-side drain step (Task 4).

drain consumes enrich_inbox/*.json: validates each file against the contract,
applies each extraction through Phase 1's apply() (injected as a stub here),
marks that thread's chunks enriched, feeds merge-review answers into entity
resolution, and deletes the file on full success. Malformed files are
quarantined to enrich_inbox/bad/ rather than crashing the daemon.

apply() is graph_write.apply, injected as a parameter. These tests pass a
recording stub matching its real signature: apply(store, extraction, *,
doc_ids, entity_index=None). drain accepts an embedder= but does not forward
it to apply (the structural apply takes no embedder), so the stub does not
accept one. entity_index is forwarded only when write_time_dedup_enabled is
True (default since Task 5.3), so the stub must accept (and can ignore) it.
The end-to-end round trip against the real apply lives in
test_integration_spool.py.
"""

import json

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
        "org": "Acme",
        "content_type": "update",
        "summary": "A short summary.",
        "entities": [],
        "topics": [],
        "actions": [],
        "relations": [],
        "messages": [
            {"message_id": f"{thread_id}-m1", "sender": "Joel <joel@example.org>",
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

    def __call__(self, store, extraction, *, doc_ids, entity_index=None):
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


def test_drain_skips_invalid_extraction_not_whole_batch(store, home):
    # A per-extraction contract violation (org missing) is skipped individually,
    # NOT quarantined with the whole file — one bad extraction must not discard
    # a batch. The file is still consumed; the skipped thread's chunks stay
    # enriched=0 and re-queue next prepare.
    bad_env = _envelope("t-bad", org=None)
    _write_inbox(home, "violation.json", _batch("batch-bad", [bad_env]))

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    assert not (home / "enrich_inbox" / "bad" / "violation.json").exists()
    assert not (home / "enrich_inbox" / "violation.json").exists()  # consumed
    assert app.calls == [], "the invalid extraction must not be applied"
    assert summary["quarantined"] == 0
    assert summary["skipped"] == 1


def test_drain_applies_good_skips_bad_in_mixed_batch(store, home):
    # One valid + one invalid extraction: the valid applies, the invalid is
    # skipped, and the batch is not quarantined.
    good = _envelope("t-good")
    bad = _envelope("t-bad", content_type="not-a-real-type")
    _seed_chunk(store, "d-good", "t-good")  # the valid extraction's thread has chunks
    _write_inbox(home, "mixed.json", _batch("batch-mixed", [good, bad]))

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    assert [c["thread_id"] for c in app.calls] == ["t-good"]
    assert summary["skipped"] == 1
    assert summary["quarantined"] == 0


def test_drain_sanitizes_empty_relations_keeps_extraction(store, home):
    # An otherwise-valid extraction with one good relation and one stub relation
    # (empty fields) — the LLM noise that used to fail the whole batch. The stub
    # is dropped; the extraction applies with its good relation intact.
    env = _envelope("t-rel", relations=[
        {"source_name": "Joel", "type": "works_at", "target_name": "Sam"},
        {"source_name": "", "type": "", "target_name": ""},
    ])
    _seed_chunk(store, "d-rel", "t-rel")  # the extraction's thread has chunks
    _write_inbox(home, "rel.json", _batch("batch-rel", [env]))

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    assert summary["quarantined"] == 0 and summary["skipped"] == 0
    assert summary["dropped_items"] == 1
    assert len(app.calls) == 1
    assert len(app.calls[0]["extraction"]["relations"]) == 1  # only valid one


def test_drain_quarantines_wrapper_violation(store, home):
    # A malformed merge_answer (irreversible-merge risk) is a WRAPPER violation
    # and still quarantines the whole file.
    env = _envelope("t-ok")
    batch = _batch("batch-wrap", [env],
                   merge_answers=[{"pair_id": "p1", "same": "false"}])  # not bool
    _write_inbox(home, "wrap.json", batch)

    app = RecordingApply()
    summary = drain.drain(store, home=home, apply=app)

    assert (home / "enrich_inbox" / "bad" / "wrap.json").exists()
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
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", "Acme", "2026-04-01")
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", "Acme", "2026-04-02")  # mentions=2
    store.upsert_entity("j-chelliah", "J Chelliah", "person", "Acme", "2026-04-01")  # mentions=1

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
    store.upsert_entity("ent-a", "Alpha", "person", "Acme", "2026-04-01")
    store.upsert_entity("ent-b", "Beta", "person", "Acme", "2026-04-01")
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


def test_drain_merge_answers_use_configured_review_cap(store, home, monkeypatch):
    """drain's merge_answers call site sources cap= from
    config.review_max_apply_per_run, the same established pattern as the four
    review_* BLOCK_DRAINERS registrations (see
    test_block_drainers_use_configured_review_cap below)."""
    from mcpbrain import config

    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    config.write_config(str(home), {"review_max_apply_per_run": 3})

    captured = {}

    def _fake(store_arg, answers, *, cap):
        captured["cap"] = cap
        return {"merged": 0, "guarded": 0, "capped": 0, "skipped": 0}

    monkeypatch.setattr(drain.review_apply, "apply_duplicate_verdicts", _fake)

    ans = {"pair_id": _pair_id("ghost-1", "ghost-2"), "same": True, "canonical": ""}
    _write_inbox(home, "batch.json", _batch("batch-1", [], merge_answers=[ans]))

    drain.drain(store, home=home, apply=RecordingApply())

    assert captured["cap"] == 3


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
                  "quarantined": 0, "entities": 0, "relations": 0,
                  "skipped": 0, "dropped_items": 0}
    assert app2.calls == []


def test_drain_merge_failure_is_nonfatal(store, home, monkeypatch):
    # A per-answer merge failure inside _apply_merge_answers is swallowed
    # (try/continue), so file_ok stays True: the file is still deleted and the
    # successful extraction's chunks stay marked. Merges are idempotent, so a
    # dropped one is re-adjudicated next cycle rather than blocking the file.
    store.upsert_entity("ent-a", "Alpha Longname", "person", "Acme", "2026-04-01")
    store.upsert_entity("ent-b", "Beta", "person", "Acme", "2026-04-01")

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
    store.upsert_entity("ent-a", "Alpha Longname", "person", "Acme", "2026-04-01")
    store.upsert_entity("ent-a", "Alpha Longname", "person", "Acme", "2026-04-02")  # mentions=2
    store.upsert_entity("ent-b", "Beta", "person", "Acme", "2026-04-01")
    _seed_chunk(store, "d-a", "t-a")
    _seed_chunk(store, "d-b", "t-b")
    ans = {"pair_id": _pair_id("ent-a", "ent-b"), "same": True, "canonical": "Alpha Longname"}
    _write_inbox(home, "batch.json",
                 _batch("batch-1", [_envelope("t-a"), _envelope("t-b")], merge_answers=[ans]))

    summary = drain.drain(store, home=home, apply=RecordingApply())

    assert summary == {"files": 1, "applied": 2, "marked": 2, "merges": 1,
                       "quarantined": 0, "entities": 2, "relations": 2,
                       "skipped": 0, "dropped_items": 0}


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


def test_consumed_unit_file_deleted(store, home):
    # Work-queue: draining a unit's result deletes its unit file + lease claim.
    units = home / "enrich_queue" / "units"
    claims = home / "enrich_queue" / "claims"
    units.mkdir(parents=True, exist_ok=True)
    claims.mkdir(parents=True, exist_ok=True)
    (units / "u-1.json").write_text("{}")
    (claims / "u-1").write_text("")
    inbox_obj = dict(_batch("batch-1", [_envelope("t-1")]))
    inbox_obj["unit_id"] = "u-1"
    _write_inbox(home, "u-1.json", inbox_obj)
    drain.drain(store, home=home, apply=RecordingApply())
    assert not (units / "u-1.json").exists()
    assert not (claims / "u-1").exists()


def test_drain_attaches_unit_messages_before_apply(tmp_path, monkeypatch):
    # When the model omits messages[] (as allowed by Task 2.3), drain reads the
    # unit file from enrich_queue/units/ and injects the unit's original messages
    # into the extraction before apply() so graph_write derives lead msg/date/sender
    # from authoritative data, not the model's echo.
    from mcpbrain.store import Store

    home = tmp_path
    (home / "enrich_inbox").mkdir(parents=True)
    units_dir = home / "enrich_queue" / "units"
    units_dir.mkdir(parents=True)

    s = Store(tmp_path / "brain.db", dim=4)
    s.init()

    unit_id = "u-test-inject"
    unit_messages = [{"message_id": "m1", "sender": "a@x.org", "date": "2026-02-01"}]

    # Write the unit file — this carries the authoritative thread messages
    unit_file = units_dir / f"{unit_id}.json"
    unit_file.write_text(json.dumps({
        "unit_id": unit_id,
        "kind": "thread",
        "threads": [{"thread_id": "t-inject", "messages": unit_messages}],
    }))

    # Seed a chunk so doc_ids_for_messages has something to return
    _seed_chunk(s, "d-inject", "t-inject", message_id="m1")

    # The extraction the model returns — messages[] intentionally absent
    ext_no_messages = {
        "thread_id": "t-inject",
        "org": "Acme",
        "content_type": "update",
        "summary": "A summary without messages.",
        "entities": [],
        "topics": ["logistics"],
        "actions": [],
        "relations": [],
    }

    # Write the inbox file as if brain_enrich_push wrote it (no messages in extraction)
    inbox_obj = {
        "unit_id": unit_id,
        "extractions": [ext_no_messages],
        "merge_answers": [],
    }
    _write_inbox(home, f"{unit_id}.json", inbox_obj)

    captured = {}

    def fake_apply(store, extraction, *, doc_ids, **kw):
        captured["messages"] = extraction.get("messages")
        return {"entities": 0, "relations": 0, "actions": 0}

    drain.drain(s, home=home, apply=fake_apply)

    assert captured["messages"] == unit_messages, (
        f"drain must inject unit messages before apply(); got {captured.get('messages')!r}"
    )


def test_drain_does_not_overwrite_model_messages(tmp_path, monkeypatch):
    # When the model DOES supply messages[], drain must NOT overwrite them with
    # unit messages. The model's messages are authoritative for the extraction.
    from mcpbrain.store import Store

    home = tmp_path
    (home / "enrich_inbox").mkdir(parents=True)
    units_dir = home / "enrich_queue" / "units"
    units_dir.mkdir(parents=True)

    s = Store(tmp_path / "brain.db", dim=4)
    s.init()

    unit_id = "u-test-nooverwrite"
    unit_messages = [{"message_id": "unit-m1", "sender": "unit@x.org", "date": "2026-01-01"}]
    model_messages = [{"message_id": "model-m1", "sender": "model@x.org", "date": "2026-02-01"}]

    # Write the unit file with its own messages
    unit_file = units_dir / f"{unit_id}.json"
    unit_file.write_text(json.dumps({
        "unit_id": unit_id,
        "kind": "thread",
        "threads": [{"thread_id": "t-nooverwrite", "messages": unit_messages}],
    }))

    # Seed a chunk so doc_ids_for_messages has something to return
    _seed_chunk(s, "d-nooverwrite", "t-nooverwrite", message_id="model-m1")

    # The extraction the model returns — messages[] PRESENT
    ext_with_messages = {
        "thread_id": "t-nooverwrite",
        "org": "Acme",
        "content_type": "update",
        "summary": "A summary with model messages.",
        "entities": [],
        "topics": ["logistics"],
        "actions": [],
        "relations": [],
        "messages": model_messages,
    }

    # Write the inbox file with model-supplied messages
    inbox_obj = {
        "unit_id": unit_id,
        "extractions": [ext_with_messages],
        "merge_answers": [],
    }
    _write_inbox(home, f"{unit_id}.json", inbox_obj)

    captured = {}

    def fake_apply(store, extraction, *, doc_ids, **kw):
        captured["messages"] = extraction.get("messages")
        return {"entities": 0, "relations": 0, "actions": 0}

    drain.drain(s, home=home, apply=fake_apply)

    assert captured["messages"] == model_messages, (
        f"drain must NOT overwrite model-supplied messages; "
        f"expected {model_messages!r}, got {captured.get('messages')!r}"
    )


def test_drain_recovers_doc_ids_from_unit_when_model_ids_unresolvable(tmp_path):
    # I-1: a VALID content-bearing extraction whose model-supplied message ids do not
    # resolve to chunks must recover the chunks from the unit's CANONICAL messages
    # (authoritative), apply with those doc_ids, and mark them enriched — not silently
    # "apply but mark nothing" and re-queue forever.
    from mcpbrain.store import Store

    home = tmp_path
    (home / "enrich_inbox").mkdir(parents=True)
    units_dir = home / "enrich_queue" / "units"
    units_dir.mkdir(parents=True)
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()

    unit_id = "u-recover"
    unit_messages = [{"message_id": "good-m1", "sender": "a@x.org", "date": "2026-02-01"}]
    (units_dir / f"{unit_id}.json").write_text(json.dumps({
        "unit_id": unit_id, "kind": "thread",
        "threads": [{"thread_id": "t-recover", "messages": unit_messages}],
    }))
    _seed_chunk(s, "d-recover", "t-recover", message_id="good-m1")

    # Model supplies messages[] (so injection is skipped) but with an id that does NOT
    # resolve to any chunk.
    ext = {
        "thread_id": "t-recover", "org": "Acme", "content_type": "update",
        "summary": "Valid content.", "entities": [], "topics": ["x"],
        "actions": [], "relations": [],
        "messages": [{"message_id": "bad-m1", "sender": "a@x.org", "date": "2026-02-01"}],
    }
    _write_inbox(home, f"{unit_id}.json", {"unit_id": unit_id, "extractions": [ext], "merge_answers": []})

    captured = {}

    def fake_apply(store, extraction, *, doc_ids, **kw):
        captured["doc_ids"] = list(doc_ids)
        return {"entities": 0, "relations": 0, "actions": 0}

    summary = drain.drain(s, home=home, apply=fake_apply)
    assert captured.get("doc_ids") == ["d-recover"], (
        f"drain must recover doc_ids from the unit's canonical messages; got {captured.get('doc_ids')!r}"
    )
    assert summary["applied"] == 1 and summary["marked"] == 1


def test_drain_caps_valid_extraction_with_unrecoverable_chunks(tmp_path):
    # I-1: a VALID content-bearing extraction we cannot tie to ANY chunk (model rewrote
    # the thread_id) must NOT be counted as a phantom apply, and must bump the Q8 cap on
    # the unit's chunks so the underlying chunk can't re-queue forever.
    from mcpbrain.store import Store

    home = tmp_path
    (home / "enrich_inbox").mkdir(parents=True)
    units_dir = home / "enrich_queue" / "units"
    units_dir.mkdir(parents=True)
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()

    unit_id = "u-phantom"
    unit_messages = [{"message_id": "u-m1", "sender": "a@x.org", "date": "2026-02-01"}]
    (units_dir / f"{unit_id}.json").write_text(json.dumps({
        "unit_id": unit_id, "kind": "thread",
        "threads": [{"thread_id": "t-real", "messages": unit_messages}],
    }))
    _seed_chunk(s, "d-real", "t-real", message_id="u-m1")

    # Model returns a valid extraction but with a thread_id that matches NO unit thread
    # and no resolvable messages.
    ext = {
        "thread_id": "t-PHANTOM", "org": "Acme", "content_type": "update",
        "summary": "Valid content.", "entities": [], "topics": ["x"],
        "actions": [], "relations": [],
    }
    _write_inbox(home, f"{unit_id}.json", {"unit_id": unit_id, "extractions": [ext], "merge_answers": []})

    bumped = []
    orig = s.bump_enrich_attempts
    s.bump_enrich_attempts = lambda dids: (bumped.append(list(dids)) or orig(dids))

    apply_calls = []
    summary = drain.drain(s, home=home, apply=lambda *a, **k: apply_calls.append(1) or {})
    assert not apply_calls, "must NOT apply (write phantom-provenance edges) when no chunk is identifiable"
    assert summary.get("applied", 0) == 0, "must not count a phantom apply"
    assert any("d-real" in c for c in bumped), f"must bump the unit's chunk toward the cap; got {bumped!r}"


def test_drain_attempt_cap_fires_when_invalid_extraction_has_unit_messages(tmp_path):
    """Fix 2 regression: message injection must happen BEFORE validate_extraction.

    When an extraction is contract-invalid AND the model omitted messages[], the Q8
    attempt-cap logic must still bump and (eventually) consume the chunk. Before the
    fix, injection happened after validate_extraction — so a contract-invalid
    extraction with no model messages had no ids to look up and the chunk re-queued
    forever without ever hitting the attempt cap.

    This test:
    1. Writes a unit file carrying the authoritative thread messages.
    2. Posts a contract-invalid extraction (missing required 'org') with no messages[].
    3. Verifies that bump_enrich_attempts IS called (i.e., injection happened first).
    """
    from mcpbrain.store import Store

    home = tmp_path
    (home / "enrich_inbox").mkdir(parents=True)
    units_dir = home / "enrich_queue" / "units"
    units_dir.mkdir(parents=True)

    s = Store(tmp_path / "brain.db", dim=4)
    s.init()

    unit_id = "u-test-cap"
    unit_messages = [{"message_id": "m-cap-1", "sender": "a@x.org", "date": "2026-02-01",
                      "labels": "INBOX", "subject": "Test"}]

    # Authoritative unit file with messages for this thread
    (units_dir / f"{unit_id}.json").write_text(json.dumps({
        "unit_id": unit_id,
        "kind": "thread",
        "threads": [{"thread_id": "t-cap", "messages": unit_messages}],
    }))

    # Seed the chunk so doc_ids_for_messages returns something
    _seed_chunk(s, "d-cap", "t-cap", message_id="m-cap-1")

    # Contract-invalid extraction: 'org' is None which fails validate_extraction.
    # No messages[] supplied — relies on unit injection to provide them.
    invalid_ext = {
        "thread_id": "t-cap",
        "org": None,          # invalid — will fail validate_extraction
        "content_type": "update",
        "summary": "A short summary.",
        "entities": [],
        "topics": [],
        "actions": [],
        "relations": [],
        # messages[] intentionally absent — must be injected from unit file
    }

    inbox_obj = {
        "unit_id": unit_id,
        "extractions": [invalid_ext],
        "merge_answers": [],
    }
    _write_inbox(home, f"{unit_id}.json", inbox_obj)

    bump_calls = []
    original_bump = s.bump_enrich_attempts

    def recording_bump(doc_ids):
        bump_calls.append(list(doc_ids))
        return original_bump(doc_ids)

    s.bump_enrich_attempts = recording_bump

    summary = drain.drain(s, home=home, apply=RecordingApply())

    assert summary["skipped"] == 1, "the contract-invalid extraction must be skipped"
    assert bump_calls, (
        "bump_enrich_attempts must be called — message injection must happen before "
        "validate_extraction so the Q8 attempt-cap has ids to work with"
    )
    # The chunk's doc_id must appear in the bumped set
    all_bumped = [did for call in bump_calls for did in call]
    assert "d-cap" in all_bumped, (
        f"chunk d-cap must be in the bumped set; got {bump_calls!r}"
    )


# --- Task 4.2: review_* BLOCK_DRAINERS pull their cap from config -----------
#
# Tasks 2.1-3.2 registered the four review_* BLOCK_DRAINERS with a literal
# cap=50 ("cap is a literal until Task 4.1 wires this to
# config.review_max_apply_per_run"). Task 4.1 added the config accessor;
# Task 4.2 wires it in. These tests exercise the registration lambdas
# directly (not the apply_*_verdicts functions, which the review_apply tests
# already cover with an explicit cap= argument) to confirm the wiring itself.

@pytest.mark.parametrize("block_key,fn_name", [
    ("review_orphan", "apply_orphan_verdicts"),
    ("review_missing_org", "apply_missing_org_verdicts"),
    ("review_ownerless", "apply_ownerless_verdicts"),
    ("review_org", "apply_org_verdicts"),
])
def test_block_drainers_use_configured_review_cap(
        tmp_path, monkeypatch, block_key, fn_name):
    """Each review_* registration passes config.review_max_apply_per_run's
    value as cap=, not a hardcoded 50."""
    from mcpbrain import config

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    config.write_config(str(tmp_path), {"review_max_apply_per_run": 3})

    captured = {}

    def _fake(store, verdicts, *, cap, **kwargs):
        captured["cap"] = cap
        return {}

    monkeypatch.setattr(drain.review_apply, fn_name, _fake)

    drain.BLOCK_DRAINERS[block_key](None, {block_key: []})

    assert captured["cap"] == 3
