"""End-to-end spool round trip: pending.json -> extraction -> drain -> graph grew.

This is the plan's acceptance test. It exercises the real prepare, contract,
drain and graph_write.apply over a seeded store. The only stand-in is the
extractor session itself: 6.1 uses a stub extractor helper, 6.2 uses the real
extractor_driver.run_extractor with a monkeypatched run_claude. Nothing here
touches Claude, Gemini or the network.

The 6.1/6.2 tests monkeypatch prepare's indirection seams
(_group_unenriched_threads, _reassemble_thread, the context builders,
_org_domain_lines) for unit-level isolation; those seams are retained as a
monkeypatch surface. test_real_phase1_round_trip exercises the real Phase-1
functions end-to-end with no seam patching. graph_write.apply IS real and
writes entities/relations/email_context, so the "graph grew" assertion is a
genuine integration signal.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from mcpbrain import drain, graph_write, prepare
from mcpbrain.store import Store
from tests.helpers import stub_extractor


# --- batch object the prepare seams return --------------------------------


@dataclass
class FakeBatch:
    """Mirrors the Phase-1 batch contract prepare codes against:
    .thread_id, .doc_ids, .chunks (chunks are passed to _reassemble_thread)."""
    thread_id: str
    doc_ids: list
    chunks: list


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A spool root. MCPBRAIN_HOME points prepare's atomic write here; drain
    reads the same root via its home= override."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "enrich_inbox").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


def _seed_chunk(store, doc_id, thread_id, message_id, date="2026-04-18"):
    """Insert one un-enriched chunk carrying thread_id + message_id metadata."""
    store.upsert_chunk(
        doc_id, "body text", f"hash-{doc_id}",
        {"thread_id": thread_id, "message_id": message_id, "date": date},
    )


def _message_for(thread_id, message_id, date="2026-04-18",
                 sender="Joel Chelliah <joel@centrepoint.church>"):
    return {
        "message_id": message_id,
        "sender": sender,
        "date": date,
        "labels": "INBOX",
        "subject": "Hall B for Wednesday college",
        "text": "Can you confirm Hall B is free?",
    }


def _enriched_count(store, doc_ids):
    with store._connect() as db:
        rows = db.execute(
            "SELECT doc_id, enriched FROM chunks WHERE doc_id IN (%s)"
            % ",".join("?" * len(doc_ids)),
            doc_ids,
        ).fetchall()
    return {r["doc_id"]: r["enriched"] for r in rows}


def _entity_count(store):
    return len(store.entities_for_resolution())


def _relation_count(store):
    with store._connect() as db:
        return db.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0]


def _patch_prepare_seams(monkeypatch, batches, messages_by_thread,
                         merge_review_pairs=None):
    """Wire prepare's Phase-1 indirection seams to fakes keyed to the store.

    _group_unenriched_threads returns the given FakeBatches; _reassemble_thread
    maps a batch's chunks back to that thread's messages; the context builders
    return minimal data; _org_domain_lines returns a couple of lines. When
    merge_review_pairs is given, _merge_review_block is forced to return it (so
    a merge can be driven without standing up the real candidate finder); when
    it is None the real _merge_review_block runs against the store.
    """
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: list(batches))

    # chunks carry the thread_id; map back to that thread's messages.
    def _reassemble(chunks):
        tid = chunks[0]["thread_id"]
        return messages_by_thread[tid]
    monkeypatch.setattr(prepare, "_reassemble_thread", _reassemble)

    monkeypatch.setattr(prepare, "_build_known_people",
                        lambda store, batch_thread_ids: [])
    monkeypatch.setattr(prepare, "_read_projects", lambda store: [])
    monkeypatch.setattr(prepare, "_read_areas", lambda store: [])
    monkeypatch.setattr(prepare, "_org_domain_lines",
                        lambda: ["centrepoint.church -> Centrepoint"])

    if merge_review_pairs is not None:
        monkeypatch.setattr(prepare, "_merge_review_block",
                            lambda store: list(merge_review_pairs))


def _two_thread_setup(store):
    """Seed two threads (one chunk each) and return (batches, messages_by_thread).

    chunks carry thread_id so _reassemble_thread (faked) can key off them, and
    so the faked batch's .chunks list is what _reassemble receives."""
    seeds = {
        "t-alpha": ("d-alpha", "m-alpha-1"),
        "t-beta": ("d-beta", "m-beta-1"),
    }
    batches = []
    messages_by_thread = {}
    for tid, (doc_id, msg_id) in seeds.items():
        _seed_chunk(store, doc_id, tid, msg_id)
        chunk = {"thread_id": tid, "doc_id": doc_id, "message_id": msg_id}
        batches.append(FakeBatch(thread_id=tid, doc_ids=[doc_id], chunks=[chunk]))
        messages_by_thread[tid] = [_message_for(tid, msg_id)]
    return batches, messages_by_thread


