"""bin/clean_ingestion.py — attended, reversible ingestion clean-up (Task 10)."""
import json
import shutil
import sqlite3

import pytest

from bin import clean_ingestion as ci
from mcpbrain.store import Store

OWNER = "dana@example.org"
NOTE_A = "note-" + "a" * 32          # session capture 1 (older)
NOTE_B = "note-" + "b" * 32          # session capture 2 (same session, latest)
NOTE_C = "note-" + "c" * 32          # an ordinary owner note
M1, M2, M3, M4 = "a" * 15 + "1", "a" * 15 + "2", "a" * 15 + "3", "a" * 15 + "4"
M6, M7 = "a" * 15 + "6", "a" * 15 + "7"
T1, T2, T4, T6 = "b" * 15 + "1", "b" * 15 + "2", "b" * 15 + "4", "b" * 15 + "6"
GONE_MSG = "c" * 15 + "9"
GONE_FILE = "1GoneFileIdAbcdefghijklmnopqrstuv"
TABLES = ("chunks", "actions", "email_entities", "entity_relations",
          "entity_observations", "entities", "relation_sources",
          "observation_sources", "cleanup_log")


class FakeEmbedder:
    """Texts in the same SAME group embed identically; every other text gets
    its own basis vector (cosine 0 to everything else)."""
    SAME = [{"Email the venue about the booking", "email the venue re the booking"},
            {"Send the roster"}]

    def __init__(self):
        self.seen: dict[str, int] = {}

    def embed_passages(self, texts):
        out = []
        for t in texts:
            g = next((i for i, s in enumerate(self.SAME) if t in s), None)
            idx = g if g is not None else self.seen.setdefault(t, 10 + len(self.seen))
            v = [0.0] * 64
            v[idx] = 1.0
            out.append(v)
        return out


def _chunk(db, doc_id, meta, *, state=""):
    db.execute("INSERT INTO chunks(doc_id, text, content_hash, metadata, embedded, "
               "enriched, enrich_state) VALUES (?, ?, ?, ?, 0, 1, ?)",
               (doc_id, "text of " + doc_id, "h-" + doc_id, json.dumps(meta), state))


def _action(db, text, *, src="", thread="", owner="", deadline="", created="2026-09-01"):
    return db.execute(
        "INSERT INTO actions(text, owner, source, source_doc_id, thread_id, deadline, "
        "created_at) VALUES (?, ?, 'email', ?, ?, ?, ?)",
        (text, owner, src, thread, deadline, created)).lastrowid


