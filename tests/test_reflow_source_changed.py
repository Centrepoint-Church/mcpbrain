"""Dry run #2 findings D1-D3 (2026-09-24 extraction-fidelity): when a reflow
reads "source changed", and what the ordinary path's stale-tail sweep leaves.

Fixture shapes follow the real store copy (legacy positional Gmail tails with
no chunk_total, repeated HTML-entity padding, a pre-split `cal-<eid>` row kept
beside its `cal-<eid>-0/-1` split). Text, names and ids are synthetic."""
import pytest

from mcpbrain.store import Store
from mcpbrain.sync.normalise import Chunk
from mcpbrain.sync.reflow_handler import ReflowContext


class _Emb:
    dim = 4
    def embed_passages(self, xs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4); s.init(); return s


def _ctx(s, tmp_path, **kw):
    return ReflowContext(s, _Emb(), str(tmp_path), **kw)


def _enrich(s, *doc_ids):
    with s._connect(write=True) as db:
        for d in doc_ids:
            db.execute("UPDATE chunks SET enriched=1, enriched_version=3 WHERE doc_id=?", (d,))


def _entities(s):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Dana Okafor','person')"
                   " ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')"
                   " ON CONFLICT DO NOTHING")


def _relation(s, doc_id, rel="works_at"):
    _entities(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1',?,'e2',?)", (rel, doc_id))


def _refs(s, doc_id):
    """A non-relation reference of every kind, pointing at doc_id."""
    _entities(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source)"
                   " VALUES('e1','role','Treasurer',?)", (doc_id,))
        db.execute("INSERT INTO actions(text, source_doc_id, waiting_on_cleared_by_doc_id)"
                   " VALUES('Send the budget',?,?)", (doc_id, doc_id))
        db.execute("INSERT INTO recall_feedback(doc_id, event_type) VALUES(?,'exposure')",
                   (doc_id,))


def _relations(s):
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT relation, source_doc_id, invalidated_at, superseded_reason "
            "FROM entity_relations ORDER BY id")]


def _map(s, owner):
    with s._connect() as db:
        return {r[0]: (r[1], r[2]) for r in db.execute(
            "SELECT old_doc_id, new_doc_id, reason FROM reflow_map WHERE owner=? ORDER BY id",
            (owner,))}


def _outcome(s, owner):
    with s._connect() as db:
        r = db.execute("SELECT outcome FROM reflow_owners WHERE owner=?", (owner,)).fetchone()
    return r[0] if r else None


# ---- D1: Gmail messages are immutable -- never "source changed" ------------

_GMD = {"source_type": "gmail", "message_id": "M", "thread_id": "T",
        "content_type": "email_body"}

_BODY = ["Hi Dana, the Northgate Trust grant report is attached for review.",
         "Marcus Reyes asked that the budget table be checked before Friday.",
         "Priya Anand will send the signed copy once the board has met."]


def _gmail_new(texts, total=None):
    total = total or len(texts)
    return [Chunk(f"gmail-M-body-{i}", t, f"n{i}",
                  {**_GMD, "split_version": 1, "chunk_index": i, "chunk_total": total})
            for i, t in enumerate(texts)]


def _patch_gmail(monkeypatch, new):
    from mcpbrain.sync import gmail
    import mcpbrain.sync.normalise as nm
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, []))
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: list(new))
    monkeypatch.setattr(gmail, "handle_gmail_item",
                        lambda *a, **k: pytest.fail("Gmail took the ordinary path"))


def test_gmail_legacy_positional_tail_is_carried_not_ordinary(tmp_path, monkeypatch):
    """body-0..2 (current chunker, chunk_total 3) plus a LEGACY tail body-3..5
    (pre-B5 chunker: chunk_index, no chunk_total) whose text is pieces of the
    same message. Stitched old text != new text, but the message cannot have
    changed: apply the plan -- legacy ids deleted and remapped onto the chunk
    holding their text, no relation invalidated."""
    s = _store(tmp_path)
    for i, t in enumerate(_BODY):
        s.upsert_chunk(f"gmail-M-body-{i}", t, f"b{i}",
                       {**_GMD, "chunk_index": i, "chunk_total": 3})
    legacy = {3: "the budget table be checked", 4: "once the board has met",
              5: "grant report is attached"}
    for i, t in legacy.items():
        s.upsert_chunk(f"gmail-M-body-{i}", t, f"l{i}", {**_GMD, "chunk_index": i})
    _enrich(s, *[f"gmail-M-body-{i}" for i in range(6)])
    _relation(s, "gmail-M-body-0", "works_at")
    _relation(s, "gmail-M-body-4", "member_of")
    _refs(s, "gmail-M-body-5")
    _patch_gmail(monkeypatch, _gmail_new(_BODY))

    assert _ctx(s, tmp_path, gmail_service=object()).handle(
        {"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None

    assert _outcome(s, "M") == "carried"
    rows = s.owner_chunks(["gmail-M-"])
    assert [r["doc_id"] for r in rows] == [f"gmail-M-body-{i}" for i in range(3)]
    assert all(r["enriched"] == 1 for r in rows)
    rels = _relations(s)
    assert all(r["invalidated_at"] is None for r in rels)
    assert [r["source_doc_id"] for r in rels] == ["gmail-M-body-0", "gmail-M-body-2"]
    m = _map(s, "M")
    assert m["gmail-M-body-3"] == ("gmail-M-body-1", "exact")
    assert m["gmail-M-body-4"] == ("gmail-M-body-2", "exact")
    assert m["gmail-M-body-5"] == ("gmail-M-body-0", "exact")
    with s._connect() as db:
        assert db.execute("SELECT source FROM entity_observations").fetchone()[0] \
            == "gmail-M-body-0"
    assert ("reflow:gmail", "M") not in s.reflow_candidates(50)


def test_gmail_repetitive_padding_is_carried_not_ordinary(tmp_path, monkeypatch):
    """A marketing email padded with a repeated entity run: the seam overlap
    between its old chunks is ambiguous, so the stitched old text loses part of
    the run and differs from the re-split text. Still immutable: carry."""
    pad = " ".join(["&#8199;&#847;"] * 60)
    full = f"Northgate Trust spring appeal {pad} Give today to the building fund"
    words = full.split()
    # old split: overlap of 10 words at the seam -- which a longest-overlap
    # stitch over a uniform run over-removes
    old0, old1 = " ".join(words[:34]), " ".join(words[24:])
    s = _store(tmp_path)
    s.upsert_chunk("gmail-M-body-0", old0, "b0", {**_GMD, "chunk_index": 0, "chunk_total": 2})
    s.upsert_chunk("gmail-M-body-1", old1, "b1", {**_GMD, "chunk_index": 1, "chunk_total": 2})
    _enrich(s, "gmail-M-body-0", "gmail-M-body-1")
    _relation(s, "gmail-M-body-1")
    from mcpbrain.reflow import norm, stitch
    assert stitch([old0, old1])[0] != norm(full)          # the ambiguity is real
    _patch_gmail(monkeypatch, _gmail_new([full]))

    assert _ctx(s, tmp_path, gmail_service=object()).handle(
        {"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None

    assert _outcome(s, "M") == "carried"
    assert [r["doc_id"] for r in s.owner_chunks(["gmail-M-"])] == ["gmail-M-body-0"]
    rels = _relations(s)
    assert rels[0]["invalidated_at"] is None
    assert rels[0]["source_doc_id"] == "gmail-M-body-0"
