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
_REAL_CHECK_INSTALLED = drain._check_installed

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
    # The repo's own tests import the working tree; the real guard refuses that.
    monkeypatch.setattr(drain, "_check_installed", lambda: (None, "0.0-test"))
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




# -- fix round 1 ---------------------------------------------------------------

def test_refuses_to_run_working_tree_code(home, monkeypatch, capsys):
    """The drain must run the INSTALLED package (the daemon's exact code and
    dependencies); under pytest mcpbrain is the working tree, so the real
    guard refuses."""
    _live()
    _fakes(monkeypatch)
    monkeypatch.setattr(drain, "_check_installed", _REAL_CHECK_INSTALLED)
    before = config.store_path().read_bytes()
    assert drain.main(["--yes"]) == 2
    assert drain.main(["--check"]) == 2
    assert "working tree" in capsys.readouterr().err
    assert config.store_path().read_bytes() == before


def test_check_mode_runs_the_gates_writes_nothing_and_skips_daemon_detection(
        home, monkeypatch):
    s = _live()
    _fakes(monkeypatch)
    monkeypatch.setattr(drain, "_daemon_alive", lambda: "pid 1 (mcpbrain daemon)")
    before = config.store_path().read_bytes()
    assert drain.main(["--check"]) == 0
    s.set_cursor(REFLOW_HALT_CURSOR, "x")
    assert drain.main(["--check"]) == 2
    s.set_cursor(REFLOW_HALT_CURSOR, "")
    (home / "backup_state.json").write_text(json.dumps({"last_success": time.time() - 90000}))
    assert drain.main(["--check"]) == 2
    assert drain.main(["--check", "--no-backup-check"]) == 0
    assert _reflow_rows(s) == []
    s2 = Store(config.store_path(), dim=4)
    assert s2.reflow_candidates(10)          # nothing drained
    del before


def _marker(home):
    return home / "reflow_drain.STORE_CHECK_FAILED"


def test_integrity_failure_exits_5_and_writes_the_marker(home, monkeypatch):
    _live()
    _fakes(monkeypatch)
    monkeypatch.setattr(drain, "_checks", lambda store, home: {
        "integrity_check": ["row 3 missing from index"], "foreign_key_check": 0,
        "rebuilt_store": True})
    assert drain.main(["--yes"]) == 5
    assert "row 3 missing from index" in _marker(home).read_text()


def test_a_malformed_store_during_the_drain_is_a_store_check_failure(home, monkeypatch):
    import sqlite3
    _live()
    _fakes(monkeypatch)

    def boom(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(drain, "drain", boom)
    assert drain.main(["--yes"]) == 5
    assert "malformed" in _marker(home).read_text()


def test_a_malformed_store_in_the_checks_is_a_store_check_failure(home, monkeypatch):
    import sqlite3
    _live()
    _fakes(monkeypatch)

    def boom(store, home):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(drain, "_checks", boom)
    assert drain.main(["--yes"]) == 5
    assert _marker(home).exists()


def test_an_existing_marker_refuses_every_mode(home, monkeypatch, capsys):
    s = _live()
    _fakes(monkeypatch)
    _marker(home).write_text("integrity_check: bad\n")
    assert drain.main(["--yes"]) == 2
    assert drain.main(["--check"]) == 2
    assert "STORE_CHECK_FAILED" in capsys.readouterr().err
    assert _reflow_rows(s) == [] and s.reflow_candidates(10)


def test_signal_handlers_are_restored_before_the_store_checks(home, monkeypatch):
    import signal as _signal
    _live()
    _fakes(monkeypatch)
    before = _signal.getsignal(_signal.SIGINT)
    seen = {}

    def checks(store, home):
        seen["h"] = _signal.getsignal(_signal.SIGINT)
        return {"integrity_check": "ok", "foreign_key_check": 0, "rebuilt_store": False}

    monkeypatch.setattr(drain, "_checks", checks)
    assert drain.main(["--yes"]) == 0
    assert seen["h"] is before


def test_foreign_key_violations_on_a_rebuilt_store_exit_5(home, monkeypatch):
    _live()
    _fakes(monkeypatch)
    monkeypatch.setattr(drain, "_checks", lambda store, home: {
        "integrity_check": "ok", "foreign_key_check": 2, "rebuilt_store": True})
    assert drain.main(["--yes"]) == 5
    _marker(home).unlink()
    monkeypatch.setattr(drain, "_checks", lambda store, home: {
        "integrity_check": "ok", "foreign_key_check": 2, "rebuilt_store": False})
    assert drain.main(["--yes"]) == 0


def test_a_second_signal_within_the_debounce_window_is_ignored():
    """uv/tty deliver two SIGINTs per keypress: the second (within 1.5 s) must
    not abort the in-flight owner; a genuine second press later does."""
    t = {"now": 100.0}
    drain._STOP.reset()
    h = drain._make_signal_handler(clock=lambda: t["now"])
    h(2, None)
    assert drain._STOP.reason == "interrupt"
    t["now"] += 0.2
    h(2, None)                                # the duplicate: ignored
    t["now"] += 2.0
    with pytest.raises(KeyboardInterrupt):
        h(2, None)
    drain._STOP.reset()


def test_safe_stream_swallows_a_dead_terminal():
    class Dead:
        def write(self, s):
            raise OSError(5, "Input/output error")

        def flush(self):
            raise BrokenPipeError

    st = drain._SafeStream(Dead())
    st.write("x")
    st.flush()


# -- the wrapper, behaviourally (fake PATH) ---------------------------------------

_FAKE_PY = """#!/bin/sh
# fake interpreter: --check exits $FAKE_CHECK_RC; --yes exits $FAKE_RC,
# optionally signalling its parent (the wrapper) first.
case "$*" in *--check*) exit "${FAKE_CHECK_RC:-0}";; esac
echo DRAIN-RUNNING
[ -n "$FAKE_MARKER" ] && echo "integrity_check: bad" > "$MCPBRAIN_HOME/reflow_drain.STORE_CHECK_FAILED"
if [ -n "$FAKE_SIG" ]; then
  sleep 0.5
  kill -"$FAKE_SIG" "$PPID"
  sleep 0.3
fi
exit "${FAKE_RC:-0}"
"""


def _harness(tmp_path):
    b = tmp_path / "fakebin"
    b.mkdir()
    calls = tmp_path / "calls.log"

    def w(name, body):
        (b / name).write_text("#!/bin/sh\n" + body)
        (b / name).chmod(0o755)
    w("launchctl", f'echo "$*" >> "{calls}"\n[ "$1" = print ] && exit 113\nexit 0\n')
    w("pgrep", 'case "$*" in *daemon*) [ -n "$FAKE_DAEMON_PID" ] && '
               '{ echo "$FAKE_DAEMON_PID"; exit 0; };; esac\nexit 1\n')
    w("caffeinate", "exit 0\n")
    w("sqlite3", f'echo "sqlite3 $*" >> "{calls}"\n'
                 'for a; do last="$a"; done\n'
                 'f=$(printf "%s" "$last" | sed "s/^VACUUM INTO .//; s/.$//")\n'
                 'echo snap > "$f"\n')
    (b / "python").write_text(_FAKE_PY)
    (b / "python").chmod(0o755)
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    (app / "brain.sqlite3").write_text("x")
    hm = tmp_path / "userhome"
    (hm / "Library" / "LaunchAgents").mkdir(parents=True)
    (hm / "Library" / "LaunchAgents" / "com.mcpbrain.plist").write_text("<plist/>")
    import os
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "HOME": str(hm),
           "MCPBRAIN_HOME": str(app), "MCPBRAIN_PY": str(b / "python"),
           "REFLOW_DRAIN_DAEMON_WAIT_S": "2"}
    return env, calls, app