def _rel(db, a, b, rel, *, src="", strength=1):
    return db.execute(
        "INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id, "
        "strength, valid_from) VALUES (?, ?, ?, ?, ?, '2026-05-01')",
        (a, rel, b, src, strength)).lastrowid


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "owner_name": "Dana", "owner_full_name": "Dana Okafor", "owner_email": OWNER,
        "ingest": {"generated_gmail_subject_prefixes": ["Brief — "],
                   "generated_drive_folders": ["My Drive/Daily Briefs"]}}))
    s = Store(home / "brain.sqlite3", dim=4)
    s.init()
    ids = {}
    with s._connect(write=True) as db:
        gm = {"source_type": "gmail"}
        _chunk(db, f"gmail-{M1}-body-0", {**gm, "message_id": M1, "thread_id": T1,
                                          "labels": "INBOX", "sender": "x@other.org"})
        _chunk(db, f"gmail-{M1}-att-0-0", {**gm, "message_id": M1, "thread_id": T1,
                                           "content_type": "email_attachment"})
        _chunk(db, f"gmail-{M2}-body-0", {**gm, "message_id": M2, "thread_id": T2,
                                          "labels": "DRAFT",
                                          "date": "Wed, 23 Sep 2026 10:00:00 +0000"})
        # A draft's attachment with no labels (old row) — classified by parent.
        _chunk(db, f"gmail-{M2}-att-0-0", {**gm, "message_id": M2, "thread_id": T2,
                                           "content_type": "email_attachment"})
        _chunk(db, f"gmail-{M3}-body-0", {**gm, "message_id": M3, "thread_id": T2,
                                          "labels": "DRAFT",
                                          "date": "Mon, 21 Sep 2026 10:00:00 +0000"})
        _chunk(db, f"gmail-{M4}-body-0", {**gm, "message_id": M4, "thread_id": T4,
                                          "labels": "SENT", "subject": "Brief — Mon 1 Sep",
                                          "sender": OWNER, "to": OWNER})
        # A thread with inbound mail AND a draft the actions were anchored on.
        _chunk(db, f"gmail-{M6}-body-0", {**gm, "message_id": M6, "thread_id": T6,
                                          "labels": "INBOX", "sender": "x@other.org"})
        _chunk(db, f"gmail-{M7}-body-0", {**gm, "message_id": M7, "thread_id": T6,
                                          "labels": "DRAFT"})
        _chunk(db, "gdrive-FBRIEF-0", {"source_type": "gdrive", "file_id": "FBRIEF",
                                       "folder_path": "My Drive/Daily Briefs"})
        sess = {"source": "note", "tags": "session",
                "title": "Session abcdef12 2026-09-01 work"}
        _chunk(db, f"{NOTE_A}-0", {**sess, "note_id": NOTE_A, "captured_at": "2026-09-01T10"})
        _chunk(db, f"{NOTE_A}-1", {**sess, "note_id": NOTE_A, "captured_at": "2026-09-01T10"})
        _chunk(db, f"{NOTE_B}-0", {**sess, "note_id": NOTE_B, "captured_at": "2026-09-01T12"})
        _chunk(db, f"{NOTE_C}-0", {"source": "note", "note_id": NOTE_C, "title": "Venue"})
        _chunk(db, f"{NOTE_C}-1", {"source": "note", "note_id": NOTE_C, "title": "Venue"})
        _chunk(db, "note-consolidated-x", {"source": "note",
                                           "note_id": "note-consolidated-x"})
        _chunk(db, f"enriched-{T1}", {"source_type": "gmail", "thread_id": T1})
        _chunk(db, "cal-EV1", {"event_id": "EV1"})

        ids["primary"] = _action(db, "Reply to the board", src=M1, thread=T1)
        ids["draft"] = _action(db, "Send draft reply", src=M2, thread=T2)
        ids["draft_mixed"] = _action(db, "Fill in the form", src=M7, thread=T6)
        ids["brief"] = _action(db, "Brief item", src=M4, thread=T4)
        ids["session"] = _action(db, "Write lesson 2", thread=f"{NOTE_A}-0")
        ids["note1"] = _action(db, "Email the venue about the booking",
                               thread=f"{NOTE_C}-0", created="2026-09-02")
        ids["note2"] = _action(db, "email the venue re the booking",
                               thread=f"{NOTE_C}-1", deadline="2026-10-01",
                               created="2026-09-03")
        ids["consolidated"] = _action(db, "Summary item", thread="note-consolidated-x")
        ids["digest"] = _action(db, "Digest item", src="enriched-zzzz", thread="zzzz-t")
        ids["gone"] = _action(db, "Gone file item", src=GONE_FILE, thread=GONE_FILE)
        ids["alias"] = _action(db, "Alias owner item", src=M1, thread=T1,
                               owner="Dana Okafor")
        ids["claude"] = _action(db, "Claude owner item", src=M1, thread=T1, owner="Claude")
        ids["marcus"] = _action(db, "Send the roster", src=M1, thread=T1, owner="Marcus")
        ids["priya"] = _action(db, "Send the roster", src=M1, thread=T1, owner="Priya")
        ids["blank"] = _action(db, "Send the roster", src=M1, thread=T1,
                               created="2026-08-01")

        for eid, typ, n in (("p1", "person", 10), ("t-topic", "topic", 5),
                            ("p2", "person", 0), ("p3", "person", 1),
                            ("p4", "person", 0)):
            db.execute("INSERT INTO entities(id, name, type, email_count, degree) "
                       "VALUES (?, ?, ?, ?, 4)", (eid, eid, typ, n))
        for mid, eid in ((M1, "p1"), (M2, "p1"), (f"enriched-{T1}", "t-topic"),
                         (GONE_MSG, "p1"), (M2, "p3")):
            db.execute("INSERT INTO email_entities(message_id, entity_id, role) "
                       "VALUES (?, ?, 'x')", (mid, eid))
        ids["r_legacy"] = _rel(db, "p1", "t-topic", "involved_in", strength=30)
        ids["r_primary"] = _rel(db, "p1", "p2", "knows", src=f"gmail-{M1}-body-0",
                                strength=7)
        ids["r_session"] = _rel(db, "p2", "t-topic", "involved_in", src=f"{NOTE_A}-0",
                                strength=1)
        ids["r_session_hot"] = _rel(db, "p1", "p2", "works_at", src=f"{NOTE_A}-1",
                                    strength=4)
        ids["r_two"] = _rel(db, "p2", "p1", "manages", src=f"gmail-{M1}-body-0",
                            strength=9)
        ids["r_att"] = _rel(db, "t-topic", "p2", "mentions", src=f"gmail-{M1}-att-0-0",
                            strength=5)
        db.execute("INSERT INTO relation_sources VALUES (?, 't5', 'primary_mail', '')",
                   (ids["r_two"],))
        db.execute("INSERT INTO relation_sources VALUES (?, ?, 'primary_mail', '')",
                   (ids["r_att"], T1))
        ids["o_label"] = db.execute(
            "INSERT INTO entity_observations(entity_id, attribute, value, source) "
            "VALUES ('p1', 'role', 'Treasurer', 'llm_extraction')").lastrowid
        # p3 is known ONLY from a draft; p4 has no links at all (unknown).
        ids["o_draft_only"] = db.execute(
            "INSERT INTO entity_observations(entity_id, attribute, value, source) "
            "VALUES ('p3', 'role', 'Venue Manager', 'llm_extraction')").lastrowid
        ids["o_unlinked"] = db.execute(
            "INSERT INTO entity_observations(entity_id, attribute, value, source) "
            "VALUES ('p4', 'role', 'Coach', 'llm_extraction')").lastrowid
        ids["o_cal"] = db.execute(
            "INSERT INTO entity_observations(entity_id, attribute, value, source) "
            "VALUES ('p2', 'occurrence', 'x', 'cal-EV1')").lastrowid
    return {"home": home, "store": s, "ids": ids}


