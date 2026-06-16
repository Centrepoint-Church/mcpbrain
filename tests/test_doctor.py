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
