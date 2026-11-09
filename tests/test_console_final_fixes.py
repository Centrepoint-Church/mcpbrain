"""Final whole-branch review fixes for the console redesign.

1  Include in the graph: derived chunks refused, enriched chunks kept enriched.
2  Graph tidy-up's lint findings are its input, not Needs-you items.
3  Stall recovery with "Start when I log in" off.
5  Tidy summaries name the entity, not its id.
6  Re-include and prioritise leave change_log rows; re-include is undoable.
7  The change feed folds ingest skips so a heavy day can't truncate it.
8  /api/changes hours is clamped to 1..720.
10 Records-repo git writes share one lock with a worded timeout.
"""
from __future__ import annotations

import json
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcpbrain import change_undo, config
from mcpbrain.store import Store


# --- helpers -----------------------------------------------------------------

def _store(tmp_path, name="s.sqlite3") -> Store:
    s = Store(str(tmp_path / name), dim=4)
    s.init()
    return s


def _cfg(home, **cfg):
    config.write_config(str(home), cfg)


def _chunk(store, doc_id, *, thread, labels="INBOX", subject="Hall B", **extra):
    meta = {"source_type": "gmail", "thread_id": thread, "message_id": doc_id,
            "subject": subject, "sender": "Sam Chen <sam@example.org>",
            "to": "dana@example.org", "date": "Mon, 2 Nov 2026 09:00:00 +0000",
            "labels": labels, "content_type": "email_body", **extra}
    store.upsert_chunk(doc_id, f"Can you confirm the booking for {doc_id}?",
                       "h-" + doc_id, meta)


def _cols(store, doc_id) -> dict:
    with store._connect() as db:
        r = db.execute("SELECT enrich_state, enriched, enriched_version, enrich_attempts, "
                       "metadata FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
    d = dict(r)
    d["force_enrich"] = json.loads(d.pop("metadata") or "{}").get("force_enrich")
    return d


def _changes(store, change_type):
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM change_log WHERE change_type=? ORDER BY id", (change_type,))]


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
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(h))
    return str(h)


@pytest.fixture
def server(tmp_path, home):
    from mcpbrain.control_api import ControlServer
    s = _store(tmp_path)
    srv = ControlServer(FakeDaemon(), home=home, store=s)
    srv.start()
    yield srv, s
    srv.stop()


# --- 1. Include in the graph ---------------------------------------------------

def test_reinclude_enriched_cold_chunk_keeps_enriched(tmp_path):
    s = _store(tmp_path)
    _chunk(s, "promo-e", thread="t-e", labels="INBOX,CATEGORY_PROMOTIONS")
    s.mark_enriched(["promo-e"])
    s.set_enrich_state(["promo-e"], "cold")
    before = _cols(s, "promo-e")
    out = s.reinclude_chunks(["promo-e"])
    assert out == {"changed": 1, "skipped": 0}
    after = _cols(s, "promo-e")
    assert after["enrich_state"] == ""
    assert after["enriched"] == 1
    assert after["enriched_version"] == before["enriched_version"]
    assert "promo-e" not in {c["doc_id"] for c in s.unenriched_chunks()}


def test_reinclude_refuses_derived_chunk_and_it_stays_cold(tmp_path, monkeypatch):
    from mcpbrain import prepare
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, salience_gate=True)
    s = _store(tmp_path)
    s.upsert_chunk("note-cons-1", "Consolidated: Dana and Sam met about Hall B.", "h-c",
                   {"observation_type": "consolidated", "source": "note"})
    _chunk(s, "draft-1", thread="t-draft", labels="DRAFT")
    prepare.prepare_units(s, thread_cap=10, char_budget=100000,
                          resolution_due=False, home=str(tmp_path))
    assert _cols(s, "note-cons-1")["enrich_state"] == "cold"
    assert _cols(s, "draft-1")["enrich_state"] == "cold"
    out = s.reinclude_chunks(["note-cons-1", "draft-1"])
    assert out == {"changed": 0, "skipped": 2}
    prepare.prepare_units(s, thread_cap=10, char_budget=100000,
                          resolution_due=False, home=str(tmp_path))
    for d in ("note-cons-1", "draft-1"):
        c = _cols(s, d)
        assert c["enrich_state"] == "cold" and c["force_enrich"] is None


