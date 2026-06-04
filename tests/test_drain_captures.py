"""drain_captures: validate -> dedupe -> apply -> change_log -> delete."""
import json

from mcpbrain import drain
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
    drain.drain_captures(s, home=tmp_path)
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM chunks "
                       "WHERE doc_id LIKE 'note-%'").fetchone()[0]
    assert n == 1
    assert len(s.recent_changes(10)) == 1


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
    assert acts[0]["owner"] == "Josh"        # config default owner
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

    original_upsert = s.upsert_chunk
    def boom(*a, **kw):
        raise RuntimeError("disk full")
    monkeypatch.setattr(s, "upsert_chunk", boom)

    n = drain.drain_captures(s, home=tmp_path)
    assert n == 0
    # file must still be there for retry
    assert list((tmp_path / "capture_inbox").glob("cap-fail.json"))


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
