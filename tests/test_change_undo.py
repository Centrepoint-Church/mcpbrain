"""Every automatic change is recorded with what it replaced, and can be undone.

Each writer test runs the REAL writer against a tmp store (and, for records-repo
writers, a tmp git records repo), checks the change_log row carries a JSON
revert_ref with before/after, undoes it through change_undo.undo, and checks the
prior value is back.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mcpbrain import change_undo
from mcpbrain.store import Store

CHANGED_SINCE = "This has changed since; undo the later change first"


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


def _home(tmp_path, repo=None, **cfg) -> str:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    if repo is not None:
        cfg["records_dir"] = str(repo)
    (home / "config.json").write_text(json.dumps(cfg))
    return str(home)


def _last(s, change_type) -> dict:
    with s._connect() as db:
        row = db.execute("SELECT * FROM change_log WHERE change_type=? ORDER BY id DESC LIMIT 1",
                         (change_type,)).fetchone()
    assert row is not None, f"no {change_type} change recorded"
    return dict(row)


def _payload(row) -> dict:
    p = json.loads(row["revert_ref"])
    assert "before" in p and "after" in p
    return p


def _head_subject(repo) -> str:
    return _git(repo, "log", "-1", "--format=%s").strip()


def _entity(s, eid) -> dict | None:
    with s._connect() as db:
        r = db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    return dict(r) if r else None


ACME_CFG = {"orgs": [{"name": "Acme", "domains": ["example.org"]}]}


# --- profile_updated -----------------------------------------------------------

def _write_profile(s, eid, text):
    from mcpbrain.profile_synth import drain_profiles
    drain_profiles(s, {"profile_synthesis": [{"entity_id": eid, "profile": text}]})


def test_profile_updated_undo_restores_prior(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    _write_profile(s, "dana-okafor", "Dana runs finance at Acme.")
    row = _last(s, "profile_updated")
    p = _payload(row)
    assert p["before"]["profile"] == ""
    assert p["after"]["profile"] == "Dana runs finance at Acme."
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": True, "reason": ""}
    assert _entity(s, "dana-okafor")["profile"] == ""


def test_undo_refuses_when_changed_since(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    _write_profile(s, "dana-okafor", "First profile.")
    first = _last(s, "profile_updated")
    _write_profile(s, "dana-okafor", "Second profile.")
    out = change_undo.undo(s, str(tmp_path), first["id"])
    assert out == {"ok": False, "reason": CHANGED_SINCE}
    assert _entity(s, "dana-okafor")["profile"] == "Second profile."


def test_undo_twice_is_refused(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    _write_profile(s, "dana-okafor", "First profile.")
    row = _last(s, "profile_updated")
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": False, "reason": "Already undone"}


def test_undo_unknown_change(tmp_path):
    s = _store(tmp_path)
    out = change_undo.undo(s, str(tmp_path), 9999)
    assert out["ok"] is False and out["reason"] == change_undo.NOT_FOUND


def test_not_undoable_types_are_refused(tmp_path):
    s = _store(tmp_path)
    cid = s.record_change("ingest_skip", ref_id="x", summary="skipped")
    out = change_undo.undo(s, str(tmp_path), cid)
    assert out["ok"] is False and out["reason"] == change_undo.NOT_UNDOABLE


# --- community_titled ----------------------------------------------------------

def _seed_community(s, cid=7, title="Old title", summary="Old summary"):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary) "
                   "VALUES(?,0,?,?)", (cid, title, summary))


def _community(s, cid=7):
    with s._connect() as db:
        return dict(db.execute("SELECT title, summary FROM community_summaries "
                               "WHERE community_id=?", (cid,)).fetchone())


def test_community_titled_undo_restores_prior(tmp_path):
    from mcpbrain.community_synth import drain_communities
    s = _store(tmp_path)
    _seed_community(s)
    drain_communities(s, {"community_synthesis": [
        {"community_id": 7, "title": "Acme finance", "summary": "The finance circle"}]})
    row = _last(s, "community_titled")
    p = _payload(row)
    assert p["before"] == {"title": "Old title", "summary": "Old summary"}
    assert p["after"] == {"title": "Acme finance", "summary": "The finance circle"}
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert _community(s) == {"title": "Old title", "summary": "Old summary"}


# --- consolidation_pass --------------------------------------------------------

def test_consolidation_pass_undo_removes_notes_tiers_and_graduation(tmp_path, monkeypatch):
    from mcpbrain import consolidation
    s = _store(tmp_path)
    repo = _records(tmp_path)
    home = _home(tmp_path, repo, consolidation=True)
    cluster = []
    for i in range(5):
        doc = f"acme-ep-{i}"
        s.upsert_chunk(doc, f"Acme budget review item {i}", f"h{i}", {})
        s.set_chunk_tier(doc, "warm")
        cluster.append({"doc_id": doc, "text": f"Acme budget review item {i}",
                        "metadata": {}, "salience": 5.0})
    monkeypatch.setattr(consolidation, "_unconsolidated_salience", lambda store: 1e9)
    monkeypatch.setattr(consolidation, "_episodic_chunks_for_consolidation",
                        lambda store, cap: cluster)
    monkeypatch.setattr(consolidation, "_cluster", lambda chunks, embedder=None: [cluster])
    summary = "Acme budget review settled the Q3 numbers. Dana Okafor owns the follow-up."
    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt: summary)

    out = consolidation.consolidate(s, home)
    assert out["notes_written"] == 1
    row = _last(s, "consolidation_pass")
    p = _payload(row)
    notes = p["before"]["notes"]
    assert notes and all(s.get_chunk(d) for d in notes)
    assert p["before"]["tiers"] == {f"acme-ep-{i}": "warm" for i in range(5)}
    grads = p["before"]["graduated"]
    assert len(grads) == 1 and (repo / grads[0]).exists()

    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert all(s.get_chunk(d) is None for d in notes)
    with s._connect() as db:
        tiers = {r["doc_id"]: r["memory_tier"] for r in db.execute(
            "SELECT doc_id, memory_tier FROM chunks WHERE doc_id LIKE 'acme-ep-%'")}
    assert tiers == {f"acme-ep-{i}": "warm" for i in range(5)}
    assert not (repo / grads[0]).exists()
    assert grads[0] not in (repo / "MEMORY.md").read_text()
    assert _head_subject(repo) == "undo: consolidation"


# --- voice_apply ---------------------------------------------------------------

def test_voice_apply_undo_restores_file_and_commits(tmp_path):
    from mcpbrain import voice_apply
    s = _store(tmp_path)
    repo = _records(tmp_path)
    home = _home(tmp_path, repo, procedural_memory=True)
    original = (repo / "context" / "voice.md").read_text()
    s.insert_voice_suggestion("style", "Lead with the ask", 0.9, [], "seen in drafts")
    assert voice_apply.apply_suggestions(s, home)["applied"] == 1
    row = _last(s, "voice_apply")
    p = _payload(row)
    assert p["before"]["content"] == original
    assert "Lead with the ask" in p["after"]["content"]
    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert (repo / "context" / "voice.md").read_text() == original
    assert _head_subject(repo) == "undo: voice"


def test_voice_apply_undo_refused_after_hand_edit(tmp_path):
    from mcpbrain import voice_apply
    s = _store(tmp_path)
    repo = _records(tmp_path)
    home = _home(tmp_path, repo, procedural_memory=True)
    s.insert_voice_suggestion("style", "Lead with the ask", 0.9, [], "seen in drafts")
    voice_apply.apply_suggestions(s, home)
    (repo / "context" / "voice.md").write_text("hand edited\n")
    row = _last(s, "voice_apply")
    assert change_undo.undo(s, home, row["id"]) == {"ok": False, "reason": CHANGED_SINCE}
    assert (repo / "context" / "voice.md").read_text() == "hand edited\n"


# --- gardener_write ------------------------------------------------------------

def test_gardener_write_recorded_and_undone(tmp_path, monkeypatch):
    from mcpbrain.tools import make_brain_gardener_apply
    s = _store(tmp_path)
    repo = _records(tmp_path)
    home = _home(tmp_path, repo)
    monkeypatch.setenv("MCPBRAIN_HOME", home)
    original = (repo / "reference" / "acme-notes.md").read_text()
    tool = make_brain_gardener_apply(s)
    out = asyncio.run(tool("reference", "acme-notes.md", "# Acme notes\n\nUpdated line.\n"))
    assert out["applied"] is True
    row = _last(s, "gardener_write")
    p = _payload(row)
    assert p["before"] == {"path": "reference/acme-notes.md", "content": original}
    assert p["after"]["content"] == "# Acme notes\n\nUpdated line.\n"
    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert (repo / "reference" / "acme-notes.md").read_text() == original
    assert _head_subject(repo) == "undo: gardener"


# --- bandit_applied ------------------------------------------------------------

def test_bandit_applied_recorded_and_undone(tmp_path, monkeypatch):
    from mcpbrain import config, threshold_bandit
    s = _store(tmp_path)
    home = _home(tmp_path, recall_max_distance=0.80, bandit_auto_apply=True)
    threshold_bandit.init_bandit_table(s)
    monkeypatch.setattr(s, "all_feedback_rows", lambda: [{"event_type": "used"}])
    monkeypatch.setattr(threshold_bandit, "recommend", lambda store: 0.65)
    report = threshold_bandit.advisory_report(s, home)
    assert report["auto_apply"] is True
    assert config.recall_max_distance(home) == pytest.approx(0.65)
    row = _last(s, "bandit_applied")
    p = _payload(row)
    assert p["before"] == {"recall_max_distance": 0.80}
    assert p["after"] == {"recall_max_distance": 0.65}
    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert config.recall_max_distance(home) == pytest.approx(0.80)


# --- graph tidy-up appliers ----------------------------------------------------

def test_tidy_suppress_undo(tmp_path):
    from mcpbrain.review_apply import apply_orphan_verdicts
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    s.record_finding("lint:orphan_entity", "acme-junk", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]
    apply_orphan_verdicts(s, [{"finding_id": fid, "verdict": "suppress"}], cap=5)
    row = _last(s, "tidy_suppress")
    p = _payload(row)
    assert p["before"]["suppressed"] is None and p["after"]["suppressed"]
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id='acme-junk'"
                          ).fetchone() is None


def test_tidy_org_undo(tmp_path):
    from mcpbrain.review_apply import apply_missing_org_verdicts
    s = _store(tmp_path)
    home = _home(tmp_path, **ACME_CFG)
    s.upsert_entity("sam-chen", "Sam Chen", "person", org="")
    s.record_finding("lint:missing_org", "sam-chen", summary="no org")
    fid = s.open_findings("lint:missing_org")[0]["id"]
    apply_missing_org_verdicts(s, [{"finding_id": fid, "verdict": "assign", "org": "Acme"}],
                               cap=5, home=home)
    assert _entity(s, "sam-chen")["org"] == "Acme"
    row = _last(s, "tidy_org")
    assert _payload(row)["before"]["org"] == ""
    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert _entity(s, "sam-chen")["org"] == ""


def test_tidy_org_from_ambiguous_org_verdict_undo(tmp_path):
    from mcpbrain.review_apply import apply_org_verdicts
    s = _store(tmp_path)
    home = _home(tmp_path, **ACME_CFG)
    s.upsert_entity("pat-nguyen", "Pat Nguyen", "person", org="external")
    s.record_finding("lint:ambiguous_org", "pat-nguyen", summary="ambiguous")
    fid = s.open_findings("lint:ambiguous_org")[0]["id"]
    apply_org_verdicts(s, [{"finding_id": fid, "verdict": "canonicalize",
                            "canonical_org": "Acme"}], cap=5, home=home)
    row = _last(s, "tidy_org")
    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert _entity(s, "pat-nguyen")["org"] == "external"


def test_tidy_org_rewrite_undo(tmp_path):
    from mcpbrain.review_apply import apply_org_verdicts
    s = _store(tmp_path)
    home = _home(tmp_path, **ACME_CFG)
    s.upsert_entity("sam-chen", "Sam Chen", "person", org="Acme Inc")
    s.upsert_entity("pat-nguyen", "Pat Nguyen", "person", org="Acme Inc")
    s.record_finding("lint:duplicate_org", "Acme Inc", summary="variant")
    fid = s.open_findings("lint:duplicate_org")[0]["id"]
    apply_org_verdicts(s, [{"finding_id": fid, "verdict": "canonicalize",
                            "canonical_org": "Acme"}], cap=5, home=home)
    assert _entity(s, "sam-chen")["org"] == "Acme"
    row = _last(s, "tidy_org_rewrite")
    p = _payload(row)
    assert p["before"]["org"] == "Acme Inc"
    assert sorted(p["before"]["entity_ids"]) == ["pat-nguyen", "sam-chen"]
    assert change_undo.undo(s, home, row["id"])["ok"] is True
    assert _entity(s, "sam-chen")["org"] == "Acme Inc"
    assert _entity(s, "pat-nguyen")["org"] == "Acme Inc"


def test_tidy_owner_undo(tmp_path):
    from mcpbrain.review_apply import apply_ownerless_verdicts
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Send the Acme budget")
    s.record_finding("lint:ownerless_action", str(aid), summary="no owner")
    fid = s.open_findings("lint:ownerless_action")[0]["id"]
    apply_ownerless_verdicts(s, [{"finding_id": fid, "verdict": "owner",
                                  "owner": "Sam Chen", "owner_entity_id": "sam-chen"}], cap=5)
    row = _last(s, "tidy_owner")
    assert _payload(row)["before"] == {"owner": "", "owner_entity_id": ""}
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    with s._connect() as db:
        r = db.execute("SELECT owner, owner_entity_id FROM actions WHERE id=?", (aid,)).fetchone()
    assert (r["owner"], r["owner_entity_id"]) == ("", "")


def test_tidy_merge_undo_from_duplicate_verdicts(tmp_path):
    from mcpbrain.review_apply import apply_duplicate_verdicts
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    s.upsert_entity("dana-okafor-2", "Dana Okafor", "person", org="")
    out = apply_duplicate_verdicts(
        s, [{"pair_id": "dana-okafor|dana-okafor-2", "same": True, "canonical": "Dana Okafor"}],
        cap=5)
    assert out["merged"] == 1
    row = _last(s, "tidy_merge")
    p = _payload(row)
    assert p["before"]["snapshot"]["merge_log_id"] == p["after"]["merge_log_id"]
    gone = p["after"]["loser_id"]
    assert _entity(s, gone) is None
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert _entity(s, "dana-okafor") is not None and _entity(s, "dana-okafor-2") is not None


def test_tidy_merge_undo_from_org_curate(tmp_path):
    from mcpbrain.org_curate import apply_org_merge_answers
    s = _store(tmp_path)
    s.upsert_entity("acme", "Acme", "org")
    s.upsert_entity("acme-ltd", "Acme Ltd", "org")
    out = apply_org_merge_answers(s, [{"pair_id": "acme|acme-ltd", "same": True,
                                       "canonical": "Acme"}], cap=5)
    assert out["merged"] == 1
    row = _last(s, "tidy_merge")
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert _entity(s, "acme") is not None and _entity(s, "acme-ltd") is not None


# --- capture_action_update -----------------------------------------------------

def test_capture_action_update_undo(tmp_path):
    from mcpbrain import drain
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Book the Acme venue")
    inbox = tmp_path / "capture_inbox"
    inbox.mkdir()
    (inbox / "cap-1.json").write_text(json.dumps(
        {"kind": "action_update", "captured_at": "x", "source": "desktop",
         "action_id": aid, "status": "done"}))
    drain.drain_captures(s, home=tmp_path)
    row = _last(s, "capture_action_update")
    p = _payload(row)
    assert p["before"]["status"] == "open" and p["after"]["status"] == "done"
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    with s._connect() as db:
        r = db.execute("SELECT status, resolved_at FROM actions WHERE id=?", (aid,)).fetchone()
    assert r["status"] == "open" and (r["resolved_at"] or "") == ""


# --- legacy revert_ref forms ---------------------------------------------------

def test_legacy_action_snoozed_undo(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Call Acme")
    assert s.snooze_action(aid, "2026-12-01")
    row = _last(s, "action_snoozed")
    assert row["revert_ref"] == "snoozed_until:"
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    with s._connect() as db:
        r = db.execute("SELECT snoozed_until FROM actions WHERE id=?", (aid,)).fetchone()
    assert (r["snoozed_until"] or "") == ""


def test_legacy_role_corrected_undo(tmp_path):
    from mcpbrain.graph_write import fetch_role, write_role_observation
    from mcpbrain.profile_audit import drain_audit
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    write_role_observation(s, "dana-okafor", "Finance Officer", "profile_audit",
                           "2026-01-01", "high")
    drain_audit(s, {"profile_audit": [{"entity_id": "dana-okafor", "corrections": [
        {"field": "role", "new_value": "Finance Director", "evidence": "signature"}]}]})
    assert fetch_role(s, "dana-okafor") == "Finance Director"
    row = _last(s, "role_corrected")
    assert row["revert_ref"] == "role:Finance Officer"
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert fetch_role(s, "dana-okafor") == "Finance Officer"


def test_legacy_org_corrected_undo(tmp_path):
    from mcpbrain.profile_audit import drain_audit
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme Inc")
    drain_audit(s, {"profile_audit": [{"entity_id": "dana-okafor", "corrections": [
        {"field": "org", "new_value": "Acme", "evidence": "signature"}]}]})
    row = _last(s, "org_corrected")
    assert row["revert_ref"] == "org:Acme Inc"
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert _entity(s, "dana-okafor")["org"] == "Acme Inc"


def test_legacy_graph_correction_undo(tmp_path):
    from mcpbrain import graph_corrections as gc
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person")
    s.upsert_entity("acme", "Acme", "org")
    gw.upsert_relation(s, "dana-okafor", "works_at", "acme", valid_from="2026-01-01")
    out = gc.submit(s, {"basis": "user_stated", "reason": "Dana told me",
                        "op": "reject_relation", "entity_a": "dana-okafor",
                        "relation": "works_at", "entity_b": "acme"})
    assert out["status"] == "applied"
    row = _last(s, "graph_corrected")
    assert row["revert_ref"] == f"correction:{out['correction_id']}"
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    assert s.get_correction(out["correction_id"])["status"] == "reverted"


def test_legacy_entity_suppressed_undo(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    assert graph_view.suppress_entity(s, "acme-junk")["ok"] is True
    row = _last(s, "entity_suppressed")
    assert change_undo.undo(s, str(tmp_path), row["id"])["ok"] is True
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id='acme-junk'"
                          ).fetchone() is None


# --- prune, feed, added_since --------------------------------------------------

def test_prune_keeps_undoable_rows_for_30_days(tmp_path):
    s = _store(tmp_path)
    rj = change_undo.revert_json({"profile": "a"}, {"profile": "b"})
    old = s.record_change("profile_updated", ref_id="old", revert_ref=rj)
    young1 = s.record_change("profile_updated", ref_id="young1", revert_ref=rj)
    young2 = s.record_change("profile_updated", ref_id="young2", revert_ref=rj)
    stamp = (datetime.now(timezone.utc) - timedelta(days=31)).strftime("%Y-%m-%d %H:%M:%S")
    with s._connect(write=True) as db:
        db.execute("UPDATE change_log SET created_at=? WHERE id=?", (stamp, old))
    for i in range(600):
        s.record_change("ingest_skip", ref_id=str(i), summary="plain")
    s.prune_change_log(500)
    with s._connect() as db:
        ids = {r[0] for r in db.execute("SELECT id FROM change_log")}
    assert young1 in ids and young2 in ids
    assert old not in ids
    assert len(ids) == 502


def test_feed_groups_by_type_and_minute(tmp_path):
    s = _store(tmp_path)
    rj = change_undo.revert_json({"profile": ""}, {"profile": "x"})
    for i in range(6):
        s.record_change("profile_updated", ref_id=f"e{i}", revert_ref=rj)
    s.record_change("ingest_skip", ref_id="f", summary="skipped")
    stamp = "2026-11-09 10:15:00"
    with s._connect(write=True) as db:
        db.execute("UPDATE change_log SET created_at=?", (stamp,))
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    items = change_undo.feed(s, hours=24, now=now)
    prof = [i for i in items if i["change_type"] == "profile_updated"]
    assert len(prof) == 6
    assert len({i["group"] for i in prof}) == 1
    assert all(i["undoable"] and not i["undone"] for i in prof)
    skip = [i for i in items if i["change_type"] == "ingest_skip"]
    assert skip and skip[0]["undoable"] is False
    assert set(prof[0]) >= {"id", "change_type", "summary", "ref_id", "created_at",
                            "undoable", "undone", "group"}
    # outside the window
    assert change_undo.feed(s, hours=1, now=now) == []


def test_feed_marks_undone(tmp_path):
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    _write_profile(s, "dana-okafor", "First.")
    row = _last(s, "profile_updated")
    change_undo.undo(s, str(tmp_path), row["id"])
    items = change_undo.feed(s, hours=24)
    it = next(i for i in items if i["id"] == row["id"])
    assert it["undone"] is True and it["undoable"] is False
    assert all(i["change_type"] != "change_undone" for i in items)


def test_added_since_reads_relation_and_observation_sources(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person")
    s.upsert_entity("acme", "Acme", "org")
    s.upsert_entity("sam-chen", "Sam Chen", "person")
    s.upsert_entity("pat-nguyen", "Pat Nguyen", "person")
    rid = gw.upsert_relation(s, "dana-okafor", "works_at", "acme", valid_from="2026-01-01")
    now = datetime.now(timezone.utc)
    fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO relation_sources(relation_id, source_key, source_class, "
                   "first_seen) VALUES(?,?,?,?)", (rid, "msg-1", "email", fresh))
        oid = db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source, "
                         "valid_from) VALUES('sam-chen','role','Analyst','x','2026-01-01')"
                         ).lastrowid
        db.execute("INSERT INTO observation_sources(observation_id, source_key, source_class, "
                   "first_seen) VALUES(?,?,?,?)", (oid, "msg-2", "email", fresh))
        oid2 = db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source, "
                          "valid_from) VALUES('pat-nguyen','role','Lead','x','2026-01-01')"
                          ).lastrowid
        db.execute("INSERT INTO observation_sources(observation_id, source_key, source_class, "
                   "first_seen) VALUES(?,?,?,?)", (oid2, "msg-3", "email", stale))
    got = change_undo.added_since(s, hours=24)
    ids = {g["entity_id"] for g in got}
    assert ids == {"dana-okafor", "acme", "sam-chen"}
    assert all(set(g) == {"entity_id", "name", "type"} for g in got)


# --- routes --------------------------------------------------------------------

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


def test_routes_undo_feed_added_and_circle_title(tmp_path):
    from mcpbrain.control_api import ControlServer
    s = _store(tmp_path)
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Acme")
    _write_profile(s, "dana-okafor", "First.")
    row = _last(s, "profile_updated")
    _seed_community(s, cid=3, title="Old", summary="Kept summary")
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=s)
    srv.start()
    try:
        code, body = _req(srv, "GET", "/api/changes?hours=24")
        assert code == 200 and any(i["id"] == row["id"] for i in body["items"])
        assert _req(srv, "POST", f"/api/changes/{row['id']}/undo") == (200, {"ok": True})
        code, body = _req(srv, "POST", f"/api/changes/{row['id']}/undo")
        assert code == 409 and body == {"ok": False, "reason": "Already undone"}
        code, _ = _req(srv, "POST", "/api/changes/99999/undo")
        assert code == 404
        code, body = _req(srv, "GET", "/api/changes/added?hours=24")
        assert code == 200 and "items" in body

        code, body = _req(srv, "POST", "/api/circles/3/title", {"title": "Acme leaders"})
        assert code == 200 and body["ok"] is True
        assert _community(s, 3) == {"title": "Acme leaders", "summary": "Kept summary"}
        crow = _last(s, "community_titled")
        assert crow["source"] == "dashboard"
        assert _payload(crow)["before"]["title"] == "Old"
        code, _ = _req(srv, "POST", "/api/circles/404/title", {"title": "Nope"})
        assert code == 404
        code, _ = _req(srv, "POST", "/api/circles/3/title", {"title": "  "})
        assert code == 400
    finally:
        srv.stop()


# --- fix round 1: changed again by a DIFFERENT change type or by hand -----------

def _suppression(s, eid):
    with s._connect() as db:
        r = db.execute("SELECT reason, suppressed_at FROM entity_suppressions WHERE entity_id=?",
                       (eid,)).fetchone()
    return dict(r) if r else None


def test_tidy_suppress_undo_refused_after_hand_resuppress(tmp_path):
    from mcpbrain.review_apply import apply_orphan_verdicts
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    s.record_finding("lint:orphan_entity", "acme-junk", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]
    apply_orphan_verdicts(s, [{"finding_id": fid, "verdict": "suppress"}], cap=5)
    row = _last(s, "tidy_suppress")
    s.unsuppress_entity("acme-junk")
    s.suppress_entity("acme-junk", reason="user hid it")
    held = _suppression(s, "acme-junk")
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": False, "reason": CHANGED_SINCE}
    assert _suppression(s, "acme-junk") == held


def test_capture_action_update_undo_refused_after_reopen_then_close(tmp_path):
    from mcpbrain import drain
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Book the Acme venue")
    inbox = tmp_path / "capture_inbox"
    inbox.mkdir()
    (inbox / "cap-1.json").write_text(json.dumps(
        {"kind": "action_update", "captured_at": "x", "source": "desktop",
         "action_id": aid, "status": "done"}))
    drain.drain_captures(s, home=tmp_path)
    row = _last(s, "capture_action_update")
    s.set_action_status(aid, "open", resolved_by="dashboard")
    s.set_action_status(aid, "done", resolved_by="dashboard")
    with s._connect() as db:
        held = dict(db.execute("SELECT status, resolved_by, resolved_at FROM actions WHERE id=?",
                               (aid,)).fetchone())
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": False, "reason": CHANGED_SINCE}
    with s._connect() as db:
        assert dict(db.execute("SELECT status, resolved_by, resolved_at FROM actions "
                               "WHERE id=?", (aid,)).fetchone()) == held


def test_legacy_snooze_without_date_is_refused(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Call Acme")
    with s._connect(write=True) as db:
        db.execute("UPDATE actions SET snoozed_until='2026-12-24' WHERE id=?", (aid,))
    cid = s.record_change("action_snoozed", ref_id=str(aid), summary="Snoozed",
                          revert_ref="snoozed_until:")
    assert change_undo.undo(s, str(tmp_path), cid) == {"ok": False, "reason": CHANGED_SINCE}
    with s._connect() as db:
        assert db.execute("SELECT snoozed_until FROM actions WHERE id=?",
                          (aid,)).fetchone()[0] == "2026-12-24"


def test_entity_suppressed_undo_refused_after_later_tidy_suppress(tmp_path):
    from mcpbrain import graph_view
    from mcpbrain.review_apply import apply_orphan_verdicts
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    graph_view.suppress_entity(s, "acme-junk")
    row = _last(s, "entity_suppressed")
    s.unsuppress_entity("acme-junk")
    s.record_finding("lint:orphan_entity", "acme-junk", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]
    apply_orphan_verdicts(s, [{"finding_id": fid, "verdict": "suppress",
                               "reason": "orphan"}], cap=5)
    held = _suppression(s, "acme-junk")
    assert held is not None
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": False, "reason": CHANGED_SINCE}
    assert _suppression(s, "acme-junk") == held


def test_entity_suppressed_undo_refused_when_reason_differs(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    graph_view.suppress_entity(s, "acme-junk")
    row = _last(s, "entity_suppressed")
    s.suppress_entity("acme-junk", reason="user hid it")
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": False, "reason": CHANGED_SINCE}
    assert _suppression(s, "acme-junk")["reason"] == "user hid it"


def test_consolidation_undo_refused_when_graduated_file_edited(tmp_path, monkeypatch):
    from mcpbrain import consolidation
    s = _store(tmp_path)
    repo = _records(tmp_path)
    home = _home(tmp_path, repo, consolidation=True)
    cluster = []
    for i in range(5):
        doc = f"acme-ep-{i}"
        s.upsert_chunk(doc, f"Acme budget review item {i}", f"h{i}", {})
        s.set_chunk_tier(doc, "warm")
        cluster.append({"doc_id": doc, "text": f"Acme budget review item {i}",
                        "metadata": {}, "salience": 5.0})
    monkeypatch.setattr(consolidation, "_unconsolidated_salience", lambda store: 1e9)
    monkeypatch.setattr(consolidation, "_episodic_chunks_for_consolidation",
                        lambda store, cap: cluster)
    monkeypatch.setattr(consolidation, "_cluster", lambda chunks, embedder=None: [cluster])
    monkeypatch.setattr(consolidation, "_call_claude",
                        lambda prompt: "Acme budget review settled the Q3 numbers.")
    consolidation.consolidate(s, home)
    row = _last(s, "consolidation_pass")
    p = _payload(row)
    rel = p["before"]["graduated"][0]
    assert p["after"]["graduated_content"][rel] == (repo / rel).read_text()
    (repo / rel).write_text("hand edited\n")
    head = _head_subject(repo)
    assert change_undo.undo(s, home, row["id"]) == {"ok": False, "reason": CHANGED_SINCE}
    assert (repo / rel).read_text() == "hand edited\n"
    assert all(s.get_chunk(d) for d in p["before"]["notes"])
    with s._connect() as db:
        assert {r[0] for r in db.execute(
            "SELECT memory_tier FROM chunks WHERE doc_id LIKE 'acme-ep-%'")} == {"hot"}
    assert _head_subject(repo) == head


# --- entity_unsuppressed (Settings > Hidden entities > Restore) -----------------

def test_entity_unsuppressed_undo_hides_it_again_with_the_prior_reason(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    s.suppress_entity("acme-junk", reason="No links to anything")
    before = _suppression(s, "acme-junk")
    out = graph_view.restore_entity(s, "acme-junk")
    assert out["ok"] is True
    assert _suppression(s, "acme-junk") is None
    row = _last(s, "entity_unsuppressed")
    assert row["id"] == out["change_id"] and row["ref_id"] == "acme-junk"
    p = _payload(row)
    assert p["before"]["suppressed"] == before and p["after"]["suppressed"] is None
    assert change_undo.undo(s, str(tmp_path), row["id"]) == {"ok": True, "reason": ""}
    assert _suppression(s, "acme-junk") == before
    # A second undo of the same restore is refused.
    assert change_undo.undo(s, str(tmp_path), row["id"])["reason"] == "Already undone"


def test_entity_unsuppressed_undo_refused_when_hidden_again_since(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    s.suppress_entity("acme-junk", reason="first")
    row_id = graph_view.restore_entity(s, "acme-junk")["change_id"]
    s.suppress_entity("acme-junk", reason="user hid it")
    held = _suppression(s, "acme-junk")
    assert change_undo.undo(s, str(tmp_path), row_id) == {"ok": False, "reason": CHANGED_SINCE}
    assert _suppression(s, "acme-junk") == held


def test_restore_after_graph_hide_refuses_the_older_hide_undo(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    graph_view.suppress_entity(s, "acme-junk")
    hide = _last(s, "entity_suppressed")
    graph_view.restore_entity(s, "acme-junk")
    # Already visible: undoing the hide has nothing to take back.
    assert change_undo.undo(s, str(tmp_path), hide["id"]) == {"ok": False,
                                                              "reason": CHANGED_SINCE}


def test_restore_entity_not_hidden(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    assert graph_view.restore_entity(s, "acme-junk") == {"ok": False, "error": "not_hidden"}
    assert graph_view.restore_entity(s, "nobody") == {"ok": False, "error": "not_found"}
