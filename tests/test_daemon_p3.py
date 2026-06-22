"""Phase 3 daemon tests — communities (Sub-task 1.3) and lint (Sub-task 2.3).

Tests for maybe_communities() and maybe_lint(), modelled on the maybe_resolve
tests in test_daemon.py. Each test is self-contained with a minimal store fixture.
"""

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
    """After the interval elapses AND the graph has grown materially, it runs."""
    clock = _Clock()
    store, daemon = _daemon(tmp_path, communities_interval_s=100.0, clock=clock)

    fake_summary = {"communities": 2, "entities": 8}
    with patch("mcpbrain.communities.run", return_value=fake_summary):
        daemon.maybe_communities()

    # Grow the graph past the change threshold, then advance past the interval.
    with store._connect() as db:
        for i in range(30):
            db.execute("INSERT INTO entities(id,name,type) VALUES(?,?,'person')", (f"e{i}", f"E{i}"))
    clock.advance(110.0)
    with patch("mcpbrain.communities.run", return_value=fake_summary) as mock_run:
        result = daemon.maybe_communities()

    assert result is not None
    mock_run.assert_called_once()


def test_maybe_communities_skipped_when_graph_idle(tmp_path):
    """Change-driven: after the interval, an UNCHANGED graph is not re-clustered."""
    clock = _Clock()
    store, daemon = _daemon(tmp_path, communities_interval_s=100.0, clock=clock)
    with patch("mcpbrain.communities.run", return_value={"communities": 1}):
        daemon.maybe_communities()                       # first run clusters + marks
    clock.advance(110.0)                                 # interval elapsed, but no graph change
    with patch("mcpbrain.communities.run") as mock_run:
        result = daemon.maybe_communities()
    assert result == {"communities": "skipped_no_change"}
    mock_run.assert_not_called()


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


def test_pending_synthesis_kept_until_drained(tmp_path):
    """_pending_synthesis survives run_one() until the drain reports synthesis
    answers. prepare REWRITES pending.json every cycle, so a one-shot attach
    was overwritten a minute later unless the extractor read the file inside a
    single interval (live 2026-06-05 loss)."""
    store, daemon = _synth_daemon(tmp_path, synthesise_interval_s=3600.0)
    daemon._pending_synthesis = [{"thread_id": "t-pre"}]

    # No synthesis answers drained yet -> stash kept, re-sent next cycle.
    with patch("mcpbrain.daemon.run_cycle",
               return_value={"enrich": {"mode": "spool", "drain": {}}}):
        daemon.run_one()
    assert daemon._pending_synthesis == [{"thread_id": "t-pre"}]

    # Drain reports the answers came back -> stash cleared.
    with patch("mcpbrain.daemon.run_cycle",
               return_value={"enrich": {"mode": "spool",
                                        "drain": {"synthesis_written": 1}}}):
        daemon.run_one()
    assert daemon._pending_synthesis == []


def test_stamp_enrich_log_is_the_probe_signal(tmp_path, monkeypatch):
    # Regression: nothing wrote logs/enrich.log, so the enrichment health probe was
    # stuck on Idle forever. _stamp_enrich_log is now the writer, and it must produce
    # a file the probe reads as a fresh drain ("Running").
    from mcpbrain import daemon as _daemon, probes as _probes
    monkeypatch.setattr(_daemon, "app_dir", lambda: tmp_path)
    log = tmp_path / "logs" / "enrich.log"
    assert _probes.probe_enrichment(str(tmp_path))["state"] == "needs_action"  # no stamp yet
    _daemon._stamp_enrich_log({"applied": 5, "marked": 5, "files": 2, "merges": 0})
    assert log.exists() and "applied=5" in log.read_text()
    assert _probes.probe_enrichment(str(tmp_path))["state"] == "ok"            # now Running