def _run_wrapper(tmp_path, dead_stdout=False, real_py=None, marker=False, **fake):
    import os
    import threading
    env, calls, app = _harness(tmp_path)
    if marker:
        (app / "reflow_drain.STORE_CHECK_FAILED").write_text("integrity_check: bad\n")
    if real_py is not None:
        env["MCPBRAIN_PY"] = str(_real_fake_python(tmp_path, app, real_py))
    env.update({k: str(v) for k, v in fake.items()})
    cmd = ["bash", str(_ROOT / "bin" / "reflow_drain.sh")]
    if not dead_stdout:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
        out, rc = p.stdout + p.stderr, p.returncode
    else:
        r, w = os.pipe()
        proc = subprocess.Popen(cmd, env=env, stdout=w, stderr=w)
        os.close(w)
        buf = b""

        def reader():
            nonlocal buf
            with os.fdopen(r, "rb", buffering=0) as f:
                while b"DRAIN-RUNNING" not in buf:
                    chunk = f.read(1)
                    if not chunk:
                        return
                    buf += chunk
            # leaving the with-block closes the read end: the terminal is gone
        t = threading.Thread(target=reader)
        t.start()
        t.join(30)
        rc = proc.wait(timeout=60)
        out = buf.decode()
    log = "".join(p.read_text() for p in (app / "logs").glob("reflow_drain*.log")) \
        if (app / "logs").exists() else ""
    lines = calls.read_text().splitlines() if calls.exists() else []
    return rc, out, log, lines, app


def _boots(lines, verb):
    return sum(1 for ln in lines if ln.startswith(verb))


def test_wrapper_script_parses():
    assert subprocess.run(["bash", "-n", str(_ROOT / "bin" / "reflow_drain.sh")]
                          ).returncode == 0


@pytest.mark.parametrize("rc", [0, 1, 3, 4, 130])
def test_wrapper_bootstraps_exactly_once_for_every_ordinary_exit(tmp_path, rc):
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_RC=rc)
    assert got == rc
    assert _boots(lines, "bootout") == 1 and _boots(lines, "bootstrap") == 1
    assert any("-readonly" in ln for ln in lines if ln.startswith("sqlite3"))


def test_wrapper_never_bootstraps_after_an_integrity_failure(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_RC=5)
    assert got == 5
    assert _boots(lines, "bootout") == 1 and _boots(lines, "bootstrap") == 0
    assert "launchctl bootstrap gui/" in out and "launchctl bootstrap gui/" in log


