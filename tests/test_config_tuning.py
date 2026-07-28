"""daemon_tuning must reject values that pass a naive `<= 0` check."""
import json

from mcpbrain import config




def test_tuning_rejects_non_finite_values(tmp_path):
    """NaN/Inf pass a `val <= 0` check, so "inf" would silently disable the
    watchdog (stall_s) and unbound the "bounded" bulk-lock acquire."""
    (tmp_path / "config.json").write_text(json.dumps(
        {"tuning": {"stall_s": "inf", "bulk_lock_acquire_s": "nan",
                    "cycle_budget_s": "-inf"}}))
    got = config.daemon_tuning(
        tmp_path,
        {"stall_s": 1800.0, "bulk_lock_acquire_s": 5.0, "cycle_budget_s": 60.0},
    )
    assert got["stall_s"] == 1800.0
    assert got["bulk_lock_acquire_s"] == 5.0
    assert got["cycle_budget_s"] == 60.0


def test_sheet_char_budget_defaults_and_rejects_nonsense(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    assert config.sheet_char_budget(str(tmp_path)) == 2_000_000

    (tmp_path / "config.json").write_text('{"sheet_char_budget": 50000}')
    assert config.sheet_char_budget(str(tmp_path)) == 50_000

    (tmp_path / "config.json").write_text('{"sheet_char_budget": "lots"}')
    assert config.sheet_char_budget(str(tmp_path)) == 2_000_000

    (tmp_path / "config.json").write_text('{"sheet_char_budget": -1}')
    assert config.sheet_char_budget(str(tmp_path)) == 2_000_000
