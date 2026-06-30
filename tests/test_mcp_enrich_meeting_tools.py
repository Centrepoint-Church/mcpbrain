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
    assert enrich and "brain_enrich_units" in enrich and "enrich-batch" in enrich
    gardener = ri("gardener")
    # Phase 2: GARDENER-PROTECTED block removed; gardener is now hygiene-only with
    # explicit constraints on what it cannot touch and a weekly digest step.
    assert gardener and "HYGIENE" in gardener         # still a hygiene-only task
    assert "context/voice.md" in gardener             # voice.md still off-limits to gardener
    assert "weekly digest" in gardener.lower()        # Phase 2c: digest step wired in
    mp = ri("meeting-packs")
    assert mp and "context_hash" in mp and "brain_meetings_today" in mp
    rg = ri("reference-gardener")
    assert rg and "reference/_proposals/" in rg       # proposals/changelog path still present
    assert "brain_note" in rg and "propose" in rg.lower()
    # Phase 2 option 1: auto-apply routes through the guarded MCP tool, not raw git.
    assert "brain_gardener_apply" in rg
    assert "asserts_person_role" in rg

def test_routine_enrich_describes_fanout(tmp_path):
    enrich = mcp_server._routine_instructions("enrich")
    assert "brain_enrich_units" in enrich
    assert "enrich-batch" in enrich                  # dispatches the cache-anchored agent
    assert "subagent" in enrich.lower()
    assert "haiku" in enrich.lower()                 # extraction subagents run on Haiku
    assert "model: haiku" in enrich.lower()          # model set EXPLICITLY in dispatch
                                                     # (frontmatter alone is not honored)
    # requeue guard: a non-conforming reply means the unit derailed (no push) and must
    # be re-dispatched, not counted done.
    assert "requeue" in enrich.lower()
    assert "re-dispatch" in enrich.lower() and "derail" in enrich.lower()


def _agent_file():
    from pathlib import Path
    return Path(__file__).resolve().parents[1] / "plugin" / "agents" / "enrich-batch.md"


def test_enrich_agent_rules_in_sync():
    # The enrich-batch agent embeds the extraction rules in its SYSTEM PROMPT so every
    # sibling subagent shares one cacheable prefix. That copy must stay byte-identical
    # to the canonical rules the daemon/pull use — bin/sync_agents.py regenerates it.
    text = _agent_file().read_text()
    b, e = "<!-- SHARED-EXTRACTION-RULES:BEGIN -->", "<!-- SHARED-EXTRACTION-RULES:END -->"
    embedded = text[text.index(b) + len(b):text.index(e)].strip()
    assert embedded and embedded == mcp_server._enrich_rules()


def test_enrich_agent_is_haiku_and_skips_wire_rules():
    # Model is set in frontmatter (so the orchestrator need not override it), and the
    # agent pulls with_rules=false (rules already in its prompt — not re-sent uncached).
    text = _agent_file().read_text()
    assert "model: haiku" in text
    assert "with_rules=false" in text
    assert "brain_enrich_pull" in text and "brain_enrich_push" in text


def test_pull_unit_leads_with_cacheable_prefix(tmp_path):
    # Cache-friendliness: the unit pull must put the byte-stable rules first, then
    # context, so the serialized prefix is reusable across units (variable unit_id
    # trails). Guards against a reorder that would bust prompt caching.
    _write_units(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}], context={"owner_name": "Jo"})
    uid = asyncio.run(mcp_server.make_brain_enrich_units(str(tmp_path))())["units"][0]["unit_id"]
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(unit_id=uid))
    keys = list(out)
    assert keys[0] == "rules" and keys[1] == "context"   # stable prefix leads
    assert keys.index("unit_id") > keys.index("context")  # variable field trails


# --- work queue: producer -> units -> pull(unit_id) -> push(unit_id) -> drain ---

def _write_units(tmp_path, **data):
    from mcpbrain import prepare
    data.setdefault("batch_id", "b1")
    data.setdefault("context", {"owner_name": "Jo", "known_people": [{"name": "Ann"}]})
    return prepare.write_units(data, home=str(tmp_path))