def test_reinclude_refuses_own_automated_mail(tmp_path):
    from mcpbrain.source_class import Rules
    s = _store(tmp_path)
    _chunk(s, "brief-1", thread="t-b", subject="[Brief] Monday",
           sender="dana@example.org")
    s.set_enrich_state(["brief-1"], "cold")
    rules = Rules(owner_emails=frozenset({"dana@example.org"}), subject_prefixes=("[Brief]",))
    assert s.reinclude_chunks(["brief-1"], rules=rules) == {"changed": 0, "skipped": 1}
    assert _cols(s, "brief-1")["enrich_state"] == "cold"


def test_salience_cold_chunk_is_included_and_stays_eligible(tmp_path, monkeypatch):
    from mcpbrain import prepare
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, salience_gate=True)
    s = _store(tmp_path)
    _chunk(s, "promo-1", thread="t-promo", labels="INBOX,CATEGORY_PROMOTIONS")
    prepare.prepare_units(s, thread_cap=10, char_budget=100000,
                          resolution_due=False, home=str(tmp_path))
    assert _cols(s, "promo-1")["enrich_state"] == "cold"
    assert s.reinclude_chunks(["promo-1"]) == {"changed": 1, "skipped": 0}
    prepare.prepare_units(s, thread_cap=10, char_budget=100000,
                          resolution_due=False, home=str(tmp_path))
    c = _cols(s, "promo-1")
    assert c["enrich_state"] != "cold" and c["force_enrich"] is True
    assert "promo-1" in {x["doc_id"] for x in s.unenriched_chunks()}


def test_cold_list_marks_includable_and_enriched(tmp_path):
    from mcpbrain import source_controls as sc
    s = _store(tmp_path)
    _chunk(s, "promo-1", thread="t1", labels="INBOX,CATEGORY_PROMOTIONS")
    _chunk(s, "promo-e", thread="t2", labels="INBOX,CATEGORY_PROMOTIONS")
    s.upsert_chunk("note-cons-1", "Consolidated note.", "h-c",
                   {"observation_type": "consolidated", "source": "note"})
    s.mark_enriched(["promo-e"])
    s.set_enrich_state(["promo-1", "promo-e", "note-cons-1"], "cold")
    items = {i["doc_id"]: i for i in sc.list_cold(s, str(tmp_path))["items"]}
    assert items["promo-1"]["includable"] is True and items["promo-1"]["enriched"] is False
    assert items["promo-e"]["includable"] is True and items["promo-e"]["enriched"] is True
    assert items["note-cons-1"]["includable"] is False


def test_reinclude_route_reports_skipped(server):
    srv, s = server
    _chunk(s, "promo-1", thread="t1", labels="INBOX,CATEGORY_PROMOTIONS")
    s.upsert_chunk("note-cons-1", "Consolidated note.", "h-c",
                   {"observation_type": "consolidated", "source": "note"})
    s.set_enrich_state(["promo-1", "note-cons-1"], "cold")
    code, out = _req(srv, "POST", "/api/chunks/reinclude",
                     {"doc_ids": ["promo-1", "note-cons-1"]})
    assert code == 200 and out == {"ok": True, "changed": 1, "skipped": 1}


# --- 6. change_log rows for re-include and prioritise ------------------------

def test_reinclude_is_recorded_and_undo_restores_exactly(tmp_path, home):
    s = _store(tmp_path)
    _chunk(s, "promo-1", thread="t1", labels="INBOX,CATEGORY_PROMOTIONS")
    _chunk(s, "promo-e", thread="t2", labels="INBOX,CATEGORY_PROMOTIONS")
    s.mark_enriched(["promo-e"])
    s.bump_enrich_attempts(["promo-1"])
    s.set_enrich_state(["promo-1", "promo-e"], "cold")
    before = {d: _cols(s, d) for d in ("promo-1", "promo-e")}
    s.reinclude_chunks(["promo-1", "promo-e"])
    rows = _changes(s, "chunks_reincluded")
    assert len(rows) == 1 and rows[0]["revert_ref"]
    feed = change_undo.feed(s, hours=1)
    assert next(r for r in feed if r["change_type"] == "chunks_reincluded")["undoable"] is True
    assert change_undo.undo(s, home, rows[0]["id"]) == {"ok": True, "reason": ""}
    assert {d: _cols(s, d) for d in ("promo-1", "promo-e")} == before


