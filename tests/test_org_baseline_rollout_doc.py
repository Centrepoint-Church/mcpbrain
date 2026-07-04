# tests/test_org_baseline_rollout_doc.py
from pathlib import Path

DOC = Path("docs/ORG-BASELINE-ROLLOUT.md")


def test_runbook_exists_and_states_the_master_gate():
    text = DOC.read_text()
    # The load-bearing facts the runbook MUST state (guards against drift).
    assert "fleet_secret" in text            # the master enable gate
    assert "org-config.json" in text         # how the pin is distributed
    assert "org_pin" in text
    assert "role" in text and "org_curator" in text
    # enable order: curator publishes a snapshot BEFORE members import
    assert "before" in text.lower()


def test_runbook_matches_code_gates():
    # If any of these flip default in code, the runbook's "default ON" claim is
    # stale — this test forces the doc and code to be reconciled together.
    from mcpbrain import config
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        assert config.org_import_enabled(home) is True
        assert config.ingest_cache_enabled(home) is True
        assert config.org_contrib_enabled(home) is True
        assert config.fleet_pin(home).is_pinned is False  # nothing moves without the secret
