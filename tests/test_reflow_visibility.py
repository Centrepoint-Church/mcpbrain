"""Reflow progress and halt state surfaced in doctor, /api/status and the
dashboard payload, plus the attended bin/reflow.py CLI built on top."""
import os
import subprocess
import sys
from pathlib import Path

from mcpbrain import dashboard
from mcpbrain.doctor import reflow_line
from mcpbrain.store import Store

_BIN = Path(__file__).resolve().parents[1] / "bin" / "reflow.py"


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _run(*args, home):
    # Forwards FASTEMBED_CACHE_PATH (when set, e.g. this machine's persistent
    # model cache) so these subprocess calls hit get_embedder's already-cached
    # weights instead of a cold download to a throwaway tmp_path -- the CLI
    # under test always resolves `dim` through the embedder (bin/repair.py's
    # convention; there is no config.embed_dim), regardless of subcommand.
    env = {"MCPBRAIN_HOME": str(home), "PATH": ""}
    cache = os.environ.get("FASTEMBED_CACHE_PATH")
    if cache:
        env["FASTEMBED_CACHE_PATH"] = cache
    return subprocess.run([sys.executable, str(_BIN), *args],
                          capture_output=True, text=True, env=env)


def test_reflow_line_idle_progress_and_halt(tmp_path):
    s = _store(tmp_path)
    assert reflow_line(s).startswith("✅")
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    assert "1 queued" in reflow_line(s)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    line = reflow_line(s)
    assert line.startswith("❌") and "bin/reflow.py" in line


def test_reflow_line_skips_on_store_failure():
    class _Broken:
        def reflow_stats(self):
            raise RuntimeError("boom")

    line = reflow_line(_Broken())
    assert line.startswith("➖")


def test_dashboard_stats_passes_through_reflow_block(tmp_path):
    import sqlite3

    db_path = tmp_path / "a.sqlite3"
    with sqlite3.connect(str(db_path)) as db:
        db.execute("CREATE TABLE entities(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE entity_observations(id INTEGER PRIMARY KEY)")
        db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, enrich_state TEXT)")

    class _Store:
        _path = str(db_path)

        def list_communities(self):
            return []

    status = {
        "chunk_count": 0, "enriched_count": 0, "spool": {"pending": 0, "inbox": 0},
        "backfill": {}, "connections": {}, "reflow": {"queued": 3},
    }
    out = dashboard.stats(_Store(), str(tmp_path), status)
    assert out["reflow"] == {"queued": 3}


# -- bin/reflow.py: `status` must never write, and must never create the ----
# -- store or its tables -- see the 2026-09-10 sync_cursors corruption ------
# -- incident this guards against (CLAUDE.md). ------------------------------

