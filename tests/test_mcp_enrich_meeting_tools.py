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


def test_enrich_pull_drops_oversized_optional_blocks(tmp_path):
    # Regression: a huge optional block (e.g. community_synthesis for a community
    # with thousands of members) must not push the pull past the MCP token cap.
    # Oversized optional blocks are dropped (re-attached next cycle); the core
    # thread extraction still comes back.
    (tmp_path / "enrich_queue").mkdir()
    huge = [{"community_id": 1, "member_count": 6000,
             "members": [f"Person {i}" for i in range(6000)]}]  # ~hundreds of KB
    (tmp_path / "enrich_queue" / "pending.json").write_text(json.dumps({
        "batch_id": "b1", "context": {}, "merge_review": [],
        "community_synthesis": huge,
        "threads": [{"thread_id": "t1", "body": "hi"}]}))
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))())
    assert len(json.dumps(out)) <= mcp_server._PULL_MAX_CHARS + 6000
    assert "community_synthesis" not in out          # oversized block dropped
    assert out["threads"][0]["thread_id"] == "t1"     # core extraction survives


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


def test_enrich_push_forwards_all_answer_blocks(tmp_path):
    # Regression: the batch requests synthesis/profile/community/memory/audit work
    # and the rules tell the LLM how to answer, but push used to drop everything
    # except extractions + merge_answers. Every present block must reach the inbox
    # (the daemon's drainers read each by key).
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    out = asyncio.run(push(
        batch_id="b1", extractions=[{"thread_id": "t1"}],
        merge_answers=[{"pair_id": "a|b", "same": True, "canonical": "X"}],
        synthesis=[{"thread_id": "t1", "contextual_summary": "..."}],
        profile_synthesis=[{"entity_id": "e1", "profile": "..."}],
        community_synthesis=[{"community_id": 1, "title": "Facilities", "summary": "..."}],
        memory_distil=[{"doc_id": "n1", "verdict": "keep"}],
        profile_audit=[{"entity_id": "e1", "corrections": []}],
    ))
    assert out["written"] is True
    d = json.loads((tmp_path / "enrich_inbox" / "b1.json").read_text())
    for k in ("merge_answers", "synthesis", "profile_synthesis",
              "community_synthesis", "memory_distil", "profile_audit"):
        assert k in d, f"{k} not forwarded to the inbox"
    assert d["community_synthesis"][0]["title"] == "Facilities"


def test_enrich_push_omits_blocks_not_supplied(tmp_path):
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    asyncio.run(push(batch_id="b2", extractions=[{"thread_id": "t1"}]))
    d = json.loads((tmp_path / "enrich_inbox" / "b2.json").read_text())
    assert d["merge_answers"] == []
    for k in ("synthesis", "community_synthesis", "profile_synthesis",
              "memory_distil", "profile_audit"):
        assert k not in d


def test_enrich_push_rejects_bad_input(tmp_path):
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    assert asyncio.run(push("", []))["written"] is False
    assert asyncio.run(push("b1", "notalist"))["written"] is False


# --- fan-out: manifest + per-shard pull + sharded push ----------------------

def _write_pending(tmp_path, **extra):
    (tmp_path / "enrich_queue").mkdir(exist_ok=True)
    data = {"batch_id": "b1", "context": {"owner_name": "Jo"}, "threads": [], **extra}
    (tmp_path / "enrich_queue" / "pending.json").write_text(json.dumps(data))
    return data


def test_manifest_partitions_threads_and_freezes_snapshot(tmp_path):
    # The manifest must cover every thread exactly once across shards (disjoint),
    # carry NO bodies, and freeze a snapshot the subagents pull from.
    big = "x" * 4000
    threads = [{"thread_id": f"t{i}", "body": big} for i in range(12)]
    _write_pending(tmp_path, threads=threads)
    out = asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))())
    assert out["batch_id"] == "b1" and out["thread_total"] == 12
    covered = [tid for s in out["shards"] for tid in s["thread_ids"]]
    assert sorted(covered) == sorted(t["thread_id"] for t in threads)  # disjoint + complete
    assert len(covered) == len(set(covered))
    assert len(out["shards"]) > 1                       # 12 * 4KB split across shards
    assert "body" not in json.dumps(out["shards"])      # ids only, no bodies
    assert (tmp_path / "enrich_queue" / "active.json").exists()


