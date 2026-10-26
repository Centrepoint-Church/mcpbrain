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
    assert "24 h" in ic.summary_line({"state": "deferred_no_backup", "last_backup": 1.0})
    assert "first backup" in ic.summary_line({"state": "deferred_no_backup"})
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
        s.set_meta(ic.MARKER_KEY, json.dumps({"state": state, "error": "e",
                                              "last_backup": 1.0}))
        line = doctor.ingestion_cleanup_line(s)
        assert line.startswith(icon), (state, line)


def test_daemon_pass_is_registered_gated_and_bulk_locked():
    from mcpbrain import daemon as d
    cp = next(c for c in d._CADENCE_PASSES if c.name == "ingestion_cleanup")
    assert cp.needs_configured and not cp.needs_bulk_lock   # takes the lock itself
    assert "ingestion_cleanup_interval_s" in d._CADENCE_KEYS
    assert d._CADENCE_DEFAULTS["ingestion_cleanup_interval_s"] == 3600.0


def _bare_daemon(env):
    import threading
    from mcpbrain import daemon as d
    dm = d.Daemon.__new__(d.Daemon)
    dm._store = env["store"]
    dm._clock = lambda: 1000.0
    dm._ingestion_cleanup_interval_s = 3600.0
    dm._last_ingestion_cleanup = None
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_wait_s = 1.0
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._embedder_factory = object()
    dm._embedder_bounded = lambda timeout=None: "EMB"
    return dm


def test_daemon_pass_runs_migration_once(env, monkeypatch):
    """_run_ingestion_cleanup calls run_migration with the daemon's store and
    embedder; once the marker is done it does not call it again."""
    calls = []

    def fake(store, home, *, embedder_factory=None, **k):
        calls.append(embedder_factory())
        rec = {"state": "done", "by": "daemon", "steps": list(ic.STEPS)}
        store.set_meta(ic.MARKER_KEY, json.dumps(rec))
        return rec

    monkeypatch.setattr(ic, "run_migration", fake)
    dm = _bare_daemon(env)
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert calls == ["EMB"]
    dm._last_ingestion_cleanup = None
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert len(calls) == 1
    # A disabled cadence (interval None) never runs.
    dm._ingestion_cleanup_interval_s = None
    dm._last_ingestion_cleanup = None
    assert dm._run_ingestion_cleanup() is None


def test_daemon_pass_skips_while_model_loads_and_lexical_on_failure(env, monkeypatch):
    seen = []
    monkeypatch.setattr(ic, "run_migration",
                        lambda store, home, *, embedder_factory=None, **k:
                        seen.append(embedder_factory()) or {"state": "done"})
    dm = _bare_daemon(env)
    dm._embedder_bounded = lambda timeout=None: None          # still building
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "embedder_loading"}
    assert seen == [] and dm._last_ingestion_cleanup is None  # retried next tick

    def boom(timeout=None):
        raise RuntimeError("no model")

    dm._embedder_bounded = boom
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert seen == [ic.LEXICAL]


# ---------------------------------------------------------------------------
# Residuals (ruling I17)
# ---------------------------------------------------------------------------

def _strip_new_schema(path):
    """Make an initialised store look like one the new code never opened."""
    import sqlite3
    db = sqlite3.connect(str(path))
    for t in ("cleanup_log", "relation_sources", "observation_sources"):
        db.execute(f"DROP TABLE IF EXISTS {t}")
    db.execute("DROP INDEX IF EXISTS idx_chunks_dup_of")
    db.execute("ALTER TABLE chunks DROP COLUMN dup_of")
    db.commit()
    db.close()


def test_n1_runs_init_on_a_never_initialised_store(env):
    """N1: a store swapped in by the wizard restore was never re-initialised;
    the migration initialises it instead of recording a terminal failure."""
    from mcpbrain.store import Store
    _strip_new_schema(env["store"].path)
    fresh = Store(env["store"].path, dim=4)          # no init()
    rec = ic.run_migration(fresh, str(env["home"]),
                           embedder_factory=FakeEmbedder, last_backup=FRESH,
                           clock=lambda: NOW)
    assert rec["state"] == "done", rec.get("error")
    assert _act(env, "session")["status"] == "auto_archived"


