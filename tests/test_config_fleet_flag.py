import json
import logging
from mcpbrain import config, fleet

def _write(tmp_path, obj):
    (tmp_path / "config.json").write_text(json.dumps(obj))

def test_fleet_flag_org_overlay_wins(tmp_path):
    # Org overlay beats the local value as long as the local value isn't an
    # explicit False (that's the kill-switch case, tested separately below) —
    # here the local key is absent entirely, so the org "true" applies.
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": True}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True


def test_fleet_flag_org_overlay_wins_over_local_true(tmp_path):
    # Org overlay outranks a local True too — only an explicit local False is
    # the kill-switch; any other local value defers to the org overlay.
    _write(tmp_path, {"retrieval_expand": True,
                      "org_config": {"flags": {"retrieval_expand": False}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is False

def test_fleet_flag_top_level_fallback(tmp_path):
    _write(tmp_path, {"retrieval_expand": True})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True

def test_fleet_flag_default(tmp_path):
    _write(tmp_path, {})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is False

def test_retrieval_expand_enabled_delegates_to_fleet_flag(tmp_path):
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": True}}})
    assert config.retrieval_expand_enabled(str(tmp_path)) is True

def test_flags_is_allowlisted():
    assert "flags" in fleet._ALLOWLIST


def test_fleet_flag_local_false_overrides_org_true_kill_switch(tmp_path):
    # Emergency kill-switch: an explicit local top-level False must win over
    # an org overlay True — the local install can always shut a flag off,
    # even if the fleet has turned it on for everyone.
    _write(tmp_path, {"retrieval_expand": False,
                      "org_config": {"flags": {"retrieval_expand": True}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is False


def test_fleet_flag_org_string_true_coerces(tmp_path):
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": "true"}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True


def test_fleet_flag_org_string_false_coerces(tmp_path):
    # A local True must not block an org "false" override (no local False
    # is present here, so org still wins per precedence) — string coerces.
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": "FALSE"}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", True) is False


def test_fleet_flag_local_string_false_is_kill_switch(tmp_path):
    _write(tmp_path, {"retrieval_expand": "false",
                      "org_config": {"flags": {"retrieval_expand": True}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is False


def test_fleet_flag_garbage_value_falls_back_to_default_and_warns(tmp_path, caplog):
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": "banana"}}})
    with caplog.at_level(logging.WARNING):
        result = config.fleet_flag(str(tmp_path), "retrieval_expand", True)
    assert result is True  # falls back to the given default
    assert any("banana" in r.message for r in caplog.records)


def test_fleet_flag_int_1_and_0_coerce(tmp_path):
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": 1}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", False) is True
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": 0}}})
    assert config.fleet_flag(str(tmp_path), "retrieval_expand", True) is False


def test_fleet_flag_garbage_local_does_not_kill_org_enable(tmp_path, caplog):
    # A garbage local value (e.g. a typo'd hand-edit) must NOT be treated as
    # the kill-switch just because it coerces to a falsy default — only a
    # RECOGNIZED local False may kill the flag. The org enable must win.
    _write(tmp_path, {"retrieval_expand": "banana",
                      "org_config": {"flags": {"retrieval_expand": True}}})
    with caplog.at_level(logging.WARNING):
        result = config.fleet_flag(str(tmp_path), "retrieval_expand", False)
    assert result is True
    assert any("banana" in r.message for r in caplog.records)


def test_fleet_flag_unrecognized_org_value_local_absent_falls_to_default(tmp_path, caplog):
    # Unrecognized org value + no local override at all -> falls through to
    # the given default, with a warning (not treated as False).
    _write(tmp_path, {"org_config": {"flags": {"retrieval_expand": "maybe"}}})
    with caplog.at_level(logging.WARNING):
        result = config.fleet_flag(str(tmp_path), "retrieval_expand", True)
    assert result is True
    assert any("maybe" in r.message for r in caplog.records)
