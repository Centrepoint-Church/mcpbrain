"""Tests for the cowork cadence module."""


def test_cowork_no_longer_shells_claude():
    import mcpbrain.cowork as cw
    for n in ("run_cowork", "gardener_main", "meeting_packs_main"):
        assert not hasattr(cw, n)