def test_n2_gate_reads_restore_point_capture_time(env, monkeypatch):
    """N2: an old capture committed just now is NOT a fresh restore point."""
    import mcpbrain.probes as probes
    (env["home"] / "backup_state.json").write_text(json.dumps(
        {"ok": True, "last_success": time.time(), "restore_point": time.time() - 3 * 86400}))
    rec = ic.run_migration(env["store"], str(env["home"]), embedder_factory=FakeEmbedder)
    assert rec["state"] == "deferred_no_backup"
    (env["home"] / "backup_state.json").write_text(json.dumps(
        {"ok": True, "last_success": time.time(), "restore_point": time.time() - 600}))
    assert probes.last_restore_point(str(env["home"])) is not None
    rec = ic.run_migration(env["store"], str(env["home"]), embedder_factory=FakeEmbedder)
    assert rec["state"] == "done"


def test_n2_restore_point_falls_back_to_last_success(env):
    import mcpbrain.probes as probes
    (env["home"] / "backup_state.json").write_text(json.dumps(
        {"ok": True, "last_success": 1234.0}))
    assert probes.last_restore_point(str(env["home"])) == 1234.0


def test_n3_embedder_failure_falls_back_to_lexical(env):
    """N3: an embedder that cannot be built is not a permanent failure: the
    dedupe runs on the lexical rules, and says so in its counts."""
    def broken():
        raise RuntimeError("model download failed")

    rec = ic.run_migration(env["store"], str(env["home"]), embedder_factory=broken,
                           last_backup=FRESH, clock=lambda: NOW)
    assert rec["state"] == "done"
    assert rec["counts"]["dedup_embed"].get("dedup:lexical_fallback") == 1
    # The lexical rules still collapse the near-identical note actions.
    assert rec["counts"]["dedup_embed"].get("actions:duplicate", 0) >= 1


def test_n3_lexical_marker_object(env):
    rec = ic.run_migration(env["store"], str(env["home"]),
                           embedder_factory=lambda: ic.LEXICAL,
                           last_backup=FRESH, clock=lambda: NOW)
    assert rec["state"] == "done"
    assert rec["counts"]["dedup_embed"].get("dedup:lexical_fallback") == 1


def test_n4_cli_claims_running_before_it_runs(env, monkeypatch):
    seen = {}
    real_run = ic.run

    def spy(store, home, steps, **kw):
        seen["marker"] = ic.read_marker(store)
        # A daemon relaunched mid-run stands off.
        seen["daemon"] = ic.run_migration(store, home, embedder_factory=FakeEmbedder,
                                          last_backup=time.time())
        return real_run(store, home, steps, **kw)

    monkeypatch.setattr(ic, "run", spy)
    assert _main(env, "--yes", "--skip-snapshot-for-tests") == 0
    assert seen["marker"]["state"] == "running" and seen["marker"]["by"] == "cli"
    assert seen["daemon"]["state"] == "running" and seen["daemon"]["by"] == "cli"
    assert ic.status(env["store"])["state"] == "done"


def test_n4_stale_cli_claim_is_visible_and_not_blocking(env):
    """A crashed CLI run leaves `running` by cli; once it is older than
    CLI_CLAIM_TTL_S the daemon treats it as interrupted and runs, and the
    status line says so meanwhile."""
    old = {"state": "running", "by": "cli", "attempts": 0,
           "at": "2000-01-01T00:00:00+00:00"}
    env["store"].set_meta(ic.MARKER_KEY, json.dumps(old))
    st = ic.status(env["store"])
    assert "interrupted" in ic.summary_line(st)
    from mcpbrain import doctor
    assert doctor.ingestion_cleanup_line(env["store"]).startswith("⚠️")
    env["store"].set_meta(ic.MARKER_KEY, json.dumps(old))     # status() does not write
    assert _mig(env)["state"] == "done"