def test_producer_writes_sized_units_and_shared_context(tmp_path):
    # The producer chunks threads + blocks into immutable unit files (sized so a pull
    # fits the cap) and writes one shared context.json. Unit ids are content hashes.
    threads = [{"thread_id": f"t{i}", "body": "x" * 6000} for i in range(8)]
    summary = _write_units(tmp_path, threads=threads,
                           merge_review=[{"pair_id": "a|b"}],
                           context={"owner_name": "Jo", "known_people": [{"name": "Ann"}]})
    units = list((tmp_path / "enrich_queue" / "units").glob("*.json"))
    assert summary["units_written"] == len(units) > 1            # threads split + 1 block unit
    assert (tmp_path / "enrich_queue" / "context.json").exists()
    kinds = [json.loads(u.read_text())["kind"] for u in units]
    assert "thread" in kinds and "block" in kinds
    # content-addressed: re-running writes the SAME files (idempotent, no dupes)
    _write_units(tmp_path, threads=threads, merge_review=[{"pair_id": "a|b"}],
                 context={"owner_name": "Jo", "known_people": [{"name": "Ann"}]})
    assert len(list((tmp_path / "enrich_queue" / "units").glob("*.json"))) == len(units)


def test_units_lists_descriptors_and_claims_lease(tmp_path):
    _write_units(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}],
                 memory_distil=[{"doc_id": "n1"}])
    units_tool = mcp_server.make_brain_enrich_units(str(tmp_path))
    out = asyncio.run(units_tool())
    assert {u["kind"] for u in out["units"]} == {"thread", "block"}
    for u in out["units"]:                                       # descriptors only, no payload
        assert set(u) == {"unit_id", "kind", "block", "count"}
    # second call: all are freshly claimed -> nothing handed out again
    assert asyncio.run(units_tool()) == {"empty": True}


def test_units_relists_after_lease_expiry(tmp_path, monkeypatch):
    _write_units(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}], context={})
    units_tool = mcp_server.make_brain_enrich_units(str(tmp_path))
    first = asyncio.run(units_tool())["units"]
    assert first
    # age the claim past the lease -> reclaimable (covers a crashed subagent)
    monkeypatch.setattr(mcp_server, "_LEASE_TTL_S", -1)
    assert asyncio.run(units_tool())["units"]


def test_units_batch_caps_handout_and_leaves_rest(tmp_path, monkeypatch):
    # More units than the cap: one call returns exactly the cap and claims ONLY
    # those, leaving the rest unclaimed so the next call / an overlapping caller
    # picks them up wave-by-wave instead of waiting out the lease on the whole queue.
    monkeypatch.setenv("MCPBRAIN_ENRICH_UNITS_BATCH", "5")
    units_dir = tmp_path / "enrich_queue" / "units"
    units_dir.mkdir(parents=True)
    for i in range(13):
        (units_dir / f"u-{i:02d}.json").write_text(json.dumps(
            {"unit_id": f"u-{i:02d}", "kind": "thread", "threads": [{"thread_id": f"t{i}"}]}))
    tool = mcp_server.make_brain_enrich_units(str(tmp_path))

    first = asyncio.run(tool())["units"]
    assert len(first) == 5                                        # capped to the batch
    assert len(list((tmp_path / "enrich_queue" / "claims").glob("*"))) == 5  # only those claimed
    second = asyncio.run(tool())["units"]
    assert len(second) == 5                                       # next wave, disjoint from first
    assert not ({u["unit_id"] for u in first} & {u["unit_id"] for u in second})
    assert len(asyncio.run(tool())["units"]) == 3                 # remainder
    assert asyncio.run(tool()) == {"empty": True}                 # all leased now


def test_units_batch_defaults_to_twelve(monkeypatch):
    monkeypatch.delenv("MCPBRAIN_ENRICH_UNITS_BATCH", raising=False)
    assert mcp_server._units_batch() == 12
    monkeypatch.setenv("MCPBRAIN_ENRICH_UNITS_BATCH", "garbage")
    assert mcp_server._units_batch() == 12                        # invalid override -> default


def test_pull_unit_attaches_rules_and_context(tmp_path):
    _write_units(tmp_path, threads=[{"thread_id": "t1", "body": "hello"}],
                 context={"owner_name": "Jo", "known_people": [{"name": "Ann"}]})
    uid = asyncio.run(mcp_server.make_brain_enrich_units(str(tmp_path))())["units"][0]["unit_id"]
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(unit_id=uid))
    assert out["unit_id"] == uid and out["kind"] == "thread"
    assert out["threads"][0]["thread_id"] == "t1"
    assert out["rules"] and out["context"]["owner_name"] == "Jo"
    assert "known_people" in out["context"]                     # full context for normal sizes


def test_pull_unit_omits_rules_when_requested(tmp_path):
    # enrich-batch passes with_rules=false: the rules live in its system prompt, so
    # re-sending them in the (uncached) tool result would pay for them twice.
    _write_units(tmp_path, threads=[{"thread_id": "t1", "body": "hi"}],
                 context={"owner_name": "Jo"})
    uid = asyncio.run(mcp_server.make_brain_enrich_units(str(tmp_path))())["units"][0]["unit_id"]
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(unit_id=uid, with_rules=False))
    assert "rules" not in out
    assert out["context"]["owner_name"] == "Jo" and out["threads"][0]["thread_id"] == "t1"


