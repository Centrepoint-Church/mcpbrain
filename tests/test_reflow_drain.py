"""bin/reflow_drain.py: the attended, daemon-stopped reflow drain on the LIVE
store. A real Store in tmp_path (MCPBRAIN_HOME), fake Google services and
embedder injected through the script's seams, and the daemon detector
monkeypatched -- the tests never look at the real launchd or process table."""
import importlib.util
import json
import subprocess
import time
from pathlib import Path

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mcpbrain import config
from mcpbrain.org_contracts import DRIVE_ID_META_KEY
from mcpbrain.store import REFLOW_HALT_CURSOR, ReflowOrphanError, Store

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("reflow_drain", _ROOT / "bin" / "reflow_drain.py")
drain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drain)

PDF = "application/pdf"
M = "2026-01-01T00:00:00Z"


class _Emb:
    dim = 4

    def embed_passages(self, xs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]


class _DriveSvc:
    """files().get() returns an unchanged PDF; ids in `gone` 404."""

    def __init__(self, gone=()):
        self.gone = set(gone)

    def files(self):
        return self

    def get(self, **kw):
        fid, gone = kw["fileId"], self.gone

        class R:
            def execute(self, num_retries=0):
                if fid in gone:
                    raise HttpError(httplib2.Response({"status": 404}), b"gone")
                return {"id": fid, "name": "r.pdf", "mimeType": PDF,
                        "modifiedTime": M, "parents": []}
        return R()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(h))
    (h / "backup_state.json").write_text(json.dumps({"last_success": time.time() - 60}))
    monkeypatch.setattr(drain, "_daemon_alive", lambda: None)
    return h


def _owner(s, fid, drive_id=None):
    for i, t in enumerate(("Budget Line one", "Line two")):
        md = {"source_type": "gdrive", "file_id": fid, "mime_type": PDF,
              "modified": M, "chunk_index": i, "chunk_total": 2}
        if drive_id:
            md[DRIVE_ID_META_KEY] = drive_id
        s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"{fid}h{i}", md)


def _live(owners=(("F", "D1"),)):
    s = Store(config.store_path(), dim=4)
    s.init()
    for fid, did in owners:
        _owner(s, fid, did)
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1")
    return s


def _fakes(monkeypatch, svc=None):
    from mcpbrain.sync import drive
    from mcpbrain.sync.blocks import Heading, Paragraph
    monkeypatch.setattr(drain, "_build_services",
                        lambda: {"drive_service": svc or _DriveSvc()})
    monkeypatch.setattr(drain, "_get_embedder", lambda: _Emb())
    monkeypatch.setattr(drive, "fetch_content", lambda svc, fm, **k: drive.Content(
        text="Budget\n\nLine one\nLine two",
        blocks=[Heading(1, "Budget"), Paragraph("Line one\nLine two")]))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")


def _reflow_rows(s):
    with s._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT source, ref_id, attempts, last_error FROM sync_queue "
            "WHERE source LIKE 'reflow:%'")]


# -- gates ---------------------------------------------------------------------

def test_plan_only_without_yes_writes_nothing(home, monkeypatch):
    s = _live()
    _fakes(monkeypatch)
    path = config.store_path()
    before = path.read_bytes()
    assert drain.main([]) == 0
    assert path.read_bytes() == before
    assert _reflow_rows(s) == []


def test_refuses_when_a_daemon_is_detected(home, monkeypatch, capsys):
    s = _live()
    _fakes(monkeypatch)
    monkeypatch.setattr(drain, "_daemon_alive", lambda: "pid 4242 (mcpbrain daemon)")
    before = config.store_path().read_bytes()
    assert drain.main(["--yes"]) == 2
    assert "daemon" in capsys.readouterr().err
    assert config.store_path().read_bytes() == before
    assert _reflow_rows(s) == []


def test_refuses_while_another_process_holds_the_single_writer_lock(home, monkeypatch):
    from mcpbrain.daemon import SingleWriterLock
    _live()
    _fakes(monkeypatch)
    lock = SingleWriterLock()
    lock.acquire()
    try:
        assert drain.main(["--yes"]) == 2
    finally:
        lock.release()


def test_refuses_when_halted(home, monkeypatch, capsys):
    s = _live()
    s.set_cursor(REFLOW_HALT_CURSOR, "dangling ref in recall_feedback")
    _fakes(monkeypatch)
    assert drain.main(["--yes"]) == 2
    err = capsys.readouterr().err
    assert "halted" in err and "bin/reflow.py resume" in err
    assert _reflow_rows(s) == []


def test_refuses_on_a_stale_backup_unless_overridden(home, monkeypatch, capsys):
    s = _live()
    (home / "backup_state.json").write_text(json.dumps({"last_success": time.time() - 90000}))
    _fakes(monkeypatch)
    assert drain.main(["--yes"]) == 2
    assert "backup" in capsys.readouterr().err
    assert _reflow_rows(s) == [] and s.reflow_candidates(10)
    assert drain.main(["--yes", "--no-backup-check"]) == 0
    assert "WARNING" in capsys.readouterr().out
    assert s.reflow_candidates(10) == []