def test_n4_partial_steps_never_mark_done(env):
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--steps", "owner_aliases") == 0
    st = ic.status(env["store"])
    assert st["state"] == "pending"
    assert st["last_cli"]["steps"] == ["owner_aliases"]
    assert _mig(env)["state"] == "done"                # the daemon still runs the rest
    assert _act(env, "session")["status"] == "auto_archived"


def test_n6_no_warning_before_first_backup(env):
    from mcpbrain import doctor
    rec = ic.run_migration(env["store"], str(env["home"]), embedder_factory=FakeEmbedder,
                           clock=lambda: NOW)                # no backup state at all
    assert rec["state"] == "deferred_no_backup"
    line = doctor.ingestion_cleanup_line(env["store"])
    assert line.startswith("⏳") and "first backup" in line
    _mig(env, last_backup=STALE)                             # had one, now stale
    assert doctor.ingestion_cleanup_line(env["store"]).startswith("⚠️")


def test_n3_daemon_loads_embedder_before_taking_bulk_lock(env, monkeypatch):
    """The pass builds the embedder (bounded) BEFORE it takes _bulk_lock, and
    holds the lock only around run_migration."""
    order = []
    dm = _bare_daemon(env)

    def fake(store, home, *, embedder_factory=None, **k):
        order.append(("run", dm._bulk_lock.locked(), embedder_factory()))
        return {"state": "done", "by": "daemon"}

    def bounded(timeout=None):
        order.append(("embedder", dm._bulk_lock.locked()))
        return "EMB"

    monkeypatch.setattr(ic, "run_migration", fake)
    dm._embedder_bounded = bounded
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert order == [("embedder", False), ("run", True, "EMB")]
    assert not dm._bulk_lock.locked()


# ---------------------------------------------------------------------------
# Steps added after an install's clean-up was done (Task 8)
# ---------------------------------------------------------------------------

BASE_STEPS = [s for s in ic.STEPS if s not in ic.ADDED_STEPS]


def _old_done(env, **extra):
    rec = {"state": "done", "by": "daemon", "steps": BASE_STEPS, "skipped": [],
           "counts": {"source_session": {"actions:session": 1}},
           "at": "2026-10-23T01:54:00+00:00", **extra}
    env["store"].set_meta(ic.MARKER_KEY, json.dumps(rec))
    return rec


def test_base_steps_are_the_thirteen_that_shipped():
    assert len(BASE_STEPS) == 13 and len(ic.STEPS) == 15
    assert ic.STEPS.index("recount_mentions") == ic.STEPS.index("sources_backfill") + 1
    assert ic.OWNER_REPAIR_STEP not in ic.STEPS + ic.ADDED_STEPS


def test_fresh_install_runs_all_steps_once(env):
    rec = _mig(env)
    assert rec["state"] == "done" and rec["steps"] == list(ic.STEPS)
    assert "pending_steps" not in rec and ic.pending_added_steps(rec) == []


def test_done_install_runs_only_the_added_steps_once(env, monkeypatch):
    _old_done(env)
    ran = []
    real_run = ic.run
    monkeypatch.setattr(ic, "run", lambda store, home, steps, **kw:
                        ran.append(list(steps)) or real_run(store, home, steps, **kw))
    rec = _mig(env)
    assert ran == [list(ic.ADDED_STEPS)]
    assert rec["state"] == "done" and rec["by"] == "daemon"
    assert rec["steps"] == list(ic.STEPS) and "pending_steps" not in rec
    assert rec["counts"]["source_session"] == {"actions:session": 1}    # merged
    assert rec["counts"]["recount_mentions"]["entities:mentions"] >= 1
    assert rec["counts"]["derived_salience"]["chunks_salience:brief"] == 1
    assert _act(env, "session")["status"] == "open"     # the old steps did not rerun
    once = _dump(env["store"].path)
    rec2 = _mig(env)                                      # second call: no-op
    assert ran == [list(ic.ADDED_STEPS)] and rec2["at"] == rec["at"]
    assert _dump(env["store"].path) == once