def _main(env, *args, store=None):
    argv = ["--home", str(env["home"]), *args]
    if store is not None:
        argv += ["--store", str(store)]
    return ci.main(argv, embedder=FakeEmbedder(), dim=4)


def _dump(path):
    db = sqlite3.connect(str(path))
    try:
        return {t: db.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in TABLES}
    finally:
        db.close()


def _row(env, table, key, val):
    with env["store"]._connect() as db:
        return dict(db.execute(f"SELECT * FROM {table} WHERE {key}=?", (val,)).fetchone())


def _act(env, name):
    return _row(env, "actions", "id", env["ids"][name])


def _state(env, doc_id):
    return _row(env, "chunks", "doc_id", doc_id)


def test_dry_run_writes_nothing(env, capsys):
    path = env["store"].path
    before = _dump(path)
    assert _main(env) == 0
    assert _dump(path) == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    for step in ci.STEPS:
        assert step in out
    # Counts reflect earlier steps' action changes (rekey then dedup).
    assert "actions:duplicate=2" in out


def test_steps_apply_in_order_and_counts(env, capsys):
    rc = _main(env, "--yes", "--skip-snapshot-for-tests",
               "--steps", "owner_aliases,source_session")
    assert rc == 0
    out = capsys.readouterr().out
    assert out.index("source_session") < out.index("owner_aliases")
    assert "actions:session=1" in out and "chunks_cold:session=3" in out
    assert "actions:owner_alias=1" in out and "actions:non_person=1" in out
    assert "integrity_check ok" in out
    assert _act(env, "session")["status"] == "auto_archived"
    assert _act(env, "session")["resolved_by"] == "source_session"
    assert _act(env, "draft")["status"] == "open"            # not selected
    # Repeat captures stay in recall: cold, never superseded.
    for d in (f"{NOTE_A}-0", f"{NOTE_B}-0"):
        assert _state(env, d)["enrich_state"] == "cold"
        assert "superseded" not in json.loads(_state(env, d)["metadata"])
    assert _act(env, "alias")["owner"] == "Dana"
    assert _act(env, "alias")["prior_owner"] == "Dana Okafor"
    assert (_act(env, "claude")["owner"], _act(env, "claude")["prior_owner"]) == \
        ("unclear", "Claude")