def test_run_cycle_stamps_enrich_log_on_productive_drain(tmp_path, monkeypatch):
    # run_cycle must stamp the log when the spool drain actually applied work, and
    # must NOT stamp on an empty drain (so Idle stays honest).
    from mcpbrain import daemon as _daemon
    monkeypatch.setattr(_daemon, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(_daemon, "run_sync_cycle", lambda *a, **k: {})
    monkeypatch.setattr(_daemon.prepare, "prepare_units", lambda *a, **k: {"batch_id": None, "threads": 0})
    monkeypatch.setattr(_daemon.config, "is_configured", lambda home: True)
    log = tmp_path / "logs" / "enrich.log"

    monkeypatch.setattr(_daemon.drain, "drain", lambda *a, **k: {"files": 0, "applied": 0})
    _daemon.run_cycle(None, None, enrich_mode="spool")
    assert not log.exists()                                # empty drain -> no stamp

    monkeypatch.setattr(_daemon.drain, "drain", lambda *a, **k: {"files": 1, "applied": 3})
    _daemon.run_cycle(None, None, enrich_mode="spool")
    assert log.exists()                                   # productive drain -> stamped


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

def test_run_calls_passes_in_order(tmp_path, monkeypatch):
    """_run_periodic_passes() calls the _run_* methods in spec order via
    the CadencePass dispatch table:
    communities -> lint -> synthesise -> proactive -> waiting_on."""
    import json
    from unittest.mock import MagicMock

    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "S", "owner_email": "s@x.com", "orgs": [{"name": "O"}]}
    ))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
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
    # Patch the _run_X methods — the dispatch table calls these directly.
    daemon._run_communities = MagicMock(side_effect=lambda: call_order.append("communities"))
    daemon._run_lint = MagicMock(side_effect=lambda: call_order.append("lint"))
    daemon._run_synthesise = MagicMock(side_effect=lambda: call_order.append("synthesise"))
    daemon._run_proactive = MagicMock(side_effect=lambda: call_order.append("proactive"))
    daemon._run_waiting_on = MagicMock(side_effect=lambda: call_order.append("waiting_on"))

    daemon._run_periodic_passes()

    assert "communities" in call_order
    assert "lint" in call_order
    assert "synthesise" in call_order
    assert "proactive" in call_order
    assert "waiting_on" in call_order
    # Order: communities before lint before synthesise before proactive before waiting_on
    assert call_order.index("communities") < call_order.index("lint")
    assert call_order.index("lint") < call_order.index("synthesise")
    assert call_order.index("synthesise") < call_order.index("proactive")
    assert call_order.index("proactive") < call_order.index("waiting_on")


def test_run_one_pass_failure_does_not_block_others(tmp_path, monkeypatch):
    """If one _run_* pass raises unexpectedly, _run_periodic_passes catches it
    and the remaining passes still run."""
    import json
    from unittest.mock import MagicMock

    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "S", "owner_email": "s@x.com", "orgs": [{"name": "O"}]}
    ))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
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

    # Patch _run_X methods — the dispatch table calls these directly.
    # _run_lint raises directly (bypasses its internal try/except).
    daemon._run_communities = MagicMock(return_value={"communities": 0})
    daemon._run_lint = MagicMock(side_effect=RuntimeError("lint exploded"))
    daemon._run_synthesise = MagicMock(return_value={"synthesis_requested": 0})
    daemon._run_proactive = MagicMock(return_value={"project_no_next_action": 0})
    daemon._run_waiting_on = MagicMock(return_value={"cleared": 0})

    # Must not propagate the exception.
    daemon._run_periodic_passes()

    # All other passes still ran.
    daemon._run_communities.assert_called_once()
    daemon._run_synthesise.assert_called_once()
    daemon._run_proactive.assert_called_once()
    daemon._run_waiting_on.assert_called_once()


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


def test_cadences_from_config_absent_keys_map_to_defaults(tmp_path):
    from mcpbrain.daemon import _cadences_from_config, _CADENCE_DEFAULTS
    from mcpbrain import config

    config.write_config(str(tmp_path), {})  # no cadences block
    result = _cadences_from_config(str(tmp_path))
    # Absent keys map to defaults, except clickup_interval_s which has no default
    for key, val in result.items():
        if key == "clickup_interval_s":
            assert val is None
        else:
            assert val == _CADENCE_DEFAULTS.get(key)


