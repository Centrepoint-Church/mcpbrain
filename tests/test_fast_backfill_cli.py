import importlib.util
import os
import types
from pathlib import Path


def _load_cli():
    script = Path(__file__).resolve().parents[1] / "bin" / "fast_backfill.py"
    spec = importlib.util.spec_from_file_location("_fast_backfill_cli", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_parses_workers_and_model():
    cli = _load_cli()
    args = cli.parse_args(["--workers", "12", "--model", "haiku",
                           "--batch-size", "10", "--max-waves", "2"])
    assert args.workers == 12
    assert args.model == "haiku"
    assert args.batch_size == 10
    assert args.max_waves == 2
    assert args.force is False


def test_cli_force_flag():
    cli = _load_cli()
    args = cli.parse_args(["--force"])
    assert args.force is True


def test_main_sets_mcpbrain_home_before_store(tmp_path, monkeypatch):
    """main() must set MCPBRAIN_HOME to the resolved --home before any store
    or downstream config.app_dir() call.  The fake run_parallel_backfill
    captures the env var at call time; the assertion locks the seam."""
    cli = _load_cli()

    captured_home = {}

    def fake_run_parallel_backfill(**kwargs):
        captured_home["value"] = os.environ.get("MCPBRAIN_HOME")
        return {"status": "done", "waves": 0, "threads_dispatched": 0,
                "quarantined": 0}

    # daemon_status → None means "unreachable — proceeding as sole writer"
    monkeypatch.setattr(cli.parallel_backfill, "daemon_status", lambda *a, **k: None)
    monkeypatch.setattr(cli.parallel_backfill, "run_parallel_backfill",
                        fake_run_parallel_backfill)

    # Prevent real Store / embedder from loading
    monkeypatch.setattr("mcpbrain.store.Store", lambda *a, **k: object())
    monkeypatch.setattr("mcpbrain.embed.get_embedder",
                        lambda *a, **k: types.SimpleNamespace(dim=8))

    # Ensure MCPBRAIN_HOME is clean before the call so we're testing the
    # side-effect of main(), not a pre-existing env var.
    monkeypatch.delenv("MCPBRAIN_HOME", raising=False)

    rc = cli.main(["--home", str(tmp_path), "--max-waves", "0"])

    assert rc == 0
    assert captured_home["value"] == str(tmp_path.resolve()), (
        f"Expected MCPBRAIN_HOME={tmp_path.resolve()!r}, "
        f"got {captured_home['value']!r}"
    )