# --- 6.1: round trip with the stub extractor -------------------------------


def test_pending_to_drain_round_trip(store, home, monkeypatch):
    batches, messages_by_thread = _two_thread_setup(store)
    _patch_prepare_seams(monkeypatch, batches, messages_by_thread)

    ents_before = _entity_count(store)
    rels_before = _relation_count(store)

    prep = prepare.prepare(store, thread_cap=20, char_budget=24000,
                           resolution_due=False)
    assert prep["threads"] == 2
    pending_path = home / "enrich_queue" / "pending.json"
    assert pending_path.exists()
    pending = json.loads(pending_path.read_text())
    assert {t["thread_id"] for t in pending["threads"]} == {"t-alpha", "t-beta"}

    inbox_path = stub_extractor.run_stub_extractor(home)
    assert inbox_path is not None

    summary = drain.drain(store, home=home, apply=graph_write.apply)

    # graph grew: real apply wrote entities + a works_at relation per thread.
    assert _entity_count(store) > ents_before
    assert _relation_count(store) > rels_before
    # every chunk enriched
    assert _enriched_count(store, ["d-alpha", "d-beta"]) == {"d-alpha": 1, "d-beta": 1}
    # spool consumed: inbox file and pending.json gone
    assert not (home / "enrich_inbox" / inbox_path.name).exists()
    assert summary["files"] == 1
    assert summary["applied"] == 2
    assert summary["marked"] == 2
    pending_path.unlink(missing_ok=True)  # cleanup: drain does not consume pending.json


def test_round_trip_idempotent_second_drain_noop(store, home, monkeypatch):
    batches, messages_by_thread = _two_thread_setup(store)
    _patch_prepare_seams(monkeypatch, batches, messages_by_thread)

    prepare.prepare(store, thread_cap=20, char_budget=24000, resolution_due=False)
    stub_extractor.run_stub_extractor(home)
    drain.drain(store, home=home, apply=graph_write.apply)

    ents_after_first = _entity_count(store)
    rels_after_first = _relation_count(store)

    # No new inbox file (the extractor only writes from pending.json, which is
    # consumed). A second drain over the empty inbox is a clean no-op.
    second = drain.drain(store, home=home, apply=graph_write.apply)
    assert second == {"files": 0, "applied": 0, "marked": 0,
                      "merges": 0, "quarantined": 0,
                      "entities": 0, "relations": 0}
    assert _entity_count(store) == ents_after_first
    assert _relation_count(store) == rels_after_first


def test_round_trip_with_merge_answers(store, home, monkeypatch):
    """resolution_due -> prepare surfaces a candidate pair via the real
    _merge_review_block; the stub answers same=true; drain merges the two."""
    batches, messages_by_thread = _two_thread_setup(store)

    # Seed two similar entities the real candidate finder will flag: same type,
    # shared 'joel'/'chelliah' tokens, high token-set ratio, different canonical
    # keys (so the deterministic tier leaves them for LLM adjudication).
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person",
                        "Centrepoint", "2026-04-01")
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person",
                        "Centrepoint", "2026-04-02")  # mentions=2 -> winner
    store.upsert_entity("joel-chelliah-snr", "Joel Chelliah Snr", "person",
                        "Centrepoint", "2026-04-01")  # mentions=1 -> loser

    # Real _merge_review_block (merge_review_pairs left None).
    _patch_prepare_seams(monkeypatch, batches, messages_by_thread)

    prep = prepare.prepare(store, thread_cap=20, char_budget=24000,
                           resolution_due=True)
    assert prep["merge_pairs"] >= 1, "the seeded pair should surface for adjudication"

    stub_extractor.run_stub_extractor(home)

    ents_before_drain = _entity_count(store)
    summary = drain.drain(store, home=home, apply=graph_write.apply)

    assert summary["merges"] == 1
    # the loser folded into the winner: one fewer entity than before the drain,
    # offset by any new entities apply created. Assert the merge directly.
    assert store.get_entity("joel-chelliah-snr") is None
    assert store.get_entity("joel-chelliah") is not None
    merges = store.list_entity_merges()
    assert any(m["winner_id"] == "joel-chelliah"
               and m["loser_id"] == "joel-chelliah-snr"
               and m["method"] == "llm" for m in merges)
    # the structural apply still ran for both threads
    assert summary["applied"] == 2
    # sanity: the two seeded entities (joel-chelliah collapses two upserts into
    # one id with mentions=2, plus joel-chelliah-snr) were present before drain.
    assert ents_before_drain >= 2


# --- 6.2: round trip through the real extractor_driver ---------------------