def test_apply_config_rewires_cadences(tmp_path):
    from mcpbrain.daemon import Daemon

    store = _make_store(tmp_path, name="t7.db")
    emb = _FakeEmbedder()

    # Start with no cadences configured.
    daemon = Daemon(store, emb, interval_s=1.0)
    assert daemon._communities_interval_s is None

    new_config = {"cadences": {"communities_interval_s": 500.0}}

    with patch("mcpbrain.daemon._backup_from_config", return_value=(None, None)), \
         patch("mcpbrain.daemon.config.write_config"), \
         patch("mcpbrain.daemon.config.enrich_mode", return_value="static"), \
         patch("mcpbrain.daemon._cadences_from_config", return_value={
             "communities_interval_s": 500.0,
             "lint_interval_s": None,
             "synthesise_interval_s": None,
             "proactive_interval_s": None,
             "waiting_on_interval_s": None,
             "blocks_interval_s": None,
             "audit_interval_s": None,
             "clickup_interval_s": None,
             "stale_reextract_interval_s": None,
             "auto_update_interval_s": None,
             "verify_interval_s": None,
             "feedback_aggregate_interval_s": None,
             "org_backfill_interval_s": None,
         }):
        daemon.apply_config(new_config)

    assert daemon._communities_interval_s == 500.0
    assert daemon._lint_interval_s is None


# ---------------------------------------------------------------------------
# Task 8 — maybe_blocks and maybe_audit cadence tests
# ---------------------------------------------------------------------------

def _blocks_daemon(tmp_path, *, blocks_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path, name="blk.sqlite3")
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "dblk.lock"),
        blocks_interval_s=blocks_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def _audit_daemon(tmp_path, *, audit_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path, name="aud.sqlite3")
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "daud.lock"),
        audit_interval_s=audit_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_blocks_off_when_unconfigured(tmp_path):
    """blocks_interval_s not supplied -> maybe_blocks() returns None and
    build_profile_requests is never called."""
    store, daemon = _blocks_daemon(tmp_path)  # no blocks_interval_s

    with patch("mcpbrain.profile_synth.build_profile_requests") as mock_build:
        result = daemon.maybe_blocks()

    assert result is None
    mock_build.assert_not_called()


def test_maybe_blocks_runs_when_due(tmp_path):
    """blocks_interval_s set -> first call is due and returns summary with
    all three block counts. Stashes results in _pending_blocks."""
    store, daemon = _blocks_daemon(tmp_path, blocks_interval_s=3600.0)

    fake_profiles = [{"entity_id": "e-1", "name": "Alice"}]
    fake_communities = [{"community_id": "c-1"}]
    fake_distil = [{"memory_id": "m-1"}]

    with patch("mcpbrain.profile_synth.build_profile_requests",
               return_value=fake_profiles), \
         patch("mcpbrain.community_synth.build_community_requests",
               return_value=fake_communities), \
         patch("mcpbrain.memory_distil.build_distil_requests",
               return_value=fake_distil):
        result = daemon.maybe_blocks()

    assert result is not None
    assert result["profile_synthesis_requested"] == 1
    assert result["community_synthesis_requested"] == 1
    assert result["memory_distil_requested"] == 1
    assert daemon._pending_blocks == {
        "profile_synthesis": fake_profiles,
        "community_synthesis": fake_communities,
        "memory_distil": fake_distil,
    }


def test_maybe_blocks_not_due_before_interval(tmp_path):
    """After a successful run, a call before the interval elapses returns None."""
    clock = _Clock()
    store, daemon = _blocks_daemon(tmp_path, blocks_interval_s=100.0, clock=clock)

    with patch("mcpbrain.profile_synth.build_profile_requests", return_value=[]), \
         patch("mcpbrain.community_synth.build_community_requests", return_value=[]), \
         patch("mcpbrain.memory_distil.build_distil_requests", return_value=[]):
        first = daemon.maybe_blocks()
    assert first is not None

    clock.advance(50.0)
    with patch("mcpbrain.profile_synth.build_profile_requests") as mock_build:
        second = daemon.maybe_blocks()
    assert second is None
    mock_build.assert_not_called()


def test_maybe_blocks_swallows_errors(tmp_path):
    """build_profile_requests raising -> error dict returned, _last_blocks NOT advanced."""
    clock = _Clock()
    store, daemon = _blocks_daemon(tmp_path, blocks_interval_s=100.0, clock=clock)

    with patch("mcpbrain.profile_synth.build_profile_requests",
               side_effect=RuntimeError("blocks-boom")):
        result = daemon.maybe_blocks()

    assert result is not None
    assert "error" in result
    assert "blocks-boom" in result["error"]
    assert daemon._last_blocks is None