def test_full_run_outcomes(env, capsys):
    assert _main(env, "--yes") == 0                          # with a real snapshot
    out = capsys.readouterr().out
    assert "verified snapshot:" in out and "integrity_check ok" in out
    for name, by in (("session", "source_session"), ("draft", "source_draft"),
                     ("brief", "source_brief"), ("consolidated", "source_consolidated"),
                     ("digest", "source_digest"), ("gone", "source_gone")):
        a = _act(env, name)
        assert (a["status"], a["resolved_by"]) == ("auto_archived", by), name
    # rekey + dedup: both note actions keyed on the base note; the dated one kept.
    assert _act(env, "note1")["thread_id"] == NOTE_C
    assert _act(env, "note1")["prior_thread_id"] == f"{NOTE_C}-0"
    assert _act(env, "note2")["status"] == "open"
    assert _act(env, "note1")["resolved_by"] == "dedup_embed"
    # draft versions: older one superseded, all draft chunks (incl. the
    # label-less attachment, via its parent) cold.
    assert json.loads(_state(env, f"gmail-{M3}-body-0")["metadata"])["superseded"] is True
    for d in (f"gmail-{M2}-body-0", f"gmail-{M3}-body-0", f"gmail-{M2}-att-0-0",
              f"gmail-{M4}-body-0", "gdrive-FBRIEF-0", "note-consolidated-x",
              f"enriched-{T1}"):
        assert _state(env, d)["enrich_state"] == "cold", d


def test_attachment_of_draft_classified_via_parent(env):
    with env["store"]._connect() as db:
        ctx = ci.Ctx(home=str(env["home"]), db=db, dry=True)
    assert ctx.res.doc[f"gmail-{M2}-att-0-0"][0] == "draft"
    assert ctx.res.doc[f"gmail-{M1}-att-0-0"][0] == "primary_doc"


def test_dedup_keeps_owner_distinct_actions(env):
    assert _main(env, "--yes", "--skip-snapshot-for-tests",
                 "--steps", "dedup_embed") == 0
    assert _act(env, "marcus")["status"] == "open"
    assert _act(env, "priya")["status"] == "open"
    # The blank-owner copy merges into ONE named owner (owner beats earliest).
    assert _act(env, "blank")["resolved_by"] == "dedup_embed"


def test_sources_backfill_strength_and_legacy(env):
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    ids = env["ids"]
    rel = lambda n: _row(env, "entity_relations", "id", ids[n])   # noqa: E731
    # Legacy row: strength unchanged, prior saved, no source rows.
    assert (rel("r_legacy")["strength"], rel("r_legacy")["legacy_strength"]) == (30, 30)
    # One primary source -> 1 (prior 7 saved first).
    assert (rel("r_primary")["strength"], rel("r_primary")["legacy_strength"]) == (1, 7)
    # Distinct evidence sources: existing t5 + backfilled T1 -> 2.
    assert rel("r_two")["strength"] == 2
    # Attachment provenance keys on the same thread already recorded -> 1.
    assert rel("r_att")["strength"] == 1
    # Session-sourced relation is derived, strength 0, source row recorded.
    assert (rel("r_session")["derived"], rel("r_session")["strength"]) == (1, 0)
    # Re-observed since its derived first sighting: stays live, strength kept
    # (prior saved as legacy), derived-class source row recorded.
    hot = rel("r_session_hot")
    assert (hot["derived"], hot["strength"], hot["legacy_strength"]) == (0, 4, 4)
    with env["store"]._connect() as db:
        srcs = {tuple(r) for r in db.execute(
            "SELECT relation_id, source_key, source_class FROM relation_sources")}
        osrc = db.execute("SELECT source_key, source_class FROM observation_sources "
                          "WHERE observation_id=?", (ids["o_cal"],)).fetchone()
    assert (ids["r_primary"], T1, "primary_mail") in srcs
    assert (ids["r_session"], NOTE_A, "session") in srcs
    assert (ids["r_session_hot"], NOTE_A, "session") in srcs
    assert not any(r[0] == ids["r_legacy"] for r in srcs)
    assert tuple(osrc) == ("cal-EV1", "primary_event")
    # email_count = non-derived links: M1 kept; draft / digest / gone derived.
    assert _row(env, "entities", "id", "p1")["email_count"] == 1
    with env["store"]._connect() as db:
        links = {r["message_id"]: (r["derived"], r["derived_reason"])
                 for r in db.execute("SELECT * FROM email_entities")}
    assert links[M1] == (0, None)
    assert links[M2] == (1, None) and links[f"enriched-{T1}"] == (1, None)
    assert links[GONE_MSG] == (1, "source_gone")


