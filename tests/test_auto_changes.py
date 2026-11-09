"""Apply, ask first or off for every kind of automatic change.

Each writer runs for real against a tmp store (and a tmp git records repo for
the records-file kinds). In `ask` mode a writer must leave its data untouched,
record no undoable change, and queue one pending_changes row plus one
`pending_change` finding. Approving applies through auto_changes.APPLY, the
same function apply mode calls, and records the undoable change then.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcpbrain import auto_changes, change_undo, config
from mcpbrain.store import Store


# --- fixtures ----------------------------------------------------------------

def _store(tmp_path) -> Store:
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _records(tmp_path) -> Path:
    repo = tmp_path / "records"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "t")
    (repo / "memory").mkdir()
    (repo / "MEMORY.md").write_text("# Memory Index\n\n## Project facts\n")
    (repo / "context").mkdir()
    (repo / "context" / "voice.md").write_text("# Voice\n\nPlain and direct.\n")
    (repo / "reference").mkdir()
    (repo / "reference" / "acme-notes.md").write_text("# Acme notes\n\nOriginal line.\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An app home the writers read through config.app_dir()."""
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(h))
    return str(h)


def _cfg(home, **cfg):
    config.write_config(home, cfg)


def _modes(home, **modes):
    config.write_config(home, {"auto_changes": modes})


def _changes(s, change_type=None) -> list[dict]:
    with s._connect() as db:
        if change_type:
            rows = db.execute("SELECT * FROM change_log WHERE change_type=? ORDER BY id",
                              (change_type,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM change_log ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _undoable(s) -> list[dict]:
    return [c for c in _changes(s) if c["revert_ref"]]


def _pending(s) -> list[dict]:
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM pending_changes ORDER BY id").fetchall()]


def _pending_findings(s) -> list[dict]:
    return s.open_findings(auto_changes.FINDING_TYPE)


def _all_pending_findings(s) -> list[dict]:
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM proactive_findings WHERE finding_type=?",
            (auto_changes.FINDING_TYPE,)).fetchall()]


def _entity(s, eid) -> dict:
    with s._connect() as db:
        return dict(db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone())


def _assert_queued_once(s, kind):
    rows = _pending(s)
    assert len(rows) == 1 and rows[0]["kind"] == kind and rows[0]["status"] == "pending"
    found = _pending_findings(s)
    assert len(found) == 1 and found[0]["ref_id"] == str(rows[0]["id"])
    assert _undoable(s) == []
    return rows[0]


def _assert_approved(s, row, change_type):
    out = auto_changes.decide(s, config.app_dir(), row["id"], approve=True)
    assert out == {"ok": True, "reason": ""}
    after = _pending(s)[0]
    assert after["status"] == "applied" and after["decided_at"]
    change = _changes(s, change_type)[-1]
    assert after["change_log_id"] == change["id"]
    assert change["revert_ref"], "approval must record an undoable change"
    assert _pending_findings(s) == []
    return change


def _assert_declined(s, row):
    out = auto_changes.decide(s, config.app_dir(), row["id"], approve=False)
    assert out == {"ok": True, "reason": ""}
    assert _pending(s)[0]["status"] == "declined"
    assert _pending_findings(s) == []
    assert _undoable(s) == []


# --- modes -------------------------------------------------------------------

def test_defaults_match_the_table(home):
    assert {k: config.auto_change_mode(home, k) for k in config.AUTO_CHANGE_KINDS} == {
        "profiles": "apply", "circles": "apply", "reference": "apply", "voice": "apply",
        "consolidation": "apply", "tidy": "apply", "draft_style": "advise"}


@pytest.mark.parametrize("legacy, kind, mode", [
    ({"gardener_auto_apply": True}, "reference", "apply"),
    ({"gardener_auto_apply": False}, "reference", "ask"),
    ({"voice_auto_apply": True}, "voice", "apply"),
    ({"voice_auto_apply": False}, "voice", "ask"),
    ({"consolidation": True}, "consolidation", "apply"),
    ({"consolidation": False}, "consolidation", "off"),
    ({"bandit_auto_apply": True}, "draft_style", "apply"),
    ({"bandit_auto_apply": False}, "draft_style", "advise"),
])
def test_legacy_keys_give_the_default(home, legacy, kind, mode):
    _cfg(home, **legacy)
    assert config.auto_change_mode(home, kind) == mode


def test_auto_changes_block_overrides_legacy_keys(home):
    _cfg(home, gardener_auto_apply=False, consolidation=False,
         auto_changes={"reference": "apply", "consolidation": "ask", "profiles": "off"})
    assert config.auto_change_mode(home, "reference") == "apply"
    assert config.auto_change_mode(home, "consolidation") == "ask"
    assert config.auto_change_mode(home, "profiles") == "off"


def test_invalid_mode_falls_back_to_default(home):
    _cfg(home, auto_changes={"profiles": "sometimes", "draft_style": "ask"})
    assert config.auto_change_mode(home, "profiles") == "apply"
    assert config.auto_change_mode(home, "draft_style") == "advise"


def test_unknown_kind_raises(home):
    with pytest.raises(ValueError):
        config.auto_change_mode(home, "nonsense")


# --- propose / decide basics ---------------------------------------------------