def _seed_real_chunk(store, doc_id, thread_id, message_id, *, sender, subject,
                     date="2026-04-18", labels="INBOX"):
    """Insert one un-enriched chunk with full message provenance in metadata.

    The metadata keys mirror what the real Gmail sync writes and what
    thread_enrich.reassemble_thread reads (thread_id groups, message_id keys the
    message, sender/date/subject/labels become message fields). upsert_chunk
    leaves enriched=0, so group_unenriched_threads picks the chunk up.
    """
    store.upsert_chunk(
        doc_id, "Can you confirm Hall B is free for Wednesday college?",
        f"hash-{doc_id}",
        {"thread_id": thread_id, "message_id": message_id, "sender": sender,
         "subject": subject, "date": date, "labels": labels, "chunk_index": 0},
    )


def test_real_phase1_round_trip(store, home, monkeypatch):
    """End-to-end loop with the REAL Phase-1 functions, NO seam monkeypatching.

    Seeds real chunks for two threads: one led by a noise sender (noreply@),
    one led by a real human. Real prepare -> the real noise filter, running on
    the real reassemble_thread output, drops the noise thread and keeps the
    human one. The stub extractor + real drain then grow the graph and mark the
    human thread's chunks enriched.
    """
    # Human thread: lead sender is a real person -> kept.
    _seed_real_chunk(store, "d-human", "t-human", "m-human-1",
                     sender="Joel Chelliah <joel@centrepoint.church>",
                     subject="Hall B for Wednesday college")
    # Noise thread: lead sender is automated (noreply) -> dropped by the filter.
    _seed_real_chunk(store, "d-noise", "t-noise", "m-noise-1",
                     sender="noreply@notifications.example.com",
                     subject="Your weekly digest")

    ents_before = _entity_count(store)
    rels_before = _relation_count(store)

    # Real prepare: no prepare.* seam is patched here.
    prep = prepare.prepare(store, thread_cap=20, char_budget=24000,
                           resolution_due=False)

    # Only the human thread survives the real noise filter.
    assert prep["threads"] == 1
    pending_path = home / "enrich_queue" / "pending.json"
    assert pending_path.exists()
    pending = json.loads(pending_path.read_text())
    assert {t["thread_id"] for t in pending["threads"]} == {"t-human"}

    # The noise thread's chunk was marked enriched (so it never re-queues),
    # while the human thread's chunk is still pending until drain applies it.
    assert _enriched_count(store, ["d-noise"]) == {"d-noise": 1}
    assert _enriched_count(store, ["d-human"]) == {"d-human": 0}

    # Stub extractor over the real pending.json -> inbox file keyed to t-human.
    inbox_path = stub_extractor.run_stub_extractor(home)
    assert inbox_path is not None

    # Real drain with the real graph_write.apply.
    summary = drain.drain(store, home=home, apply=graph_write.apply)

    # Graph grew and the human thread's chunk is now enriched; file consumed.
    assert _entity_count(store) > ents_before
    assert _relation_count(store) > rels_before
    assert _enriched_count(store, ["d-human"]) == {"d-human": 1}
    assert summary["files"] == 1
    assert summary["applied"] == 1
    assert summary["marked"] == 1
    assert not (home / "enrich_inbox" / inbox_path.name).exists()
    pending_path.unlink(missing_ok=True)  # drain does not consume pending.json


def test_integration_driver_round_trip(store, home, monkeypatch):
    """Same round trip as 6.1, but the inbox file is produced by the real
    extractor_driver.run_extractor with a monkeypatched run_claude that returns
    the fixture-derived batch JSON. Proves the driver wiring end to end without
    a live Claude."""
    from mcpbrain import extractor_driver

    batches, messages_by_thread = _two_thread_setup(store)
    _patch_prepare_seams(monkeypatch, batches, messages_by_thread)

    ents_before = _entity_count(store)
    rels_before = _relation_count(store)

    prepare.prepare(store, thread_cap=20, char_budget=24000, resolution_due=False)

    # Fake run_claude: parse the pending payload off the end of the prompt and
    # build the same batch the stub extractor would, returning it as JSON text.
    def fake_run_claude(prompt, *, model=None, timeout=None):
        pending_text = prompt.split("=== pending.json ===")[-1]
        pending = json.loads(pending_text)
        return json.dumps(stub_extractor.build_batch(pending))

    inbox_path = extractor_driver.run_extractor(home=home, run_claude=fake_run_claude)
    assert inbox_path is not None

    summary = drain.drain(store, home=home, apply=graph_write.apply)

    assert _entity_count(store) > ents_before
    assert _relation_count(store) > rels_before
    assert _enriched_count(store, ["d-alpha", "d-beta"]) == {"d-alpha": 1, "d-beta": 1}
    assert summary["applied"] == 2
    assert not (home / "enrich_inbox" / Path(inbox_path).name).exists()
