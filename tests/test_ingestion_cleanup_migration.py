"""The daemon's one-shot ingestion clean-up (ruling I13): gates, the shared
marker with the attended CLI, failure handling, status/doctor visibility."""
import json
import time

from mcpbrain import ingestion_cleanup as ic
from tests import test_clean_ingestion as _base
from tests.test_clean_ingestion import FakeEmbedder, _act, _dump, _main

env = _base.env          # the clean-up's fixture store, re-exported for pytest

NOW = 1_800_000_000.0
FRESH = NOW - 3600
STALE = NOW - 2 * 86400


def _mig(env, *, last_backup=FRESH, emb=None):
    return ic.run_migration(env["store"], str(env["home"]),
                            embedder_factory=lambda: emb or FakeEmbedder(),
                            last_backup=last_backup, clock=lambda: NOW)


def test_runs_once_records_done_and_refuses_twice(env):
    path = env["store"].path
    rec = _mig(env)
    assert rec["state"] == "done" and rec["by"] == "daemon"
    assert rec["counts"]["source_session"]["actions:session"] == 1
    assert _act(env, "session")["status"] == "auto_archived"
    once = _dump(path)
    rec2 = _mig(env)                         # second call: no-op
    assert rec2["state"] == "done" and rec2["at"] == rec["at"]
    assert _dump(path) == once
    assert ic.status(env["store"])["state"] == "done"


def _norm(dump):
    """Mask the run's own timestamps (resolved_at, cleanup_log.at, ...)."""
    import re
    ts = re.compile(r"^\"?\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00\"?$")
    return {t: sorted(tuple("<ts>" if isinstance(v, str) and ts.match(v) else v
                            for v in r) for r in rows)
            for t, rows in dump.items()}


def test_matches_the_attended_full_run(env, tmp_path):
    """Daemon path and CLI path produce the same rows (same steps, same log)."""
    import shutil
    copy = tmp_path / "copy.sqlite3"
    shutil.copy2(env["store"].path, copy)
    assert _main(env, "--yes", "--skip-snapshot-for-tests", store=copy) == 0
    _mig(env)
    assert _norm(_dump(env["store"].path)) == _norm(_dump(copy))


def test_deferred_without_recent_backup_then_runs(env):
    path = env["store"].path
    before = _dump(path)
    # No backup recorded at all (the probe finds none in this home) ...
    rec = ic.run_migration(env["store"], str(env["home"]),
                           embedder_factory=FakeEmbedder, clock=lambda: NOW)
    assert rec["state"] == "deferred_no_backup"
    # ... and one older than 24 h: both defer, writing nothing.
    assert _mig(env, last_backup=STALE)["state"] == "deferred_no_backup"
    assert _dump(path) == before
    assert _mig(env)["state"] == "done"


def test_kill_switch_disables(env):
    cfg = json.loads((env["home"] / "config.json").read_text())
    cfg["ingestion_cleanup_enabled"] = False
    (env["home"] / "config.json").write_text(json.dumps(cfg))
    before = _dump(env["store"].path)
    assert _mig(env)["state"] == "disabled"
    assert _dump(env["store"].path) == before
    cfg["ingestion_cleanup_enabled"] = True
    (env["home"] / "config.json").write_text(json.dumps(cfg))
    assert _mig(env)["state"] == "done"


def test_no_brief_rules_skips_source_brief(env):
    cfg = json.loads((env["home"] / "config.json").read_text())
    cfg.pop("ingest")
    (env["home"] / "config.json").write_text(json.dumps(cfg))
    rec = _mig(env)
    assert rec["state"] == "done" and rec["skipped"] == ["source_brief"]
    assert "source_brief" not in rec["counts"]
    assert _act(env, "brief")["status"] == "open"
    assert _act(env, "session")["status"] == "auto_archived"