def test_status_never_writes_to_the_store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    db_path = tmp_path / "brain.sqlite3"
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    out = _run("status", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "queued" in out.stdout
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime


def test_status_on_missing_store_exits_nonzero_without_creating_a_file(tmp_path):
    db_path = tmp_path / "brain.sqlite3"
    assert not db_path.exists()

    out = _run("status", home=tmp_path)

    assert out.returncode != 0
    assert not db_path.exists()
    # Nor any sibling artifact init() would have created (WAL/shm sidecars).
    # (tmp_path always contains "ostmp" here -- the suite's own autouse
    # _isolate_daemon_tempdir fixture, unrelated to this CLI.)
    assert [p.name for p in tmp_path.iterdir()] == ["ostmp"]


def test_status_on_uninitialized_store_exits_nonzero_and_creates_no_tables(tmp_path):
    """A store file that exists but was never store.init()'d (e.g. touched by
    something else) must fail loudly, not silently create the reflow tables."""
    db_path = tmp_path / "brain.sqlite3"
    db_path.touch()
    before_bytes = db_path.read_bytes()

    out = _run("status", home=tmp_path)

    assert out.returncode != 0
    assert "reflow tables not present" in out.stderr
    assert db_path.read_bytes() == before_bytes


def test_resume_without_yes_makes_no_write(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    db_path = tmp_path / "brain.sqlite3"
    before_bytes = db_path.read_bytes()

    out = _run("resume", home=tmp_path)

    assert out.returncode == 1
    assert "halted:" in out.stdout
    assert db_path.read_bytes() == before_bytes


def test_resume_yes_clears_the_halt(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")

    out = _run("resume", "--yes", home=tmp_path)

    assert out.returncode == 0
    assert "halt cleared" in out.stdout
    assert s.get_cursor("reflow:halted") == ""


# -- final review I5: every terminal outcome, N, and the seed's gate reason ----

def _seed_owner(s, fid, n=2, enriched=True):
    for i in range(n):
        s.upsert_chunk(f"gdrive-{fid}-{i}", f"text {fid} {i}", f"{fid}{i}",
                       {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                        "chunk_total": n, "mime_type": "application/pdf"})
    if enriched:
        with s._connect(write=True) as db:
            db.execute("UPDATE chunks SET enriched=1 WHERE doc_id LIKE ?", (f"gdrive-{fid}-%",))


def test_reflow_stats_counts_every_outcome_and_remaining(tmp_path):
    s = _store(tmp_path)
    _seed_owner(s, "A"); _seed_owner(s, "B"); _seed_owner(s, "C")
    s.record_reflow_outcome("X", "drive", "ordinary")
    s.record_reflow_outcome("Y", "gmail", "gave_up")
    s.record_reflow_outcome("Z", "gmail", "source_gone")
    st = s.reflow_stats(live_remaining=True)
    assert st["owners_done"] == 3
    assert st["by_outcome"] == {"ordinary": 1, "gave_up": 1, "source_gone": 1}
    assert st["remaining"] == 3 and st["total"] == 6


def test_carried_means_covered_and_enriched(tmp_path):
    from mcpbrain.reflow import plan
    from mcpbrain.sync.normalise import Chunk
    s = _store(tmp_path)
    _seed_owner(s, "F", n=2, enriched=False)
    old = s.owner_chunks(["gdrive-F-"])
    new = [Chunk("gdrive-F-0", "text F 0\ntext F 1", "n", {"source_type": "gdrive",
                 "file_id": "F", "chunk_index": 0}, ["text F 0", "text F 1"])]
    out = s.apply_reflow("F", "drive", plan(old, new), [[0.1] * 4])
    assert out["carried"] == 0 and out["uncovered"] == 0      # covered, not enriched
    assert out["inherited_unenriched"] == 1
    st = s.reflow_stats()
    assert st["by_outcome"] == {"carried": 1} and st["chunks_carried"] == 0


def test_reflow_owners_outcome_column_migrates_an_existing_store(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("DROP TABLE reflow_owners")
        db.execute("CREATE TABLE reflow_owners(owner TEXT PRIMARY KEY, source TEXT NOT NULL,"
                   " at TEXT NOT NULL, chunks_new INTEGER NOT NULL, carried INTEGER NOT NULL,"
                   " reenrich INTEGER NOT NULL)")
        db.execute("INSERT INTO reflow_owners VALUES('F','drive','t',1,1,0)")
    s.init()
    assert s.reflow_stats()["by_outcome"] == {"carried": 1}


def test_doctor_is_not_idle_while_the_seed_is_blocked(tmp_path):
    import json
    s = _store(tmp_path)
    _seed_owner(s, "A")
    s.set_cursor("reflow:last_seed", json.dumps({"status": "no_recent_backup", "at": "t"}))
    line = reflow_line(s)
    assert not line.startswith("✅") and "no_recent_backup" in line


def test_doctor_reports_n_of_total(tmp_path):
    import json
    s = _store(tmp_path)
    _seed_owner(s, "A")
    s.record_reflow_outcome("X", "drive", "carried")
    s.set_cursor("reflow:last_seed", json.dumps({"status": "ok", "at": "t"}))
    line = reflow_line(s)
    assert line.startswith("⏳") and "1 of 2" in line


def test_doctor_idle_only_when_nothing_remains(tmp_path):
    import json
    s = _store(tmp_path)
    s.set_cursor("reflow:last_seed", json.dumps({"status": "no_recent_backup", "at": "t"}))
    assert reflow_line(s).startswith("✅")


def test_dashboard_page_renders_the_reflow_state():
    html = (Path(dashboard.__file__).parent / "wizard" / "dashboard.html").read_text()
    assert 'id="d-reflow"' in html and "reflowText(data.reflow)" in html
    assert "blocked: " in html


def test_bin_reflow_status_reports_outcomes_and_remaining(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    _seed_owner(s, "A")
    s.record_reflow_outcome("X", "drive", "gave_up")
    out = _run("status", home=tmp_path)
    assert out.returncode == 0, out.stderr
    assert "'remaining': 1" in out.stdout and "'gave_up': 1" in out.stdout


# -- residual R3: the dashboard text says "blocked: <reason>" ----------------

def _reflow_text(r):
    import json
    import re
    import shutil
    import pytest
    if not shutil.which("node"):
        pytest.skip("node not installed")
    html = (Path(dashboard.__file__).parent / "wizard" / "dashboard.html").read_text()
    fn = re.search(r"function reflowText\(r\)\{.*?\n\}\n", html, flags=re.S).group(0)
    js = f"const fmt = String;\n{fn}\nprocess.stdout.write(reflowText({json.dumps(r)}));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return out.stdout


def test_dashboard_reflow_text_reports_every_blocked_status():
    for status in ("no_recent_backup", "disabled", "error"):
        r = {"owners_done": 3, "queued": 0, "remaining": 5, "total": 8,
             "last_seed": {"status": status, "remaining": 5}}
        assert _reflow_text(r) == "blocked: " + status.replace("_", " ")
    # Blocked even when rows are still queued, and even when remaining is 0:
    # a gated seed is never "idle"/"done".
    r = {"owners_done": 3, "queued": 2, "remaining": 0,
         "last_seed": {"status": "disabled", "remaining": 0}}
    assert _reflow_text(r) == "blocked: disabled"
    assert _reflow_text({"owners_done": 0, "queued": 0, "remaining": 0,
                         "last_seed": {"status": "ok"}}) == "idle"
    assert _reflow_text({"halted": "x"}) == "halted — see doctor"


# -- hardening H5: doctor and dashboard agree on "blocked" -------------------
# Blocked = work remains (queued or remaining) AND the last seed was gated.
# Once the backlog is done a stale backup is not a block.

def _gated(s, status, remaining):
    import json
    s.set_cursor("reflow:last_seed", json.dumps({"status": status, "remaining": remaining,
                                                "sources": ["reflow:drive"]}))


def test_doctor_done_backlog_with_stale_backup_is_not_blocked(tmp_path):
    s = _store(tmp_path)
    _gated(s, "no_recent_backup", 0)
    line = reflow_line(s)
    assert line.startswith("✅") and "BLOCKED" not in line


def test_doctor_queued_rows_with_a_gated_seed_are_blocked(tmp_path):
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    _gated(s, "disabled", 0)
    assert "BLOCKED: last seed disabled" in reflow_line(s)


def test_dashboard_done_backlog_with_stale_backup_is_not_blocked():
    for status in ("no_recent_backup", "disabled", "error"):
        assert _reflow_text({"owners_done": 4, "queued": 0, "remaining": 0,
                             "last_seed": {"status": status, "remaining": 0}}) == "done (4)"
        assert _reflow_text({"owners_done": 0, "queued": 0, "remaining": 0,
                             "last_seed": {"status": status}}) == "idle"


def test_dashboard_gated_seed_with_unknown_remaining_is_blocked():
    """remaining == null (a first-ever error tick, or a failed count) is
    UNKNOWN, not zero: a gated seed must still read as blocked."""
    for status in ("no_recent_backup", "disabled", "error"):
        assert _reflow_text({"owners_done": 0, "queued": 0, "remaining": None,
                             "last_seed": {"status": status, "remaining": None}}
                            ) == "blocked: " + status.replace("_", " ")
    assert _reflow_text({"owners_done": 0, "queued": 0, "remaining": None,
                         "last_seed": {"status": "ok"}}) == "idle"


# -- dry run #2 D4: "re-enrich" split into uncovered vs inherited-unenriched --

def _d4_owner(s):
    """Old: chunk 0 enriched, chunk 1 never enriched (e.g. cold by design).
    New: chunk 0 = old 0 (carried), chunk 1 = old 1 (covered, inherits
    enriched=0), chunk 2 = genuinely new text (uncovered)."""
    from mcpbrain.reflow import plan
    from mcpbrain.sync.normalise import Chunk
    _seed_owner(s, "F", n=2, enriched=False)
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1 WHERE doc_id='gdrive-F-0'")
        db.execute("UPDATE chunks SET enrich_state='cold' WHERE doc_id='gdrive-F-1'")
    old = s.owner_chunks(["gdrive-F-"])
    md = {"source_type": "gdrive", "file_id": "F"}
    new = [Chunk("gdrive-F-0", "text F 0", "a", {**md, "chunk_index": 0}, ["text F 0"]),
           Chunk("gdrive-F-1", "text F 1", "b", {**md, "chunk_index": 1}, ["text F 1"]),
           Chunk("gdrive-F-2", "speaker notes for Northgate Trust", "c",
                 {**md, "chunk_index": 2}, ["speaker notes for Northgate Trust"])]
    return s.apply_reflow("F", "drive", plan(old, new), [[0.1] * 4] * 3)


def test_apply_reflow_reports_uncovered_and_inherited_unenriched(tmp_path):
    s = _store(tmp_path)
    out = _d4_owner(s)
    assert (out["carried"], out["uncovered"], out["inherited_unenriched"]) == (1, 1, 1)
    assert "reenrich" not in out
    st = s.reflow_stats()
    assert (st["chunks_carried"], st["chunks_uncovered"],
            st["chunks_inherited_unenriched"]) == (1, 1, 1)
    assert "chunks_reenrich" not in st


def test_reflow_owners_gains_the_split_columns_on_an_existing_store(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("DROP TABLE reflow_owners")
        db.execute("CREATE TABLE reflow_owners(owner TEXT PRIMARY KEY, source TEXT NOT NULL,"
                   " at TEXT NOT NULL, chunks_new INTEGER NOT NULL, carried INTEGER NOT NULL,"
                   " reenrich INTEGER NOT NULL, outcome TEXT NOT NULL DEFAULT 'carried')")
        db.execute("INSERT INTO reflow_owners VALUES('G','drive','t',1,1,0,'carried')")
    s.init()
    _d4_owner(s)
    s.record_reflow_outcome("X", "gmail", "source_gone")
    st = s.reflow_stats()
    assert (st["chunks_carried"], st["chunks_uncovered"],
            st["chunks_inherited_unenriched"]) == (2, 1, 1)


def test_doctor_labels_uncovered_and_inherited_separately(tmp_path):
    s = _store(tmp_path)
    _d4_owner(s)
    line = reflow_line(s)
    assert "1 chunks carried" in line
    assert "1 new text to enrich" in line and "1 inherited unenriched" in line
    assert "re-enrich" not in line


def _reflow_chunks_text(r):
    import json
    import re
    import shutil
    import pytest
    if not shutil.which("node"):
        pytest.skip("node not installed")
    html = (Path(dashboard.__file__).parent / "wizard" / "dashboard.html").read_text()
    fn = re.search(r"function reflowChunksText\(r\)\{.*?\n\}\n", html, flags=re.S).group(0)
    js = f"const fmt = String;\n{fn}\nprocess.stdout.write(reflowChunksText({json.dumps(r)}));"
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return out.stdout


def test_dashboard_reflow_tooltip_splits_the_chunk_counts():
    html = (Path(dashboard.__file__).parent / "wizard" / "dashboard.html").read_text()
    assert "reflowChunksText(data.reflow)" in html
    assert _reflow_chunks_text({"chunks_carried": 5, "chunks_uncovered": 2,
                                "chunks_inherited_unenriched": 3}) == \
        "5 chunks carried · 2 new text to enrich · 3 inherited unenriched"
    assert _reflow_chunks_text(None) == ""


def _unequal_count(s, owner):
    with s._connect() as db:
        return db.execute("SELECT unequal_lineages FROM reflow_owners WHERE owner=?",
                          (owner,)).fetchone()[0]


def test_text_that_differed_on_a_carried_owner_is_persisted(tmp_path):
    """Text our own normaliser dropped inside a surviving chunk used to leave
    only the plan's in-memory `unequal` list: once apply_reflow returned there
    was no record an owner's old and new text differed. It is now a column,
    a stat and a doctor clause."""
    s = _store(tmp_path)
    out = _d4_owner(s)                          # new text appended: one lineage differs
    assert out["unequal_lineages"] == 1
    assert _unequal_count(s, "F") == 1
    st = s.reflow_stats()
    assert st["owners_text_differed"] == 1
    assert "1 owner(s) whose text differed" in reflow_line(s)


def test_identical_text_records_no_difference(tmp_path):
    from mcpbrain.reflow import plan
    from mcpbrain.sync.normalise import Chunk
    s = _store(tmp_path)
    _seed_owner(s, "F", n=1)
    old = s.owner_chunks(["gdrive-F-"])
    md = {"source_type": "gdrive", "file_id": "F", "chunk_index": 0}
    out = s.apply_reflow("F", "drive", plan(old, [Chunk("gdrive-F-0", "text F 0", "a", md,
                                                        ["text F 0"])]), [[0.1] * 4])
    assert out["unequal_lineages"] == 0 and _unequal_count(s, "F") == 0
    assert s.reflow_stats()["owners_text_differed"] == 0
    assert "whose text differed" not in reflow_line(s)


def test_unequal_column_migrates_an_existing_store(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("DROP TABLE reflow_owners")
        db.execute("CREATE TABLE reflow_owners(owner TEXT PRIMARY KEY, source TEXT NOT NULL,"
                   " at TEXT NOT NULL, chunks_new INTEGER NOT NULL, carried INTEGER NOT NULL,"
                   " reenrich INTEGER NOT NULL, outcome TEXT NOT NULL DEFAULT 'carried',"
                   " uncovered INTEGER NOT NULL DEFAULT 0,"
                   " inherited_unenriched INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO reflow_owners VALUES('G','drive','t',1,1,0,'carried',0,0)")
    s.init()
    assert _unequal_count(s, "G") == 0
    _d4_owner(s)
    assert s.reflow_stats()["owners_text_differed"] == 1
