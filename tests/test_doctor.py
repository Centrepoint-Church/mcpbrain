"""Tests for mcpbrain.doctor — injected probes + injected repairs, no OS side effects.

doctor reuses probes.all_connections and a repair layer. Every test injects a
fake `conns` dict (the probe output shape) and fake `repairs` callables, so no
real launchd/git/agent side effects occur. The disposition table lives in
doctor; these tests assert the behaviour it drives.
"""

from mcpbrain import doctor


def _conns(**states):
    """Build an all-ok probe dict, overriding individual keys.

    Shape mirrors probes.all_connections: name -> {state, detail, last_verified}.
    Pass e.g. claude="needs_action" to flip one probe.
    """
    base = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
            for k in ("google", "claude", "backup", "records", "enrichment")}
    for name, state in states.items():
        base[name] = {"state": state, "detail": state, "last_verified": None}
    return base


class _Recorder:
    """A repair callable that records it was called and returns a fixed result."""

    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def __call__(self, *a, **k):
        self.calls += 1
        if not self.ok:
            raise RuntimeError("repair blew up")


def test_all_ok_exit_zero_no_repairs():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=_conns(), repairs=repairs)
    assert code == 0
    assert all(r.calls == 0 for r in repairs.values())
    assert "mcpbrain doctor" in msg


def test_not_started_optional_renders_distinct_glyph_not_green_check():
    # backup not configured = deliberately optional, not a fault and not
    # "healthy". Must render with the ➖ glyph, NOT a green ✅, and not counted.
    conns = _conns(backup="not_started")
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns,
                                  repairs={"daemon": _Recorder(), "agent": _Recorder(),
                                           "records": _Recorder()})
    assert code == 0  # optional unconfigured features are not actionable
    assert "➖ Backup" in msg
    assert "optional — not configured" in msg
    assert "✅ Backup" not in msg  # never a green check for "not set up"


def test_daemon_down_repair_called_reprobe_fixed():
    daemon = _Recorder()
    repairs = {"daemon": daemon, "agent": _Recorder(), "records": _Recorder()}
    # First probe: claude needs_action (daemon down). After repair, reprobe ok.
    conns = _conns(claude="needs_action")
    reprobed = {"claude": {"state": "ok", "detail": "Connected", "last_verified": None}}

    def fake_reprobe(home, key, fallback):
        return reprobed.get(key, fallback)

    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns, repairs=repairs,
                                  reprobe=fake_reprobe,
                                  agent_installed=lambda h, p: True)
    assert daemon.calls == 1
    assert "fixed" in msg
    # daemon was the only problem and it fixed → exit 0 IF nothing else needs action.
    # scheduled-tasks line keys off enrichment (ok here) so it does not add need_action.
    assert code == 0


def test_agent_missing_install_called():
    agent = _Recorder()
    repairs = {"daemon": _Recorder(), "agent": agent, "records": _Recorder()}
    # claude needs_action AND the OS agent is reported missing → install, not restart.
    conns = _conns(claude="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: {"state": "ok", "detail": "ok",
                                                           "last_verified": None},
                                  agent_installed=lambda h, p: False)
    assert agent.calls == 1
    assert repairs["daemon"].calls == 0
    assert "fixed" in msg


def test_records_missing_bootstrap_called():
    records = _Recorder()
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": records}
    conns = _conns(records="not_started")
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: {"state": "ok", "detail": "Ready",
                                                           "last_verified": None})
    assert records.calls == 1
    assert "fixed" in msg
    assert code == 0


def test_google_expired_guided_no_repair_exit1():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    conns = _conns(google="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f)
    assert all(r.calls == 0 for r in repairs.values())
    assert "mcpbrain auth" in msg
    assert code == 1


def test_repair_failure_reported_exit1():
    repairs = {"daemon": _Recorder(ok=False), "agent": _Recorder(),
               "records": _Recorder()}
    conns = _conns(claude="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f,
                                  agent_installed=lambda h, p: True)  # → daemon repair
    assert "repair failed" in msg
    assert code == 1


def test_scheduled_tasks_inferred_from_enrichment():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    # enrichment needs_action → scheduled-tasks line is guided, but NOT double-counted.
    conns = _conns(enrichment="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f)
    assert "Scheduled tasks" in msg
    assert "/mcpbrain:install" in msg
    assert all(r.calls == 0 for r in repairs.values())
    # enrichment is guided (1 need_action) + scheduled-tasks is NOT double-counted
    # so code == 1 (one issue: enrichment itself)
    assert code == 1


def test_output_is_never_empty():
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True, conns=_conns(), repairs={})
    assert msg.strip(), "doctor must always print a report, even on the all-ok path"
    assert "mcpbrain doctor" in msg


def test_doctor_reports_over_window_chunks(tmp_path, monkeypatch):
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "y" * 3000, "h1", {"source_type": "gdrive"})

    assert store.count_chunks_longer_than(2000) == 1


