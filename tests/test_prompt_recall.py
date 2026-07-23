"""UserPromptSubmit auto-recall hook: flag-gating, skip rules, formatting/caps,
de-dup, and fail-open. Never hits the network — _recall is monkeypatched."""
import io
import json
from pathlib import Path

from mcpbrain import prompt_recall as pr


def _run(home, hook, *, out=None, now=1000.0):
    out = out or io.StringIO()
    pr.user_prompt_submit(str(home), stdin=io.StringIO(json.dumps(hook)), out=out, now=now)
    return out.getvalue()


def _hits(*pairs):
    # pairs of (doc_id, score, text)
    return [{"doc_id": d, "score": s, "text": t} for d, s, t in pairs]


def test_flag_off_is_instant_noop(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"prompt_recall": False}))
    called = {"n": 0}
    monkeypatch.setattr(pr, "_recall", lambda *a, **k: called.update(n=called["n"] + 1) or [])
    # stdin that would raise if read past the flag-gate -> proves no read happens
    pr.user_prompt_submit(str(tmp_path), stdin=io.StringIO("garbage"), out=io.StringIO())
    assert called["n"] == 0


def test_default_on_when_flag_absent(tmp_path, monkeypatch):
    # no config.json at all -> prompt_recall defaults ON
    monkeypatch.setattr(pr, "_recall",
                        lambda home, q: _hits(("d1", 1.0, "Taryn leads the launch team")))
    out = _run(tmp_path, {"prompt": "who leads the launch team?", "session_id": "s1"})
    assert "launch team" in out
    assert "additionalContext" in out


def test_worth_recalling_rules():
    assert pr._worth_recalling("who is on the launch team?")
    assert not pr._worth_recalling("")
    assert not pr._worth_recalling("hi")           # too short
    assert not pr._worth_recalling("/clear extra") # slash command


def test_slash_and_short_prompts_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_recall", lambda home, q: _hits(("d1", 1.0, "x" * 50)))
    assert _run(tmp_path, {"prompt": "/compact", "session_id": "s"}) == ""
    assert _run(tmp_path, {"prompt": "yes", "session_id": "s"}) == ""


def test_fail_open_when_recall_empty(tmp_path, monkeypatch):
    # simulates daemon down / timeout (._recall returns [])
    monkeypatch.setattr(pr, "_recall", lambda home, q: [])
    assert _run(tmp_path, {"prompt": "a substantive question about the migration",
                           "session_id": "s"}) == ""


def test_format_relative_floor_trims_weak_tail():
    hits = _hits(("d1", 1.0, "strong hit alpha"),
                 ("d2", 0.9, "strong hit beta"),
                 ("d3", 0.2, "weak tail gamma"))
    block, injected = pr._format_context(hits, seen=set())
    assert "alpha" in block and "beta" in block
    assert "gamma" not in block          # below _REL_FLOOR * top
    assert list(injected) == ["d1", "d2"]
    assert injected["d1"] == "strong hit alpha"   # snippet text persisted


def test_format_caps_count_and_snippet():
    hits = _hits(*[(f"d{i}", 1.0, ("word " * 100)) for i in range(6)])
    block, injected = pr._format_context(hits, seen=set())
    assert len(injected) <= pr._KEEP
    for line in block.splitlines()[1:]:
        assert len(line) <= pr._SNIPPET + 2  # "- " prefix


def test_format_dedups_against_seen():
    hits = _hits(("d1", 1.0, "already shown"), ("d2", 1.0, "fresh hit"))
    block, injected = pr._format_context(hits, seen={"d1"})
    assert "fresh hit" in block
    assert "already shown" not in block
    assert list(injected) == ["d2"]


def test_format_returns_empty_when_nothing_survives():
    assert pr._format_context([], seen=set()) == ("", {})
    # all below floor except the top, but top is de-duped away -> nothing
    hits = _hits(("d1", 1.0, "shown"), ("d2", 0.1, "weak"))
    assert pr._format_context(hits, seen={"d1"}) == ("", {})


def test_session_dedup_persists_across_prompts(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "_recall",
                        lambda home, q: _hits(("d1", 1.0, "the one fact")))
    first = _run(tmp_path, {"prompt": "tell me the one fact please", "session_id": "sX"})
    assert "the one fact" in first
    # same doc_id, same session -> not re-injected
    second = _run(tmp_path, {"prompt": "remind me of the one fact again", "session_id": "sX"})
    assert second == ""


def test_seen_file_prunes_stale_siblings(tmp_path):
    d = tmp_path / "recall_seen"
    d.mkdir()
    old = d / "old.json"
    old.write_text(json.dumps(["x"]))
    import os
    os.utime(old, (1.0, 1.0))  # ancient mtime
    state, path = pr._load_seen(str(tmp_path), "sNew", now=pr._SEEN_TTL_S + 10_000)
    assert not old.exists()     # pruned
    assert state["injected"] == {}


