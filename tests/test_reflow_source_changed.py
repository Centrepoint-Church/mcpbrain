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


# ---- D2: calendar/anarlog "changed" is bidirectional containment ----------

_CMD = {"source_type": "calendar", "event_id": "E", "summary": "Board meeting"}
_EVENT = ("Board meeting\nWhen: 2026-05-07T10:30:00+08:00 to 2026-05-07T11:30:00+08:00\n"
          "Agenda: Dana Okafor opens with the Northgate Trust grant update. "
          "Marcus Reyes presents the budget. Priya Anand reports on the roster. "
          "Close with prayer and next steps.")


def _event_split(text=_EVENT):
    """Two overlapping pieces, as chunk_text would cut them (3-word overlap)."""
    w = text.split()
    return " ".join(w[:20]), " ".join(w[17:])


def _cal_new(texts):
    return [Chunk(f"cal-E-{i}", t, f"n{i}",
                  {**_CMD, "split_version": 1, "chunk_index": i, "chunk_total": len(texts)})
            for i, t in enumerate(texts)]


def _seed_split_event(s, *, legacy=False):
    if legacy:        # the pre-split single row, never deleted by upsert-only sync
        s.upsert_chunk("cal-E", _EVENT, "legacy", dict(_CMD))
    a, b = _event_split()
    s.upsert_chunk("cal-E-0", a, "c0", {**_CMD, "chunk_index": 0, "chunk_total": 2})
    s.upsert_chunk("cal-E-1", b, "c1", {**_CMD, "chunk_index": 1, "chunk_total": 2})
    _enrich(s, *(["cal-E"] if legacy else []), "cal-E-0", "cal-E-1")


def _cal_ctx(s, tmp_path, monkeypatch, new, ordinary):
    from mcpbrain.sync import calendar
    monkeypatch.setattr(calendar, "normalise_calendar", lambda ev: list(new))

    class _Cal:
        def events(self):
            return self
        def get(self, **k):
            class R:
                def execute(self, num_retries=0):
                    return {"id": "E"}
            return R()
    return _ctx(s, tmp_path, calendar_service=_Cal(), normal_handlers={"calendar": ordinary})


def test_calendar_duplicate_legacy_row_is_carried_not_ordinary(tmp_path, monkeypatch):
    """The live shape: `cal-E` (pre-split, whole event) AND `cal-E-0/-1` for
    the same event, so the stitched old text holds the event twice. Nothing
    changed: carry, delete `cal-E` and remap it onto `cal-E-0`."""
    s = _store(tmp_path); _seed_split_event(s, legacy=True)
    _relation(s, "cal-E"); _refs(s, "cal-E")
    ctx = _cal_ctx(s, tmp_path, monkeypatch, _cal_new(_event_split()),
                   lambda it: pytest.fail("calendar took the ordinary path"))
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None
    assert _outcome(s, "E") == "carried"
    rows = s.owner_chunks(["cal-E"])
    assert [r["doc_id"] for r in rows] == ["cal-E-0", "cal-E-1"]
    assert all(r["enriched"] == 1 for r in rows)
    assert _map(s, "E")["cal-E"] == ("cal-E-0", "exact")
    rel = _relations(s)[0]
    assert rel["invalidated_at"] is None and rel["source_doc_id"] == "cal-E-0"
    with s._connect() as db:
        assert db.execute("SELECT source FROM entity_observations").fetchone()[0] == "cal-E-0"


def test_calendar_edited_description_is_ordinary(tmp_path, monkeypatch):
    s = _store(tmp_path); _seed_split_event(s, legacy=True)
    edited = _EVENT.replace("presents the budget", "presents the revised capital budget")
    seen = []
    ctx = _cal_ctx(s, tmp_path, monkeypatch, _cal_new(_event_split(edited)), seen.append)
    monkeypatch.setattr(s, "apply_reflow", lambda *a, **k: pytest.fail("reached apply_reflow"))
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None
    assert seen and _outcome(s, "E") == "ordinary"


def test_calendar_removed_sentence_is_ordinary(tmp_path, monkeypatch):
    """Every new chunk's text is still found in the old text (a pure removal),
    so only the old->new direction catches it."""
    s = _store(tmp_path); _seed_split_event(s)
    shorter = _EVENT.replace(" Close with prayer and next steps.", "")
    seen = []
    ctx = _cal_ctx(s, tmp_path, monkeypatch, _cal_new(_event_split(shorter)), seen.append)
    monkeypatch.setattr(s, "apply_reflow", lambda *a, **k: pytest.fail("reached apply_reflow"))
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None
    assert seen and _outcome(s, "E") == "ordinary"


def test_calendar_unchanged_resplit_is_carried(tmp_path, monkeypatch):
    """The same event re-split differently (one chunk now) is not a change."""
    s = _store(tmp_path); _seed_split_event(s)
    new = [Chunk("cal-E", _EVENT, "n", {**_CMD, "split_version": 1, "chunk_index": 0,
                                        "chunk_total": 1})]
    ctx = _cal_ctx(s, tmp_path, monkeypatch, new,
                   lambda it: pytest.fail("calendar took the ordinary path"))
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None
    assert _outcome(s, "E") == "carried"
    assert [r["doc_id"] for r in s.owner_chunks(["cal-E"])] == ["cal-E"]