def test_primary_rows_untouched(env):
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    a = _act(env, "primary")
    assert (a["status"], a["thread_id"], a["owner"]) == ("open", T1, "")
    for d in (f"gmail-{M1}-body-0", f"gmail-{M1}-att-0-0", f"{NOTE_C}-0", "cal-EV1"):
        st = _state(env, d)
        assert st["enrich_state"] == "", d
        assert "superseded" not in json.loads(st["metadata"])
    assert _row(env, "entity_relations", "id", env["ids"]["r_primary"])["derived"] == 0
    assert _row(env, "entity_observations", "id", env["ids"]["o_label"])["derived"] == 0


@pytest.mark.parametrize("step", ci.STEPS)
def test_each_step_undo_restores_rows(env, step):
    path = env["store"].path
    prior = list(ci.STEPS[:ci.STEPS.index(step)])
    if prior:
        assert _main(env, "--yes", "--skip-snapshot-for-tests",
                     "--steps", ",".join(prior)) == 0
    before = _dump(path)
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", step) == 0
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", step) == 0
    assert _dump(path) == before
    # Idempotent: a second undo finds nothing.
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", step) == 0
    assert _dump(path) == before


def test_full_undo_in_reverse_restores_everything(env):
    path = env["store"].path
    before = _dump(path)
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    assert _dump(path) != before
    for step in reversed(ci.STEPS):
        assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", step) == 0
    assert _dump(path) == before


def test_rerun_is_idempotent(env):
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    path = env["store"].path
    once = _dump(path)
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    assert _dump(path) == once


def test_refuses_with_daemon_running(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ci.restore, "daemon_running", lambda home: True)
    path = env["store"].path
    before = _dump(path)
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 1
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", "dedup_embed") == 1
    assert "REFUSED" in capsys.readouterr().out
    assert _dump(path) == before
    # A copy outside the app dir may be cleaned with the daemon up.
    copy = tmp_path / "copy" / "brain.sqlite3"
    copy.parent.mkdir()
    shutil.copy2(path, copy)
    assert _main(env, "--yes", "--skip-snapshot-for-tests", store=copy) == 0
    assert _dump(path) == before
    assert _dump(copy) != before


def test_draft_anchored_thread_with_evidence_is_requeued(env, capsys):
    path = env["store"].path
    prior = ",".join(ci.STEPS[:ci.STEPS.index("source_draft")])
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", prior) == 0
    before = _dump(path)
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "source_draft") == 0
    out = capsys.readouterr().out
    assert "threads_requeued=1" in out          # T6 only; T2 holds drafts alone
    assert _act(env, "draft_mixed")["resolved_by"] == "source_draft"
    assert _state(env, f"gmail-{M6}-body-0")["enriched"] == 0
    assert _state(env, f"gmail-{M1}-body-0")["enriched"] == 1
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", "source_draft") == 0
    assert _dump(path) == before
    assert _state(env, f"gmail-{M6}-body-0")["enriched"] == 1
    assert _act(env, "draft_mixed")["status"] == "open"


def test_refuses_without_brief_rules(env, capsys):
    cfg = json.loads((env["home"] / "config.json").read_text())
    cfg.pop("ingest")
    (env["home"] / "config.json").write_text(json.dumps(cfg))
    path = env["store"].path
    before = _dump(path)
    assert _main(env) == 0
    assert "NO BRIEF RULE IS CONFIGURED" in capsys.readouterr().out
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 1
    assert "--no-brief-rules" in capsys.readouterr().out
    assert _dump(path) == before
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--no-brief-rules") == 0
    assert _act(env, "brief")["status"] == "open"     # read as primary mail