def test_propose_records_row_and_finding(tmp_path):
    s = _store(tmp_path)
    pid = auto_changes.propose(s, "profiles", "dana-okafor", "New profile for Dana Okafor",
                               {"entity_id": "dana-okafor", "profile": "x"})
    row = _pending(s)[0]
    assert row["id"] == pid and row["kind"] == "profiles" and row["target"] == "dana-okafor"
    assert json.loads(row["payload"]) == {"entity_id": "dana-okafor", "profile": "x"}
    assert row["status"] == "pending" and row["created_at"]
    f = _pending_findings(s)
    assert len(f) == 1 and f[0]["ref_id"] == str(pid)
    assert f[0]["summary"] == "New profile for Dana Okafor"


def test_propose_same_target_updates_the_pending_row(tmp_path):
    s = _store(tmp_path)
    a = auto_changes.propose(s, "profiles", "dana-okafor", "v1", {"profile": "one"})
    b = auto_changes.propose(s, "profiles", "dana-okafor", "v2", {"profile": "two"})
    assert a == b
    rows = _pending(s)
    assert len(rows) == 1 and json.loads(rows[0]["payload"]) == {"profile": "two"}
    assert len(_pending_findings(s)) == 1


def test_propose_rejects_unknown_kind(tmp_path):
    s = _store(tmp_path)
    with pytest.raises(ValueError):
        auto_changes.propose(s, "nonsense", "x", "x", {})


def test_decide_unknown_and_twice(tmp_path, home):
    s = _store(tmp_path)
    assert auto_changes.decide(s, home, 999, approve=True) == {
        "ok": False, "reason": auto_changes.NOT_FOUND}
    pid = auto_changes.propose(s, "profiles", "x", "x", {"entity_id": "x", "profile": "p"})
    assert auto_changes.decide(s, home, pid, approve=False)["ok"] is True
    assert auto_changes.decide(s, home, pid, approve=True) == {
        "ok": False, "reason": auto_changes.ALREADY_DECIDED}


def test_failed_approval_stays_pending_with_reason(tmp_path, home):
    s = _store(tmp_path)
    pid = auto_changes.propose(s, "profiles", "ghost", "x",
                               {"entity_id": "ghost", "profile": "p"})
    out = auto_changes.decide(s, home, pid, approve=True)
    assert out["ok"] is False and out["reason"]
    assert _pending(s)[0]["status"] == "pending"
    assert len(_pending_findings(s)) == 1


def test_apply_registry_covers_every_kind():
    assert set(auto_changes.APPLY) == set(config.AUTO_CHANGE_KINDS)


# --- profiles ------------------------------------------------------------------

def _drain_profile(s, text="Dana runs finance at Acme."):
    from mcpbrain.profile_synth import drain_profiles
    return drain_profiles(s, {"profile_synthesis": [
        {"entity_id": "dana-okafor", "profile": text}]})


def _dana(s):
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")


def test_profiles_apply_is_todays_behaviour(tmp_path, home):
    s = _store(tmp_path)
    _dana(s)
    assert _drain_profile(s)["profiles_written"] == 1
    assert _entity(s, "dana-okafor")["profile"] == "Dana runs finance at Acme."
    assert _pending(s) == []


def test_profiles_ask_queues_and_approve_applies(tmp_path, home):
    s = _store(tmp_path)
    _dana(s)
    _modes(home, profiles="ask")
    assert _drain_profile(s)["profiles_written"] == 0
    assert _entity(s, "dana-okafor")["profile"] in (None, "")
    row = _assert_queued_once(s, "profiles")
    assert "Dana Okafor" in row["summary"]
    change = _assert_approved(s, row, "profile_updated")
    assert _entity(s, "dana-okafor")["profile"] == "Dana runs finance at Acme."
    assert change_undo.undo(s, home, change["id"])["ok"] is True
    assert _entity(s, "dana-okafor")["profile"] == ""


def test_profiles_ask_decline_leaves_data(tmp_path, home):
    s = _store(tmp_path)
    _dana(s)
    _modes(home, profiles="ask")
    _drain_profile(s)
    _assert_declined(s, _pending(s)[0])
    assert _entity(s, "dana-okafor")["profile"] in (None, "")


def test_profiles_off_does_nothing(tmp_path, home):
    s = _store(tmp_path)
    _dana(s)
    _modes(home, profiles="off")
    assert _drain_profile(s)["profiles_written"] == 0
    assert _entity(s, "dana-okafor")["profile"] in (None, "")
    assert _pending(s) == [] and _changes(s) == [] and _all_pending_findings(s) == []


# --- circles -------------------------------------------------------------------

def _seed_community(s, cid=7):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary) "
                   "VALUES(?,0,'','')", (cid,))


def _community(s, cid=7):
    with s._connect() as db:
        return dict(db.execute("SELECT title, summary FROM community_summaries "
                               "WHERE community_id=?", (cid,)).fetchone())


def _drain_circle(s):
    from mcpbrain.community_synth import drain_communities
    return drain_communities(s, {"community_synthesis": [
        {"community_id": 7, "title": "Acme finance", "summary": "The finance circle"}]})


def test_circles_ask_queues_and_approve_applies(tmp_path, home):
    s = _store(tmp_path)
    _seed_community(s)
    _modes(home, circles="ask")
    assert _drain_circle(s)["communities_written"] == 0
    assert _community(s) == {"title": "", "summary": ""}
    row = _assert_queued_once(s, "circles")
    _assert_approved(s, row, "community_titled")
    assert _community(s) == {"title": "Acme finance", "summary": "The finance circle"}


