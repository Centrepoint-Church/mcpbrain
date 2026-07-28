"""Tests for mcpbrain/semantic.py (Phase 1, Task 5) — the semantic layer.

Offline only — no network, no fastembed download. The embedder used here is a
FakeEmbedder mirroring tests/test_index.py / tests/test_retrieval.py.
"""

from datetime import datetime, timezone

from mcpbrain import graph_write as gw
from mcpbrain import thread_enrich
from mcpbrain.chunking import content_hash
from mcpbrain.semantic import build_semantic_doc
from mcpbrain.store import Store
from mcpbrain.retrieval import hybrid_search


class FakeEmbedder:
    """Routes 'budget'/'hall'-bearing text to one axis, everything else to another.

    embed_passages and embed_query agree on the routing so a query that shares
    the doc's distinctive term lands on the same vector axis.
    """
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "hall" in t.lower() else [0, 1.0, 0, 0]
                for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "hall" in text.lower() else [0, 1.0, 0, 0]


def _store(tmp_path):
    s = Store(tmp_path / "sem.sqlite3", dim=4)
    s.init()
    return s


def _clock():
    return datetime(2026, 5, 30, tzinfo=timezone.utc)


def _ext():
    return {
        "thread_id": "t-sem-001",
        "org": "Acme",
        "content_type": "request",
        "summary": "Joel asks Sam to confirm Hall B availability for Wednesday college.",
        "contextual_summary": "Term-one room booking follow-up.",
        "entities": [
            {"name": "Joel Chelliah", "type": "person", "org": "Acme",
             "role": "Senior Pastor"},
            {"name": "Acme Corp", "type": "org", "org": "Acme",
             "role": ""},
        ],
        "topics": ["facilities", "college"],
        "actions": [
            {"description": "Confirm Hall B is free for Wednesday college.",
             "owner_name": "Sam", "owner_fallback": "", "due_date": "2026-04-30",
             "project_id": "", "area_id": ""},
        ],
        "reply_needed": True, "reply_reason": "",
        "resolved_action_ids": [], "updated_actions": [], "relations": [],
        "messages": [
            {"message_id": "m-1",
             "sender": "Joel Chelliah <joel@example.org>",
             "date": "2026-04-18", "labels": "INBOX",
             "subject": "Hall B for Wednesday college"},
        ],
    }


# --- 5.1 synthesised doc builder -----------------------------------------

def test_build_semantic_doc_text():
    ext = _ext()
    lead = ext["messages"][0]
    text, metadata = build_semantic_doc(ext, lead)

    # Org prefix + subject line.
    assert "[Acme] Email: Hall B for Wednesday college" in text
    # Summary line.
    assert "Joel asks" in text and "Hall B" in text
    # People line names the relevant person.
    assert "People: Joel Chelliah" in text
    # Actions line.
    assert "Actions:" in text
    assert "Confirm Hall B is free for Wednesday college." in text
    # Topics line.
    assert "Topics: facilities, college" in text

    # Metadata shape (Qdrant payload bits dropped; these remain). date and
    # message_id were added by this plan's C2/C3 fix — the enriched chunk had
    # no date-recency or message-level provenance before.
    assert set(metadata) == {"source_type", "thread_id", "subject", "org",
                              "content_type", "date", "message_id"}
    assert metadata["source_type"] == "gmail_enriched_v2"
    assert metadata["thread_id"] == "t-sem-001"
    assert metadata["subject"] == "Hall B for Wednesday college"
    assert metadata["org"] == "Acme"
    assert metadata["content_type"] == "request"


def test_build_semantic_doc_omits_org_prefix_when_unknown():
    ext = _ext()
    ext["org"] = "unknown"
    text, metadata = build_semantic_doc(ext, ext["messages"][0])
    assert text.startswith("Email: Hall B for Wednesday college")
    assert metadata["org"] == "unknown"


# --- 5.2 embed the synthesised doc into the index ------------------------