def test_legacy_seen_file_upgraded_to_state(tmp_path):
    d = tmp_path / "recall_seen"
    d.mkdir()
    f = pr._seen_path(str(tmp_path), "sLegacy")
    f.write_text(json.dumps(["d1", "d2"]))  # legacy bare-list format
    state, _ = pr._load_seen(str(tmp_path), "sLegacy", now=1000.0)
    assert set(state["injected"]) == {"d1", "d2"}
    assert state["used"] == []


# --- quote-back accept signal ------------------------------------------------

def _transcript(tmp_path, *assistant_texts):
    p = tmp_path / "transcript.jsonl"
    lines = [json.dumps({"type": "assistant",
                         "message": {"content": [{"type": "text", "text": t}]}})
             for t in assistant_texts]
    p.write_text("\n".join(lines))
    return str(p)


def test_quoteback_credits_when_snippet_reappears(tmp_path, monkeypatch):
    recorded = {}
    monkeypatch.setattr(pr, "_record_used",
                        lambda home, ids, sid: recorded.update(ids=ids, sid=sid))
    # d1 was injected earlier; the assistant's reply quotes its distinctive words
    state = {"injected": {"d1": "Taryn leads the Easter launch volunteer team"},
             "used": []}
    tp = _transcript(tmp_path,
                     "As noted, Taryn leads the Easter launch volunteer team this year.")
    newly = pr._detect_quoteback(str(tmp_path), tp, state, "s1")
    assert newly == ["d1"]
    assert "d1" in state["used"]          # idempotency guard updated


def test_quoteback_no_credit_when_absent(tmp_path):
    state = {"injected": {"d1": "Taryn leads the Easter launch volunteer team"},
             "used": []}
    tp = _transcript(tmp_path, "Let me check the budget spreadsheet figures instead.")
    assert pr._detect_quoteback(str(tmp_path), tp, state, "s1") == []
    assert state["used"] == []


def test_quoteback_not_recredited(tmp_path):
    state = {"injected": {"d1": "Taryn leads the Easter launch volunteer team"},
             "used": ["d1"]}            # already credited
    tp = _transcript(tmp_path, "Taryn leads the Easter launch volunteer team.")
    assert pr._detect_quoteback(str(tmp_path), tp, state, "s1") == []


def test_quoteback_ignores_short_snippets(tmp_path):
    # too few distinctive tokens (<_QB_MIN_TOKENS) to judge reliably
    state = {"injected": {"d1": "ok sure yes"}, "used": []}
    tp = _transcript(tmp_path, "ok sure yes indeed absolutely certainly")
    assert pr._detect_quoteback(str(tmp_path), tp, state, "s1") == []


# --- expansion (retrieval_expand) --------------------------------------------

def test_format_context_expanded_keeps_larger_context():
    long_text = "sentence. " * 200  # ~2000 chars — a stitched parent
    results = [{"doc_id": "d1", "score": 1.0, "text": long_text}]
    block, injected = pr._format_context(results, set(), expanded=True)
    assert len(injected["d1"]) > pr._SNIPPET          # not truncated to the flat 200-char cap
    assert len(block) <= pr._EXPANDED_MAX_TOTAL + 100  # bounded by the expanded budget


def test_format_context_flat_unchanged():
    results = [{"doc_id": "d1", "score": 1.0, "text": "x" * 999}]
    block, injected = pr._format_context(results, set(), expanded=False)
    assert len(injected["d1"]) <= pr._SNIPPET          # flat path still 200-char capped


def test_format_context_expanded_keeps_second_best_parent():
    # Regression for the drop bug: expand_hits' _head_tail ordering puts the
    # 2nd-best parent LAST for a 3-parent set ([rank0, rank2, rank1]). The old
    # _format_context re-truncated under _EXPANDED_MAX_TOTAL, silently dropping
    # whatever landed last in that reordered sequence — i.e. the 2nd-best
    # parent, keeping only 1st + 3rd. Three parents whose combined size exceeds
    # the old total cap must now all survive (expand_hits already bound them to
    # its own char_budget upstream; _format_context must trust that, not re-cap).
    big = "sentence. " * 300  # ~3000 chars each; 3x exceeds _EXPANDED_MAX_TOTAL (4000)
    results = [
        {"doc_id": "rank0", "score": 1.0, "text": big},
        {"doc_id": "rank2", "score": 0.8, "text": big},
        {"doc_id": "rank1", "score": 0.9, "text": big},  # 2nd-best, last in head-tail order
    ]
    block, injected = pr._format_context(results, set(), expanded=True)
    assert {"rank0", "rank1", "rank2"} == set(injected)
    assert "rank1" in block or injected.get("rank1")  # sanity: present at all


def test_recall_requests_expand_when_flag_on(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"retrieval_expand": True}))
    (tmp_path / "control_port").write_text("9999")
    (tmp_path / "control_token").write_text("tok")
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"results": []}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _FakeResp()

    monkeypatch.setattr(pr.urllib.request, "urlopen", _fake_urlopen)
    pr._recall(str(tmp_path), "some query")
    assert captured["body"]["expand"] is True

    (tmp_path / "config.json").write_text(json.dumps({"retrieval_expand": False}))
    pr._recall(str(tmp_path), "some query")
    assert captured["body"]["expand"] is False
