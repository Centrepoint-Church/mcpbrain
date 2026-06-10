"""Offline tests for bin/dry_run_spool.py — the spool dry-run orchestrator.

The orchestrator runs the full spool loop (prepare -> extract -> drain ->
re-drain) against a real Store and reports PASS/FAIL. These tests exercise the
real Phase-1 seams over seeded chunks (mirroring
test_integration_spool.test_real_phase1_round_trip), inject a fake run_claude
so no live Claude/claude_pool is touched, and assert the graph grew, the second
drain is a no-op, and the Gemini tripwire fired never (and fires when provoked).

bin/ is not a package, so the script is loaded via importlib from its path.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from mcpbrain import enrich
from mcpbrain.store import Store
from tests.helpers import stub_extractor

_SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "dry_run_spool.py"


def _load_orchestrator():
    spec = importlib.util.spec_from_file_location("dry_run_spool", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dry_run_spool = _load_orchestrator()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "enrich_inbox").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


def _seed_real_chunk(store, doc_id, thread_id, message_id, *, sender, subject,
                     date="2026-04-18", labels="INBOX"):
    """One un-enriched chunk with full message provenance, mirroring the real
    Gmail sync so the real Phase-1 seams pick it up."""
    store.upsert_chunk(
        doc_id, "Can you confirm Hall B is free for Wednesday college?",
        f"hash-{doc_id}",
        {"thread_id": thread_id, "message_id": message_id, "sender": sender,
         "subject": subject, "date": date, "labels": labels, "chunk_index": 0},
    )


def _fake_run_claude(prompt, *, model=None, timeout=None):
    """Parse the pending payload off the prompt tail and build a valid batch."""
    pending_text = prompt.split("=== pending.json ===")[-1]
    pending = json.loads(pending_text)
    return json.dumps(stub_extractor.build_batch(pending))


def test_dry_run_grows_graph_and_is_idempotent(store, home):
    _seed_real_chunk(store, "d-human", "t-human", "m-human-1",
                     sender="Joel Chelliah <joel@example.org>",
                     subject="Hall B for Wednesday college")

    result = dry_run_spool.run_dry_run(
        store, home=home, run_claude=_fake_run_claude, thread_cap=3)

    assert result["prepared"] >= 1
    assert result["extracted_path"] is not None
    assert result["drain1"]["applied"] >= 1
    # apply-processed counts are the gate signal: the real apply links entities
    # for this thread, so apply_entities > 0 even when net store delta may be 0.
    assert result["apply_entities"] > 0
    assert result["apply_entities"] == result["drain1"]["entities"]
    assert result["apply_relations"] == result["drain1"]["relations"]
    assert result["entities_after"] > result["entities_before"]
    assert result["relations_after"] > result["relations_before"]
    assert result["drain2_noop"] is True
    assert result["gemini_called"] is False


def test_print_report_gate_uses_apply_counts(capsys):
    """The activity gate keys off apply-processed counts, not net store delta.

    A no-op apply (applied>0 but apply_entities+relations==0) FAILS; a
    known-entity thread (apply_entities>0 with zero net store delta) PASSES.
    """
    base = {
        "prepared": 1,
        "extracted_path": "inbox.json",
        "drain1": {"files": 1, "applied": 1, "marked": 1, "merges": 0,
                   "quarantined": 0, "entities": 0, "relations": 0},
        "drain2": {"files": 0, "applied": 0, "marked": 0, "merges": 0,
                   "quarantined": 0, "entities": 0, "relations": 0},
        "entities_before": 5, "entities_after": 5,
        "relations_before": 3, "relations_after": 3,
        "drain2_noop": True,
        "gemini_called": False,
    }

    # No-op apply: nothing written -> activity FAILS -> overall False.
    noop = dict(base, apply_entities=0, apply_relations=0)
    noop["drain1"] = dict(base["drain1"], entities=0, relations=0)
    assert dry_run_spool._print_report(noop, home=None) is False

    # Known-entity thread: apply linked 2 entities, net store delta still 0.
    known = dict(base, apply_entities=2, apply_relations=0)
    known["drain1"] = dict(base["drain1"], applied=1, quarantined=0,
                           entities=2, relations=0)
    assert dry_run_spool._print_report(known, home=None) is True


def test_dry_run_nothing_to_enrich(store, home):
    """No chunks seeded -> prepare yields zero threads -> reported, not crashed."""
    result = dry_run_spool.run_dry_run(
        store, home=home, run_claude=_fake_run_claude, thread_cap=3)

    assert result["prepared"] == 0
    assert result["extracted_path"] is None
    assert result["drain2_noop"] is True
    assert result["gemini_called"] is False
    # nothing-to-do is a valid outcome: _print_report should return True.
    assert dry_run_spool._print_report(result, home=None) is True


def test_gemini_tripwire_fires_if_client_constructed(store, home, monkeypatch):
    """Prove the tripwire works: a run_claude that touches the Gemini client
    constructor mid-run must raise AssertionError, and the constructor must be
    restored afterwards."""
    original = enrich.make_gemini_client

    def run_claude_that_builds_gemini(prompt, *, model=None, timeout=None):
        enrich.make_gemini_client("fake-key")  # should hit the tripwire
        return _fake_run_claude(prompt, model=model, timeout=timeout)

    _seed_real_chunk(store, "d-human", "t-human", "m-human-1",
                     sender="Joel Chelliah <joel@example.org>",
                     subject="Hall B for Wednesday college")

    with pytest.raises(AssertionError, match="Gemini constructed during spool dry-run"):
        dry_run_spool.run_dry_run(
            store, home=home, run_claude=run_claude_that_builds_gemini, thread_cap=3)

    # Tripwire restored the real constructor in its finally block.
    assert enrich.make_gemini_client is original
