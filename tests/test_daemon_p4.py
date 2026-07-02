"""Phase 4 daemon tests — Session-4 brain-review cadence (Task 4.2).

Tests for _run_review(), modelled directly on test_daemon_p3.py's
_run_resolve_entities tests: no constructor param, attrs set post-construction
(same as resolve_entities_interval_s / _last_resolve_entities), OFF unless
review_interval_s is set. _run_review builds review units from open
graph-hygiene findings (mcpbrain.review.build_review_units) and stashes them
into self._pending_blocks under the matching review_* key for the existing
enrich block-unit pipeline to pick up.
"""

from unittest.mock import patch

from mcpbrain.daemon import Daemon, SingleWriterLock
from mcpbrain.store import Store


class _FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _Clock:
    """List-controlled monotonic clock for deterministic 'due' checks."""

    def __init__(self, value: float = 0.0):
        self._value = value

    def __call__(self) -> float:
        return self._value

    def advance(self, by: float) -> None:
        self._value += by


def _make_store(tmp_path, name="p4.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _review_daemon(tmp_path, *, review_interval_s=None, clock=None):
    store = _make_store(tmp_path, name="review.sqlite3")
    daemon = Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "rv.lock"),
        clock=clock or _Clock(),
    )
    daemon._review_interval_s = review_interval_s
    return store, daemon


def test_run_review_off_when_unconfigured(tmp_path):
    """review_interval_s not supplied (None) -> _run_review() returns None
    without calling review.build_review_units (the numeric-interval
    kill-switch: 0/negative maps to None via _cadences_from_config)."""
    store, daemon = _review_daemon(tmp_path)  # no interval -> None
    with patch("mcpbrain.review.build_review_units") as mock_build:
        result = daemon._run_review()
    assert result is None
    mock_build.assert_not_called()


def test_run_review_kill_switch_zero_maps_to_none(tmp_path):
    """A configured 0 maps the interval attr to None via _cadences_from_config
    (the documented power-user kill-switch), so _run_review() is a no-op
    exactly like the never-configured case."""
    from mcpbrain.daemon import _cadences_from_config
    from mcpbrain import config

    config.write_config(str(tmp_path), {"cadences": {"review_interval_s": 0}})
    cadences = _cadences_from_config(str(tmp_path))
    assert cadences["review_interval_s"] is None

    store, daemon = _review_daemon(
        tmp_path, review_interval_s=cadences["review_interval_s"])
    store.record_finding("lint:orphan_entity", "e1", summary="orphan entity")
    with patch("mcpbrain.review.build_review_units") as mock_build:
        result = daemon._run_review()
    assert result is None
    mock_build.assert_not_called()


def test_run_review_runs_when_due_stashes_block_units(tmp_path):
    """With review_interval_s set and an open lint:orphan_entity finding,
    one call to _run_review() produces review block-units in
    self._pending_blocks under the correct 'review_orphan' key."""
    store, daemon = _review_daemon(tmp_path, review_interval_s=86400.0)
    store.record_finding("lint:orphan_entity", "e1", summary="orphan entity")

    result = daemon._run_review()

    assert result == {"review_orphan": 1}
    assert "review_orphan" in daemon._pending_blocks
    units = daemon._pending_blocks["review_orphan"]
    assert len(units) == 1
    assert units[0]["packet"]["finding_type"] == "lint:orphan_entity"
    assert units[0]["packet"]["ref_id"] == "e1"
    assert daemon._last_review is not None


def test_run_review_swallows_errors(tmp_path):
    """An exception during the build is swallowed, never crashes the loop,
    logs a warning (not asserted here), and does NOT advance _last_review."""
    store, daemon = _review_daemon(tmp_path, review_interval_s=100.0)
    with patch("mcpbrain.review.build_review_units",
               side_effect=RuntimeError("boom")):
        result = daemon._run_review()
    assert result["review"] is False
    assert daemon._last_review is None


def test_run_review_not_due_before_interval(tmp_path):
    """After a successful run, a call within the interval returns None and
    does not touch build_review_units again."""
    clock = _Clock()
    store, daemon = _review_daemon(tmp_path, review_interval_s=100.0, clock=clock)
    store.record_finding("lint:orphan_entity", "e1", summary="orphan entity")

    first = daemon._run_review()
    assert first is not None

    clock.advance(50.0)
    with patch("mcpbrain.review.build_review_units") as mock_build:
        second = daemon._run_review()
    assert second is None
    mock_build.assert_not_called()


def test_run_review_runs_again_after_interval(tmp_path):
    """After the interval elapses fully, the pass is due again."""
    clock = _Clock()
    store, daemon = _review_daemon(tmp_path, review_interval_s=100.0, clock=clock)
    store.record_finding("lint:orphan_entity", "e1", summary="orphan entity")

    first = daemon._run_review()
    assert first is not None

    clock.advance(100.0)
    second = daemon._run_review()
    assert second is not None
    assert second == {"review_orphan": 1}


# ---------------------------------------------------------------------------
# Fix round 1 — _run_blocks must MERGE into _pending_blocks, not replace it
# ---------------------------------------------------------------------------

def test_run_blocks_merges_preserving_undrained_review_batch(tmp_path):
    """Reproduces the replace-vs-merge race: an undrained review_* batch
    stashed by _run_review() must survive a co-firing _run_blocks() call.

    Before the fix, _run_blocks() did `self._pending_blocks = {...}` (a full
    replace), which would silently wipe out any review_* keys sitting in
    _pending_blocks while awaiting subagent pull/push. The fix changes that
    to `self._pending_blocks.update({...})`, an additive merge, so
    _run_blocks() still correctly sets/overwrites its own three keys every
    time it fires, without clobbering keys it doesn't own.
    """
    store = _make_store(tmp_path, name="blocks_merge.sqlite3")
    daemon = Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "bm.lock"),
        blocks_interval_s=86400.0,
        clock=_Clock(),
    )

    # Simulate an undrained review batch left in _pending_blocks by a prior
    # _run_review() tick that a subagent hasn't pulled/pushed verdicts for yet.
    sentinel_review_batch = [{"packet": {"finding_type": "lint:orphan_entity",
                                          "ref_id": "e1"}}]
    daemon._pending_blocks["review_orphan"] = sentinel_review_batch

    fake_profiles = [{"entity_id": "e-1", "name": "Alice"}]
    fake_communities = [{"community_id": "c-1"}]
    fake_distil = [{"memory_id": "m-1"}]

    with patch("mcpbrain.profile_synth.build_profile_requests",
               return_value=fake_profiles), \
         patch("mcpbrain.community_synth.build_community_requests",
               return_value=fake_communities), \
         patch("mcpbrain.memory_distil.build_distil_requests",
               return_value=fake_distil):
        result = daemon._run_blocks()

    assert result == {
        "profile_synthesis_requested": 1,
        "community_synthesis_requested": 1,
        "memory_distil_requested": 1,
    }
    # The undrained review batch must survive the merge, untouched.
    assert daemon._pending_blocks["review_orphan"] is sentinel_review_batch
    # _run_blocks()'s own three keys are still set correctly.
    assert daemon._pending_blocks["profile_synthesis"] == fake_profiles
    assert daemon._pending_blocks["community_synthesis"] == fake_communities
    assert daemon._pending_blocks["memory_distil"] == fake_distil
