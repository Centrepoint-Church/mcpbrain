"""session-start prints bounded priming; session-end captures a real session only."""
import io
import json
from pathlib import Path

from mcpbrain import session_hooks


def test_session_start_prints_hot_and_degrades(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text(
        "# Hot\n\n## Just decided\n- **2026-06-10:** shipped the thing\n## Open\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    # no control_port/token in home -> actions degrade, never crash
    session_hooks.session_start(str(tmp_path / "home"))
    out = capsys.readouterr().out
    assert "shipped the thing" in out
    assert "actions" in out.lower()  # heading present even when unavailable


def test_session_end_captures_substantial(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "Plan the migration in detail"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Here is the plan ..."}},
        {"type": "user", "message": {"role": "user", "content": "Great, do step one"}},
    ]))
    hook = {"transcript_path": str(transcript), "session_id": "s1", "cwd": str(tmp_path)}
    captured = {}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: captured.setdefault("env", env) or Path("x"))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert captured["env"]["kind"] == "ingest"
    assert "migration" in captured["env"]["content"].lower()


def test_session_end_skips_trivial(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "hi"}}))
    hook = {"transcript_path": str(transcript), "session_id": "s2", "cwd": str(tmp_path)}
    called = {"n": 0}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: called.update(n=called["n"] + 1))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert called["n"] == 0  # single trivial turn -> skipped