def test_failure_records_failed_and_never_retries(env, monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("disk full")

    monkeypatch.setattr(ic, "run", boom)
    rec = _mig(env)
    assert rec["state"] == "failed" and "disk full" in rec["error"]
    assert _mig(env)["state"] == "failed"
    assert len(calls) == 1


def test_integrity_failure_records_failed(env, monkeypatch):
    monkeypatch.setattr(ic, "_integrity", lambda store: "*** page 7 corrupt")
    rec = _mig(env)
    assert rec["state"] == "failed" and "integrity_check" in rec["error"]
    assert "counts" in rec
    monkeypatch.setattr(ic, "_integrity", lambda store: "ok")
    assert _mig(env)["state"] == "failed"            # stays failed


def test_interrupted_runs_give_up_after_max_attempts(env, monkeypatch):
    env["store"].set_meta(ic.MARKER_KEY, json.dumps(
        {"state": "running", "attempts": ic.MAX_ATTEMPTS}))
    rec = _mig(env)
    assert rec["state"] == "failed" and "interrupted" in rec["error"]
    env["store"].set_meta(ic.MARKER_KEY, json.dumps({"state": "running", "attempts": 1}))
    assert _mig(env)["state"] == "done"              # a retry within the budget runs


def test_cli_full_run_claims_marker_so_daemon_never_runs(env, monkeypatch):
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    assert ic.status(env["store"])["by"] == "cli"
    monkeypatch.setattr(ic, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert _mig(env)["state"] == "done"


def test_cli_undo_claims_marker_so_daemon_does_not_reapply(env):
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "owner_aliases") == 0
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", "owner_aliases") == 0
    before = _dump(env["store"].path)
    assert _mig(env)["state"] == "done"
    assert _dump(env["store"].path) == before


def test_cli_full_run_after_daemon_is_a_noop_unless_rerun(env, capsys):
    _mig(env)
    path = env["store"].path
    once = _dump(path)
    capsys.readouterr()
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    out = capsys.readouterr().out
    assert "already applied" in out and "done by daemon" in out
    assert _dump(path) == once
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--rerun") == 0
    assert _dump(path) == once                       # idempotent


def test_daemon_run_undoes_like_the_cli(env):
    path = env["store"].path
    before = _dump(path)
    _mig(env)
    for step in reversed(ic.STEPS):
        assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", step) == 0
    assert _dump(path) == before


def test_summary_line_states():
    assert "not yet run" in ic.summary_line({"state": "pending"})
    assert "24 h" in ic.summary_line({"state": "deferred_no_backup"})
    assert "FAILED" in ic.summary_line({"state": "failed", "error": "x"})
    assert "done by daemon" in ic.summary_line(
        {"state": "done", "by": "daemon", "at": "t", "counts": {"a": {"b": 2}}})


def test_clock_default_is_wall_time():
    assert abs(time.time() - ic.time.time()) < 5


def test_doctor_line_per_state(env):
    from mcpbrain import doctor
    s = env["store"]
    assert doctor.ingestion_cleanup_line(s).startswith("⏳")
    for state, icon in (("deferred_no_backup", "⚠️"), ("failed", "❌"),
                        ("disabled", "➖"), ("done", "✅")):
        s.set_meta(ic.MARKER_KEY, json.dumps({"state": state, "error": "e"}))
        line = doctor.ingestion_cleanup_line(s)
        assert line.startswith(icon), (state, line)


def test_daemon_pass_is_registered_gated_and_bulk_locked():
    from mcpbrain import daemon as d
    cp = next(c for c in d._CADENCE_PASSES if c.name == "ingestion_cleanup")
    assert cp.needs_bulk_lock and cp.needs_configured
    assert "ingestion_cleanup_interval_s" in d._CADENCE_KEYS
    assert d._CADENCE_DEFAULTS["ingestion_cleanup_interval_s"] == 3600.0


def test_daemon_pass_runs_migration_once(env, monkeypatch):
    """_run_ingestion_cleanup calls run_migration with the daemon's store and
    embedder; once the marker is done it does not call it again."""
    from mcpbrain import daemon as d
    calls = []

    def fake(store, home, *, embedder_factory=None, **k):
        calls.append(embedder_factory())
        store.set_meta(ic.MARKER_KEY, json.dumps({"state": "done", "by": "daemon"}))
        return {"state": "done", "by": "daemon"}

    monkeypatch.setattr(ic, "run_migration", fake)
    dm = d.Daemon.__new__(d.Daemon)
    dm._store = env["store"]
    dm._clock = lambda: 1000.0
    dm._ingestion_cleanup_interval_s = 3600.0
    dm._last_ingestion_cleanup = None
    dm._embedder_obj = "EMB"
    monkeypatch.setattr(d.Daemon, "_embedder", property(lambda self: self._embedder_obj))
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert calls == ["EMB"]
    dm._last_ingestion_cleanup = None
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert len(calls) == 1
    # A disabled cadence (interval None) never runs.
    dm._ingestion_cleanup_interval_s = None
    dm._last_ingestion_cleanup = None
    assert dm._run_ingestion_cleanup() is None