def test_apply_indexes_semantic_doc(tmp_path):
    s = _store(tmp_path)
    fake = FakeEmbedder()
    gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock, embedder=fake)

    chunk = s.get_chunk("enriched-t-sem-001")
    assert chunk is not None
    assert chunk["metadata"]["source_type"] == "gmail_enriched_v2"

    # Embedded immediately: nothing left unembedded, and it is retrievable.
    assert all(c["doc_id"] != "enriched-t-sem-001"
               for c in s.unembedded_chunks())
    ids = [r["doc_id"] for r in hybrid_search(s, fake, "Hall B booking", limit=5)]
    assert "enriched-t-sem-001" in ids


def test_apply_semantic_doc_deferred_when_no_embedder(tmp_path):
    s = _store(tmp_path)
    gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock)  # embedder=None

    chunk = s.get_chunk("enriched-t-sem-001")
    assert chunk is not None
    # Left at embedded=0 so the daemon's index_pending pass picks it up.
    pending_ids = [c["doc_id"] for c in s.unembedded_chunks()]
    assert "enriched-t-sem-001" in pending_ids


def test_apply_semantic_doc_idempotent(tmp_path):
    s = _store(tmp_path)
    fake = FakeEmbedder()
    gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock, embedder=fake)
    gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock, embedder=fake)

    with s._connect() as db:
        n = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE doc_id='enriched-t-sem-001'"
        ).fetchone()[0]
    assert n == 1


def test_apply_semantic_doc_marked_enriched(tmp_path):
    # The synthesised doc is enrichment OUTPUT: it must carry enriched=1 (so it
    # never re-enters the enrichment backlog) AND embedded=0 in the deferred
    # path (so index_pending still embeds it).
    s = _store(tmp_path)
    gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock)  # embedder=None

    with s._connect() as db:
        row = db.execute(
            "SELECT enriched, embedded FROM chunks WHERE doc_id='enriched-t-sem-001'"
        ).fetchone()
    assert row is not None
    assert row["enriched"] == 1
    assert row["embedded"] == 0


def test_apply_semantic_doc_not_in_unenriched_backlog(tmp_path):
    # A real thread chunk carrying t-sem-001 exists alongside the synthesised
    # doc. After enrichment, the synthesised doc must stay out of the enrichment
    # queue even though it shares the thread_id grouping key.
    s = _store(tmp_path)

    real_meta = {"source_type": "gmail", "thread_id": "t-sem-001",
                 "message_id": "m-1", "subject": "Hall B for Wednesday college"}
    s.upsert_chunk(doc_id="m-1-0", text="Original thread body.",
                   content_hash=content_hash("Original thread body."),
                   metadata=real_meta)

    gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock)  # embedder=None

    # Mark the thread's real chunks enriched, mirroring the daemon after a pass.
    s.mark_enriched(["m-1-0"])

    batches = thread_enrich.group_unenriched_threads(s, thread_cap=10)
    all_doc_ids = [d for b in batches for d in b.doc_ids]
    assert "enriched-t-sem-001" not in all_doc_ids


def test_apply_summary_notes_semantic_doc(tmp_path):
    s = _store(tmp_path)
    summary = gw.apply(s, _ext(), doc_ids=["t-sem-001"], clock=_clock,
                       embedder=FakeEmbedder())
    assert summary["semantic_doc"] == "enriched-t-sem-001"


def test_the_enriched_chunk_carries_a_date():
    """C2: 21,162 gmail_enriched_v2 chunks carry no date in any form, so
    importance.recency_decay returns its neutral 0.5 fallback for all of them.
    These are the LLM-digested summaries — the highest-value chunks in the store
    — and the only significant population the recency axis cannot rank. Their
    source chunks DO carry dates; the value was simply not propagated."""
    from mcpbrain.importance import recency_decay
    from mcpbrain.semantic import build_semantic_doc

    thread = {"subject": "Hall B", "sender": "sam@example.com",
              "date": "Tue, 02 Jun 2026 16:30:01 +0800", "labels": "INBOX"}

    _text, meta = build_semantic_doc({"thread_id": "t1", "summary": "x"}, thread)

    assert meta["date"] == "Tue, 02 Jun 2026 16:30:01 +0800"
    assert recency_decay(meta) != 0.5, "still date-blind to the ranker"


