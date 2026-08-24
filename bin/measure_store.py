"""Measure a derived store: size, per-table bytes, row counts, planner stats."""
import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcpbrain import config  # noqa: E402
from mcpbrain.store import Store  # noqa: E402


def measure(path) -> dict:
    path = Path(path)
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        ps = db.execute("PRAGMA page_size").fetchone()[0]
        pc = db.execute("PRAGMA page_count").fetchone()[0]
        fl = db.execute("PRAGMA freelist_count").fetchone()[0]
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        rows = {}
        for t in names:
            try:
                rows[t] = db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
            except sqlite3.OperationalError:
                continue
        stat1 = bool(db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone())
        return {"file_bytes": pc * ps, "page_size": ps, "page_count": pc,
                "freelist_bytes": fl * ps, "table_bytes": {}, "row_counts": rows,
                "has_stat1": stat1}
    finally:
        db.close()


def _sample_msg_ids(store) -> list[str]:
    """Sample message IDs from chunks metadata."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT json_extract(metadata, '$.message_id') "
            "FROM chunks "
            "WHERE json_extract(metadata, '$.message_id') IS NOT NULL "
            "LIMIT 10"
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def _sample_thread_id(store) -> str:
    """Sample a single thread ID from chunks metadata."""
    with store._connect() as db:
        row = db.execute(
            "SELECT json_extract(metadata, '$.thread_id') "
            "FROM chunks "
            "WHERE json_extract(metadata, '$.thread_id') IS NOT NULL "
            "LIMIT 1"
        ).fetchone()
    return row[0] if row else ""


def _sample_file_id(store) -> str:
    """Sample a single file ID from chunks metadata."""
    with store._connect() as db:
        row = db.execute(
            "SELECT json_extract(metadata, '$.file_id') "
            "FROM chunks "
            "WHERE json_extract(metadata, '$.file_id') IS NOT NULL "
            "LIMIT 1"
        ).fetchone()
    return row[0] if row else ""


def latency(store) -> dict:
    """Time the 0.7.105 benchmark methods through the real Store API."""
    out = {}
    probes = {
        "doc_ids_for_messages": lambda: store.doc_ids_for_messages(_sample_msg_ids(store)),
        "thread_chunks": lambda: store.thread_chunks(_sample_thread_id(store)),
        "chunks_for_file": lambda: store.chunks_for_file(_sample_file_id(store)),
        "inbound_chunks_since": lambda: store.inbound_chunks_since("2026-01-01"),
    }
    for name, fn in probes.items():
        t0 = time.perf_counter()
        fn()
        out[name] = (time.perf_counter() - t0) * 1000  # ms
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--latency", action="store_true",
                    help="Measure latency of 0.7.105 methods")
    ap.add_argument("--home", default=None, help="Home directory (for testing)")
    ns = ap.parse_args(argv)

    home = ns.home or str(config.app_dir())
    store_file = Path(home) / "brain.sqlite3"

    if ns.latency:
        # Open the store in read-only mode to get latency measurements
        store = Store(store_file, dim=384, read_only=True)
        m = measure(store_file)
        lat = latency(store)
        output = {**m, "latency": lat}
        print(json.dumps(output, indent=2))
    else:
        m = measure(store_file)
        print(json.dumps(m, indent=2))


if __name__ == "__main__":
    main()
