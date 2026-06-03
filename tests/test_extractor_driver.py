"""Tests for the claude_pool-backed extractor driver (mcpbrain/extractor_driver.py).

The driver is the Nexus dry-run face of the extractor: file in
(enrich_queue/pending.json), file out (enrich_inbox/<batch_id>.json). It is
runner-agnostic: run_claude is injected so tests pass a fake and the module
stays importable on the Mac where claude_pool is absent. NO real run_claude,
no network, ever.

Home resolution matches prepare: tests set MCPBRAIN_HOME and the driver
resolves spool paths via config.app_dir().
"""

import json
from pathlib import Path

from mcpbrain import extractor_driver
from mcpbrain.contract import validate_batch_file

FIXTURE = Path(__file__).parent / "fixtures" / "spool" / "pending_basic.json"


def _seed_pending(home: Path) -> dict:
    """Copy pending_basic.json into home/enrich_queue/pending.json. Return its dict."""
    pending = json.loads(FIXTURE.read_text())
    queue_dir = home / "enrich_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    (queue_dir / "pending.json").write_text(json.dumps(pending))
    return pending


def _valid_batch_for(pending: dict) -> dict:
    """A batch whose extractions match pending_basic's two threads and validates."""
    extractions = []
    for thread in pending["threads"]:
        extractions.append({
            "thread_id": thread["thread_id"],
            "org": "Centrepoint",
            "content_type": "request",
            "summary": "A plain sentence.",
            "entities": [],
            "topics": [],
            "actions": [],
            "relations": [],
            "messages": [
                {"message_id": m["message_id"], "sender": m["sender"],
                 "date": m["date"], "labels": m.get("labels", ""),
                 "subject": m.get("subject", "")}
                for m in thread["messages"]
            ],
        })
    return {
        "batch_id": pending["batch_id"],
        "extractions": extractions,
        "merge_answers": [],
    }


def test_driver_reads_pending_writes_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    pending = _seed_pending(tmp_path)
    batch = _valid_batch_for(pending)

    fake = lambda prompt, **kw: json.dumps(batch)
    out_path = extractor_driver.run_extractor(run_claude=fake)

    assert out_path is not None
    written = Path(out_path)
    assert written == tmp_path / "enrich_inbox" / f"{pending['batch_id']}.json"
    assert written.exists()

    data = json.loads(written.read_text())
    assert validate_batch_file(data) == []
    assert data["batch_id"] == pending["batch_id"]
    assert len(data["extractions"]) == len(pending["threads"])


def test_driver_passes_prompt_and_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    pending = _seed_pending(tmp_path)
    batch = _valid_batch_for(pending)

    captured = {}

    def fake(prompt, **kw):
        captured["prompt"] = prompt
        return json.dumps(batch)

    extractor_driver.run_extractor(run_claude=fake)

    prompt = captured["prompt"]
    prompt_doc = (Path(__file__).parent.parent / "mcpbrain" / "enrich_prompt.md").read_text()
    assert prompt_doc in prompt, "driver must feed the enrich_prompt.md text"
    # The raw pending.json payload must travel in the prompt too.
    assert pending["batch_id"] in prompt
    assert pending["threads"][0]["thread_id"] in prompt


def test_non_json_reply_raises_and_no_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _seed_pending(tmp_path)

    fake = lambda prompt, **kw: "this is not json at all"

    import pytest
    with pytest.raises(ValueError, match="extractor answer was not JSON"):
        extractor_driver.run_extractor(run_claude=fake)

    inbox_dir = tmp_path / "enrich_inbox"
    assert not inbox_dir.exists() or list(inbox_dir.iterdir()) == []


def test_contract_invalid_batch_raises_and_no_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _seed_pending(tmp_path)

    # Valid JSON but missing required fields (no extractions, no batch_id)
    bad_batch = {"batch_id": "b-test", "extractions": [{"thread_id": "x"}]}

    import pytest
    fake = lambda prompt, **kw: json.dumps(bad_batch)
    with pytest.raises(ValueError, match="extractor batch failed validation"):
        extractor_driver.run_extractor(run_claude=fake)

    inbox_dir = tmp_path / "enrich_inbox"
    assert not inbox_dir.exists() or list(inbox_dir.iterdir()) == []


def test_unsafe_batch_id_raises_and_no_file_outside_inbox(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    pending = _seed_pending(tmp_path)
    batch = _valid_batch_for(pending)
    batch["batch_id"] = "../escape"

    import pytest
    fake = lambda prompt, **kw: json.dumps(batch)
    with pytest.raises(ValueError, match="batch_id contains unsafe path characters"):
        extractor_driver.run_extractor(run_claude=fake)

    # No file should be written outside enrich_inbox
    escaped = tmp_path / "escape.json"
    assert not escaped.exists()


def test_driver_no_pending_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))

    calls = []

    def fake(prompt, **kw):
        calls.append(prompt)
        return "{}"

    out_path = extractor_driver.run_extractor(run_claude=fake)

    assert out_path is None
    assert calls == [], "run_claude must not be called when pending.json is absent"


# --- CLI entry (python -m mcpbrain.extractor_driver) -----------------------


def test_main_prints_wrote_and_returns_0(tmp_path, monkeypatch, capsys):
    captured = {}

    def fake_run_extractor(*, home=None, model="sonnet", timeout=600):
        captured["home"] = home
        captured["model"] = model
        captured["timeout"] = timeout
        return "/some/home/enrich_inbox/batch-x.json"

    monkeypatch.setattr(extractor_driver, "run_extractor", fake_run_extractor)

    rc = extractor_driver.main(
        ["--home", str(tmp_path), "--model", "opus", "--timeout", "42"])

    assert rc == 0
    assert captured == {"home": str(tmp_path), "model": "opus", "timeout": 42}
    out = capsys.readouterr().out
    assert "wrote /some/home/enrich_inbox/batch-x.json" in out


def test_main_no_pending_prints_message_and_returns_0(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(extractor_driver, "run_extractor",
                        lambda **kw: None)

    rc = extractor_driver.main(["--home", str(tmp_path)])

    assert rc == 0
    out = capsys.readouterr().out
    assert f"no pending.json at {tmp_path}/enrich_queue" in out
    assert "nothing to extract" in out


def test_main_missing_claude_pool_prints_hint_and_nonzero(monkeypatch, capsys):
    def boom(**kw):
        raise ModuleNotFoundError("No module named 'claude_pool'")

    monkeypatch.setattr(extractor_driver, "run_extractor", boom)

    rc = extractor_driver.main([])

    assert rc != 0
    out = capsys.readouterr().out
    assert "claude_pool not importable" in out
    assert "PYTHONPATH=/home/josh/ops-brain/src" in out


def test_main_defaults(monkeypatch):
    captured = {}

    def fake_run_extractor(*, home=None, model="sonnet", timeout=600):
        captured["home"] = home
        captured["model"] = model
        captured["timeout"] = timeout
        return None

    monkeypatch.setattr(extractor_driver, "run_extractor", fake_run_extractor)

    rc = extractor_driver.main([])

    assert rc == 0
    assert captured == {"home": None, "model": "sonnet", "timeout": 600}
