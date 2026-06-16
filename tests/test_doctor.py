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
