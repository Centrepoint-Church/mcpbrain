"""The repair CLI is attended and destructive; its guardrails are the tests.

Precedent: bin/consolidate.py — 91 lines, backup first, gold gate printed, all
logic in the library.
"""
import subprocess
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin" / "repair.py"


def _run(*args, home):
    return subprocess.run([sys.executable, str(_BIN), *args],
                          capture_output=True, text=True,
                          env={"MCPBRAIN_HOME": str(home), "PATH": ""})


def test_dry_run_is_the_default(tmp_path):
    """Nothing destructive may happen without an explicit --apply. The one
    guardrail that matters most: this operates on an 11 GB irreplaceable store."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "|  |  |", "h1", {})

    out = _run("purge-empty", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "dry run" in out.stdout.lower()
    assert Store(tmp_path / "brain.sqlite3", dim=4).get_chunk("d1") is not None


def test_apply_refuses_without_enough_free_disk(tmp_path, monkeypatch):
    """The backup is a full copy of an 11 GB file. A previous session filled this
    machine's disk to zero copying that database and the emergency cleanup
    destroyed an unrelated application's data. Refuse rather than risk it."""
    import bin.repair as repair

    monkeypatch.setattr(repair, "_free_bytes", lambda path: 1024)

    ok, why = repair.preflight(tmp_path / "brain.sqlite3", db_bytes=11 * 1024**3)

    assert ok is False
    assert "disk" in why.lower()


def test_purge_reports_what_it_would_do_without_doing_it(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    for i in range(3):
        store.upsert_chunk(f"d{i}", "|  |  |", f"h{i}", {})
    store.upsert_chunk("keep", "real content", "hk", {})

    out = _run("purge-empty", home=tmp_path)

    assert "3" in out.stdout
    assert Store(tmp_path / "brain.sqlite3", dim=4).count_content_free() == 3


def test_reingest_phase_reports_the_stale_file_count(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "legacy", "h1",
                       {"source_type": "gdrive", "file_id": "f1"})

    out = _run("reingest-stale", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "1" in out.stdout


def test_an_unknown_phase_exits_nonzero(tmp_path):
    out = _run("delete-everything", home=tmp_path)

    assert out.returncode != 0


def test_purge_apply_halts_cleanly_on_a_graph_cited_chunk(tmp_path):
    """purge_doc_ids raises ValueError, deleting NOTHING in that batch, if the
    graph cites any doc_id in it (all-or-nothing, by design — see
    tests/test_purge.py::test_purge_is_all_or_nothing). This measured zero
    cited doc_ids among the 68,193 live content-free chunks on 2026-07-28, but
    if it ever DOES fire the CLI must not let a bare traceback through: it must
    print which doc_id is cited and exit non-zero, and — since purge_doc_ids is
    all-or-nothing — every content-free chunk in that same batch (cited or not)
    must survive untouched rather than be silently orphaned or partially purged."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-cited", "|  |  |", "h1", {})
    store.upsert_chunk("d-uncited", "|  |  |", "h2", {})
    with store._connect(write=True) as db:
        db.execute("INSERT INTO entity_relations"
                   "(entity_a,relation,entity_b,source_doc_id,valid_from) "
                   "VALUES('e1','mentioned_with','e2','d-cited','2026-01-01')")

    out = _run("purge-empty", "--apply", home=tmp_path)

    assert out.returncode != 0
    assert "d-cited" in (out.stdout + out.stderr)
    # The presentational requirement, not just "it happens to fail": no raw
    # Python stack trace — the ValueError must be caught and reported as a
    # one-line CLI message, not let through as an uncaught-exception crash
    # (whose default traceback would ALSO happen to mention "d-cited", since
    # that's inside the exception's own str() — so this is the assertion that
    # actually distinguishes "handled" from "crashed").
    assert "Traceback (most recent call last)" not in out.stderr
    assert "refusing" in (out.stdout + out.stderr).lower()
    reopened = Store(tmp_path / "brain.sqlite3", dim=4)
    assert reopened.get_chunk("d-cited") is not None, "cited chunk must survive"
    assert reopened.get_chunk("d-uncited") is not None, (
        "purge_doc_ids is all-or-nothing: nothing in the same batch is deleted")