def test_run_doctor_reports_the_oversize_chunk_line(tmp_path, monkeypatch):
    """When oversize chunks exist, the chunk window line should report them with
    the ⚠️ glyph.

    The store file MUST be named brain.sqlite3 — the real one (config.store_path).
    This test used to seed 'b.sqlite3' and still passed, because run_doctor opened
    that same wrong name; the OperationalError from the missing real file was
    swallowed into '➖ skipped', so this line never printed on a real install."""
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "y" * 3000, "h1", {"source_type": "gdrive"})

    code, msg = doctor.run_doctor(str(tmp_path), model_present=lambda h: True,
                                  conns=_conns(), repairs={})
    assert "⚠️ Chunk window" in msg
    assert "1 chunk(s)" in msg


def test_run_doctor_chunk_window_skip_on_no_store(tmp_path):
    """When brain.sqlite3 doesn't exist (fresh install), the chunk window line
    should report ➖ skipped, not vanish entirely."""
    code, msg = doctor.run_doctor(str(tmp_path), model_present=lambda h: True,
                                  conns=_conns(), repairs={})
    assert "➖ Chunk window" in msg
    assert "skipped" in msg


def test_embedder_weights_present_reports_ok():
    # Weights cached → silent green line, no repair, no need_action.
    embedder = _Recorder()
    repairs = {"daemon": _Recorder(), "agent": _Recorder(),
               "records": _Recorder(), "embedder": embedder}
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True,
                                  conns=_conns(), repairs=repairs)
    assert "✅ Embedder" in msg and "model weights cached" in msg
    assert embedder.calls == 0
    assert code == 0


def test_embedder_weights_missing_auto_repair_heals():
    # First check says missing → warm/download → second check says present → fixed.
    seen = {"n": 0}

    def present(_home):
        seen["n"] += 1
        return seen["n"] > 1  # missing on first call, present after the repair

    embedder = _Recorder()
    repairs = {"daemon": _Recorder(), "agent": _Recorder(),
               "records": _Recorder(), "embedder": embedder}
    code, msg = doctor.run_doctor("/tmp/home", model_present=present,
                                  conns=_conns(), repairs=repairs)
    assert embedder.calls == 1
    assert "Embedder" in msg and "downloading... ✅ fixed" in msg
    assert code == 0  # the only problem was the embedder, and it healed


def test_embedder_repair_failure_needs_action():
    # Warm raises (e.g. offline) → reported as needing action, exit 1.
    embedder = _Recorder(ok=False)
    repairs = {"daemon": _Recorder(), "agent": _Recorder(),
               "records": _Recorder(), "embedder": embedder}
    code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: False,
                                  conns=_conns(), repairs=repairs)
    assert embedder.calls == 1
    assert "re-download failed" in msg and "needs network" in msg
    assert code == 1


def test_cli_dispatches_doctor(monkeypatch):
    import mcpbrain.cli as cli
    called = {}
    monkeypatch.setattr("mcpbrain.doctor.run_doctor_main",
                        lambda rest: called.setdefault("rest", rest))
    cli.main(["doctor", "--whatever"])
    assert "rest" in called
    assert called["rest"] == ["--whatever"]


def test_default_repairs_dispatch_to_real_agents_and_records(monkeypatch, tmp_path):
    """The REAL repair closures must call agents.*/records.* with the exact
    argument shapes those functions expect. Every other test injects fakes, so
    without this a signature drift in install_agent/restart_agent/
    ensure_records_repo would ship a no-op doctor with a green suite.
    """
    from mcpbrain import agents, config, records

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    config.write_config(str(tmp_path), {
        "owner_full_name": "Sam Admin", "owner_email": "sam@acme.org",
        "records_dir": str(tmp_path / "records"),
    })
    calls = {}
    monkeypatch.setattr(agents, "restart_agent",
                        lambda platform: calls.setdefault("restart", {"platform": platform}))
    monkeypatch.setattr(agents, "install_agent",
                        lambda platform, *, mcpbrain_bin, home: calls.setdefault(
                            "install", {"platform": platform, "bin": mcpbrain_bin, "home": home}))
    monkeypatch.setattr(records, "ensure_records_repo",
                        lambda repo_dir, **kw: calls.setdefault(
                            "records", {"repo_dir": repo_dir, **kw}) or repo_dir)

    repairs = doctor._default_repairs(str(tmp_path), "darwin", "/usr/local/bin/mcpbrain")
    assert set(repairs) == {"daemon", "agent", "records", "embedder", "baseline",
                            "ocr", "connector"}

    # Invoking each closure must dispatch to the real function with valid kwargs
    # (a TypeError here is exactly the production-only failure we're guarding).
    repairs["daemon"]()
    repairs["agent"]()
    repairs["records"]()

    assert calls["restart"] == {"platform": "darwin"}
    assert calls["install"] == {"platform": "darwin",
                                "bin": "/usr/local/bin/mcpbrain",
                                "home": str(tmp_path)}
    assert calls["records"]["repo_dir"] == str(tmp_path / "records")
    assert calls["records"]["git_name"] == "Sam Admin"
    assert calls["records"]["git_email"] == "sam@acme.org"


