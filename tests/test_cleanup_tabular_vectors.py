from pathlib import Path
import subprocess
import sys

_BIN = Path(__file__).resolve().parents[1] / "bin" / "cleanup_tabular_vectors.py"


def test_dry_run_reports_without_deleting(tmp_path, capsys):
    from mcpbrain.store import Store

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "x" * 3000, "h1", {"content_subtype": "table"})
    with store._connect() as db:
        rowid = db.execute(
            "SELECT rowid FROM chunks WHERE doc_id='gdrive-f1-0'").fetchone()["rowid"]
    store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])

    out = subprocess.run(
        [sys.executable, str(_BIN), "--home", str(tmp_path)],
        capture_output=True, text=True)

    assert out.returncode == 0, out.stderr
    assert "1" in out.stdout  # reports 1 candidate
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE rowid=?", (rowid,)
        ).fetchone()[0] == 1, "dry run must not delete anything"


def test_apply_deletes_matching_vec_chunks_rows(tmp_path):
    from mcpbrain.store import Store

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "x" * 3000, "h1", {"content_subtype": "table"})
    store.upsert_chunk("gdrive-f2-0", "y" * 100, "h2", {"content_subtype": "table"})
    store.upsert_chunk("gmail-m1-0", "z" * 3000, "h3", {"content_subtype": "prose"})
    for doc_id in ("gdrive-f1-0", "gdrive-f2-0", "gmail-m1-0"):
        with store._connect() as db:
            rowid = db.execute(
                "SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["rowid"]
        store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])

    subprocess.run(
        [sys.executable, str(_BIN), "--home", str(tmp_path),
         "--apply"],
        capture_output=True, text=True, check=True)

    with store._connect() as db:
        remaining = {r["doc_id"] for r in db.execute(
            "SELECT c.doc_id FROM chunks c JOIN vec_chunks v ON v.rowid=c.rowid"
        ).fetchall()}
    # Only the oversize table chunk (f1) is deleted; the short table chunk
    # (f2, under 2000 chars) and the prose chunk (m1) survive.
    assert remaining == {"gdrive-f2-0", "gmail-m1-0"}


def test_apply_spares_a_chunk_already_re_rendered_at_the_current_version(tmp_path):
    """A table chunk at CHUNKER_VERSION has already been through the new
    renderer -- if it's still over 2000 chars (a legitimately dense row
    group), deleting its vector would be permanent data loss: embedded stays
    1, so stale_chunker_ids (version-gated) never re-selects it and nothing
    re-queues it for embedding."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.store import Store

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4)
    store.init()
    store.upsert_chunk("gdrive-old-0", "x" * 3000, "h1",
                       {"content_subtype": "table", "chunker_version": CHUNKER_VERSION - 1})
    store.upsert_chunk("gdrive-fresh-0", "y" * 3000, "h2",
                       {"content_subtype": "table", "chunker_version": CHUNKER_VERSION})
    for doc_id in ("gdrive-old-0", "gdrive-fresh-0"):
        with store._connect() as db:
            rowid = db.execute(
                "SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["rowid"]
        store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])

    subprocess.run(
        [sys.executable, str(_BIN), "--home", str(tmp_path), "--apply"],
        capture_output=True, text=True, check=True)

    with store._connect() as db:
        remaining = {r["doc_id"] for r in db.execute(
            "SELECT c.doc_id FROM chunks c JOIN vec_chunks v ON v.rowid=c.rowid"
        ).fetchall()}
    assert remaining == {"gdrive-fresh-0"}
