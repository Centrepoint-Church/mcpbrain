"""drain_captures: validate -> dedupe -> apply -> change_log -> delete."""
import json

from mcpbrain import drain
import mcpbrain.chunking as drain_chunking
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _spool(home, name, env):
    d = home / "capture_inbox"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps(env))


def _ingest_env(title="T", content="C"):
    return {"kind": "ingest", "captured_at": "2026-06-04T12:00:00Z",
            "source": "code", "title": title, "content": content,
            "tags": "a", "observation_type": "memory", "org": ""}


def test_ingest_becomes_chunk_and_change_row(tmp_path):
    s = _store(tmp_path)
    _spool(tmp_path, "cap-1.json", _ingest_env())
    n = drain.drain_captures(s, home=tmp_path)
    assert n == 1
    assert not list((tmp_path / "capture_inbox").glob("cap-*.json"))  # deleted
    chunks = s.recent_changes(10)
    assert chunks[0]["change_type"] == "capture_ingest"
    # the chunk exists and is queued for embedding
    with s._connect() as db:
        row = db.execute("SELECT doc_id, embedded FROM chunks "
                         "WHERE doc_id LIKE 'note-%'").fetchone()
    assert row is not None and row["embedded"] == 0


def test_ingest_retry_is_idempotent(tmp_path):
    s = _store(tmp_path)
    _spool(tmp_path, "cap-1.json", _ingest_env())
    drain.drain_captures(s, home=tmp_path)
    _spool(tmp_path, "cap-2.json", _ingest_env())  # same content
    assert drain.drain_captures(s, home=tmp_path) == 0  # idempotent: nothing applied
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM chunks "
                       "WHERE doc_id LIKE 'note-%'").fetchone()[0]
    assert n == 1
    assert len(s.recent_changes(10)) == 1


def test_ingest_crash_retry_keeps_single_change_row(tmp_path):
    """Crash-retry: the chunk was applied on the first run but the file was not
    unlinked before the crash. Re-draining the same envelope must not write a
    second change_log row (the upsert no-ops on unchanged content)."""
    s = _store(tmp_path)
    _spool(tmp_path, "cap-1.json", _ingest_env())
    drain.drain_captures(s, home=tmp_path)
    # Simulate the crash window: chunk is present, envelope re-appears.
    _spool(tmp_path, "cap-1.json", _ingest_env())  # same content, re-processed
    applied = drain.drain_captures(s, home=tmp_path)
    assert applied == 0
    capture_changes = [c for c in s.recent_changes(10)
                       if c["change_type"] == "capture_ingest"]
    assert len(capture_changes) == 1


def test_action_create_and_dedupe(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    env = {"kind": "action_create", "captured_at": "x", "source": "desktop",
           "text": "File the BAS", "owner": "", "deadline": "2026-07-01",
           "org": "", "project_id": "", "area_id": ""}
    _spool(tmp_path, "cap-1.json", env)
    _spool(tmp_path, "cap-2.json", env)  # duplicate
    drain.drain_captures(s, home=tmp_path)
    acts = s.unified_actions(status="open")
    assert len(acts) == 1
    assert acts[0]["owner"] == ""             # unconfigured install: empty owner
    assert acts[0]["source"] == "capture"


def test_action_create_canonicalizes_owner_alias(tmp_path, monkeypatch):
    # A caller (MCP tool caller, another agent) may pass an alias variant of
    # the configured owner ("Josh Kemp" for owner_name="Joshua") instead of
    # leaving owner empty. The written row must carry the canonical owner
    # string, or it becomes invisible to brain_actions(owner="") (which
    # matches config.owner_name() exactly).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "owner_name": "Joshua", "owner_full_name": "Joshua Kemp",
    }))
    s = _store(tmp_path)
    env = {"kind": "action_create", "captured_at": "x", "source": "desktop",
           "text": "File the BAS", "owner": "joshua kemp", "deadline": "",
           "org": "", "project_id": "", "area_id": ""}
    _spool(tmp_path, "cap-1.json", env)
    drain.drain_captures(s, home=tmp_path)
    acts = s.unified_actions(status="open")
    assert len(acts) == 1
    assert acts[0]["owner"] == "Joshua"


