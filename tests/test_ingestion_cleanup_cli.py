"""`mcpbrain clean-ingestion` dispatches to the packaged clean-up (ruling I13)."""
import pytest

from mcpbrain import cli, ingestion_cleanup


def test_cli_dispatches_with_args(monkeypatch):
    seen = {}

    def fake_main(argv):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(ingestion_cleanup, "main", fake_main)
    with pytest.raises(SystemExit) as ei:
        cli.main(["clean-ingestion", "--home", "/x", "--yes", "--undo", "dedup_embed"])
    assert ei.value.code == 0
    assert seen["argv"] == ["--home", "/x", "--yes", "--undo", "dedup_embed"]
