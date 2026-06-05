"""Generic optional-block plumbing: prepare merges extra_blocks; drain
dispatches registered per-key drainers (synthesis keeps its existing path)."""
import json

from mcpbrain import drain, prepare
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_prepare_merges_extra_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # No unenriched threads -> prepare returns zero summary and writes nothing,
    # so test the merge helper directly.
    data = {"batch_id": "b1", "threads": [], "context": {}, "merge_review": []}
    merged = prepare.attach_extra_blocks(data, {"profile_synthesis": [{"x": 1}],
                                                "empty_block": []})
    assert merged["profile_synthesis"] == [{"x": 1}]
    assert "empty_block" not in merged      # empty lists stay off the contract


def test_drain_dispatches_registered_block(tmp_path):
    s = _store(tmp_path)
    (tmp_path / "enrich_inbox").mkdir(parents=True)
    seen = {}

    def fake_drainer(store, inbox_obj):
        seen["items"] = inbox_obj["my_block"]
        return {"written": len(inbox_obj["my_block"])}

    drain.BLOCK_DRAINERS["my_block"] = fake_drainer
    try:
        env = {"thread_id": "t1", "org": "unknown", "content_type": "update",
               "summary": "s", "entities": [], "topics": [], "actions": [],
               "relations": [],
               "messages": [{"message_id": "m1", "sender": "A <a@b.c>",
                             "date": "2026-06-01", "labels": "", "subject": "x"}],
               "resolved_action_ids": [], "updated_actions": [],
               "reply_needed": False, "reply_reason": ""}
        (tmp_path / "enrich_inbox" / "b1.json").write_text(json.dumps(
            {"batch_id": "b1", "extractions": [env], "merge_answers": [],
             "my_block": [{"id": 1}]}))
        drain.drain(s, home=tmp_path, apply=lambda st, e, *, doc_ids: {})
        assert seen["items"] == [{"id": 1}]
    finally:
        del drain.BLOCK_DRAINERS["my_block"]


def test_block_drainer_failure_does_not_break_file(tmp_path):
    s = _store(tmp_path)
    (tmp_path / "enrich_inbox").mkdir(parents=True)

    def bad_drainer(store, inbox_obj):
        raise RuntimeError("boom")

    drain.BLOCK_DRAINERS["bad_block"] = bad_drainer
    try:
        env = {"thread_id": "t1", "org": "unknown", "content_type": "update",
               "summary": "s", "entities": [], "topics": [], "actions": [],
               "relations": [],
               "messages": [{"message_id": "m1", "sender": "A <a@b.c>",
                             "date": "2026-06-01", "labels": "", "subject": "x"}],
               "resolved_action_ids": [], "updated_actions": [],
               "reply_needed": False, "reply_reason": ""}
        (tmp_path / "enrich_inbox" / "b1.json").write_text(json.dumps(
            {"batch_id": "b1", "extractions": [env], "merge_answers": [],
             "bad_block": [{"id": 1}]}))
        summary = drain.drain(s, home=tmp_path, apply=lambda st, e, *, doc_ids: {})
        assert summary["applied"] == 1       # extraction still applied
        assert not (tmp_path / "enrich_inbox" / "b1.json").exists()  # file consumed
    finally:
        del drain.BLOCK_DRAINERS["bad_block"]