def test_pull_unit_block_returns_items(tmp_path):
    _write_units(tmp_path, threads=[], merge_review=[{"pair_id": "a|b"}, {"pair_id": "c|d"}])
    units = asyncio.run(mcp_server.make_brain_enrich_units(str(tmp_path))())["units"]
    bu = next(u for u in units if u["kind"] == "block")
    out = asyncio.run(mcp_server.make_brain_enrich_pull(str(tmp_path))(unit_id=bu["unit_id"]))
    assert out["block"] == "merge_review" and len(out["items"]) == 2


def test_push_unit_writes_inbox_and_drain_deletes_unit(tmp_path):
    from mcpbrain import drain as _drain
    _write_units(tmp_path, threads=[{"thread_id": "t1",
                 "messages": [{"message_id": "t1"}]}], context={})
    uid = asyncio.run(mcp_server.make_brain_enrich_units(str(tmp_path))())["units"][0]["unit_id"]
    out = asyncio.run(mcp_server.make_brain_enrich_push(str(tmp_path))(
        unit_id=uid, extractions=[{"thread_id": "t1", "messages": [{"message_id": "t1"}]}]))
    assert out["written"] is True
    inbox = json.loads((tmp_path / "enrich_inbox" / f"{uid}.json").read_text())
    assert inbox["unit_id"] == uid
    # drain applies + deletes the unit file and its claim
    (tmp_path / "enrich_queue" / "claims").mkdir(parents=True, exist_ok=True)
    (tmp_path / "enrich_queue" / "claims" / uid).touch()

    class _Store:
        def apply_extraction(self, *a, **k): pass
        def mark_enriched(self, *a, **k): pass
        def recent_changes(self, *a, **k): return []
    _drain.drain(_Store(), home=str(tmp_path), apply=lambda *a, **k: {"entities": 0, "relations": 0})
    assert not (tmp_path / "enrich_queue" / "units" / f"{uid}.json").exists()
    assert not (tmp_path / "enrich_queue" / "claims" / uid).exists()


def test_push_requires_unit_or_batch(tmp_path):
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    assert asyncio.run(push())["written"] is False               # neither id
    assert asyncio.run(push(unit_id="u1", extractions="notalist"))["written"] is False


def test_push_rejects_missing_extractions_without_block_answers(tmp_path):
    # A thread-unit subagent that narratively describes results without calling the
    # tool properly must be caught at the tool boundary — not silently written as an
    # empty-extractions inbox file that drains the unit without applying any work.
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    # Missing extractions with no block answers: must error, not {"written": True}
    r1 = asyncio.run(push(unit_id="u1"))
    assert r1["written"] is False, f"expected written=False for missing extractions, got {r1}"
    assert "extractions" in r1.get("error", "").lower(), f"error should mention extractions: {r1}"
    # extractions=None with no block answers: same rejection
    r2 = asyncio.run(push(unit_id="u1", extractions=None))
    assert r2["written"] is False, f"expected written=False for extractions=None, got {r2}"
    # extractions as a non-list (e.g. a narrated string or a dict): must reject
    r3 = asyncio.run(push(unit_id="u1", extractions={"thread_id": "t1"}))
    assert r3["written"] is False, f"expected written=False for dict extractions, got {r3}"
    r4 = asyncio.run(push(unit_id="u1", extractions=42))
    assert r4["written"] is False, f"expected written=False for int extractions, got {r4}"


def test_push_allows_missing_extractions_for_block_units(tmp_path):
    # Block units (merge_review, synthesis, memory_distil, etc.) legitimately push
    # with no thread extractions — the answer is in the block-specific field.
    # These must NOT be rejected by the extractions guard.
    push = mcp_server.make_brain_enrich_push(str(tmp_path))
    r1 = asyncio.run(push(unit_id="u1", merge_answers=[{"pair_id": "a|b", "same": False, "canonical": ""}]))
    assert r1["written"] is True, f"block push with merge_answers should succeed, got {r1}"
    r2 = asyncio.run(push(unit_id="u2", synthesis=[{"thread_id": "t1", "contextual_summary": "..."}]))
    assert r2["written"] is True, f"block push with synthesis should succeed, got {r2}"
    r3 = asyncio.run(push(unit_id="u3", memory_distil=[{"doc_id": "n1", "verdict": "keep", "reason": "x"}]))
    assert r3["written"] is True, f"block push with memory_distil should succeed, got {r3}"


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
