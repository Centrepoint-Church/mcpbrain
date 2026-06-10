from mcpbrain import enrich_backfill


def test_local_claude_runner_invokes_cli(monkeypatch):
    seen = {}
    class _Result:
        stdout = '{"batch_id": "b1"}'
        returncode = 0
    def fake_run(cmd, *, input=None, capture_output=None, text=None, timeout=None):
        seen["cmd"] = cmd; seen["input"] = input; seen["timeout"] = timeout
        return _Result()
    monkeypatch.setattr(enrich_backfill, "_find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(enrich_backfill.subprocess, "run", fake_run)
    out = enrich_backfill.local_claude_runner("PROMPT", model="sonnet", timeout=120)
    assert out == '{"batch_id": "b1"}'
    assert seen["cmd"][0] == "/usr/bin/claude"
    assert "PROMPT" == seen["input"]            # prompt piped via stdin
    assert seen["timeout"] == 120