# ---------------------------------------------------------------------------
# watchdog restart limiter (surfaced from /api/status)
# ---------------------------------------------------------------------------

def _ok_repairs():
    return {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}


def test_watchdog_limit_reached_is_actionable():
    """"Stops self-restarting and stays visibly stuck" is only visible if
    something says so — doctor is where a user looks."""
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(),
        daemon_status={"watchdog_exits": 3, "watchdog_limit_reached": True})
    assert "❌ Watchdog" in msg
    assert "restart limit reached" in msg
    assert code == 1


def test_watchdog_recovered_restarts_are_reported_but_not_actionable():
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(),
        daemon_status={"watchdog_exits": 1, "watchdog_limit_reached": False})
    assert "✅ Watchdog" in msg
    assert "1 self-restart(s)" in msg
    assert code == 0


def test_watchdog_clean_reports_no_restarts():
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(),
        daemon_status={"watchdog_exits": 0, "watchdog_limit_reached": False})
    assert "✅ Watchdog" in msg
    assert "no stall restarts" in msg
    assert code == 0


def test_watchdog_line_omitted_when_the_daemon_is_unreachable():
    """A down daemon is already reported by the Daemon line; don't double-report."""
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(), daemon_status={})
    assert "Watchdog" not in msg
    assert code == 0


def test_live_daemon_status_degrades_to_none_without_a_daemon(tmp_path):
    assert doctor._live_daemon_status(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# offline flag (Task 6, Step 6): doctor must stay usable with zero live HTTP
# ---------------------------------------------------------------------------

def test_offline_skips_the_live_daemon_status_probe(monkeypatch):
    called = {"n": 0}

    def spy(home):
        called["n"] += 1
        return None

    monkeypatch.setattr(doctor, "_live_daemon_status", spy)
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(), offline=True)
    assert called["n"] == 0
    assert code == 0


def test_offline_false_still_calls_the_live_probe_by_default(monkeypatch):
    called = {"n": 0}

    def spy(home):
        called["n"] += 1
        return None

    monkeypatch.setattr(doctor, "_live_daemon_status", spy)
    doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs())
    assert called["n"] == 1


def test_offline_is_ignored_when_daemon_status_is_explicitly_injected():
    # An explicit injection (as every other doctor test does) always wins,
    # offline or not — offline only changes what happens on the None default.
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(), offline=True,
        daemon_status={"watchdog_exits": 0, "watchdog_limit_reached": False})
    assert "✅ Watchdog" in msg


def test_cli_offline_flag_is_parsed_and_forwarded(monkeypatch):
    seen = {}

    def fake_run_doctor(home, *, offline=False):
        seen["offline"] = offline
        return (0, "ok")

    monkeypatch.setattr(doctor, "run_doctor", fake_run_doctor)
    monkeypatch.setattr("mcpbrain.config.app_dir", lambda: "/tmp/home")
    try:
        doctor.run_doctor_main(["--offline"])
    except SystemExit:
        pass
    assert seen["offline"] is True


def test_doctor_reports_repair_state(tmp_path, monkeypatch):
    """The repair's progress has to be visible without running the CLI, or
    'is it done?' becomes a guess."""
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "|  |  |", "h1", {})
    store.upsert_chunk("gdrive-f1-0", "legacy text", "h2",
                       {"source_type": "gdrive", "file_id": "f1"})

    assert store.count_content_free() == 1
    assert [d["id"] for d in
           store.stale_chunker_ids(table_version=3, other_version=2, limit=10)] == ["f1"]

    # Asserting the store methods is not enough: the lines are only useful if
    # run_doctor actually prints them, and its own `except Exception` turns any
    # failure to open the store (e.g. the wrong filename) into a silent
    # "➖ Repair state skipped".
    code, msg = doctor.run_doctor(str(tmp_path), model_present=lambda h: True,
                                  conns=_conns(), repairs={})
    assert "⚠️ content-free chunks: 1" in msg, msg
    assert "⚠️ Items awaiting re-chunk: 1" in msg, msg
    assert "Repair state" not in msg, f"the repair-state block was skipped: {msg}"
