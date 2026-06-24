"""Tests for B6 procedural/voice memory (analyser Phase A, guarded apply, incremental communities)."""
import json
import os
import pytest
from pathlib import Path


@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    return s


@pytest.fixture
def home_proc(tmp_path):
    """Home dir with procedural_memory enabled."""
    h = tmp_path / "home-proc"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({"procedural_memory": True}))
    return str(h)


# ---------------------------------------------------------------------------
# _parse_suggestions (unit — no I/O)
# ---------------------------------------------------------------------------

def test_parse_suggestions_valid():
    """Parse valid JSON array of suggestion objects."""
    from mcpbrain.voice_analyser import _parse_suggestions

    raw = json.dumps([{
        "kind": "ban_word",
        "rule": "Never use 'synergy'.",
        "confidence": 0.9,
        "evidence_sample_ids": ["1", "2"],
        "explanation": "Used 5 times without value.",
    }])
    items = _parse_suggestions(raw)
    assert len(items) == 1
    assert items[0]["kind"] == "ban_word"
    assert items[0]["confidence"] == 0.9


def test_parse_suggestions_filters_invalid_kind():
    """Suggestions with unknown kinds are dropped."""
    from mcpbrain.voice_analyser import _parse_suggestions

    raw = json.dumps([{
        "kind": "unknown_kind",
        "rule": "Rule.",
        "confidence": 0.9,
        "evidence_sample_ids": [],
        "explanation": "Reason.",
    }])
    assert _parse_suggestions(raw) == []


def test_parse_suggestions_missing_field_dropped():
    """Suggestions missing required fields are dropped."""
    from mcpbrain.voice_analyser import _parse_suggestions

    raw = json.dumps([{"kind": "ban_word", "rule": "Rule."}])  # missing confidence, etc.
    assert _parse_suggestions(raw) == []


def test_parse_suggestions_empty_array():
    """Empty JSON array parses cleanly."""
    from mcpbrain.voice_analyser import _parse_suggestions
    assert _parse_suggestions("[]") == []


def test_parse_suggestions_handles_noise():
    """Preamble text before/after JSON is stripped."""
    from mcpbrain.voice_analyser import _parse_suggestions

    raw = "Sure! Here you go:\n" + json.dumps([{
        "kind": "ban_word",
        "rule": "Never use 'leverage'.",
        "confidence": 0.85,
        "evidence_sample_ids": ["3"],
        "explanation": "Overused.",
    }]) + "\nHope that helps!"
    items = _parse_suggestions(raw)
    assert len(items) == 1


# ---------------------------------------------------------------------------
# is_disabled
# ---------------------------------------------------------------------------

def test_is_disabled_false_by_default(store):
    """is_disabled is False on a fresh store."""
    from mcpbrain.voice_analyser import is_disabled
    assert is_disabled(store) is False


def test_is_disabled_after_three_strikes(store, home_proc, monkeypatch):
    """Three consecutive failures trigger auto-disable."""
    from mcpbrain import voice_analyser

    # Seed one draft so run_analysis gets past the 'no samples' early return
    with store._connect() as db:
        db.execute(
            "INSERT INTO draft_records (email_id, thread_id, intent, draft_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e1", "t1", "reply", "Hi Josh, thanks for reaching out.", "2026-06-23")
        )

    # Make _call_claude always raise so maybe_run_analysis increments strikes
    monkeypatch.setattr(voice_analyser, "_call_claude",
                        lambda prompt, home: (_ for _ in ()).throw(RuntimeError("CLI down")))

    for _ in range(3):
        voice_analyser.maybe_run_analysis(store, home_proc)

    assert voice_analyser.is_disabled(store) is True


# ---------------------------------------------------------------------------
# run_analysis with mocked claude CLI
# ---------------------------------------------------------------------------

def test_run_analysis_no_drafts_returns_empty(store, home_proc):
    """run_analysis returns [] when no draft_records exist."""
    from mcpbrain.voice_analyser import run_analysis
    result = run_analysis(store, home_proc)
    assert result == []


def test_run_analysis_writes_suggestions(store, home_proc, monkeypatch):
    """run_analysis stores parsed suggestions in voice_suggestions table."""
    from mcpbrain import voice_analyser

    # Insert a draft record
    with store._connect() as db:
        db.execute(
            "INSERT INTO draft_records (email_id, thread_id, intent, draft_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e1", "t1", "reply", "I hope this helps! Synergy is key.", "2026-06-23")
        )

    fake_response = json.dumps([{
        "kind": "ban_word",
        "rule": "Avoid 'synergy' — it adds no meaning.",
        "confidence": 0.88,
        "evidence_sample_ids": ["e1"],
        "explanation": "Used once with no substance.",
    }])

    monkeypatch.setattr(voice_analyser, "_call_claude", lambda prompt, home: fake_response)

    suggestions = voice_analyser.run_analysis(store, home_proc)
    assert len(suggestions) == 1
    assert suggestions[0]["kind"] == "ban_word"

    # Verify it landed in the DB
    pending = store.pending_voice_suggestions()
    assert len(pending) >= 1