def test_action_update_closes_open_action(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Old task")
    _spool(tmp_path, "cap-1.json",
           {"kind": "action_update", "captured_at": "x", "source": "desktop",
            "action_id": aid, "status": "done"})
    drain.drain_captures(s, home=tmp_path)
    assert s.unified_actions(status="open") == []
    assert s.recent_changes(5)[0]["change_type"] == "capture_action_update"


def test_invalid_envelope_quarantined(tmp_path):
    s = _store(tmp_path)
    _spool(tmp_path, "cap-bad.json", {"kind": "telepathy"})
    (tmp_path / "capture_inbox" / "cap-notjson.json").write_text("{nope")
    drain.drain_captures(s, home=tmp_path)
    bad = list((tmp_path / "capture_inbox" / "bad").glob("*.json"))
    assert len(bad) == 2


def test_store_write_failure_preserves_file(tmp_path, monkeypatch):
    """A SQLite error during apply must NOT delete the spool file."""
    s = _store(tmp_path)
    _spool(tmp_path, "cap-fail.json", _ingest_env(title="Will fail"))

    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(s, "upsert_chunk", boom)

    n = drain.drain_captures(s, home=tmp_path)
    assert n == 0
    # file must still be there for retry
    assert list((tmp_path / "capture_inbox").glob("cap-fail.json"))


def test_short_note_stays_one_chunk_with_bare_doc_id(tmp_path):
    """The common case — 2,109 of 3,299 live notes — must not change shape."""
    s = _store(tmp_path)
    _spool(tmp_path, "cap-short.json", _ingest_env(title="T", content="a short body"))
    drain.drain_captures(s, home=tmp_path)
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert "-" not in rows[0]["doc_id"].removeprefix("note-")
    assert rows[0]["metadata"]["note_id"] == rows[0]["doc_id"]
    assert rows[0]["metadata"]["chunk_total"] == 1


def test_long_note_is_chunked_with_suffixed_doc_ids(tmp_path):
    """Notes bypassed chunk_text entirely, so only the first ~2,000 chars of a
    133,791-char note were ever embedded. 1,192 live notes / 21.1MB are affected."""
    body = "\n\n".join(f"para {i} " + "y" * 400 for i in range(30))
    s = _store(tmp_path)
    _spool(tmp_path, "cap-long.json", _ingest_env(title="T", content=body))
    drain.drain_captures(s, home=tmp_path)
    with s._connect() as db:
        ids = [r[0] for r in db.execute(
            "SELECT doc_id FROM chunks WHERE doc_id LIKE 'note-%' ORDER BY doc_id")]
    assert len(ids) > 1
    assert all(i.rsplit("-", 1)[1].isdigit() for i in ids)
    base = ids[0].rsplit("-", 1)[0]
    # Lossless: note_chunks reassembles the original body verbatim.
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == base
    assert rows[0]["text"] == f"T\n\n{body}"


def test_single_paragraph_note_is_split_losslessly(tmp_path):
    """The case that used to be stored whole, and is the ENTIRE remaining recall
    hole: a note whose body is one paragraph larger than the chunk budget.

    chunk_text cannot round-trip it -- _split_paragraph word-splits via
    para.split() (collapsing internal whitespace) and duplicates the last
    `overlap` words across a boundary, so the capture path used to detect the
    failed round-trip and store the note whole, leaving everything past the
    first ~2,000 chars unembedded. Measured on the live store that was 930
    notes / 16,022,678 chars of tail, and no overlap setting fixed it
    (overlap=0 rescued zero). split_lossless carries each break's separator on
    the preceding piece, so "" rejoins it exactly.
    """
    from mcpbrain.chunking import CHUNK_JOIN, chunk_text, split_lossless

    body = " ".join(f"word{i}" for i in range(700))   # ONE paragraph, no blank lines
    assert CHUNK_JOIN.join(chunk_text(body)) != body   # chunk_text still cannot
    assert "".join(split_lossless(body)) == body       # split_lossless can

    s = _store(tmp_path)
    _spool(tmp_path, "cap-para.json", _ingest_env(title="T", content=body))
    drain.drain_captures(s, home=tmp_path)

    with s._connect() as db:
        ids = [r[0] for r in db.execute(
            "SELECT doc_id FROM chunks WHERE doc_id LIKE 'note-%' ORDER BY doc_id")]
    assert len(ids) > 1                               # split, not stored whole
    assert all(i.rsplit("-", 1)[1].isdigit() for i in ids)

    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["metadata"]["split"] == "lossless"
    assert rows[0]["text"] == f"T{CHUNK_JOIN}{body}"  # byte-exact round trip


def test_unsplittable_note_falls_back_to_one_whole_row(tmp_path, monkeypatch):
    """The guard behind split_lossless. It should be unreachable, but if a future
    change ever broke the round-trip invariant the note must still be stored
    whole -- never split into pieces that cannot reassemble it, because
    note_chunks serves the reassembly AS the note and the sweep deletes the only
    whole-body row."""
    monkeypatch.setattr(drain_chunking, "split_lossless",
                        lambda text, max_chars=1800: ["broken", "pieces"])
    s = _store(tmp_path)
    _spool(tmp_path, "cap-bad.json",
           _ingest_env(title="T", content="x" * 5000))
    drain.drain_captures(s, home=tmp_path)

    with s._connect() as db:
        ids = [r[0] for r in db.execute(
            "SELECT doc_id FROM chunks WHERE doc_id LIKE 'note-%'")]
    assert len(ids) == 1                              # stored whole
    assert "-" not in ids[0].removeprefix("note-")    # bare base id
    assert s.note_chunks(observation_type="memory")[0]["metadata"]["chunk_total"] == 1

def test_action_update_reopen(tmp_path):
    """Reopening a done action succeeds and is logged."""
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Old task")
    # close it first
    s.set_action_status(aid, "done", resolved_by="test", only_if_open=True)
    assert s.unified_actions(status="open") == []
    # reopen via capture
    _spool(tmp_path, "cap-reopen.json",
           {"kind": "action_update", "captured_at": "x", "source": "desktop",
            "action_id": aid, "status": "open"})
    drain.drain_captures(s, home=tmp_path)
    acts = s.unified_actions(status="open")
    assert len(acts) == 1
    changes = s.recent_changes(5)
    assert any(c["change_type"] == "capture_action_update" for c in changes)


# --- ingestion integrity: one cold note per Claude session ---

import hashlib  # noqa: E402


def _session_env(content, sid="sess-abc-123", title=None, tags="session"):
    return {"kind": "ingest", "captured_at": "2026-10-22T01:00:00Z",
            "source": "session_end_hook",
            "title": title or f"Session {sid[:8]} 2026-10-22",
            "content": content, "tags": tags, "observation_type": "note",
            "claude_session_id": sid}


def _note_rows(s, prefix="note-"):
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT doc_id, text, metadata, enrich_state FROM chunks "
            "WHERE doc_id LIKE ? ORDER BY doc_id", (prefix + "%",))]