def test_maybe_audit_off_when_unconfigured(tmp_path):
    """audit_interval_s not supplied -> maybe_audit() returns None and
    build_audit_requests is never called."""
    store, daemon = _audit_daemon(tmp_path)  # no audit_interval_s

    with patch("mcpbrain.profile_audit.build_audit_requests") as mock_build:
        result = daemon.maybe_audit()

    assert result is None
    mock_build.assert_not_called()


def test_maybe_audit_runs_when_due(tmp_path):
    """audit_interval_s set -> first call is due and returns audit_requested count.
    Stashes requests in _pending_audit."""
    store, daemon = _audit_daemon(tmp_path, audit_interval_s=3600.0)

    fake_reqs = [{"entity_id": "e-1", "name": "Taryn", "role": "Executive Pastor",
                  "profile": "...", "org": "Acme"}]

    with patch("mcpbrain.profile_audit.build_audit_requests",
               return_value=fake_reqs) as mock_build:
        result = daemon.maybe_audit()

    assert result is not None
    assert result["audit_requested"] == 1
    assert daemon._pending_audit == {"profile_audit": fake_reqs}
    mock_build.assert_called_once_with(store)


def test_maybe_audit_not_due_before_interval(tmp_path):
    """After a successful run, a call before the interval elapses returns None."""
    clock = _Clock()
    store, daemon = _audit_daemon(tmp_path, audit_interval_s=100.0, clock=clock)

    with patch("mcpbrain.profile_audit.build_audit_requests", return_value=[]):
        first = daemon.maybe_audit()
    assert first is not None

    clock.advance(50.0)
    with patch("mcpbrain.profile_audit.build_audit_requests") as mock_build:
        second = daemon.maybe_audit()
    assert second is None
    mock_build.assert_not_called()


def test_maybe_audit_swallows_errors(tmp_path):
    """build_audit_requests raising -> error dict returned, _last_audit NOT advanced."""
    clock = _Clock()
    store, daemon = _audit_daemon(tmp_path, audit_interval_s=100.0, clock=clock)

    with patch("mcpbrain.profile_audit.build_audit_requests",
               side_effect=RuntimeError("audit-boom")):
        result = daemon.maybe_audit()

    assert result is not None
    assert "error" in result
    assert "audit-boom" in result["error"]
    assert daemon._last_audit is None


def test_pending_blocks_and_audit_merged_in_run_one(tmp_path):
    """run_one() passes merged _pending_blocks + _pending_audit as extra_blocks,
    re-attaching every cycle until the drain reports each key's answers."""
    store, daemon = _blocks_daemon(tmp_path)

    daemon._pending_blocks = {"profile_synthesis": [{"entity_id": "e-1"}],
                              "community_synthesis": []}   # empty: filtered out
    daemon._pending_audit = {"profile_audit": [{"entity_id": "e-2"}]}

    seen = []

    def fake_run_cycle(store, embedder, *, extra_blocks=None, **kw):
        seen.append(extra_blocks)
        return {"enrich": {"mode": "spool", "drain": {}}}

    # Two cycles with no block answers drained: re-attached BOTH times
    # (prepare rewrites pending.json each cycle; a one-shot attach is lost).
    with patch("mcpbrain.daemon.run_cycle", side_effect=fake_run_cycle):
        daemon.run_one()
        daemon.run_one()
    assert seen == [{
        "profile_synthesis": [{"entity_id": "e-1"}],
        "profile_audit": [{"entity_id": "e-2"}],
    }] * 2
    assert daemon._pending_blocks.get("profile_synthesis")
    assert daemon._pending_audit.get("profile_audit")