def test_manifest_appends_blocks_shard(tmp_path):
    _write_pending(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}],
                   merge_review=[{"pair_id": "a|b"}], memory_distil=[{"doc_id": "n1"}])
    out = asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))())
    block_shards = [s for s in out["shards"] if s["with_blocks"]]
    assert len(block_shards) == 1 and block_shards[0]["thread_ids"] == []
    assert out["blocks"] == {"merge_review": 1, "memory_distil": 1}


def test_manifest_empty_when_no_spool(tmp_path):
    assert asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))()) == {"empty": True}


def test_pull_by_thread_ids_returns_only_that_shard(tmp_path):
    threads = [{"thread_id": f"t{i}", "body": "b"} for i in range(6)]
    _write_pending(tmp_path, threads=threads)
    asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))())  # freezes snapshot
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(
        thread_ids=["t1", "t3"]))
    assert out["batch_id"] == "b1"
    assert {t["thread_id"] for t in out["threads"]} == {"t1", "t3"}
    assert out["rules"]                                  # self-contained
    assert "threads_total" not in out                    # shard path, not head-slice


def test_pull_by_ids_survives_pending_shift(tmp_path):
    # The daemon re-prepares pending.json every cycle; the frozen snapshot means a
    # subagent that pulls AFTER pending.json changed still gets its shard's threads.
    _write_pending(tmp_path, threads=[{"thread_id": "t1", "body": "orig"}])
    asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))())
    _write_pending(tmp_path, batch_id="b2", threads=[{"thread_id": "z9", "body": "new"}])
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(thread_ids=["t1"]))
    assert out["batch_id"] == "b1" and out["threads"][0]["body"] == "orig"


def test_pull_with_blocks_returns_blocks_and_no_threads(tmp_path):
    _write_pending(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}],
                   merge_review=[{"pair_id": "a|b"}], synthesis=[{"thread_id": "t1"}])
    asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))())
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(with_blocks=True))
    assert out["threads"] == []
    assert out["merge_review"] == [{"pair_id": "a|b"}]
    assert out["synthesis"] == [{"thread_id": "t1"}]
    assert out["rules"]


def test_pull_with_blocks_bounds_oversized(tmp_path):
    huge = [{"community_id": 1, "members": [f"P{i}" for i in range(6000)]}]
    _write_pending(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}],
                   community_synthesis=huge, merge_review=[{"pair_id": "a|b"}])
    asyncio.run(mcp_server.make_brain_enrich_manifest(str(tmp_path))())
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(with_blocks=True))
    assert len(json.dumps(out)) <= mcp_server._PULL_MAX_CHARS + 6000
    assert "community_synthesis" not in out              # oversized block dropped
    assert out["merge_review"] == [{"pair_id": "a|b"}]    # small block survives


def test_push_with_shard_writes_sharded_file(tmp_path):
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    out = asyncio.run(push("b1", [{"thread_id": "t1"}], [], shard=3))
    assert out["written"] is True
    target = tmp_path / "enrich_inbox" / "b1.3.json"
    assert target.exists()                                # <batch>.<shard>.json
    assert json.loads(target.read_text())["batch_id"] == "b1"
    # legacy (no shard) still writes <batch>.json — both drain via the *.json glob
    asyncio.run(push("b1", [{"thread_id": "t2"}], [], shard=0))
    assert (tmp_path / "enrich_inbox" / "b1.0.json").exists()


def test_routine_enrich_describes_fanout(tmp_path):
    enrich = mcp_server._routine_instructions("enrich")
    assert "brain_enrich_manifest" in enrich
    assert "brain_enrich_pull" in enrich and "brain_enrich_push" in enrich
    assert "subagent" in enrich.lower()


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