def test_session_base_id_is_stable_and_namespaced(tmp_path):
    s = _store(tmp_path)
    _spool(tmp_path, "cap-1.json", _session_env("user: hello\n\nassistant: hi"))
    drain.drain_captures(s, home=tmp_path)
    expect = "note-session-" + hashlib.sha256(b"sess-abc-123").hexdigest()[:24]
    rows = _note_rows(s)
    assert [r["doc_id"] for r in rows] == [expect]
    meta = json.loads(rows[0]["metadata"])
    assert meta["claude_session_id"] == "sess-abc-123"
    assert meta["note_id"] == expect


def test_recapture_replaces_not_appends(tmp_path):
    s = _store(tmp_path)
    _spool(tmp_path, "cap-1.json", _session_env("user: first window"))
    drain.drain_captures(s, home=tmp_path)
    _spool(tmp_path, "cap-2.json",
           _session_env("user: first window\n\nassistant: and the latest reply"))
    assert drain.drain_captures(s, home=tmp_path) == 1
    notes = s.note_chunks()
    assert len(notes) == 1
    assert notes[0]["text"].endswith("and the latest reply")


def test_recapture_removes_pieces_beyond_new_count(tmp_path):
    s = _store(tmp_path)
    long_body = "\n\n".join(f"para {i} " + "y" * 400 for i in range(30))
    _spool(tmp_path, "cap-1.json", _session_env(long_body))
    drain.drain_captures(s, home=tmp_path)
    assert len(_note_rows(s)) > 1
    # shrink to a single piece: every old -<i> row must go, bare id remains
    _spool(tmp_path, "cap-2.json", _session_env("user: short now"))
    drain.drain_captures(s, home=tmp_path)
    rows = _note_rows(s)
    assert len(rows) == 1 and "-" not in rows[0]["doc_id"].removeprefix("note-session-")
    # grow again: the bare row must not linger beside the -<i> pieces
    _spool(tmp_path, "cap-3.json", _session_env(long_body + "\n\nmore"))
    drain.drain_captures(s, home=tmp_path)
    rows = _note_rows(s)
    assert len(rows) > 1
    assert all(r["doc_id"].rsplit("-", 1)[1].isdigit() for r in rows)
    notes = s.note_chunks()
    assert len(notes) == 1 and notes[0]["text"].endswith("more")


def test_session_notes_are_cold(tmp_path):
    s = _store(tmp_path)
    long_body = "\n\n".join(f"para {i} " + "y" * 400 for i in range(30))
    _spool(tmp_path, "cap-1.json", _session_env(long_body))
    # legacy session envelope: no claude_session_id, but session-tagged
    legacy = _session_env("user: legacy window", tags="session,pre-compact")
    legacy.pop("claude_session_id")
    _spool(tmp_path, "cap-2.json", legacy)
    _spool(tmp_path, "cap-3.json", _ingest_env(title="Plain", content="ordinary note"))
    drain.drain_captures(s, home=tmp_path)
    rows = _note_rows(s)
    sess = [r for r in rows if "session" in json.loads(r["metadata"]).get("tags", "")]
    plain = [r for r in rows if r not in sess]
    assert len(sess) > 2
    assert all(r["enrich_state"] == "cold" for r in sess)
    assert plain and all((r["enrich_state"] or "") != "cold" for r in plain)
    # legacy path keeps today's content-hash id
    assert any(r["doc_id"].startswith("note-") and not r["doc_id"].startswith("note-session-")
               for r in sess)
    # cold chunks never reach the enrichment backlog
    backlog = {c["doc_id"] for c in s.unenriched_chunks(limit=1000)}
    assert not backlog & {r["doc_id"] for r in sess}