def test_reinclude_undo_refused_when_changed_since(tmp_path, home):
    s = _store(tmp_path)
    _chunk(s, "promo-1", thread="t1", labels="INBOX,CATEGORY_PROMOTIONS")
    s.set_enrich_state(["promo-1"], "cold")
    s.reinclude_chunks(["promo-1"])
    s.mark_enriched(["promo-1"])       # enrichment ran since
    cid = _changes(s, "chunks_reincluded")[0]["id"]
    out = change_undo.undo(s, home, cid)
    assert out == {"ok": False, "reason": change_undo.CHANGED_SINCE}
    assert _cols(s, "promo-1")["enriched"] == 1


def test_prioritise_is_recorded_not_undoable(tmp_path):
    s = _store(tmp_path)
    _chunk(s, "c0", thread="t0")
    assert s.prioritise_chunks(["c0"]) == 1
    rows = _changes(s, "chunks_prioritised")
    assert len(rows) == 1 and not rows[0]["revert_ref"]
    assert "can't be undone" in rows[0]["summary"]
    feed = change_undo.feed(s, hours=1)
    assert next(r for r in feed if r["change_type"] == "chunks_prioritised")["undoable"] is False
    assert s.prioritise_chunks(["c0", "nope"]) == 1
    assert s.prioritise_chunks(["nope"]) == 0
    assert len(_changes(s, "chunks_prioritised")) == 2   # nothing moved, nothing recorded


# --- 2. tidy-handled lint findings are not Needs-you items --------------------

TIDY_TYPES = ("lint:orphan_entity", "lint:missing_org", "lint:ownerless_action",
              "lint:ambiguous_org", "lint:duplicate_org", "org_unrecognised")


def _seed_lint(s):
    for t in TIDY_TYPES:
        s.record_finding(t, "ref-" + t, summary="found " + t)
    s.record_finding("agent_stderr", "a.err", summary="boom", severity="warning")


@pytest.mark.parametrize("mode", ["off", "ask", "apply"])
def test_tidy_lint_findings_hidden_in_every_mode(server, home, mode):
    from mcpbrain import console_stats
    srv, s = server
    _cfg(home, auto_changes={"tidy": mode})
    _seed_lint(s)
    code, out = _req(srv, "GET", "/api/findings")
    assert code == 200
    assert [i["finding_type"] for i in out["items"]] == ["agent_stderr"]
    status = {"open_findings": s.open_findings_count(), "connections": {}, "backfill": {}}
    assert console_stats.stats(s, home, status)["needs_you"]["findings"] == 1


def test_pending_tidy_row_shows_once(server, home):
    from mcpbrain.drain import BLOCK_DRAINERS
    srv, s = server
    _cfg(home, auto_changes={"tidy": "ask"})
    s.upsert_entity("acme-junk", "Acme Junk", "person")
    s.record_finding("lint:orphan_entity", "acme-junk", summary="orphan")
    fid = s.open_findings("lint:orphan_entity")[0]["id"]
    BLOCK_DRAINERS["review_orphan"](s, {"review_orphan": [
        {"finding_id": fid, "verdict": "suppress", "reason": "no links"}]})
    _, pending = _req(srv, "GET", "/api/pending")
    assert len(pending["items"]) == 1 and pending["items"][0]["kind"] == "tidy"
    _, findings = _req(srv, "GET", "/api/findings")
    assert findings["items"] == []


# --- 5. tidy summaries carry names ------------------------------------------

