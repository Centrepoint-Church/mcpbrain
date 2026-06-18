"""Tests for run_cycle's enrich_mode branch (spool | off).

run_cycle picks the enrichment source from enrich_mode:
  - spool  -> prepare.prepare then drain.drain (extractor runs out of band)
  - off    -> neither path runs

The spool path resolves graph_write.apply through daemon._graph_apply, retained
as a monkeypatch surface. These tests patch _graph_apply to a stub to avoid
driving the full graph write. prepare and drain are spied via monkeypatch on
the daemon module's references to them.
"""

import mcpbrain.daemon as daemon_module
from mcpbrain.daemon import Daemon, SingleWriterLock, run_cycle
from mcpbrain.store import Store


class FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] for _ in texts]


class FakeStore:
    """Minimal store: run_sync_cycle with no services touches nothing."""

    def __init__(self, unenriched=None):
        self._unenriched = unenriched or []

    def unenriched_chunks(self, limit=None):
        return self._unenriched if limit is None else self._unenriched[:limit]


def _spy(calls, name):
    def fn(*args, **kwargs):
        calls.append((name, args, kwargs))
        return {}
    return fn


def test_run_cycle_spool_drains(monkeypatch):
    """spool: run_cycle calls prepare then drain."""
    calls = []
    monkeypatch.setattr(daemon_module.prepare, "prepare_units", _spy(calls, "prepare_units"))
    monkeypatch.setattr(daemon_module.drain, "drain", _spy(calls, "drain"))
    stub_apply = object()
    monkeypatch.setattr(daemon_module, "_graph_apply", lambda: stub_apply)

    store = FakeStore()
    res = run_cycle(store, FakeEmbedder(), enrich_mode="spool")

    names = [c[0] for c in calls]
    assert names == ["prepare_units", "drain"]
    # drain received the lazily-resolved apply and the embedder.
    drain_call = next(c for c in calls if c[0] == "drain")
    assert drain_call[2]["apply"] is stub_apply
    assert "spool" in res["enrich"]["mode"]


def test_run_cycle_spool_passes_resolution_due(monkeypatch):
    """spool: resolution_due flows through to prepare so the merge-review block
    is appended exactly when the deterministic resolve tier would also fire."""
    calls = []
    monkeypatch.setattr(daemon_module.prepare, "prepare_units", _spy(calls, "prepare_units"))
    monkeypatch.setattr(daemon_module.drain, "drain", _spy(calls, "drain"))
    monkeypatch.setattr(daemon_module, "_graph_apply", lambda: object())

    run_cycle(FakeStore(), FakeEmbedder(), enrich_mode="spool", resolution_due=True)

    prepare_call = next(c for c in calls if c[0] == "prepare_units")
    assert prepare_call[2]["resolution_due"] is True


def test_run_cycle_off_skips_enrich(monkeypatch):
    """off: neither the spool path runs."""
    calls = []
    monkeypatch.setattr(daemon_module.prepare, "prepare_units", _spy(calls, "prepare_units"))
    monkeypatch.setattr(daemon_module.drain, "drain", _spy(calls, "drain"))

    res = run_cycle(FakeStore(), FakeEmbedder(), enrich_mode="off")

    assert calls == []
    assert res["enrich"]["mode"] == "off"


def test_run_cycle_default_is_off(monkeypatch):
    """No enrich_mode arg defaults to "off" (matching config.enrich_mode), so a
    caller that forgets to pass a mode does NOT silently run any enrichment path.
    The live daemon resolves the real mode from config and passes it in."""
    calls = []
    monkeypatch.setattr(daemon_module.prepare, "prepare_units", _spy(calls, "prepare_units"))
    monkeypatch.setattr(daemon_module.drain, "drain", _spy(calls, "drain"))

    run_cycle(FakeStore(), FakeEmbedder())

    assert calls == []  # no enrichment runs by default


# ---------------------------------------------------------------------------
# End-to-end wiring: enrich_mode flows constructor -> run_one -> run_cycle
# ---------------------------------------------------------------------------

def _make_daemon_for_run_one(tmp_path, enrich_mode):
    """Build a minimal Daemon wired for run_one calls.

    Mirrors the _make_daemon helper in test_control_api_post.py: Store from a
    tmp sqlite3, FakeEmbedder (dim=4), explicit services={} so no auth is
    attempted, and an explicit lock path so the POSIX lock goes under tmp_path.
    """
    store = Store(tmp_path / "modes.sqlite3", dim=4)
    store.init()
    return Daemon(
        store,
        FakeEmbedder(),
        services={},
        lock=SingleWriterLock(tmp_path / "modes.lock"),
        enrich_mode=enrich_mode,
    )


def test_run_one_off_skips_enrich(tmp_path, monkeypatch):
    """enrich_mode="off" constructed on the Daemon flows through run_one into
    run_cycle: neither spool (prepare/drain) runs, and the result carries
    enrich.mode=="off".

    _graph_apply is also patched as a belt-and-braces guard (the off branch
    should never reach it, but the patch ensures a missing graph_write import
    cannot hide a routing bug).
    """
    calls = []
    monkeypatch.setattr(daemon_module.prepare, "prepare_units", _spy(calls, "prepare_units"))
    monkeypatch.setattr(daemon_module.drain, "drain", _spy(calls, "drain"))
    monkeypatch.setattr(daemon_module, "_graph_apply", lambda: object())

    daemon = _make_daemon_for_run_one(tmp_path, enrich_mode="off")
    result = daemon.run_one()

    assert result is not None
    assert calls == [], "off mode must not call prepare or drain"
    assert result["enrich"]["mode"] == "off"