def test_circles_off_and_decline(tmp_path, home):
    s = _store(tmp_path)
    _seed_community(s)
    _modes(home, circles="off")
    _drain_circle(s)
    assert _pending(s) == [] and _changes(s) == []
    _modes(home, circles="ask")
    _drain_circle(s)
    _assert_declined(s, _pending(s)[0])
    assert _community(s) == {"title": "", "summary": ""}


# --- reference (gardener) --------------------------------------------------------

def _gardener(s, content="# Acme notes\n\nUpdated line.\n"):
    from mcpbrain.tools import make_brain_gardener_apply
    return asyncio.run(make_brain_gardener_apply(s)("reference", "acme-notes.md", content))


def test_reference_ask_queues_for_review(tmp_path, home):
    s = _store(tmp_path)
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), auto_changes={"reference": "ask"})
    original = (repo / "reference" / "acme-notes.md").read_text()
    out = _gardener(s)
    assert out["applied"] is False and out["queued"] is True
    assert "queued for review" in out["status"]
    assert (repo / "reference" / "acme-notes.md").read_text() == original
    row = _assert_queued_once(s, "reference")
    assert row["target"] == "reference/acme-notes.md"
    _assert_approved(s, row, "gardener_write")
    assert (repo / "reference" / "acme-notes.md").read_text() == "# Acme notes\n\nUpdated line.\n"


def test_reference_legacy_false_queues(tmp_path, home):
    s = _store(tmp_path)
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), gardener_auto_apply=False)
    assert _gardener(s)["queued"] is True


def test_reference_approval_refused_when_file_changed_since(tmp_path, home):
    s = _store(tmp_path)
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), auto_changes={"reference": "ask"})
    _gardener(s)
    (repo / "reference" / "acme-notes.md").write_text("# Acme notes\n\nHand edit.\n")
    out = auto_changes.decide(s, home, _pending(s)[0]["id"], approve=True)
    assert out["ok"] is False and "changed" in out["reason"]
    assert (repo / "reference" / "acme-notes.md").read_text() == "# Acme notes\n\nHand edit.\n"


def test_reference_ask_still_rejects_bad_writes(tmp_path, home):
    s = _store(tmp_path)
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), auto_changes={"reference": "ask"})
    from mcpbrain.tools import make_brain_gardener_apply
    out = asyncio.run(make_brain_gardener_apply(s)("reference", "missing.md", "x\n"))
    assert out["applied"] is False and "error" in out
    assert _pending(s) == []


def test_reference_off_writes_nothing(tmp_path, home):
    s = _store(tmp_path)
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), auto_changes={"reference": "off"})
    original = (repo / "reference" / "acme-notes.md").read_text()
    out = _gardener(s)
    assert out["applied"] is False and "off" in out["error"]
    assert (repo / "reference" / "acme-notes.md").read_text() == original
    assert _pending(s) == [] and _changes(s) == []


def test_reference_routine_reads_the_mode():
    text = (Path(__file__).parent.parent / "mcpbrain" / "routines"
            / "reference-gardener.md").read_text()
    assert "auto_change_mode" in text
    assert "gardener_auto_apply_enabled" not in text
    assert "propose-only** (default)" not in text


# --- voice ---------------------------------------------------------------------

def _voice_home(tmp_path, home, mode):
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), procedural_memory=True,
         auto_changes={"voice": mode})
    return repo


def test_voice_ask_queues_and_approve_applies(tmp_path, home):
    from mcpbrain import voice_apply
    s = _store(tmp_path)
    repo = _voice_home(tmp_path, home, "ask")
    original = (repo / "context" / "voice.md").read_text()
    s.insert_voice_suggestion("style", "Lead with the ask", 0.9, [], "seen in drafts")
    out = voice_apply.run_auto(s, home)
    assert out.get("applied", 0) == 0 and out["queued"] == 1
    assert (repo / "context" / "voice.md").read_text() == original
    assert len(s.pending_voice_suggestions()) == 1
    row = _assert_queued_once(s, "voice")
    # A second analysis cycle refreshes the same review, not a second one.
    voice_apply.run_auto(s, home)
    assert len(_pending(s)) == 1
    _assert_approved(s, row, "voice_apply")
    assert "Lead with the ask" in (repo / "context" / "voice.md").read_text()
    assert s.pending_voice_suggestions() == []


def test_voice_decline_is_not_requeued(tmp_path, home):
    from mcpbrain import voice_apply
    s = _store(tmp_path)
    repo = _voice_home(tmp_path, home, "ask")
    original = (repo / "context" / "voice.md").read_text()
    s.insert_voice_suggestion("style", "Lead with the ask", 0.9, [], "seen in drafts")
    voice_apply.run_auto(s, home)
    _assert_declined(s, _pending(s)[0])
    assert s.pending_voice_suggestions() == []
    assert voice_apply.run_auto(s, home)["queued"] == 0
    assert len(_pending(s)) == 1
    assert (repo / "context" / "voice.md").read_text() == original


def test_voice_apply_and_off(tmp_path, home):
    from mcpbrain import voice_apply
    s = _store(tmp_path)
    repo = _voice_home(tmp_path, home, "off")
    original = (repo / "context" / "voice.md").read_text()
    s.insert_voice_suggestion("style", "Lead with the ask", 0.9, [], "seen in drafts")
    voice_apply.run_auto(s, home)
    assert (repo / "context" / "voice.md").read_text() == original
    assert _pending(s) == [] and _changes(s) == []
    _modes(home, voice="apply")
    assert voice_apply.run_auto(s, home)["applied"] == 1
    assert "Lead with the ask" in (repo / "context" / "voice.md").read_text()