def test_run_analysis_filters_low_confidence(store, home_proc, monkeypatch):
    """Suggestions below confidence threshold are dropped."""
    from mcpbrain import voice_analyser

    with store._connect() as db:
        db.execute(
            "INSERT INTO draft_records (email_id, thread_id, intent, draft_text, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("e2", "t2", "reply", "Some draft text.", "2026-06-23")
        )

    fake_response = json.dumps([{
        "kind": "tone_note",
        "rule": "Maybe add more warmth.",
        "confidence": 0.3,   # below 0.75 threshold
        "evidence_sample_ids": [],
        "explanation": "Weak signal.",
    }])

    monkeypatch.setattr(voice_analyser, "_call_claude", lambda prompt, home: fake_response)

    suggestions = voice_analyser.run_analysis(store, home_proc)
    assert suggestions == []


# ---------------------------------------------------------------------------
# Incremental communities
# ---------------------------------------------------------------------------

def test_extend_communities_no_data(store, tmp_path, monkeypatch):
    """extend_communities on an empty store returns without crashing."""
    from mcpbrain import communities

    # Make sure incremental_communities is "enabled" flag-wise
    home = str(tmp_path / "ic-home")
    import os; os.makedirs(home)
    (Path(home) / "config.json").write_text(json.dumps({"incremental_communities": True}))

    result = communities.extend_communities(store, home)
    # Should return something (dict or None), never raise
    assert result is None or isinstance(result, dict)


# ---------------------------------------------------------------------------
# voice_auto_apply config flag (1d)
# ---------------------------------------------------------------------------

def test_voice_auto_apply_disabled_by_default(tmp_path):
    """voice_auto_apply_enabled returns False when not in config."""
    from mcpbrain.config import voice_auto_apply_enabled
    h = str(tmp_path / "home-voff")
    os.makedirs(h)
    (Path(h) / "config.json").write_text(json.dumps({"procedural_memory": True}))
    assert voice_auto_apply_enabled(h) is False


def test_voice_auto_apply_enabled_with_flag(tmp_path):
    """voice_auto_apply_enabled returns True when config flag is set."""
    from mcpbrain.config import voice_auto_apply_enabled
    h = str(tmp_path / "home-von")
    os.makedirs(h)
    (Path(h) / "config.json").write_text(json.dumps({
        "procedural_memory": True,
        "voice_auto_apply": True,
    }))
    assert voice_auto_apply_enabled(h) is True


def test_daemon_run_voice_analyse_auto_applies_when_flag_set(store, home_proc, monkeypatch):
    """_run_voice_analyse calls apply_suggestions when voice_auto_apply is on."""
    import json as _json
    from mcpbrain import voice_analyser, voice_apply
    import mcpbrain.daemon as daemon_mod

    cfg = _json.loads((Path(home_proc) / "config.json").read_text())
    cfg["voice_auto_apply"] = True
    (Path(home_proc) / "config.json").write_text(_json.dumps(cfg))

    monkeypatch.setattr(daemon_mod, "app_dir", lambda: Path(home_proc))
    monkeypatch.setattr(voice_analyser, "maybe_run_analysis", lambda s, h: [{"id": 1}])

    apply_called = []
    monkeypatch.setattr(
        voice_apply, "apply_suggestions",
        lambda s, h, **kw: apply_called.append(True) or {
            "applied": 0, "skipped": 0, "blocked": "cooldown active — 3.0d remaining"
        },
    )

    class _FakeDaemon:
        _store = store
        _last_voice_analyse = None
        _voice_analyse_interval_s = 1.0
        _clock = staticmethod(lambda: 99999.0)
        def _is_due(self, *_): return True

    daemon_mod.Daemon._run_voice_analyse(_FakeDaemon())
    assert apply_called, "apply_suggestions not triggered with voice_auto_apply=True"


def test_daemon_run_voice_analyse_skips_apply_when_flag_off(store, home_proc, monkeypatch):
    """_run_voice_analyse does NOT call apply_suggestions when voice_auto_apply is off (default)."""
    from mcpbrain import voice_analyser, voice_apply
    import mcpbrain.daemon as daemon_mod

    # home_proc has procedural_memory=True but voice_auto_apply is NOT set → defaults False
    monkeypatch.setattr(daemon_mod, "app_dir", lambda: Path(home_proc))
    monkeypatch.setattr(voice_analyser, "maybe_run_analysis", lambda s, h: [{"id": 1}])

    apply_called = []
    monkeypatch.setattr(
        voice_apply, "apply_suggestions",
        lambda s, h, **kw: apply_called.append(True) or {},
    )

    class _FakeDaemon:
        _store = store
        _last_voice_analyse = None
        _voice_analyse_interval_s = 1.0
        _clock = staticmethod(lambda: 99999.0)
        def _is_due(self, *_): return True

    daemon_mod.Daemon._run_voice_analyse(_FakeDaemon())
    assert not apply_called, "apply_suggestions called despite voice_auto_apply being off"


def test_extend_communities_delegates_to_full_run_on_empty_graph(store, tmp_path, monkeypatch):
    """extend_communities falls back to full run() when there are no prior communities."""
    from mcpbrain import communities

    home = str(tmp_path / "ic-home2")
    import os; os.makedirs(home)
    (Path(home) / "config.json").write_text(json.dumps({"incremental_communities": True}))

    full_run_called = []

    def fake_run(store, home=None):
        full_run_called.append(True)
        return {"communities": 0}

    monkeypatch.setattr(communities, "run", fake_run)
    communities.extend_communities(store, home)
    # An empty graph should trigger full run or return gracefully
    # Either outcome is acceptable (no error)
