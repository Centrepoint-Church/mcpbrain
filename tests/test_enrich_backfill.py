import json

from mcpbrain import enrich_backfill


def test_local_claude_runner_invokes_cli(monkeypatch):
    seen = {}
    class _Result:
        stdout = '{"batch_id": "b1"}'
        returncode = 0
    def fake_run(cmd, *, input=None, capture_output=None, text=None, timeout=None):
        seen["cmd"] = cmd; seen["input"] = input; seen["timeout"] = timeout
        return _Result()
    monkeypatch.setattr("mcpbrain.config.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr(enrich_backfill.subprocess, "run", fake_run)
    out = enrich_backfill.local_claude_runner("PROMPT", model="sonnet", timeout=120)
    assert out == '{"batch_id": "b1"}'
    assert seen["cmd"][0] == "/usr/bin/claude"
    assert "PROMPT" == seen["input"]            # prompt piped via stdin
    assert seen["timeout"] == 120


def test_run_backfill_refuses_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    res = enrich_backfill.run_backfill(store=object(), embedder=object())
    assert res["status"] == "not_configured" and res["batches"] == 0


def test_run_backfill_loops_until_spool_dry(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({
        "owner_name": "Sam", "owner_email": "s@x.org", "orgs": [{"name": "Org"}]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # prepare returns 2 threads, then 1, then 0 (dry) — drives 2 loop iterations
    seq = iter([{"threads": [1, 2]}, {"threads": [1]}, {"threads": []}])
    monkeypatch.setattr(enrich_backfill.prepare, "prepare", lambda *a, **k: next(seq))
    extracted = {"n": 0}
    monkeypatch.setattr(enrich_backfill.extractor_driver, "run_extractor",
                        lambda **k: extracted.__setitem__("n", extracted["n"] + 1) or "inbox.json")
    drained = {"n": 0}
    monkeypatch.setattr(enrich_backfill.drain, "drain",
                        lambda *a, **k: drained.__setitem__("n", drained["n"] + 1) or {"applied": 1})
    res = enrich_backfill.run_backfill(store=object(), embedder=object())
    assert res["status"] == "done"
    assert res["batches"] == 2 and extracted["n"] == 2 and drained["n"] == 2
