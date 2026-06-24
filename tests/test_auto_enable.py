"""Auto-graduation: data-gated flags flip ON only when genuinely ready, never
override an explicit user setting, and decay is safety-gated by a dry-run."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from mcpbrain.store import Store
from mcpbrain import auto_enable


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _home(tmp_path, **cfg):
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    (h / "config.json").write_text(json.dumps(cfg))
    return str(h)


def _seed_accept(store, n, *, span_days):
    """Insert n 'used' accept events spanning span_days."""
    now = datetime.now(timezone.utc)
    with store._connect() as db:
        for i in range(n):
            ts = (now - timedelta(days=span_days * i / max(n - 1, 1))).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute("INSERT INTO recall_feedback(doc_id, event_type, ts) VALUES(?,?,?)",
                       (f"d{i}", "used", ts))


def test_not_ready_with_thin_accept_signal(store, tmp_path):
    home = _home(tmp_path)
    _seed_accept(store, 5, span_days=10)        # only 5 events
    r = auto_enable.auto_enable_pass(store, home)
    assert "bandit_auto_apply" not in r["enabled"]
    assert "lessons" not in r["enabled"]


def test_graduates_bandit_and_lessons_when_ready(store, tmp_path):
    home = _home(tmp_path)
    _seed_accept(store, 60, span_days=10)        # >= 50 events over >= 7 days
    r = auto_enable.auto_enable_pass(store, home)
    assert set(r["enabled"]) >= {"bandit_auto_apply", "lessons"}
    # persisted to config.json
    cfg = json.loads((tmp_path / "home" / "config.json").read_text())
    assert cfg["bandit_auto_apply"] is True and cfg["lessons"] is True


def test_enough_events_but_too_short_span_not_ready(store, tmp_path):
    home = _home(tmp_path)
    _seed_accept(store, 60, span_days=2)         # plenty of events, only 2 days
    r = auto_enable.auto_enable_pass(store, home)
    assert "bandit_auto_apply" not in r["enabled"]


def test_never_overrides_explicit_user_setting(store, tmp_path):
    # user explicitly turned bandit OFF — auto-enable must respect it forever
    home = _home(tmp_path, bandit_auto_apply=False)
    _seed_accept(store, 100, span_days=30)
    r = auto_enable.auto_enable_pass(store, home)
    assert "bandit_auto_apply" not in r["enabled"]
    cfg = json.loads((tmp_path / "home" / "config.json").read_text())
    assert cfg["bandit_auto_apply"] is False


def test_disabled_when_auto_enable_off(store, tmp_path):
    home = _home(tmp_path, auto_enable=False)
    _seed_accept(store, 100, span_days=30)
    r = auto_enable.auto_enable_pass(store, home)
    assert r["enabled"] == []


def test_decay_safety_gate_blocks_when_projection_too_high(store, tmp_path):
    """Even with access data, decay must NOT graduate if a dry-run would cold-tier
    more than the cap. Seed many old, low-salience, never-accessed chunks → high
    projected demotion → decay stays off."""
    # lower thresholds so the access-count/time gate passes and the dry-run decides
    home = _home(tmp_path, auto_enable_thresholds={
        "decay_access_min": 1, "decay_min_days": 0, "decay_max_fraction": 0.40})
    old = "2020-01-01T00:00:00Z"
    with store._connect() as db:
        # one accessed chunk so the access gate clears
        db.execute("INSERT INTO chunk_quality(doc_id, last_accessed) VALUES('a0', ?)",
                   (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        for i in range(50):
            db.execute(
                "INSERT INTO chunks(doc_id, text, content_hash, metadata, embedded, salience, memory_tier) "
                "VALUES(?,?,?,?,1,1.0,'warm')",
                (f"old{i}", "x", f"h{i}", json.dumps({"source_type": "gmail", "date": old})))
    r = auto_enable.auto_enable_pass(store, home)
    assert "decay" not in r["enabled"]
    assert r["readiness"]["decay"]["ready"] is False
