"""Cowork module tests — cadences moved to Cowork Desktop Scheduled Tasks."""
import mcpbrain.cowork as cw


def test_cowork_no_longer_shells_claude():
    for n in ("run_cowork", "gardener_main", "meeting_packs_main"):
        assert not hasattr(cw, n)
