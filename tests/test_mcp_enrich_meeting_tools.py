"""Host-native MCP tools for the autonomous loops (enrich + meeting-packs).

These exist so the hourly Cowork tasks never depend on shell/curl reaching the
host: per the Cowork desktop architecture, shell + curl run in an isolated VM,
but local plugin MCP servers run natively on the host. Routing the enrich spool
and meeting packs through MCP makes those loops VM-proof.
"""
import asyncio
import json

from mcpbrain.store import Store
from mcpbrain import mcp_server


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


# --- enrich pull/push -------------------------------------------------------

def test_brain_routine_serves_bundled_protocols():
    # The recurring routines are served via MCP (brain_routine) from protocols
    # bundled in the wheel, so a scheduled task needs no plugin skill/command.
    ri = mcp_server._routine_instructions
    assert ri("does-not-exist") is None
    enrich = ri("enrich")
    assert enrich and "brain_enrich_pull" in enrich and "brain_enrich_push" in enrich
    gardener = ri("gardener")
    assert gardener and "GARDENER-PROTECTED" in gardener
    mp = ri("meeting-packs")
    assert mp and "context_hash" in mp and "brain_meetings_today" in mp
    rg = ri("reference-gardener")
    assert rg and "reference/_proposals/" in rg   # proposes for review, never overwrites
    assert "brain_note" in rg and "propose" in rg.lower()


def test_enrich_pull_bounds_response_size(tmp_path):
    # A full batch can have ~100 threads and blow past the MCP tool-result token
    # cap. pull must return only as many threads as fit its char budget, flag
    # `more`, and report the totals.
    (tmp_path / "enrich_queue").mkdir()
    big = "x" * 5000
    threads = [{"thread_id": f"t{i}", "body": big} for i in range(100)]
    (tmp_path / "enrich_queue" / "pending.json").write_text(
        json.dumps({"batch_id": "b1", "threads": threads, "context": {}, "merge_review": []}))
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))())
    assert out["threads_total"] == 100
    assert out["threads_returned"] < 100 and out["more"] is True
    assert len(json.dumps(out)) <= mcp_server._PULL_MAX_CHARS + 6000  # ~budget + one thread overshoot


def test_enrich_pull_returns_pending_batch(tmp_path):
    (tmp_path / "enrich_queue").mkdir()
    batch = {"batch_id": "b1", "threads": [{"thread_id": "t1"}], "context": {}}
    (tmp_path / "enrich_queue" / "pending.json").write_text(json.dumps(batch))
    pull = mcp_server.make_brain_enrich_pull(str(tmp_path))
    out = asyncio.run(pull())
    assert out["batch_id"] == "b1"
    assert out["threads"][0]["thread_id"] == "t1"


def test_enrich_pull_bundles_extraction_rules(tmp_path):
    # The pull response must carry the full extraction protocol in `rules`, so the
    # enrich caller is self-contained — no plugin skill file or source repo needed
    # (regression: a scheduled-task stub previously had to read rules from the repo).
    (tmp_path / "enrich_queue").mkdir()
    (tmp_path / "enrich_queue" / "pending.json").write_text(
        json.dumps({"batch_id": "b1", "threads": [{"thread_id": "t1"}], "context": {}}))
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))())
    assert "rules" in out and isinstance(out["rules"], str) and out["rules"]
    # The rules are the bundled enrich_prompt.md SHARED block — spot-check content.
    assert "extraction envelope" in out["rules"].lower()
    assert "content_type" in out["rules"]
    # Sourced from the shipped wheel file, not a skill/repo path.
    from pathlib import Path
    import mcpbrain
    canonical = (Path(mcpbrain.__file__).parent / "enrich_prompt.md").read_text()
    assert out["rules"] in canonical


def test_enrich_pull_empty_when_no_spool(tmp_path):
    pull = mcp_server.make_brain_enrich_pull(str(tmp_path))
    assert asyncio.run(pull()) == {"empty": True}


def test_enrich_pull_empty_when_threads_empty(tmp_path):
    (tmp_path / "enrich_queue").mkdir()
    (tmp_path / "enrich_queue" / "pending.json").write_text(
        json.dumps({"batch_id": "b1", "threads": []}))
    pull = mcp_server.make_brain_enrich_pull(str(tmp_path))
    assert asyncio.run(pull()) == {"empty": True}


def test_enrich_push_writes_inbox_file_drain_can_read(tmp_path):
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    extractions = [{"thread_id": "t1", "org": "Acme", "content_type": "request",
                    "summary": "s", "messages": [{"message_id": "m1", "date": "2026-06-01"}],
                    "entities": [], "actions": [], "relations": [], "topics": []}]
    out = asyncio.run(push("b1", extractions, []))
    assert out["written"] is True
    written = json.loads((tmp_path / "enrich_inbox" / "b1.json").read_text())
    assert written["batch_id"] == "b1"
    assert written["extractions"][0]["thread_id"] == "t1"
    assert written["merge_answers"] == []


def test_enrich_push_rejects_bad_input(tmp_path):
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    assert asyncio.run(push("", []))["written"] is False
    assert asyncio.run(push("b1", "notalist"))["written"] is False


# --- meeting packs ----------------------------------------------------------

def test_meeting_pack_upsert_and_get_roundtrip_with_context_hash(tmp_path):
    s = _store(tmp_path)
    upsert = mcp_server.make_brain_meeting_pack_upsert(s)
    out = asyncio.run(upsert("evt1", "Planning", "2026-06-06", "## Pack",
                             ["Alice"], "hash-1"))
    assert out["ok"] is True
    get = mcp_server.make_brain_meeting_pack_get(s)
    pack = asyncio.run(get("evt1"))
    assert pack["pack_text"] == "## Pack"
    assert pack["context_hash"] == "hash-1"


def test_meeting_pack_get_missing_returns_not_found(tmp_path):
    s = _store(tmp_path)
    get = mcp_server.make_brain_meeting_pack_get(s)
    assert asyncio.run(get("nope")) == {"found": False}


def test_meeting_pack_upsert_requires_event_id(tmp_path):
    s = _store(tmp_path)
    upsert = mcp_server.make_brain_meeting_pack_upsert(s)
    assert asyncio.run(upsert("", "t", "d", "p"))["ok"] is False


def test_meetings_today_annotates_has_pack(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.upsert_meeting_pack("evt1", "Planning", "2026-06-06", "## Pack")
    from mcpbrain import dashboard
    monkeypatch.setattr(dashboard, "calendar_today",
                        lambda home: [{"id": "evt1", "title": "Planning"},
                                      {"id": "evt2", "title": "Other"}])
    tool = mcp_server.make_brain_meetings_today(s, str(tmp_path))
    out = asyncio.run(tool())
    by_id = {e["id"]: e for e in out}
    assert by_id["evt1"]["has_pack"] is True
    assert by_id["evt2"]["has_pack"] is False