def test_the_enriched_chunk_keeps_the_message_level_link():
    """C3: enriched chunks retained thread_id but no message_id, so a fact could
    be traced to a thread but not to the message it came from."""
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc({"thread_id": "t1"}, {"subject": "s"},
                                     message_id="msg-9")

    assert meta["message_id"] == "msg-9"


def test_a_calendar_derived_enrichment_is_not_labelled_as_gmail():
    """C4: observed live — a chunk with source_type gmail_enriched_v2 whose
    thread_id was cal-e734d9f93c894a5a81e3230300748014."""
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc({"thread_id": "cal-e734d9f9"}, {"subject": "s"})

    assert meta["source_type"] == "calendar_enriched_v2"


def test_an_email_derived_enrichment_keeps_its_existing_label():
    from mcpbrain.semantic import build_semantic_doc

    _text, meta = build_semantic_doc({"thread_id": "t-1234"}, {"subject": "s"})

    assert meta["source_type"] == "gmail_enriched_v2"


def test_the_semantic_doc_never_exceeds_the_embedder_window():
    """B3, closed at the source. The semantic doc is SYNTHESISED — its length is
    ours to choose — and it is the one population chunk_text cannot bound,
    because it is written whole to keep its enriched-<thread_id> doc_id. A
    100-person thread with 60 actions used to produce a doc whose tail the
    512-token BGE model silently discarded."""
    from mcpbrain.semantic import SEMANTIC_MAX_CHARS, build_semantic_doc

    extraction = {
        "thread_id": "t1",
        "summary": "Quarterly review. " * 40,
        "entities": [{"type": "person", "name": f"Person Number {i}"}
                     for i in range(100)],
        "actions": [{"description": f"Do the thing number {i}", "due_date": "2026-07-01"}
                    for i in range(60)],
        "topics": [f"topic-{i}" for i in range(80)],
    }

    text, _meta = build_semantic_doc(extraction, {"subject": "Review",
                                                  "sender": "a@b.com",
                                                  "date": "Tue, 02 Jun 2026 16:30:01 +0800"})

    assert len(text) <= SEMANTIC_MAX_CHARS, f"semantic doc is {len(text)} chars"


def test_bounding_keeps_the_highest_value_content_first():
    """Truncation order matters: subject and summary are what a query matches.
    Dropping them to keep a Labels line would be worse than not bounding at all."""
    from mcpbrain.semantic import build_semantic_doc

    extraction = {
        "thread_id": "t1",
        "summary": "The Hall B booking is confirmed for Sunday.",
        "entities": [{"type": "person", "name": f"Person Number {i}"}
                     for i in range(400)],
        "topics": [f"topic-{i}" for i in range(400)],
    }

    text, _meta = build_semantic_doc(extraction, {"subject": "Hall B",
                                                  "sender": "a@b.com", "date": "x"})

    assert "Hall B" in text
    assert "The Hall B booking is confirmed for Sunday." in text
    assert "…" in text or "Topics:" not in text, (
        "over-budget sections must be visibly elided, not silently absent"
    )


def test_a_normal_thread_is_not_truncated():
    """The discriminator: bounding must be invisible for the ordinary case."""
    from mcpbrain.semantic import build_semantic_doc

    text, _meta = build_semantic_doc(
        {"thread_id": "t1", "summary": "Short summary.",
         "entities": [{"type": "person", "name": "Sam Taylor"}],
         "topics": ["booking"]},
        {"subject": "Hall B", "sender": "a@b.com", "date": "x"})

    assert "…" not in text
    assert "Sam Taylor" in text and "booking" in text