def test_undo_skips_drifted_rows(env, capsys):
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "owner_aliases") == 0
    with env["store"]._connect(write=True) as db:
        db.execute("UPDATE actions SET owner='Marcus' WHERE id=?", (env["ids"]["alias"],))
    capsys.readouterr()
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", "owner_aliases") == 0
    assert "SKIPPED" in capsys.readouterr().out
    assert _act(env, "alias")["owner"] == "Marcus"           # not overwritten
    assert _act(env, "claude")["owner"] == "Claude"          # undrifted: restored
    with env["store"]._connect() as db:
        left = db.execute("SELECT column_name FROM cleanup_log WHERE step='owner_aliases'"
                          ).fetchall()
    assert [r[0] for r in left] == ["owner"]


def test_refuses_when_store_is_the_configured_live_store(env, tmp_path, monkeypatch):
    copy = tmp_path / "elsewhere" / "brain.sqlite3"
    copy.parent.mkdir()
    shutil.copy2(env["store"].path, copy)
    before = _dump(copy)
    monkeypatch.setattr(ci.config, "store_path", lambda: copy)
    monkeypatch.setattr(ci.restore, "daemon_running", lambda home: True)
    assert _main(env, "--yes", "--skip-snapshot-for-tests", store=copy) == 1
    assert _dump(copy) == before


def test_snapshot_taken_before_init(env, monkeypatch):
    calls = []
    import mcpbrain.backup as backup
    real_init = ci.Store.init

    def snap(src, out, *, home=None):
        calls.append("snapshot")
        return out

    def init(self):
        calls.append("init")
        return real_init(self)

    monkeypatch.setattr(backup, "snapshot", snap)
    monkeypatch.setattr(ci.Store, "init", init)
    assert _main(env, "--yes", "--steps", "owner_aliases") == 0
    assert calls[:2] == ["snapshot", "init"]


def test_derived_observations_marks_only_derived_only_entities(env, capsys):
    """Ruling I15: an llm_extraction observation is marked derived only when
    its entity's every link/relation is derived (p3: one draft link). An
    entity with ANY other support (p1) or none at all (p4) keeps it."""
    assert _main(env) == 0
    out = capsys.readouterr().out
    assert "observations:derived_entity=1" in out           # dry run counts it
    assert "observations:llm_extraction_kept=2" in out
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    obs = lambda n: _row(env, "entity_observations", "id", env["ids"][n])  # noqa: E731
    assert obs("o_draft_only")["derived"] == 1
    assert obs("o_label")["derived"] == 0
    assert obs("o_unlinked")["derived"] == 0


def test_entity_derivation_rule():
    from mcpbrain import ingestion_cleanup as ic
    assert ic.derived_only((1, 0)) is True
    assert ic.derived_only((3, 1)) is False      # any other support keeps it
    assert ic.derived_only((0, 0)) is False      # nothing known: not derived


def test_entity_derivation_counts(env):
    from mcpbrain import ingestion_cleanup as ic
    with env["store"]._connect(write=True) as db:
        db.execute("UPDATE email_entities SET derived=1 WHERE message_id=?", (M2,))
        # A link derived by a REMOVAL is not class-derived: counts as other.
        db.execute("INSERT INTO email_entities(message_id, entity_id, role, derived, "
                   "derived_reason) VALUES ('gone-m', 'p4', 'x', 1, 'source_gone')")
        d = ic.entity_derivation(db)
    assert d["p3"] == (1, 0)
    assert d["p4"] == (0, 1)
    assert d["p1"][1] > 0


def test_legacy_strength_skips_org_relations(env):
    with env["store"]._connect(write=True) as db:
        rid = db.execute(
            "INSERT INTO entity_relations(entity_a, relation, entity_b, strength, "
            "origin) VALUES ('p1', 'partners_with', 'p2', 6, 'org')").lastrowid
    assert _main(env, "--yes", "--skip-snapshot-for-tests",
                 "--steps", "legacy_strength") == 0
    r = _row(env, "entity_relations", "id", rid)
    assert (r["strength"], r["legacy_strength"]) == (6, None)


