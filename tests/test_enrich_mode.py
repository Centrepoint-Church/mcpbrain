"""Tests for config.enrich_mode — the single source of truth for the daemon's
enrichment-source branch (spool | gemini | off).

A fresh install resolves to "off": nothing enriches until the mode is set
explicitly. An unknown value clamps to "off" so a typo never silently enables a
path.
"""

from mcpbrain import config


def test_enrich_mode_default_is_off(tmp_path):
    """No config file / no key -> "off" (fresh install enriches nothing)."""
    assert config.enrich_mode(str(tmp_path)) == "off"


def test_enrich_mode_spool(tmp_path):
    config.write_config(str(tmp_path), {"enrich_mode": "spool"})
    assert config.enrich_mode(str(tmp_path)) == "spool"


def test_enrich_mode_off(tmp_path):
    config.write_config(str(tmp_path), {"enrich_mode": "off"})
    assert config.enrich_mode(str(tmp_path)) == "off"


def test_enrich_mode_invalid_falls_back_off(tmp_path, caplog):
    """An unknown string clamps to "off" and is logged."""
    config.write_config(str(tmp_path), {"enrich_mode": "wibble"})
    with caplog.at_level("WARNING"):
        assert config.enrich_mode(str(tmp_path)) == "off"
    assert any("enrich_mode" in r.message for r in caplog.records)