def test_wrapper_refusal_by_check_costs_nothing(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_CHECK_RC=2)
    assert got == 2
    assert _boots(lines, "bootout") == 0 and _boots(lines, "bootstrap") == 0
    assert not any(ln.startswith("sqlite3") for ln in lines)


@pytest.mark.parametrize("sig,rc", [("INT", 130), ("TERM", 0)])
def test_wrapper_signal_during_the_drain_still_bootstraps_once(tmp_path, sig, rc):
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_SIG=sig, FAKE_RC=rc)
    assert _boots(lines, "bootstrap") == 1


def test_wrapper_signal_does_not_mask_an_integrity_failure(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_SIG="INT", FAKE_RC=5)
    assert got == 5 and _boots(lines, "bootstrap") == 0


def test_wrapper_hup_with_a_dead_terminal_still_bootstraps_once(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, dead_stdout=True, FAKE_SIG="HUP",
                                        FAKE_RC=0)
    assert _boots(lines, "bootstrap") == 1
    assert "bootstrap" in log


# -- fix round 2: a REAL python drain behind the wrapper ------------------------

_REAL_FAKE = """#!{exe}
# The wrapper's "installed python": runs the REAL bin/reflow_drain.py CLI
# entry (main + its os._exit), with only the store checks / drain faked.
import importlib.util, json, sqlite3, sys, time
spec = importlib.util.spec_from_file_location("reflow_drain", {script!r})
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
d._check_installed = lambda: (None, "0.0-test")
d._daemon_alive = lambda: None
d._build_services = lambda: {{}}

class E:
    dim = 4
    def embed_passages(self, xs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]

d._get_embedder = lambda: E()
mode = {mode!r}
args = [a for a in sys.argv[1:] if a not in ("-I", {script!r})]
if "--yes" in args:
    print("DRAIN-RUNNING", flush=True)
    time.sleep(0.8)                      # the terminal goes away here
    if mode == "integrity":
        d._checks = lambda store, home: {{"integrity_check": ["bad index"],
                                          "foreign_key_check": 0, "rebuilt_store": True}}
    elif mode == "malformed":
        def boom(*a, **k):
            raise sqlite3.DatabaseError("database disk image is malformed")
        d.drain = boom
    for _ in range(200):
        print("progress line that must not crash a dead terminal")
d._run_cli(args)
"""


def _real_fake_python(tmp_path, app, mode):
    import sys as _sys
    (app / "brain.sqlite3").unlink()
    s = Store(app / "brain.sqlite3", dim=4)
    s.init()
    (app / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    p = tmp_path / "fakebin" / "realpy"
    p.write_text(_REAL_FAKE.format(exe=_sys.executable,
                                   script=str(_ROOT / "bin" / "reflow_drain.py"), mode=mode))
    p.chmod(0o755)
    return p


@pytest.mark.parametrize("mode", ["integrity", "malformed"])
def test_wrapper_store_check_failure_with_a_dead_terminal_never_bootstraps(tmp_path, mode):
    got, out, log, lines, app = _run_wrapper(tmp_path, dead_stdout=True, real_py=mode)
    assert (app / "reflow_drain.STORE_CHECK_FAILED").exists()
    assert _boots(lines, "bootout") == 1 and _boots(lines, "bootstrap") == 0
    assert "launchctl bootstrap gui/" in log


def test_wrapper_real_python_clean_run_bootstraps_once(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, real_py="ok")
    assert got == 0, out
    assert _boots(lines, "bootstrap") == 1
    assert not (app / "reflow_drain.STORE_CHECK_FAILED").exists()


def test_wrapper_refuses_to_start_when_a_marker_exists(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, marker=True)
    assert got == 2
    assert _boots(lines, "bootout") == 0 and _boots(lines, "bootstrap") == 0
    assert "STORE_CHECK_FAILED" in out


def test_wrapper_never_starts_a_second_daemon_beside_a_survivor(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_DAEMON_PID="4242")
    assert got != 0
    assert _boots(lines, "bootout") == 1 and _boots(lines, "bootstrap") == 0
    assert "4242" in out and "launchctl bootstrap gui/" in out


def test_wrapper_log_is_not_garbled_by_a_dead_terminal(tmp_path):
    got, out, log, lines, app = _run_wrapper(tmp_path, dead_stdout=True, FAKE_SIG="HUP",
                                             FAKE_RC=0)
    for ln in log.splitlines():
        assert ln[:4].isdigit(), f"garbled log line: {ln!r}"


def test_wrapper_trusts_the_marker_over_a_disturbed_exit_code(tmp_path):
    """The dead-terminal shape: the store check failed (marker written) but the
    status the wrapper sees is not 5 -- still no bootstrap."""
    got, out, log, lines, app = _run_wrapper(tmp_path, FAKE_MARKER=1, FAKE_RC=120)
    assert _boots(lines, "bootout") == 1 and _boots(lines, "bootstrap") == 0
    assert "rm " in out and "STORE_CHECK_FAILED" in out