# --- consolidation ---------------------------------------------------------------

def _consolidation_setup(tmp_path, home, monkeypatch, mode):
    from mcpbrain import consolidation
    s = _store(tmp_path)
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo), auto_changes={"consolidation": mode})
    cluster = []
    for i in range(5):
        doc = f"acme-ep-{i}"
        s.upsert_chunk(doc, f"Acme budget review item {i}", f"h{i}", {})
        s.set_chunk_tier(doc, "warm")
        cluster.append({"doc_id": doc, "text": f"Acme budget review item {i}",
                        "metadata": {}, "salience": 5.0})
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET salience=5.0 WHERE doc_id LIKE 'acme-ep-%'")
    calls = []
    monkeypatch.setattr(consolidation, "_unconsolidated_salience", lambda store: 1e9)
    monkeypatch.setattr(consolidation, "_episodic_chunks_for_consolidation",
                        lambda store, cap: cluster)
    monkeypatch.setattr(consolidation, "_cluster", lambda chunks, embedder=None: [cluster])
    summary = "Acme budget review settled the Q3 numbers. Dana Okafor owns the follow-up."

    def _claude(prompt):
        calls.append(prompt)
        return summary
    monkeypatch.setattr(consolidation, "_call_claude", _claude)
    return s, repo, calls


def _tiers(s):
    with s._connect() as db:
        return {r["doc_id"]: r["memory_tier"] for r in db.execute(
            "SELECT doc_id, memory_tier FROM chunks WHERE doc_id LIKE 'acme-ep-%'")}


def _notes(s):
    with s._connect() as db:
        return [r[0] for r in db.execute(
            "SELECT doc_id FROM chunks WHERE doc_id LIKE 'note-consolidated-%'")]


def test_consolidation_ask_queues_and_approve_applies(tmp_path, home, monkeypatch):
    from mcpbrain import consolidation
    s, repo, calls = _consolidation_setup(tmp_path, home, monkeypatch, "ask")
    out = consolidation.consolidate(s, home)
    assert out["notes_written"] == 0 and out["proposed"] == 1
    assert _notes(s) == [] and set(_tiers(s).values()) == {"warm"}
    assert list((repo / "memory").iterdir()) == []
    row = _assert_queued_once(s, "consolidation")
    # While a pass waits for review, the next one doesn't spend LLM calls.
    n = len(calls)
    assert consolidation.consolidate(s, home)["skipped"] == 1
    assert len(calls) == n and len(_pending(s)) == 1
    change = _assert_approved(s, row, "consolidation_pass")
    assert _notes(s) and set(_tiers(s).values()) == {"hot"}
    assert change_undo.undo(s, home, change["id"])["ok"] is True
    assert _notes(s) == [] and set(_tiers(s).values()) == {"warm"}


def test_consolidation_off_does_nothing(tmp_path, home, monkeypatch):
    from mcpbrain import consolidation
    s, _repo, calls = _consolidation_setup(tmp_path, home, monkeypatch, "off")
    assert consolidation.should_consolidate(s, home) is False
    assert consolidation.consolidate(s, home)["notes_written"] == 0
    assert calls == [] and _notes(s) == [] and _pending(s) == [] and _changes(s) == []


def test_consolidation_apply_unchanged(tmp_path, home, monkeypatch):
    from mcpbrain import consolidation
    s, _repo, _calls = _consolidation_setup(tmp_path, home, monkeypatch, "apply")
    assert consolidation.consolidate(s, home)["notes_written"] == 1
    assert _notes(s) and _pending(s) == []
    assert _changes(s, "consolidation_pass")[-1]["revert_ref"]


# --- draft style (bandit) ----------------------------------------------------------

def _bandit(tmp_path, home, monkeypatch, **cfg):
    from mcpbrain import threshold_bandit
    s = _store(tmp_path)
    _cfg(home, recall_max_distance=0.80, **cfg)
    threshold_bandit.init_bandit_table(s)
    monkeypatch.setattr(s, "all_feedback_rows", lambda: [{"event_type": "used"}])
    monkeypatch.setattr(threshold_bandit, "recommend", lambda store: 0.65)
    return s, threshold_bandit.advisory_report(s, home)


@pytest.mark.parametrize("cfg", [{}, {"auto_changes": {"draft_style": "advise"}},
                                 {"auto_changes": {"draft_style": "off"}},
                                 {"bandit_auto_apply": True,
                                  "auto_changes": {"draft_style": "advise"}}])
def test_draft_style_advise_or_off_writes_nothing(tmp_path, home, monkeypatch, cfg):
    s, report = _bandit(tmp_path, home, monkeypatch, **cfg)
    assert report["auto_apply"] is False
    assert config.recall_max_distance(home) == pytest.approx(0.80)
    assert _changes(s) == [] and _pending(s) == []


def test_draft_style_apply_records(tmp_path, home, monkeypatch):
    s, report = _bandit(tmp_path, home, monkeypatch, auto_changes={"draft_style": "apply"})
    assert report["auto_apply"] is True
    assert config.recall_max_distance(home) == pytest.approx(0.65)
    assert _changes(s, "bandit_applied")[-1]["revert_ref"]


