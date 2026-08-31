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
