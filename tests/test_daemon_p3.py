"""Phase 3 daemon tests — communities (Sub-task 1.3) and lint (Sub-task 2.3).

Tests for maybe_communities() and maybe_lint(), modelled on the maybe_resolve
tests in test_daemon.py. Each test is self-contained with a minimal store fixture.
"""

import pytest
from unittest.mock import patch

from mcpbrain.daemon import Daemon, SingleWriterLock
from mcpbrain.store import Store


# ---------------------------------------------------------------------------
# Helpers (mirrors test_daemon.py helpers)
# ---------------------------------------------------------------------------

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


def _make_store(tmp_path, name="p3.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _daemon(tmp_path, *, communities_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "d.lock"),
        communities_interval_s=communities_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


# ---------------------------------------------------------------------------
# Sub-task 1.3 — maybe_communities tests
# ---------------------------------------------------------------------------

def test_maybe_communities_off_when_unconfigured(tmp_path):
    """communities_interval_s not supplied -> maybe_communities() returns None
    and the communities.run pass is never called."""
    store, daemon = _daemon(tmp_path)  # no communities_interval_s

    with patch("mcpbrain.communities.run") as mock_run:
        result = daemon.maybe_communities()

    assert result is None
    mock_run.assert_not_called()


def test_maybe_communities_runs_when_due_first_call(tmp_path):
    """communities_interval_s set -> first call is always due and returns the
    summary dict from communities.run."""
    store, daemon = _daemon(tmp_path, communities_interval_s=3600.0)

    fake_summary = {"communities": 3, "entities": 12}
    with patch("mcpbrain.communities.run", return_value=fake_summary) as mock_run:
        result = daemon.maybe_communities()

    assert result == fake_summary
    mock_run.assert_called_once_with(store)


def test_maybe_communities_not_due_before_interval(tmp_path):
    """After a successful run, a call within the interval returns None."""
    clock = _Clock()
    store, daemon = _daemon(tmp_path, communities_interval_s=100.0, clock=clock)

    fake_summary = {"communities": 2, "entities": 8}
    with patch("mcpbrain.communities.run", return_value=fake_summary):
        first = daemon.maybe_communities()
    assert first is not None

    # Advance less than the interval.
    clock.advance(50.0)
    with patch("mcpbrain.communities.run") as mock_run:
        second = daemon.maybe_communities()

    assert second is None
    mock_run.assert_not_called()


def test_maybe_communities_runs_again_after_interval(tmp_path):
    """After the interval elapses, maybe_communities runs again."""
    clock = _Clock()
    store, daemon = _daemon(tmp_path, communities_interval_s=100.0, clock=clock)

    fake_summary = {"communities": 2, "entities": 8}
    with patch("mcpbrain.communities.run", return_value=fake_summary):
        daemon.maybe_communities()

    # Advance past the interval.
    clock.advance(110.0)
    with patch("mcpbrain.communities.run", return_value=fake_summary) as mock_run:
        result = daemon.maybe_communities()

    assert result is not None
    mock_run.assert_called_once()


def test_maybe_communities_swallows_errors(tmp_path):
    """A communities.run exception is logged and swallowed.

    maybe_communities returns {"communities": False, "error": ...} and
    _last_communities is NOT advanced (so the next call retries).
    """
    clock = _Clock()
    store, daemon = _daemon(tmp_path, communities_interval_s=100.0, clock=clock)

    with patch("mcpbrain.communities.run", side_effect=RuntimeError("boom")):
        result = daemon.maybe_communities()

    assert result is not None
    assert result["communities"] is False
    assert "error" in result
    assert "boom" in result["error"]

    # _last_communities must NOT have been advanced — the next call is still due.
    assert daemon._last_communities is None, (
        "_last_communities must not advance after a failed run"
    )

    # Confirm the next call also runs (not suppressed by a false advance).
    fake_summary = {"communities": 1, "entities": 4}
    with patch("mcpbrain.communities.run", return_value=fake_summary) as mock_run:
        retry = daemon.maybe_communities()

    assert retry == fake_summary
    mock_run.assert_called_once()


def test_maybe_communities_advances_clock_only_on_success(tmp_path):
    """_last_communities is set on a clean run but not on a failure."""
    clock = _Clock(value=0.0)
    store, daemon = _daemon(tmp_path, communities_interval_s=10.0, clock=clock)

    # First call: error — _last_communities must stay None.
    with patch("mcpbrain.communities.run", side_effect=ValueError("oops")):
        daemon.maybe_communities()
    assert daemon._last_communities is None

    # Second call at clock=5: success — _last_communities must now be 5.
    clock.advance(5.0)
    with patch("mcpbrain.communities.run", return_value={"communities": 1}):
        daemon.maybe_communities()
    assert daemon._last_communities == 5.0


# ---------------------------------------------------------------------------
# Sub-task 2.3 — maybe_lint tests
# ---------------------------------------------------------------------------

def _lint_daemon(tmp_path, *, lint_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "dl.lock"),
        lint_interval_s=lint_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_lint_off_when_unconfigured(tmp_path):
    """lint_interval_s not supplied -> maybe_lint() returns None and
    lint_graph.run is never called."""
    store, daemon = _lint_daemon(tmp_path)  # no lint_interval_s

    with patch("mcpbrain.lint_graph.run") as mock_run:
        result = daemon.maybe_lint()

    assert result is None
    mock_run.assert_not_called()


def test_maybe_lint_runs_when_due(tmp_path):
    """lint_interval_s set -> first call is always due and returns the
    summary dict from lint_graph.run."""
    store, daemon = _lint_daemon(tmp_path, lint_interval_s=3600.0)

    fake_summary = {"findings": 2, "report_path": "/tmp/lint_2026-06-03.md"}
    with patch("mcpbrain.lint_graph.run", return_value=fake_summary) as mock_run:
        result = daemon.maybe_lint()

    assert result == fake_summary
    mock_run.assert_called_once()
    # Verify now= was passed as a kwarg
    call_kwargs = mock_run.call_args[1]
    assert "now" in call_kwargs


def test_maybe_lint_not_due_before_interval(tmp_path):
    """After a successful run, a call within the interval returns None."""
    clock = _Clock()
    store, daemon = _lint_daemon(tmp_path, lint_interval_s=100.0, clock=clock)

    fake_summary = {"findings": 0, "report_path": "/tmp/lint.md"}
    with patch("mcpbrain.lint_graph.run", return_value=fake_summary):
        first = daemon.maybe_lint()
    assert first is not None

    clock.advance(50.0)
    with patch("mcpbrain.lint_graph.run") as mock_run:
        second = daemon.maybe_lint()

    assert second is None
    mock_run.assert_not_called()


def test_maybe_lint_swallows_errors(tmp_path):
    """A lint_graph.run exception is logged and swallowed.

    maybe_lint returns {"lint": False, "error": ...} and _last_lint is NOT
    advanced (so the next call retries).
    """
    clock = _Clock()
    store, daemon = _lint_daemon(tmp_path, lint_interval_s=100.0, clock=clock)

    with patch("mcpbrain.lint_graph.run", side_effect=RuntimeError("lint-boom")):
        result = daemon.maybe_lint()

    assert result is not None
    assert result["lint"] is False
    assert "error" in result
    assert "lint-boom" in result["error"]

    # _last_lint must NOT have advanced — the next call is still due.
    assert daemon._last_lint is None, (
        "_last_lint must not advance after a failed run"
    )

    # Confirm the next call retries.
    fake_summary = {"findings": 0, "report_path": "/tmp/lint.md"}
    with patch("mcpbrain.lint_graph.run", return_value=fake_summary) as mock_run:
        retry = daemon.maybe_lint()

    assert retry == fake_summary
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Sub-task 3.4 — maybe_synthesise tests
# ---------------------------------------------------------------------------

def _synth_daemon(tmp_path, *, synthesise_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "ds.lock"),
        synthesise_interval_s=synthesise_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_synthesise_off_when_unconfigured(tmp_path):
    """synthesise_interval_s not supplied -> maybe_synthesise() returns None."""
    store, daemon = _synth_daemon(tmp_path)  # no synthesise_interval_s

    with patch("mcpbrain.synthesise_threads.build_synthesis_requests") as mock_build:
        result = daemon.maybe_synthesise()

    assert result is None
    mock_build.assert_not_called()


def test_maybe_synthesise_sets_due_flag_when_due(tmp_path):
    """synthesise_interval_s set -> first call returns summary with synthesis_requested=N
    and stashes requests in daemon._pending_synthesis."""
    store, daemon = _synth_daemon(tmp_path, synthesise_interval_s=3600.0)
    clock = _Clock()

    fake_requests = [
        {"thread_id": "t-1", "subject": "S1"},
        {"thread_id": "t-2", "subject": "S2"},
    ]
    with patch("mcpbrain.synthesise_threads.build_synthesis_requests",
               return_value=fake_requests) as mock_build:
        result = daemon.maybe_synthesise()

    assert result is not None
    assert result["synthesis_requested"] == 2
    assert daemon._pending_synthesis == fake_requests
    mock_build.assert_called_once_with(store)


def test_maybe_synthesise_not_due_before_interval(tmp_path):
    """After a successful run, a call before the interval elapses returns None."""
    clock = _Clock()
    store, daemon = _synth_daemon(tmp_path, synthesise_interval_s=100.0, clock=clock)

    fake_requests = [{"thread_id": "t-x", "subject": "X"}]
    with patch("mcpbrain.synthesise_threads.build_synthesis_requests",
               return_value=fake_requests):
        first = daemon.maybe_synthesise()
    assert first is not None

    # Advance less than the interval.
    clock.advance(50.0)
    with patch("mcpbrain.synthesise_threads.build_synthesis_requests") as mock_build:
        second = daemon.maybe_synthesise()

    assert second is None
    mock_build.assert_not_called()


def test_maybe_synthesise_swallows_errors(tmp_path):
    """build_synthesis_requests raising -> error dict returned, _last_synthesise NOT advanced."""
    clock = _Clock()
    store, daemon = _synth_daemon(tmp_path, synthesise_interval_s=100.0, clock=clock)

    with patch("mcpbrain.synthesise_threads.build_synthesis_requests",
               side_effect=RuntimeError("synth-boom")):
        result = daemon.maybe_synthesise()

    assert result is not None
    assert result["synthesis_requested"] == 0
    assert "error" in result
    assert "synth-boom" in result["error"]

    # _last_synthesise must NOT have advanced — next call retries.
    assert daemon._last_synthesise is None, (
        "_last_synthesise must not advance after a failed run"
    )

    # Confirm the next call retries.
    fake_requests = [{"thread_id": "t-retry"}]
    with patch("mcpbrain.synthesise_threads.build_synthesis_requests",
               return_value=fake_requests) as mock_build:
        retry = daemon.maybe_synthesise()

    assert retry is not None
    assert retry["synthesis_requested"] == 1
    mock_build.assert_called_once()


def test_maybe_synthesise_advances_clock_only_on_success(tmp_path):
    """_last_synthesise is set on a clean run but not on a failure."""
    clock = _Clock(value=0.0)
    store, daemon = _synth_daemon(tmp_path, synthesise_interval_s=10.0, clock=clock)

    # First call: error — _last_synthesise must stay None.
    with patch("mcpbrain.synthesise_threads.build_synthesis_requests",
               side_effect=ValueError("oops")):
        daemon.maybe_synthesise()
    assert daemon._last_synthesise is None

    # Second call at clock=5: success — _last_synthesise must be 5.
    clock.advance(5.0)
    with patch("mcpbrain.synthesise_threads.build_synthesis_requests",
               return_value=[]):
        daemon.maybe_synthesise()
    assert daemon._last_synthesise == 5.0


def test_maybe_synthesise_pending_synthesis_reset_after_run_one(tmp_path):
    """After run_one(), _pending_synthesis is reset to [] regardless of what prepare does."""
    from unittest.mock import MagicMock
    store, daemon = _synth_daemon(tmp_path, synthesise_interval_s=3600.0)

    # Pre-load pending synthesis.
    daemon._pending_synthesis = [{"thread_id": "t-pre"}]

    # Stub out run_cycle so run_one doesn't need real services.
    with patch("mcpbrain.daemon.run_cycle", return_value={"enrich": {}}):
        daemon.run_one()

    assert daemon._pending_synthesis == [], (
        "_pending_synthesis must be reset to [] after run_one"
    )


# ---------------------------------------------------------------------------
# Sub-task 4.3 — maybe_proactive tests
# ---------------------------------------------------------------------------

def _proactive_daemon(tmp_path, *, proactive_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "dp.lock"),
        proactive_interval_s=proactive_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_proactive_off_when_unconfigured(tmp_path):
    """proactive_interval_s not supplied -> maybe_proactive() returns None and
    proactive.run is never called."""
    store, daemon = _proactive_daemon(tmp_path)  # no proactive_interval_s

    with patch("mcpbrain.proactive.run") as mock_run:
        result = daemon.maybe_proactive()

    assert result is None
    mock_run.assert_not_called()


def test_maybe_proactive_runs_when_due(tmp_path):
    """proactive_interval_s set -> first call is always due and returns the
    summary dict from proactive.run."""
    store, daemon = _proactive_daemon(tmp_path, proactive_interval_s=3600.0)

    fake_summary = {"project_no_next_action": 2, "area_overdue": 1}
    with patch("mcpbrain.proactive.run", return_value=fake_summary) as mock_run:
        result = daemon.maybe_proactive()

    assert result == fake_summary
    mock_run.assert_called_once()
    # now= was passed as a kwarg
    call_kwargs = mock_run.call_args[1]
    assert "now" in call_kwargs


def test_maybe_proactive_not_due_before_interval(tmp_path):
    """After a successful run, a call within the interval returns None."""
    clock = _Clock()
    store, daemon = _proactive_daemon(tmp_path, proactive_interval_s=100.0, clock=clock)

    fake_summary = {"project_no_next_action": 0, "area_overdue": 0}
    with patch("mcpbrain.proactive.run", return_value=fake_summary):
        first = daemon.maybe_proactive()
    assert first is not None

    clock.advance(50.0)
    with patch("mcpbrain.proactive.run") as mock_run:
        second = daemon.maybe_proactive()

    assert second is None
    mock_run.assert_not_called()


def test_maybe_proactive_swallows_errors(tmp_path):
    """A proactive.run exception is logged and swallowed.

    maybe_proactive returns {"proactive": False, "error": ...} and
    _last_proactive is NOT advanced (so the next call retries).
    """
    clock = _Clock()
    store, daemon = _proactive_daemon(tmp_path, proactive_interval_s=100.0, clock=clock)

    with patch("mcpbrain.proactive.run", side_effect=RuntimeError("proactive-boom")):
        result = daemon.maybe_proactive()

    assert result is not None
    assert result["proactive"] is False
    assert "error" in result
    assert "proactive-boom" in result["error"]

    # _last_proactive must NOT have advanced.
    assert daemon._last_proactive is None, (
        "_last_proactive must not advance after a failed run"
    )

    # Confirm the next call retries.
    fake_summary = {"project_no_next_action": 0, "area_overdue": 0}
    with patch("mcpbrain.proactive.run", return_value=fake_summary) as mock_run:
        retry = daemon.maybe_proactive()

    assert retry == fake_summary
    mock_run.assert_called_once()


def test_maybe_proactive_advances_clock_only_on_success(tmp_path):
    """_last_proactive is set on a clean run but not on a failure."""
    clock = _Clock(value=0.0)
    store, daemon = _proactive_daemon(tmp_path, proactive_interval_s=10.0, clock=clock)

    # Failure does NOT advance _last_proactive
    with patch("mcpbrain.proactive.run", side_effect=ValueError("oops")):
        daemon.maybe_proactive()
    assert daemon._last_proactive is None

    # Success DOES advance _last_proactive to the start-time anchor
    clock.advance(5.0)
    with patch("mcpbrain.proactive.run", return_value={"project_no_next_action": 0, "area_overdue": 0}):
        result = daemon.maybe_proactive()
    assert result is not None
    assert daemon._last_proactive == 5.0


# ---------------------------------------------------------------------------
# Sub-task 5.3 — maybe_waiting_on tests
# ---------------------------------------------------------------------------

def _waiting_on_daemon(tmp_path, *, waiting_on_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "dw.lock"),
        waiting_on_interval_s=waiting_on_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_waiting_on_off_when_unconfigured(tmp_path):
    """waiting_on_interval_s not supplied -> maybe_waiting_on() returns None and
    waiting_on.run is never called."""
    store, daemon = _waiting_on_daemon(tmp_path)  # no waiting_on_interval_s

    with patch("mcpbrain.waiting_on.run") as mock_run:
        result = daemon.maybe_waiting_on()

    assert result is None
    mock_run.assert_not_called()


def test_maybe_waiting_on_runs_when_due(tmp_path):
    """waiting_on_interval_s set -> first call is always due and returns the
    summary dict from waiting_on.run."""
    store, daemon = _waiting_on_daemon(tmp_path, waiting_on_interval_s=3600.0)

    fake_summary = {"cleared": 2}
    with patch("mcpbrain.waiting_on.run", return_value=fake_summary) as mock_run:
        result = daemon.maybe_waiting_on()

    assert result == fake_summary
    mock_run.assert_called_once()
    # now= was passed as a kwarg
    call_kwargs = mock_run.call_args[1]
    assert "now" in call_kwargs


def test_maybe_waiting_on_swallows_errors(tmp_path):
    """A waiting_on.run exception is logged and swallowed.

    maybe_waiting_on returns {"waiting_on": False, "error": ...} and
    _last_waiting_on is NOT advanced (so the next call retries).
    """
    clock = _Clock()
    store, daemon = _waiting_on_daemon(tmp_path, waiting_on_interval_s=100.0, clock=clock)

    with patch("mcpbrain.waiting_on.run", side_effect=RuntimeError("wo-boom")):
        result = daemon.maybe_waiting_on()

    assert result is not None
    assert result["waiting_on"] is False
    assert "error" in result
    assert "wo-boom" in result["error"]

    # _last_waiting_on must NOT have advanced — the next call is still due.
    assert daemon._last_waiting_on is None, (
        "_last_waiting_on must not advance after a failed run"
    )

    # Confirm the next call retries.
    fake_summary = {"cleared": 0}
    with patch("mcpbrain.waiting_on.run", return_value=fake_summary) as mock_run:
        retry = daemon.maybe_waiting_on()

    assert retry == fake_summary
    mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Task 6 — _run_periodic_passes wiring tests
# ---------------------------------------------------------------------------

def test_run_calls_passes_in_order(tmp_path):
    """_run_periodic_passes() calls the five maybe_* methods in spec order:
    communities -> lint -> synthesise -> proactive -> waiting_on."""
    from unittest.mock import MagicMock
    from mcpbrain.embed import get_embedder
    from mcpbrain import config

    store = _make_store(tmp_path, name="t6a.db")
    emb = _FakeEmbedder()
    daemon = Daemon(
        store, emb,
        services={},
        lock=SingleWriterLock(tmp_path / "t6a.lock"),
        interval_s=1.0,
        communities_interval_s=1.0,
        lint_interval_s=1.0,
        synthesise_interval_s=1.0,
        proactive_interval_s=1.0,
        waiting_on_interval_s=1.0,
    )

    call_order = []
    daemon.maybe_communities = MagicMock(side_effect=lambda: call_order.append("communities"))
    daemon.maybe_lint = MagicMock(side_effect=lambda: call_order.append("lint"))
    daemon.maybe_synthesise = MagicMock(side_effect=lambda: call_order.append("synthesise"))
    daemon.maybe_proactive = MagicMock(side_effect=lambda: call_order.append("proactive"))
    daemon.maybe_waiting_on = MagicMock(side_effect=lambda: call_order.append("waiting_on"))

    daemon._run_periodic_passes()

    assert call_order == ["communities", "lint", "synthesise", "proactive", "waiting_on"]


def test_run_one_pass_failure_does_not_block_others(tmp_path):
    """If one maybe_* pass raises unexpectedly, _run_periodic_passes catches it
    and the remaining passes still run."""
    from unittest.mock import MagicMock

    store = _make_store(tmp_path, name="t6b.db")
    emb = _FakeEmbedder()
    daemon = Daemon(
        store, emb,
        services={},
        lock=SingleWriterLock(tmp_path / "t6b.lock"),
        interval_s=1.0,
        communities_interval_s=1.0,
        lint_interval_s=1.0,
        synthesise_interval_s=1.0,
        proactive_interval_s=1.0,
        waiting_on_interval_s=1.0,
    )

    # maybe_lint raises directly (bypasses its internal try/except).
    daemon.maybe_communities = MagicMock(return_value={"communities": 0})
    daemon.maybe_lint = MagicMock(side_effect=RuntimeError("lint exploded"))
    daemon.maybe_synthesise = MagicMock(return_value={"synthesis_requested": 0})
    daemon.maybe_proactive = MagicMock(return_value={"project_no_next_action": 0})
    daemon.maybe_waiting_on = MagicMock(return_value={"cleared": 0})

    # Must not propagate the exception.
    daemon._run_periodic_passes()

    # All other passes still ran.
    daemon.maybe_communities.assert_called_once()
    daemon.maybe_synthesise.assert_called_once()
    daemon.maybe_proactive.assert_called_once()
    daemon.maybe_waiting_on.assert_called_once()


# ---------------------------------------------------------------------------
# Task 7 — _cadences_from_config + apply_config cadence wiring
# ---------------------------------------------------------------------------

def test_cadences_from_config(tmp_path):
    from mcpbrain.daemon import _cadences_from_config
    from mcpbrain import config

    config.write_config(str(tmp_path), {
        "cadences": {
            "communities_interval_s": 86400,
            "lint_interval_s": 86400,
            "synthesise_interval_s": 86400,
            "proactive_interval_s": 21600,
            "waiting_on_interval_s": 3600,
        }
    })

    result = _cadences_from_config(str(tmp_path))
    assert result["communities_interval_s"] == 86400.0
    assert result["proactive_interval_s"] == 21600.0
    assert result["waiting_on_interval_s"] == 3600.0


def test_cadences_from_config_absent_keys_map_to_none(tmp_path):
    from mcpbrain.daemon import _cadences_from_config
    from mcpbrain import config

    config.write_config(str(tmp_path), {})  # no cadences block
    result = _cadences_from_config(str(tmp_path))
    assert all(v is None for v in result.values())


def test_apply_config_rewires_cadences(tmp_path):
    from mcpbrain import config
    from mcpbrain.daemon import Daemon

    store = _make_store(tmp_path, name="t7.db")
    emb = _FakeEmbedder()

    # Start with no cadences configured.
    daemon = Daemon(store, emb, interval_s=1.0)
    assert daemon._communities_interval_s is None

    new_config = {"cadences": {"communities_interval_s": 500.0}}

    with patch("mcpbrain.daemon._enrich_client_from_config", return_value=None), \
         patch("mcpbrain.daemon._backup_from_config", return_value=(None, None)), \
         patch("mcpbrain.daemon.config.write_config"), \
         patch("mcpbrain.daemon.config.enrich_mode", return_value="static"), \
         patch("mcpbrain.daemon._cadences_from_config", return_value={
             "communities_interval_s": 500.0,
             "lint_interval_s": None,
             "synthesise_interval_s": None,
             "proactive_interval_s": None,
             "waiting_on_interval_s": None,
         }):
        daemon.apply_config(new_config)

    assert daemon._communities_interval_s == 500.0
    assert daemon._lint_interval_s is None