def test_draft_style_apply_function(tmp_path, home):
    s = _store(tmp_path)
    _cfg(home, recall_max_distance=0.80)
    cid = auto_changes.APPLY["draft_style"](s, home, {"recommended": 0.7})
    assert config.recall_max_distance(home) == pytest.approx(0.7)
    assert _changes(s, "bandit_applied")[-1]["id"] == cid


# --- graph tidy-up -----------------------------------------------------------------

def _orphans(s, n=3):
    fids = []
    for i in range(n):
        eid = f"acme-junk-{i}"
        s.upsert_entity(eid, f"Acme Junk {i}", "person")
        s.record_finding("lint:orphan_entity", eid, summary="orphan")
    for f in s.open_findings("lint:orphan_entity"):
        fids.append(f["id"])
    return fids


def _drain_orphans(s, fids):
    from mcpbrain.drain import BLOCK_DRAINERS
    return BLOCK_DRAINERS["review_orphan"](s, {"review_orphan": [
        {"finding_id": f, "verdict": "suppress", "reason": "no links"} for f in fids]})


def _suppressed(s) -> set:
    with s._connect() as db:
        return {r[0] for r in db.execute("SELECT entity_id FROM entity_suppressions")}


def test_tidy_ask_turns_verdicts_into_pending_rows(tmp_path, home):
    s = _store(tmp_path)
    _modes(home, tidy="ask")
    fids = _orphans(s)
    _drain_orphans(s, fids)
    assert _suppressed(s) == set()
    rows = _pending(s)
    assert len(rows) == 3 and {r["kind"] for r in rows} == {"tidy"}
    assert len(_pending_findings(s)) == 3
    assert len(s.open_findings("lint:orphan_entity")) == 3
    assert _undoable(s) == []
    # approve one: applied through the same applier, finding resolved
    out = auto_changes.decide(s, home, rows[0]["id"], approve=True)
    assert out["ok"] is True
    assert len(_suppressed(s)) == 1
    change = _changes(s, "tidy_suppress")[-1]
    assert _pending(s)[0]["change_log_id"] == change["id"]
    assert change_undo.undo(s, home, change["id"])["ok"] is True
    # decline another: the review finding closes for good, nothing hidden
    assert auto_changes.decide(s, home, rows[1]["id"], approve=False)["ok"] is True
    assert len(_suppressed(s)) == 0
    f = s.get_finding(fids[1])
    assert f["resolved_at"] and f["verdict"]


def test_tidy_ask_is_capped_by_review_max_apply_per_run(tmp_path, home):
    s = _store(tmp_path)
    _cfg(home, review_max_apply_per_run=2, auto_changes={"tidy": "ask"})
    _drain_orphans(s, _orphans(s))
    assert len(_pending(s)) == 2


def test_tidy_ask_reproposal_does_not_duplicate(tmp_path, home):
    s = _store(tmp_path)
    _modes(home, tidy="ask")
    fids = _orphans(s)
    _drain_orphans(s, fids)
    _drain_orphans(s, fids)
    assert len(_pending(s)) == 3


def test_tidy_ask_passes_non_mutating_verdicts_through(tmp_path, home):
    from mcpbrain.drain import BLOCK_DRAINERS
    s = _store(tmp_path)
    _modes(home, tidy="ask")
    fids = _orphans(s, 1)
    BLOCK_DRAINERS["review_orphan"](s, {"review_orphan": [
        {"finding_id": fids[0], "verdict": "keep"}]})
    assert _pending(s) == []
    assert s.get_finding(fids[0])["verdict"] == "keep"


def test_tidy_off_does_nothing(tmp_path, home):
    s = _store(tmp_path)
    _modes(home, tidy="off")
    _drain_orphans(s, _orphans(s))
    assert _suppressed(s) == set() and _pending(s) == [] and _changes(s) == []
    assert len(s.open_findings("lint:orphan_entity")) == 3


def test_tidy_apply_unchanged(tmp_path, home):
    s = _store(tmp_path)
    _drain_orphans(s, _orphans(s))
    assert len(_suppressed(s)) == 3 and _pending(s) == []


def test_tidy_ask_merge_answers(tmp_path, home):
    s = _store(tmp_path)
    _modes(home, tidy="ask")
    s.upsert_entity("sam-chen", "Sam Chen", "person", org="Acme")
    s.upsert_entity("sam-chen-2", "Sam Chen", "person", org="Acme")
    out = auto_changes.run_tidy(s, home, "merge_answers", [
        {"pair_id": "sam-chen|sam-chen-2", "same": True, "canonical": "Sam Chen"}], cap=50)
    assert out["merged"] == 0 and out["proposed"] == 1
    assert s.get_entity("sam-chen") and s.get_entity("sam-chen-2")
    row = _pending(s)[0]
    assert "Sam Chen" in row["summary"]
    assert auto_changes.decide(s, home, row["id"], approve=True)["ok"] is True
    assert (s.get_entity("sam-chen") is None) != (s.get_entity("sam-chen-2") is None)


# --- stats -------------------------------------------------------------------------

def test_needs_you_reviews_counts_pending_changes(tmp_path, home):
    from mcpbrain import console_stats
    s = _store(tmp_path)
    auto_changes.propose(s, "profiles", "a", "a", {})
    auto_changes.propose(s, "profiles", "b", "b", {})
    s.record_finding("agent_stderr", "x.err", summary="boom")
    status = {"open_findings": s.open_findings_count(), "connections": {}, "backfill": {}}
    out = console_stats.stats(s, home, status)
    assert out["needs_you"]["reviews"] == 2
    # pending changes are listed once, as reviews, not again as findings
    assert out["needs_you"]["findings"] == 1