def test_done_install_waits_for_backup_and_stays_done(env):
    _old_done(env)
    before = _dump(env["store"].path)
    rec = _mig(env, last_backup=STALE)
    assert rec["state"] == "done" and rec["steps"] == BASE_STEPS
    assert rec["pending_steps"] == list(ic.ADDED_STEPS)
    assert rec["pending_reason"] == "no_backup"
    assert "waiting for a restore point" in ic.summary_line(rec)
    assert _dump(env["store"].path) == before
    rec = _mig(env)                                       # retried next call
    assert rec["state"] == "done" and rec["steps"] == list(ic.STEPS)
    assert "pending_steps" not in rec and "pending_reason" not in rec


def test_done_install_honours_kill_switch(env):
    _old_done(env)
    cfg = json.loads((env["home"] / "config.json").read_text())
    cfg["ingestion_cleanup_enabled"] = False
    (env["home"] / "config.json").write_text(json.dumps(cfg))
    before = _dump(env["store"].path)
    rec = _mig(env)
    assert (rec["state"], rec["pending_reason"]) == ("done", "disabled")
    assert _dump(env["store"].path) == before


def test_done_install_skipped_brief_is_not_rerun(env, monkeypatch):
    _old_done(env, steps=[s for s in BASE_STEPS if s != "source_brief"],
              skipped=["source_brief"])
    ran = []
    monkeypatch.setattr(ic, "run", lambda store, home, steps, **kw:
                        ran.append(list(steps)) or {})
    _mig(env)
    assert ran == [list(ic.ADDED_STEPS)]


def test_cli_undo_marker_runs_nothing(env, monkeypatch):
    """The done an attended --undo writes on a store the clean-up never ran on
    keeps the daemon out entirely, added steps included."""
    _old_done(env, steps=["owner_aliases"], undo=True)
    monkeypatch.setattr(ic, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert ic.pending_added_steps(ic.read_marker(env["store"])) == []
    assert _mig(env)["state"] == "done"


def test_cli_undo_after_a_full_run_keeps_added_steps_due(env):
    """An --undo after the clean-up ran keeps the prior marker (steps intact):
    added steps are still due."""
    _old_done(env)
    assert _main(env, "--yes", "--skip-snapshot-for-tests", "--undo", "owner_aliases") == 0
    rec = ic.read_marker(env["store"])
    assert rec["state"] == "done" and ic.pending_added_steps(rec) == list(ic.ADDED_STEPS)


def test_failed_stays_failed_with_added_steps(env, monkeypatch):
    env["store"].set_meta(ic.MARKER_KEY, json.dumps(
        {"state": "failed", "steps": BASE_STEPS, "error": "x"}))
    monkeypatch.setattr(ic, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert _mig(env)["state"] == "failed"


def test_added_step_error_records_failed_once(env, monkeypatch):
    _old_done(env)
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("disk full")

    monkeypatch.setattr(ic, "run", boom)
    rec = _mig(env)
    assert rec["state"] == "failed" and "disk full" in rec["error"]
    assert _mig(env)["state"] == "failed" and len(calls) == 1


def test_added_steps_interrupted_give_up(env, monkeypatch):
    _old_done(env, pending_steps=list(ic.ADDED_STEPS), pending_reason="running",
              pending_attempts=ic.MAX_ATTEMPTS)
    monkeypatch.setattr(ic, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    rec = _mig(env)
    assert rec["state"] == "failed" and "interrupted" in rec["error"]


def test_daemon_pass_runs_added_steps_without_a_model(env, monkeypatch):
    _old_done(env)
    seen = []
    monkeypatch.setattr(ic, "run_migration",
                        lambda store, home, *, embedder_factory=None, **k:
                        seen.append(embedder_factory()) or {"state": "done"})
    dm = _bare_daemon(env)
    dm._embedder_bounded = lambda timeout=None: (_ for _ in ()).throw(AssertionError)
    assert dm._run_ingestion_cleanup() == {"ingestion_cleanup": "done"}
    assert seen == [ic.LEXICAL]
