"""The reflow seed cadence: tops the reflow sync_queue up to REFLOW_WINDOW,
gated on the kill switch, the halt flag, and a backup that succeeded within
the last 24h.

Store.reflow_candidates/reflow_stats/enqueue_items and the selector rules are
unit 1d's (mcpbrain/store.py) and already tested in tests/test_reflow_selector.py
-- this file only covers the daemon cadence built on top of them.
"""
import json
import time

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _c(s, doc_id, **md):
    s.upsert_chunk(doc_id, "t " + doc_id, doc_id, md)


def test_seed_requires_recent_backup_and_tops_up_window(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    d = dmod.Daemon.__new__(dmod.Daemon)          # minimal instance, as other cadence tests do
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "no_recent_backup"}
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d._last_reflow_seed = None
    assert d._run_reflow_seed()["enqueued"] == 1


def test_seed_registered_and_defaults(tmp_path):
    from mcpbrain import daemon as dmod
    assert "reflow_seed" in {cp.name for cp in dmod._CADENCE_PASSES}
    assert dmod._CADENCE_DEFAULTS["reflow_seed_interval_s"] == 3600.0
    assert "reflow_seed_interval_s" in dmod._CADENCE_KEYS
    assert dmod._cadences_from_config(str(tmp_path))["reflow_seed_interval_s"] == 3600.0


def test_seed_not_due_returns_none(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = None, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() is None


def test_seed_disabled_by_kill_switch(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    assert d._run_reflow_seed() == {"reflow_seed": "disabled"}


def test_seed_halted(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "halted"}


def test_seed_stale_backup_still_gates(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    stale = time.time() - dmod.REFLOW_BACKUP_MAX_AGE_S - 10
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": stale}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "no_recent_backup"}


def test_seed_window_full_reports_zero_enqueued(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    items = [{"ref_id": f"F{i}", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}
             for i in range(dmod.REFLOW_WINDOW)]
    s.enqueue_items(items, source="reflow:drive")
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "window_full", "enqueued": 0}


def test_seed_backlog_empty_runs_integrity_check_once(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    calls = []

    def _fake_check(home):
        calls.append(home)
        return []

    import mcpbrain.doctor as doctor_mod
    monkeypatch.setattr(doctor_mod, "_run_integrity_check", _fake_check)
    out = d._run_reflow_seed()
    assert out == {"reflow_seed": "ok", "enqueued": 0}
    assert calls == [str(tmp_path)]
    assert s.get_cursor("reflow:integrity_checked") == "ok"

    # Second call must not re-run the check.
    d._last_reflow_seed = None
    d._run_reflow_seed()
    assert calls == [str(tmp_path)]
