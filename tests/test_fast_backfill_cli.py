import importlib.util
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