# -- the drain -----------------------------------------------------------------

def test_drains_a_small_candidate_set_to_empty(home, monkeypatch, capsys):
    s = _live(owners=[("F0", None), ("F1", None), ("F2", "D1"), ("GONE", None)])
    _fakes(monkeypatch, svc=_DriveSvc(gone={"GONE"}))
    assert drain.main(["--yes"]) == 0
    out = capsys.readouterr().out
    assert s.reflow_candidates(50) == []
    assert _reflow_rows(s) == []
    st = s.reflow_stats()
    assert st["by_outcome"] == {"carried": 3, "source_gone": 1}
    assert "carried" in out and "integrity_check: ok" in out
    # A shared-drive owner that carried queues its republish for the daemon.
    assert [f for f, _ in s.pending_publishes("D1")] == ["F2"]


def test_records_pending_publish_for_a_shared_drive_owner(home, monkeypatch):
    s = _live(owners=[("F", "D1")])
    _fakes(monkeypatch)
    assert drain.main(["--yes"]) == 0
    assert [f for f, _ in s.pending_publishes("D1")] == ["F"]


def test_max_owners_and_source_filter(home, monkeypatch):
    s = _live(owners=[("F0", None), ("F1", None), ("F2", None)])
    _fakes(monkeypatch)
    assert drain.main(["--yes", "--max-owners", "1"]) == 0
    assert s.reflow_stats()["owners_done"] == 1
    assert drain.main(["--yes", "--source", "reflow:gmail"]) == 0
    assert s.reflow_stats()["owners_done"] == 1          # nothing of gmail's to do
    assert drain.main(["--yes", "--source", "reflow:drive"]) == 0
    assert s.reflow_stats()["owners_done"] == 3


def test_leaves_ordinary_sync_rows_untouched(home, monkeypatch):
    """work_queue backs off a row with no handler; the drain registers only
    `reflow`, so it must never hand work_queue a non-reflow row."""
    s = _live()
    s.enqueue_items([{"ref_id": "MSG", "event": "upsert",
                      "modified_at": "2026-09-01T00:00:00"}], source="gmail")
    _fakes(monkeypatch)
    assert drain.main(["--yes"]) == 0
    with s._connect() as db:
        row = dict(db.execute("SELECT attempts, next_attempt_at, last_error FROM sync_queue "
                              "WHERE source='gmail'").fetchone())
    assert row == {"attempts": 0, "next_attempt_at": None, "last_error": ""}


def test_stops_with_exit_3_on_an_orphan_halt(home, monkeypatch, capsys):
    s = _live(owners=[("F0", None), ("F1", None)])
    _fakes(monkeypatch)

    def boom(self, owner, *a, **k):
        self.set_cursor(REFLOW_HALT_CURSOR, f"orphan ref for {owner}")
        raise ReflowOrphanError(f"orphan ref for {owner}")

    monkeypatch.setattr(Store, "apply_reflow", boom)
    assert drain.main(["--yes"]) == 3
    out = capsys.readouterr()
    assert "bin/reflow.py resume" in out.err + out.out
    assert s.get_cursor(REFLOW_HALT_CURSOR)
    rows = _reflow_rows(s)
    # The halting row recorded its error exactly as the daemon's work_queue
    # would; nothing else was worked after it.
    assert sum(1 for r in rows if r["last_error"]) == 1
    assert s.reflow_stats()["owners_done"] == 0


def test_aborts_when_a_daemon_appears_mid_run(home, monkeypatch):
    s = _live(owners=[(f"F{i}", None) for i in range(4)])
    _fakes(monkeypatch)
    calls = {"n": 0}

    def alive():
        calls["n"] += 1
        return None if calls["n"] == 1 else "pid 99 (mcpbrain daemon)"

    monkeypatch.setattr(drain, "_daemon_alive", alive)
    monkeypatch.setattr(drain, "RECHECK_EVERY", 1)
    assert drain.main(["--yes"]) == 4
    assert s.reflow_stats()["owners_done"] < 4


def test_ctrl_c_finishes_the_current_owner_and_exits_130(home, monkeypatch):
    s = _live(owners=[(f"F{i}", None) for i in range(3)])
    _fakes(monkeypatch)
    real = Store.apply_reflow

    def apply_then_interrupt(self, *a, **k):
        st = real(self, *a, **k)
        drain._STOP.request("interrupt")      # what the SIGINT handler does
        return st

    monkeypatch.setattr(Store, "apply_reflow", apply_then_interrupt)
    assert drain.main(["--yes"]) == 130
    assert s.reflow_stats()["owners_done"] == 1
    # Safe to re-run: the rest drain on the next invocation.
    monkeypatch.setattr(Store, "apply_reflow", real)
    assert drain.main(["--yes"]) == 0
    assert s.reflow_stats()["owners_done"] == 3


def test_wrapper_script_parses():
    assert subprocess.run(["bash", "-n", str(_ROOT / "bin" / "reflow_drain.sh")]
                          ).returncode == 0
