import json
from mcpbrain import parallel_backfill


def test_run_parallel_backfill_refuses_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    res = parallel_backfill.run_parallel_backfill(
        store=object(), embedder=object(), home=str(tmp_path),
        run_claude=lambda *a, **k: "{}", apply=lambda *a, **k: {})
    assert res["status"] == "not_configured"
    assert res["waves"] == 0


def test_helpers_are_importable():
    # The drain_backlog helpers must be reachable from the module.
    for name in ("extract_answer", "parse_extractor_json", "patch_extractions",
                 "atomic_write_inbox", "quarantine", "daemon_status"):
        assert hasattr(parallel_backfill, name), name