def test_tidy_summaries_name_the_entity(tmp_path):
    from mcpbrain.review_apply import (apply_missing_org_verdicts, apply_orphan_verdicts,
                                       apply_ownerless_verdicts)
    _cfg(tmp_path, orgs=[{"name": "Acme", "domains": ["example.org"]}])
    s = _store(tmp_path)
    s.upsert_entity("e-junk", "Acme Junk", "person")
    s.upsert_entity("e-pat", "Pat Nguyen", "person")
    with s._connect(write=True) as db:
        aid = db.execute("INSERT INTO actions(text, owner, status, source) "
                         "VALUES('Send the Hall B budget', '', 'open', 'email')").lastrowid
    s.record_finding("lint:orphan_entity", "e-junk", summary="orphan")
    s.record_finding("lint:missing_org", "e-pat", summary="no org")
    s.record_finding("lint:ownerless_action", str(aid), summary="ownerless")
    f = {x["finding_type"]: x["id"] for x in s.open_findings()}
    apply_orphan_verdicts(s, [{"finding_id": f["lint:orphan_entity"], "verdict": "suppress"}],
                          cap=10)
    apply_missing_org_verdicts(s, [{"finding_id": f["lint:missing_org"], "verdict": "assign",
                                    "org": "Acme"}], cap=10, home=str(tmp_path))
    apply_ownerless_verdicts(s, [{"finding_id": f["lint:ownerless_action"], "verdict": "owner",
                                  "owner": "Sam Chen"}], cap=10)
    sup = _changes(s, "tidy_suppress")[0]
    assert sup["ref_id"] == "e-junk" and "Acme Junk" in sup["summary"]
    assert "e-junk" not in sup["summary"]
    org = _changes(s, "tidy_org")[0]
    assert org["ref_id"] == "e-pat" and "Pat Nguyen" in org["summary"]
    assert "e-pat" not in org["summary"]
    own = _changes(s, "tidy_owner")[0]
    assert own["ref_id"] == str(aid) and "Send the Hall B budget" in own["summary"]


def test_tidy_merge_summary_names_both(tmp_path):
    from mcpbrain.review_apply import apply_duplicate_verdicts
    s = _store(tmp_path)
    s.upsert_entity("sam-chen-x1", "Sam Chen", "person", org="Acme")
    s.upsert_entity("sam-chen-x2", "Sam C", "person", org="Acme")
    apply_duplicate_verdicts(s, [{"pair_id": "sam-chen-x1|sam-chen-x2", "same": True}], cap=10)
    row = _changes(s, "tidy_merge")[0]
    assert "sam-chen-x" not in row["summary"]
    assert "Sam C" in row["summary"] and "Sam Chen" in row["summary"]


# --- 7. feed folds ingest skips -------------------------------------------------

def test_feed_folds_skips_so_a_profile_change_survives(tmp_path):
    s = _store(tmp_path)
    s.record_change("profile_updated", ref_id="dana-okafor", summary="Profile for Dana Okafor")
    with s._connect(write=True) as db:
        for i in range(600):
            db.execute("INSERT INTO change_log(change_type, ref_id, summary, source) "
                       "VALUES('ingest_skip', ?, ?, 'sync')",
                       (f"file{i}", f"unsupported_mime: file{i}.bin"))
    feed = change_undo.feed(s, hours=48)
    assert any(r["change_type"] == "profile_updated" for r in feed)
    skips = [r for r in feed if r["change_type"] == "ingest_skip"]
    assert len(skips) == 1 and skips[0]["count"] == 600
    assert skips[0]["undoable"] is False


def test_feed_single_skip_keeps_its_ref(tmp_path):
    s = _store(tmp_path)
    s.record_change("ingest_skip", ref_id="1AbCdEfGhIjKlMn", summary="extraction_empty: Notes.pdf")
    rows = change_undo.feed(s, hours=1)
    assert len(rows) == 1 and rows[0]["count"] == 1 and rows[0]["ref_id"] == "1AbCdEfGhIjKlMn"
    assert rows[0]["summary"] == "extraction_empty: Notes.pdf"


# --- 8. /api/changes hours clamp ------------------------------------------------

@pytest.mark.parametrize("hours,code", [("0", 400), ("721", 400), ("-3", 400),
                                         ("1", 200), ("720", 200), ("48", 200)])
def test_changes_hours_range(server, hours, code):
    srv, _ = server
    assert _req(srv, "GET", f"/api/changes?hours={hours}")[0] == code
    assert _req(srv, "GET", f"/api/changes/added?hours={hours}")[0] == code


# --- 3. stall recovery with login start off ------------------------------------

def _wd(tmp_path, monkeypatch, platform, *, login):
    from mcpbrain import daemon as d
    dm = d.Daemon.__new__(d.Daemon)
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.sys, "platform", platform)
    monkeypatch.setattr(d, "win_supervised", lambda home: True)
    _cfg(tmp_path, start_at_login=login)
    calls = []
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: calls.append(("exit",)))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: calls.append(("spawn",)))
    dm._agent_restart = lambda plat: calls.append(("agent", plat))
    dm._spawn_detached = lambda argv: calls.append(("helper", list(argv)))
    return dm, calls