def test_anarlog_duplicate_legacy_row_is_carried(tmp_path, monkeypatch):
    """The same containment rule for anarlog: a duplicate old row whose text
    the new chunks still hold is not a source change."""
    from mcpbrain.sync import anarlog
    s = _store(tmp_path)
    md = {"source_type": "anarlog", "session_id": "S1", "content_subtype": "notes"}
    s.upsert_chunk("anarlog-S1-notes-0", "Dana Okafor: we agreed the roster", "t0",
                   {**md, "chunk_index": 0, "chunk_total": 2})
    s.upsert_chunk("anarlog-S1-notes-1", "for the Northgate Trust weekend", "t1",
                   {**md, "chunk_index": 1, "chunk_total": 2})
    s.upsert_chunk("anarlog-S1-notes-2", "we agreed the roster", "t2",
                   {**md, "chunk_index": 2})
    _enrich(s, "anarlog-S1-notes-0", "anarlog-S1-notes-1", "anarlog-S1-notes-2")
    new = [Chunk("anarlog-S1-notes-0",
                 "Dana Okafor: we agreed the roster for the Northgate Trust weekend", "n",
                 {**md, "split_version": 1, "chunk_index": 0, "chunk_total": 1})]
    db = tmp_path / "anarlog.sqlite"; db.write_bytes(b"")
    monkeypatch.setattr(anarlog, "read_session", lambda conn, sid: {"id": sid})
    monkeypatch.setattr(anarlog, "normalise_session", lambda sess: new)
    monkeypatch.setattr(anarlog, "handle_anarlog_item",
                        lambda *a, **k: pytest.fail("anarlog took the ordinary path"))
    assert _ctx(s, tmp_path, anarlog_db=str(db)).handle(
        {"source": "reflow:anarlog", "ref_id": "S1", "attempts": 0}) is None
    assert _outcome(s, "S1") == "carried"
    assert [r["doc_id"] for r in s.owner_chunks(["anarlog-S1-"])] == ["anarlog-S1-notes-0"]


# ---- D3: the ordinary-path stale-tail sweep leaves nothing dangling --------

def test_changed_event_sweep_remaps_non_relation_refs_and_invalidates_relations(
        tmp_path, monkeypatch):
    """A genuinely edited event whose new split is shorter: the ordinary
    handler upserts cal-E-0/-1, the sweep removes cal-E-2. Its relations are
    invalidated (spec: the old text's claims are no longer evidenced); every
    OTHER reference -- observations, actions, recall feedback, chunk_quality
    -- moves to the lineage's first new chunk, logged in reflow_map as
    'source_changed'. Nothing is left pointing at a deleted id."""
    from mcpbrain.store import _REFLOW_REF_COLUMNS
    s = _store(tmp_path)
    for i in range(3):
        s.upsert_chunk(f"cal-E-{i}", f"old agenda part {i}", f"c{i}",
                       {**_CMD, "chunk_index": i, "chunk_total": 3})
    _enrich(s, "cal-E-0", "cal-E-1", "cal-E-2")
    _relation(s, "cal-E-2"); _refs(s, "cal-E-2")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunk_quality(doc_id, exposures, uses) VALUES('cal-E-0', 1, 1)")
        db.execute("INSERT INTO chunk_quality(doc_id, exposures, uses) VALUES('cal-E-2', 2, 3)")
    new = _cal_new(["moved to Friday, new agenda", "second half of the new agenda"])

    def _ordinary(item):              # what handle_calendar_item does: upsert only
        for c in new:
            s.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
    ctx = _cal_ctx(s, tmp_path, monkeypatch, new, _ordinary)
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None

    assert _outcome(s, "E") == "ordinary"
    assert [r["doc_id"] for r in s.owner_chunks(["cal-E"])] == ["cal-E-0", "cal-E-1"]
    rel = _relations(s)[0]
    assert rel["invalidated_at"] is not None
    assert rel["superseded_reason"] == "reflow_source_changed"
    with s._connect() as db:
        for table, col in _REFLOW_REF_COLUMNS:
            if table == "entity_relations":
                continue
            vals = {r[0] for r in db.execute(f"SELECT {col} FROM {table}") if r[0]}
            assert "cal-E-2" not in vals, (table, col)
        assert db.execute("SELECT source FROM entity_observations").fetchone()[0] == "cal-E-0"
        assert db.execute("SELECT source_doc_id, waiting_on_cleared_by_doc_id FROM actions"
                          ).fetchone()[:] == ("cal-E-0", "cal-E-0")
        assert db.execute("SELECT doc_id FROM recall_feedback").fetchone()[0] == "cal-E-0"
        q = [tuple(r) for r in db.execute(
            "SELECT doc_id, exposures, uses FROM chunk_quality ORDER BY doc_id")]
    assert q == [("cal-E-0", 3, 4)]
    assert _map(s, "E") == {"cal-E-2": ("cal-E-0", "source_changed")}


def test_sweep_changed_chunks_refuses_a_missing_target_and_writes_nothing(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("cal-E-2", "old tail", "c2", {**_CMD, "chunk_index": 2, "chunk_total": 3})
    _refs(s, "cal-E-2")
    with pytest.raises(ValueError, match="no chunk"):
        s.sweep_changed_chunks("E", {"cal-E-2": "cal-E-0"})
    assert s.get_chunk("cal-E-2") is not None
    with s._connect() as db:
        assert db.execute("SELECT source FROM entity_observations").fetchone()[0] == "cal-E-2"
    assert _map(s, "E") == {}
