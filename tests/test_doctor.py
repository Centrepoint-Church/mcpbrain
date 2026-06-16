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
            for k in ("google", "claude", "clickup", "backup", "records", "enrichment")}
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
    code, msg = doctor.run_doctor("/tmp/home", conns=_conns(), repairs=repairs)
    assert code == 0
    assert all(r.calls == 0 for r in repairs.values())
    assert "mcpbrain doctor" in msg


def test_not_started_optional_renders_distinct_glyph_not_green_check():
    # clickup/backup not configured = deliberately optional, not a fault and not
    # "healthy". Must render with the ➖ glyph, NOT a green ✅, and not be counted.
    conns = _conns(clickup="not_started", backup="not_started")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns,
                                  repairs={"daemon": _Recorder(), "agent": _Recorder(),
                                           "records": _Recorder()})
    assert code == 0  # optional unconfigured features are not actionable
    assert "➖ ClickUp" in msg
    assert "➖ Backup" in msg
    assert "optional — not configured" in msg
    assert "✅ ClickUp" not in msg  # never a green check for "not set up"


def test_daemon_down_repair_called_reprobe_fixed():
    daemon = _Recorder()
    repairs = {"daemon": daemon, "agent": _Recorder(), "records": _Recorder()}
    # First probe: claude needs_action (daemon down). After repair, reprobe ok.
    conns = _conns(claude="needs_action")
    reprobed = {"claude": {"state": "ok", "detail": "Connected", "last_verified": None}}

    def fake_reprobe(home, key, fallback):
        return reprobed.get(key, fallback)

    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
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
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
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
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: {"state": "ok", "detail": "Ready",
                                                           "last_verified": None})
    assert records.calls == 1
    assert "fixed" in msg
    assert code == 0


def test_google_expired_guided_no_repair_exit1():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    conns = _conns(google="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f)
    assert all(r.calls == 0 for r in repairs.values())
    assert "mcpbrain auth" in msg
    assert code == 1


def test_repair_failure_reported_exit1():
    repairs = {"daemon": _Recorder(ok=False), "agent": _Recorder(),
               "records": _Recorder()}
    conns = _conns(claude="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f,
                                  agent_installed=lambda h, p: True)  # → daemon repair
    assert "repair failed" in msg
    assert code == 1


def test_scheduled_tasks_inferred_from_enrichment():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    # enrichment needs_action → scheduled-tasks line is guided, but NOT double-counted.
    conns = _conns(enrichment="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f)
    assert "Scheduled tasks" in msg
    assert "/mcpbrain-fix" in msg
    assert all(r.calls == 0 for r in repairs.values())
    # enrichment is guided (1 need_action) + scheduled-tasks is NOT double-counted
    # so code == 1 (one issue: enrichment itself)
    assert code == 1


def test_output_is_never_empty():
    code, msg = doctor.run_doctor("/tmp/home", conns=_conns(), repairs={})
    assert msg.strip(), "doctor must always print a report, even on the all-ok path"
    assert "mcpbrain doctor" in msg


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
    assert set(repairs) == {"daemon", "agent", "records"}

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