def test_stall_login_off_darwin_kickstarts_the_agent(tmp_path, monkeypatch):
    dm, calls = _wd(tmp_path, monkeypatch, "darwin", login=False)
    dm._recover_from_stall()
    assert calls == [("agent", "darwin")]


def test_stall_login_off_darwin_falls_back_when_kickstart_fails(tmp_path, monkeypatch):
    dm, calls = _wd(tmp_path, monkeypatch, "darwin", login=False)

    def boom(plat):
        raise subprocess.CalledProcessError(113, ["launchctl"])
    dm._agent_restart = boom
    dm._recover_from_stall()
    assert calls == [("spawn",)]


def test_stall_login_off_windows_uses_start_after_helper(tmp_path, monkeypatch):
    import os
    import sys
    dm, calls = _wd(tmp_path, monkeypatch, "win32", login=False)
    dm._recover_from_stall()
    assert calls[0][0] == "helper"
    argv = calls[0][1]
    assert argv[:4] == [sys.executable, "-m", "mcpbrain.system_ops", "start-after"]
    assert argv[4] == str(os.getpid()) and argv[5] == str(tmp_path)
    assert calls[1:] == [("exit",)]


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_stall_login_on_is_unchanged(tmp_path, monkeypatch, platform):
    dm, calls = _wd(tmp_path, monkeypatch, platform, login=True)
    dm._recover_from_stall()
    assert calls == [("exit",)]


# --- 10. records-repo git lock --------------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _records(tmp_path) -> Path:
    repo = tmp_path / "records"
    (repo / "context").mkdir(parents=True)
    (repo / "state").mkdir()
    (repo / "memory").mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "t")
    (repo / "context" / "voice.md").write_text("# Voice\n\nNew line.\n")
    (repo / "MEMORY.md").write_text("# Memory Index\n")
    (repo / "state" / "decisions.md").write_text(
        "Append new decisions at the top. One line per decision.\n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


class _Holder:
    """Holds the records lock on another thread until released."""

    def __init__(self):
        from mcpbrain import records_write
        self.got, self.release = threading.Event(), threading.Event()

        def run():
            with records_write.repo_lock():
                self.got.set()
                self.release.wait(5)
        self.t = threading.Thread(target=run)
        self.t.start()
        assert self.got.wait(5)

    def done(self):
        self.release.set()
        self.t.join(5)


def test_undo_waits_on_records_lock_and_refuses_in_words(tmp_path, home, monkeypatch):
    from mcpbrain import records_write
    repo = _records(tmp_path)
    _cfg(home, records_dir=str(repo))
    monkeypatch.setattr(records_write, "LOCK_TIMEOUT_S", 0.2)
    s = _store(tmp_path)
    cid = s.record_change("voice_apply", summary="Voice updated",
                          revert_ref=change_undo.revert_json(
                              {"content": "# Voice\n\nOld line.\n"},
                              {"content": "# Voice\n\nNew line.\n"}))
    h = _Holder()
    try:
        out = change_undo.undo(s, home, cid)
    finally:
        h.done()
    assert out == {"ok": False, "reason": records_write.BUSY}
    assert "git" not in out["reason"].lower()
    assert (repo / "context" / "voice.md").read_text() == "# Voice\n\nNew line.\n"
    assert change_undo.undo(s, home, cid)["ok"] is True
    assert (repo / "context" / "voice.md").read_text() == "# Voice\n\nOld line.\n"


def test_daemon_records_writers_use_the_same_lock(tmp_path, monkeypatch):
    from mcpbrain import records_write
    repo = _records(tmp_path)
    monkeypatch.setattr(records_write, "LOCK_TIMEOUT_S", 0.2)
    h = _Holder()
    try:
        with pytest.raises(records_write.RecordsBusy):
            records_write.append_decision(str(repo), text="Acme moves Hall B to Sunday")
        with pytest.raises(records_write.RecordsBusy):
            records_write.write_memory(str(repo), slug="acme-hall", description="Hall B",
                                       body="Hall B is booked on Sundays.")
        (repo / "context" / "voice.md").write_text("# Voice\n\nThird.\n")
        with pytest.raises(records_write.RecordsBusy):
            records_write._commit_file(str(repo), "context/voice.md", "voice: test")
    finally:
        h.done()
    assert records_write.append_decision(str(repo), text="Acme moves Hall B to Sunday") is True
