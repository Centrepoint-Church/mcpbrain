"""Tests for mcpbrain.setup's tray auto-start (zero-touch onboarding)."""


def test_start_tray_now_spawns_tray(monkeypatch):
    from mcpbrain import setup

    calls = {}
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/x/mcpbrain")

    def fake_popen(args, **k):
        calls["args"] = args

        class P:
            pass

        return P()

    monkeypatch.setattr(setup.subprocess, "Popen", fake_popen, raising=False)
    setup._start_tray_now("/home")
    assert calls["args"][:2] == ["/x/mcpbrain", "tray"]