# --- routes ------------------------------------------------------------------------

class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}


def _req(srv, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {srv.token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def server(tmp_path, home):
    from mcpbrain.control_api import ControlServer
    s = _store(tmp_path)
    srv = ControlServer(FakeDaemon(), home=home, store=s)
    srv.start()
    yield srv, s
    srv.stop()


def test_pending_routes(server, home):
    srv, s = server
    _dana(s)
    a = auto_changes.propose(s, "profiles", "dana-okafor", "New profile for Dana Okafor",
                             {"entity_id": "dana-okafor", "profile": "Dana runs finance."})
    b = auto_changes.propose(s, "profiles", "sam-chen", "New profile for Sam Chen",
                             {"entity_id": "sam-chen", "profile": "Sam."})
    code, out = _req(srv, "GET", "/api/pending")
    assert code == 200
    assert [i["id"] for i in out["items"]] == [a, b]
    assert set(out["items"][0]) == {"id", "kind", "target", "summary", "created_at"}
    assert _req(srv, "POST", f"/api/pending/{a}/apply") == (200, {"ok": True})
    assert _entity(s, "dana-okafor")["profile"] == "Dana runs finance."
    code, out = _req(srv, "POST", f"/api/pending/{a}/apply")
    assert code == 409 and out["reason"] == auto_changes.ALREADY_DECIDED
    code, out = _req(srv, "POST", f"/api/pending/{b}/apply")   # no such entity
    assert code == 409 and out["ok"] is False
    assert _req(srv, "POST", f"/api/pending/{b}/decline") == (200, {"ok": True})
    assert _req(srv, "POST", "/api/pending/999/decline")[0] == 404
    assert _req(srv, "GET", "/api/pending") == (200, {"items": []})


def test_findings_list_groups_and_excludes_reviews(server):
    srv, s = server
    s.record_finding("agent_stderr", "a.err", summary="enrich agent wrote to stderr",
                     detail="trace a", severity="warning")
    s.record_finding("agent_stderr", "b.err", summary="enrich agent wrote to stderr",
                     detail="trace b", severity="warning")
    s.record_finding("lint:orphan_entity", "acme-junk", summary="Acme Junk has no links")
    s.record_finding("graph_correction", "5", summary="Proposed graph correction")
    auto_changes.propose(s, "profiles", "x", "New profile", {})
    code, out = _req(srv, "GET", "/api/findings")
    assert code == 200
    items = out["items"]
    # lint:orphan_entity is graph tidy-up's input, never a Needs-you item.
    assert {i["finding_type"] for i in items} == {"agent_stderr"}
    stderr = next(i for i in items if i["finding_type"] == "agent_stderr")
    assert stderr["count"] == 2 and stderr["detail"] in ("trace a", "trace b")
    assert set(stderr) == {"id", "finding_type", "summary", "detail", "severity",
                           "detected_at", "count"}


def test_findings_fixed_reopens_dismiss_does_not(server):
    srv, s = server
    s.record_finding("agent_stderr", "a.err", summary="boom")
    s.record_finding("agent_stderr", "b.err", summary="crash")
    fa = next(f["id"] for f in s.open_findings() if f["ref_id"] == "a.err")
    fb = next(f["id"] for f in s.open_findings() if f["ref_id"] == "b.err")
    assert _req(srv, "POST", f"/api/findings/{fa}/fixed")[0] == 200
    assert _req(srv, "POST", f"/api/findings/{fb}/dismiss")[0] == 200
    assert s.open_findings() == []
    s.record_finding("agent_stderr", "a.err", summary="boom")
    s.record_finding("agent_stderr", "b.err", summary="crash")
    assert [f["ref_id"] for f in s.open_findings()] == ["a.err"]
    assert _req(srv, "POST", "/api/findings/9999/fixed")[0] == 404


def test_findings_action_covers_the_whole_group(server):
    srv, s = server
    s.record_finding("agent_stderr", "a.err", summary="same")
    s.record_finding("agent_stderr", "b.err", summary="same")
    fid = s.open_findings()[0]["id"]
    assert _req(srv, "POST", f"/api/findings/{fid}/dismiss")[0] == 200
    assert s.open_findings() == []


def test_findings_dismiss_is_undoable(server, home):
    srv, s = server
    s.record_finding("lint:orphan_entity", "acme-junk", summary="Acme Junk has no links")
    fid = s.open_findings()[0]["id"]
    assert _req(srv, "POST", f"/api/findings/{fid}/dismiss")[0] == 200
    change = _changes(s, "finding_dismissed")[-1]
    assert change_undo.undo(s, home, change["id"])["ok"] is True
    assert [f["id"] for f in s.open_findings()] == [fid]


# --- request builders don't re-ask what waits for review ------------------------------

def test_block_requests_follow_the_mode(tmp_path, home):
    from mcpbrain.daemon import _requests_for_mode
    s = _store(tmp_path)

    def build(store):
        return [{"entity_id": "dana-okafor"}, {"entity_id": "sam-chen"}]
    assert len(_requests_for_mode(s, "profiles", build, "entity_id")) == 2
    _modes(home, profiles="off")
    assert _requests_for_mode(s, "profiles", build, "entity_id") == []
    _modes(home, profiles="ask")
    pid = auto_changes.propose(s, "profiles", "dana-okafor", "x", {})
    assert _requests_for_mode(s, "profiles", build, "entity_id") == [{"entity_id": "sam-chen"}]
    auto_changes.decide(s, home, pid, approve=False)
    assert _requests_for_mode(s, "profiles", build, "entity_id") == [{"entity_id": "sam-chen"}]


def test_merge_review_block_follows_the_mode(tmp_path, home, monkeypatch):
    from mcpbrain import prepare
    s = _store(tmp_path)
    a = {"id": "sam-chen", "name": "Sam Chen", "type": "person"}
    b = {"id": "sam-chen-2", "name": "Sam Chen", "type": "person"}
    c = {"id": "pat-nguyen", "name": "Pat Nguyen", "type": "person"}
    d = {"id": "pat-nguyen-2", "name": "Pat Nguyen", "type": "person"}
    monkeypatch.setattr(prepare, "_candidate_pairs", lambda ents, distinct: [(a, b), (c, d)])
    assert len(prepare._merge_review_block(s)) == 2
    _modes(home, tidy="off")
    assert prepare._merge_review_block(s) == []
    _modes(home, tidy="ask")
    pid = auto_changes.propose(s, "tidy", "pair:sam-chen|sam-chen-2", "Merge", {})
    assert [m["pair_id"] for m in prepare._merge_review_block(s)] == ["pat-nguyen|pat-nguyen-2"]
    auto_changes.decide(s, home, pid, approve=False)
    assert [m["pair_id"] for m in prepare._merge_review_block(s)] == ["pat-nguyen|pat-nguyen-2"]


def test_legacy_dismiss_declines_a_pending_change(server):
    srv, s = server
    pid = auto_changes.propose(s, "profiles", "x", "New profile", {})
    fid = _pending_findings(s)[0]["id"]
    code, out = _req(srv, "POST", f"/api/dashboard/findings/{fid}/dismiss")
    assert code == 200 and out["change"] == "declined"
    assert _pending(s)[0]["id"] == pid and _pending(s)[0]["status"] == "declined"


# --- fix round 1: a decision never strands its row ------------------------------------

def _queued_profile(s):
    _dana(s)
    return auto_changes.propose(s, "profiles", "dana-okafor", "New profile for Dana Okafor",
                                {"entity_id": "dana-okafor", "profile": "Dana runs finance."})


def test_exception_inside_apply_leaves_row_pending_and_listed(server, monkeypatch):
    srv, s = server
    pid = _queued_profile(s)

    def boom(store, home, payload):
        raise RuntimeError("disk on fire")
    monkeypatch.setitem(auto_changes.APPLY, "profiles", boom)
    code, out = _req(srv, "POST", f"/api/pending/{pid}/apply")
    assert code == 409 and "disk on fire" in out["reason"]
    row = _pending(s)[0]
    assert row["status"] == "pending" and "disk on fire" in row["error"]
    assert [i["id"] for i in _req(srv, "GET", "/api/pending")[1]["items"]] == [pid]


def test_exception_inside_settle_leaves_row_pending_and_listed(server, monkeypatch):
    srv, s = server
    pid = _queued_profile(s)

    def bad_settle(*a, **kw):
        raise RuntimeError("settle failed")
    monkeypatch.setattr(auto_changes, "_settle", bad_settle)
    code, out = _req(srv, "POST", f"/api/pending/{pid}/decline")
    assert code == 409 and "settle failed" in out["reason"]
    assert _pending(s)[0]["status"] == "pending"
    assert [i["id"] for i in _req(srv, "GET", "/api/pending")[1]["items"]] == [pid]
    monkeypatch.undo()
    assert _req(srv, "POST", f"/api/pending/{pid}/decline")[0] == 200


def _set_deciding(s, pid, claimed_at):
    with s._connect(write=True) as db:
        db.execute("UPDATE pending_changes SET status='deciding', decided_at=? WHERE id=?",
                   (claimed_at, pid))


def test_stale_deciding_row_is_recovered(tmp_path, home):
    s = _store(tmp_path)
    old = auto_changes.propose(s, "profiles", "a", "a", {})
    fresh = auto_changes.propose(s, "profiles", "b", "b", {})
    _set_deciding(s, old, "2026-01-01T00:00:00Z")
    _set_deciding(s, fresh, "2099-01-01T00:00:00Z")    # claimed after the cutoff
    assert auto_changes.list_pending(s) == []
    assert auto_changes.recover_stale(s, claimed_before="2026-06-01T00:00:00Z") == 1
    assert [r["id"] for r in auto_changes.list_pending(s)] == [old]
    assert s.pending_changes_count() == 1
    assert auto_changes.recover_stale(s, older_than_s=60) == 0   # 2099 claim is "now-ish"
    assert auto_changes.recover_stale(s) == 1
    assert auto_changes.decide(s, home, fresh, approve=False)["ok"] is True


def test_daemon_startup_sweep_recovers_rows_claimed_before_start(tmp_path, monkeypatch):
    import mcpbrain.daemon as daemon_mod
    s = _store(tmp_path)
    pid = auto_changes.propose(s, "consolidation", "x", "x", {})
    _set_deciding(s, pid, "2026-01-01T00:00:00Z")

    class _D:
        _store = s
    assert daemon_mod.Daemon._recover_pending_changes(_D()) == 1
    assert _pending(s)[0]["status"] == "pending"
    # a stranded consolidation review no longer blocks future passes forever
    assert auto_changes.pending_targets(s, "consolidation") == {"x"}


def test_legacy_dismiss_never_strands_a_row(server, monkeypatch):
    srv, s = server
    pid = auto_changes.propose(s, "profiles", "x", "New profile", {})
    fid = _pending_findings(s)[0]["id"]
    monkeypatch.setattr(auto_changes, "decide",
                        lambda *a, **kw: {"ok": False, "reason": "busy"})
    code, out = _req(srv, "POST", f"/api/dashboard/findings/{fid}/dismiss")
    assert code == 409 and out["reason"] == "busy"
    assert _pending(s)[0]["status"] == "pending" and _pending(s)[0]["id"] == pid
    assert [f["id"] for f in _pending_findings(s)] == [fid]


# --- fix round 1: tidy approval records its own change ------------------------------------

def test_tidy_approval_links_its_own_change_despite_a_concurrent_tidy_write(tmp_path, home,
                                                                         monkeypatch):
    s = _store(tmp_path)
    _modes(home, tidy="ask")
    fids = _orphans(s, 2)
    _drain_orphans(s, fids)
    rows = _pending(s)
    s.upsert_entity("pat-nguyen", "Pat Nguyen", "person")
    real = s.suppress_entity

    def racing_suppress(eid, reason="", *, change=None):
        # Another tidy write lands after the approval starts, before its own.
        s.update_entity_org("pat-nguyen", "Acme", change={
            "change_type": "tidy_org", "source": "review_org", "summary": "race"})
        return real(eid, reason, change=change)
    monkeypatch.setattr(s, "suppress_entity", racing_suppress)
    for r in rows:
        assert auto_changes.decide(s, home, r["id"], approve=True)["ok"] is True
    suppress_ids = [c["id"] for c in _changes(s, "tidy_suppress")]
    assert [r["change_log_id"] for r in _pending(s)] == suppress_ids
    assert len(_changes(s, "tidy_org")) == 2


def test_appliers_report_their_change_ids(tmp_path):
    from mcpbrain.review_apply import apply_orphan_verdicts
    s = _store(tmp_path)
    fids = _orphans(s, 2)
    ids: list = []
    apply_orphan_verdicts(s, [{"finding_id": f, "verdict": "suppress"} for f in fids],
                          cap=5, change_ids=ids)
    assert ids == [c["id"] for c in _changes(s, "tidy_suppress")]


# --- fix round 1: one cap per pass, applied plus proposed --------------------------------

def test_tidy_ask_cap_bounds_applied_plus_proposed(tmp_path, home):
    from mcpbrain.drain import BLOCK_DRAINERS
    s = _store(tmp_path)
    _cfg(home, review_max_apply_per_run=3, auto_changes={"tidy": "ask"},
         orgs=[{"name": "Acme", "domains": ["example.org"]}])
    for name in ("Acme Pty", "Acme Ltd"):
        s.record_finding("org_unrecognised", name, summary=f"unrecognised org {name}")
    for i in range(2):
        s.upsert_entity(f"sam-{i}", f"Sam {i}", "person", org="external")
        s.record_finding("lint:ambiguous_org", f"sam-{i}", summary="ambiguous")
    verdicts = [{"finding_id": f["id"], "verdict": "add_to_config"}
                for f in s.open_findings("org_unrecognised")]
    verdicts += [{"finding_id": f["id"], "verdict": "canonicalize", "canonical_org": "Acme"}
                 for f in s.open_findings("lint:ambiguous_org")]
    out = BLOCK_DRAINERS["review_org"](s, {"review_org": verdicts})
    assert out["suggested"] == 2 and out["proposed"] == 1
    assert out["suggested"] + out["proposed"] <= 3
    assert len(_pending(s)) == 1


# --- fix round 1: draft style off skips the bandit; text names the console setting -------

def test_draft_style_off_skips_the_bandit_run(tmp_path, home, monkeypatch):
    import mcpbrain.daemon as daemon_mod
    from mcpbrain import threshold_bandit
    s = _store(tmp_path)
    _cfg(home, drift_monitor=False, lessons=False, auto_changes={"draft_style": "off"})
    monkeypatch.setattr(daemon_mod, "app_dir", lambda: Path(home))
    calls = []
    monkeypatch.setattr(threshold_bandit, "init_bandit_table", lambda st: calls.append("init"))
    monkeypatch.setattr(threshold_bandit, "advisory_report",
                        lambda st, h: calls.append("report") or {})

    class _D:
        _store = s
        _clock = staticmethod(lambda: 1.0)
        def _is_due(self, *_): return True

    out = daemon_mod.Daemon._run_self_improve(_D())
    assert calls == [] and out["bandit"] == {"skipped": "off"}
    _modes(home, draft_style="advise")
    daemon_mod.Daemon._run_self_improve(_D())
    assert calls == ["init", "report"]


def test_advisory_text_points_at_the_console_setting(tmp_path, home, monkeypatch):
    _s, report = _bandit(tmp_path, home, monkeypatch, auto_changes={"draft_style": "advise"})
    assert "bandit_auto_apply" not in report["advisory"]
    assert "Settings > Automatic changes > Draft style tuning" in report["advisory"]


def test_advisory_report_off_recommends_nothing(tmp_path, home, monkeypatch):
    _s, report = _bandit(tmp_path, home, monkeypatch, auto_changes={"draft_style": "off"})
    assert report["auto_apply"] is False and "off" in report["advisory"]
    assert "bandit_auto_apply" not in report["advisory"]
