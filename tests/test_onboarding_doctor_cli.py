from mcpbrain import doctor, onboarding


def _healthy_conns():
    ok = {"state": "ok", "detail": "fine"}
    return {"claude": ok, "records": ok, "google": ok,
            "enrichment": ok, "backup": ok}


def test_doctor_reports_baseline_line(capsys):
    calls = []
    repairs = {"baseline": lambda: calls.append(1) or {"status": "done"}}
    code, msg = doctor.run_doctor(
        "/tmp/home", conns=_healthy_conns(), repairs=repairs,
        model_present=lambda _h: True)
    assert "Baseline" in msg
    assert "done" in msg
    assert calls == [1]


def test_doctor_baseline_degrades_when_daemon_down(capsys):
    def _boom():
        raise RuntimeError("daemon not running")
    code, msg = doctor.run_doctor(
        "/tmp/home", conns=_healthy_conns(), repairs={"baseline": _boom},
        model_present=lambda _h: True)
    assert "Baseline" in msg          # reported, not fatal
    assert code == 0                  # a down daemon is not an actionable fault here


def test_bootstrap_main_prints_summary(tmp_path, monkeypatch, capsys):
    from mcpbrain import control_client

    class _FakeCC:
        def __init__(self, home, timeout=5.0): pass
        def bootstrap_baseline(self): return {"status": "done", "cache_hits": 7}
    monkeypatch.setattr(control_client, "ControlClient", _FakeCC)
    rc = onboarding.bootstrap_main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"status": "done"' in out and '"cache_hits": 7' in out


def test_bootstrap_main_handles_daemon_down(tmp_path, monkeypatch, capsys):
    from mcpbrain import control_client

    class _FakeCC:
        def __init__(self, home, timeout=5.0): pass
        def bootstrap_baseline(self):
            raise control_client.DaemonUnavailable("no port")
    monkeypatch.setattr(control_client, "ControlClient", _FakeCC)
    rc = onboarding.bootstrap_main([])
    assert rc == 1
    assert "not running" in capsys.readouterr().out