def test_pending_blocks_cleared_per_key_when_drained(tmp_path):
    """A key's stash is cleared once the drain summary carries <key>_drained;
    unanswered keys stay stashed and keep re-attaching."""
    store, daemon = _blocks_daemon(tmp_path)
    daemon._pending_blocks = {"profile_synthesis": [{"entity_id": "e-1"}],
                              "memory_distil": [{"doc_id": "note-1"}]}
    daemon._pending_audit = {"profile_audit": [{"entity_id": "e-2"}]}

    with patch("mcpbrain.daemon.run_cycle",
               return_value={"enrich": {"mode": "spool",
                                        "drain": {"profile_synthesis_drained": 1,
                                                  "profile_audit_drained": 1}}}):
        daemon.run_one()
    assert "profile_synthesis" not in daemon._pending_blocks
    assert daemon._pending_audit == {}
    assert daemon._pending_blocks.get("memory_distil")  # unanswered: kept

    seen = []

    def fake_run_cycle(store, embedder, *, extra_blocks=None, **kw):
        seen.append(extra_blocks)
        return {"enrich": {"mode": "spool",
                           "drain": {"memory_distil_drained": 2}}}

    with patch("mcpbrain.daemon.run_cycle", side_effect=fake_run_cycle):
        daemon.run_one()
    assert seen == [{"memory_distil": [{"doc_id": "note-1"}]}]
    assert daemon._pending_blocks == {}

    # Nothing left: extra_blocks goes back to None.
    with patch("mcpbrain.daemon.run_cycle", side_effect=fake_run_cycle):
        daemon.run_one()
    assert seen[-1] is None


def test_pending_blocks_cleared_when_drained_is_zero(tmp_path):
    """A <key>_drained value of 0 (answers consumed, nothing changed) still
    clears the stash — clearing keys on presence, not truthiness."""
    store, daemon = _blocks_daemon(tmp_path)
    daemon._pending_blocks = {"memory_distil": [{"doc_id": "note-1"}]}
    daemon._pending_audit = {"profile_audit": [{"entity_id": "e-2"}]}

    with patch("mcpbrain.daemon.run_cycle",
               return_value={"enrich": {"mode": "spool",
                                        "drain": {"memory_distil_drained": 0,
                                                  "profile_audit_drained": 0}}}):
        daemon.run_one()
    assert daemon._pending_blocks == {}
    assert daemon._pending_audit == {}


# ---------------------------------------------------------------------------
# Task 4 (stale-autoclose) — maybe_stale_reextract cadence tests
# ---------------------------------------------------------------------------

def _stale_daemon(tmp_path, *, stale_reextract_interval_s=None, clock=None, **kw):
    store = _make_store(tmp_path)
    return store, Daemon(
        store, _FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "d.lock"),
        stale_reextract_interval_s=stale_reextract_interval_s,
        clock=clock or _Clock(),
        **kw,
    )


def test_maybe_stale_reextract_off_when_unconfigured(tmp_path):
    store, daemon = _stale_daemon(tmp_path)  # no interval
    with patch("mcpbrain.stale_reextract.sweep") as mock_sweep:
        result = daemon.maybe_stale_reextract()
    assert result is None
    mock_sweep.assert_not_called()


def test_maybe_stale_reextract_runs_when_due(tmp_path):
    store, daemon = _stale_daemon(tmp_path, stale_reextract_interval_s=86400.0)
    fake = {"triggered": 1, "deferred": 0, "threads": ["T1"]}
    with patch("mcpbrain.stale_reextract.sweep", return_value=fake) as mock_sweep:
        result = daemon.maybe_stale_reextract()
    assert result == fake
    mock_sweep.assert_called_once()
    assert "now" in mock_sweep.call_args[1]   # now= passed as kwarg


def test_maybe_stale_reextract_swallows_errors(tmp_path):
    clock = _Clock()
    store, daemon = _stale_daemon(tmp_path, stale_reextract_interval_s=100.0,
                                  clock=clock)
    with patch("mcpbrain.stale_reextract.sweep",
               side_effect=RuntimeError("boom")):
        result = daemon.maybe_stale_reextract()
    assert result["stale_reextract"] is False
    # _last not advanced -> next call retries
    assert daemon._last_stale_reextract is None


def test_maybe_stale_reextract_not_due_before_interval(tmp_path):
    """After a successful run, a call within the interval returns None."""
    clock = _Clock()
    store, daemon = _stale_daemon(tmp_path, stale_reextract_interval_s=100.0,
                                  clock=clock)
    fake = {"triggered": 0, "deferred": 0, "threads": []}
    with patch("mcpbrain.stale_reextract.sweep", return_value=fake):
        first = daemon.maybe_stale_reextract()
    assert first is not None

    clock.advance(50.0)
    with patch("mcpbrain.stale_reextract.sweep") as mock_sweep:
        second = daemon.maybe_stale_reextract()
    assert second is None
    mock_sweep.assert_not_called()