def test_owner_aliases_uses_the_owner_nodes_own_names(env):
    """Rule 1 (one owner identity): a name the owner's REAL node carries is the
    owner even when no config alias names it; a stranger who shares the first
    name is not. The real node's id is written on every owner row."""
    with env["store"]._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type, email_addr, aliases) "
                   "VALUES ('dana-o', 'Dana O.', 'person', ?, 'D Okafor, dokafor|Dee O')",
                   (OWNER,))
        db.execute("INSERT INTO entities(id, name, type) "
                   "VALUES ('dana-smith', 'Dana Smith', 'person')")
        node = _action(db, "Node name item", src=M1, thread=T1, owner="Dana O.")
        alias = _action(db, "Node alias item", src=M1, thread=T1, owner="Dee O")
        stranger = _action(db, "Stranger item", src=M1, thread=T1, owner="Dana Smith")
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "owner_aliases") == 0
    for aid, prior in ((node, "Dana O."), (alias, "Dee O"),
                       (env["ids"]["alias"], "Dana Okafor")):
        r = _row(env, "actions", "id", aid)
        assert (r["owner"], r["owner_entity_id"], r["prior_owner"]) == \
            ("Dana", "dana-o", prior)
    assert _row(env, "actions", "id", stranger)["owner"] == "Dana Smith"


def test_dedup_treats_owner_spellings_as_one_owner(env):
    with env["store"]._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type, email_addr, aliases) "
                   "VALUES ('dana-o', 'Dana O.', 'person', ?, '')", (OWNER,))
        a = _action(db, "Email the venue about the booking", src=M1, thread=T1,
                    owner="Dana", created="2026-09-01")
        b = _action(db, "email the venue re the booking", src=M1, thread=T1,
                    owner="Dana O.", created="2026-09-02")
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "dedup_embed") == 0
    assert _row(env, "actions", "id", a)["status"] == "open"
    assert _row(env, "actions", "id", b)["resolved_by"] == "dedup_embed"


def test_owner_aliases_rulings_i18_i19(env):
    """I18: the owner node's single-token alias is not the owner. I19: a class
    name never overrides an id that resolves to another existing person."""
    with env["store"]._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type, email_addr, aliases) "
                   "VALUES ('dana-o', 'Dana O.', 'person', ?, 'Dee|D Okafor')", (OWNER,))
        nick = _action(db, "Nickname item", src=M1, thread=T1, owner="Dee")
        other = db.execute(
            "INSERT INTO actions(text, owner, owner_entity_id, source, thread_id) "
            "VALUES ('Other person item', 'Dana', 'p1', 'email', ?)", (T1,)).lastrowid
        multi = _action(db, "Multi-token alias item", src=M1, thread=T1, owner="D Okafor")
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "owner_aliases") == 0
    assert _row(env, "actions", "id", nick)["owner"] == "Dee"
    r = _row(env, "actions", "id", other)
    assert (r["owner"], r["owner_entity_id"]) == ("Dana", "p1")
    assert _row(env, "actions", "id", multi)["owner"] == "Dana"


def test_dedup_embed_star_check_and_distinct_deadlines():
    """The clean-up shares action_owner.dedupe_groups with the daily repair."""
    import math
    from mcpbrain import action_owner as ao
    ang = math.radians(25)
    vecs = {"a": [1.0, 0.0], "b": [math.cos(ang), math.sin(ang)],
            "c": [math.cos(2 * ang), math.sin(2 * ang)]}
    rows = [{"id": i, "text": t, "owner": "", "deadline": "", "created_at": f"2026-0{i}"}
            for i, t in ((1, "a"), (2, "b"), (3, "c"))]
    got = ao.dedupe_groups(rows, vecs, lambda r: "")
    assert [(k["id"], [d["id"] for d in drop]) for k, drop in got] == [(1, [2])]
    rows2 = [dict(rows[0], deadline="2026-06-30"), dict(rows[1], deadline="2026-07-31")]
    vecs2 = {"a": [1.0, 0.0], "b": [1.0, 0.0]}
    assert ao.dedupe_groups(rows2, vecs2, lambda r: "") == []
