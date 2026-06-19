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
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "ok", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
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


def test_session_start_silent_on_compact(tmp_path, monkeypatch):
    # After a compaction the priming block is noise — session_start must emit
    # nothing so it doesn't re-inject stale continuity/actions mid-session.
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text("# Hot\n- **2026-06-10:** shipped the thing\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    out = io.StringIO()
    session_hooks.session_start(str(tmp_path / "home"), out=out, source="compact")
    assert out.getvalue() == ""


def test_session_start_full_on_clear(tmp_path, monkeypatch):
    # clear is a fresh boundary (like startup) -> full priming block.
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text("# Hot\n- **2026-06-10:** shipped the thing\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "ok", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    out = io.StringIO()
    session_hooks.session_start(str(tmp_path / "home"), out=out, source="clear")
    assert "shipped the thing" in out.getvalue()


def test_read_hook_source_defaults_to_startup():
    assert session_hooks._read_hook_source(io.StringIO("")) == "startup"
    assert session_hooks._read_hook_source(io.StringIO("not json")) == "startup"
    assert session_hooks._read_hook_source(
        io.StringIO(json.dumps({"source": "resume"}))) == "resume"


def test_session_end_skips_on_resume(tmp_path, monkeypatch):
    # resume is a continuation, not a real end -> no capture (avoids double-capture).
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "Plan the migration in detail"}},
        {"type": "user", "message": {"role": "user", "content": "and do step one now please"}},
    ]))
    hook = {"transcript_path": str(transcript), "session_id": "s9",
            "reason": "resume", "cwd": str(tmp_path)}
    called = {"n": 0}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: called.update(n=called["n"] + 1))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert called["n"] == 0


def test_session_end_captures_both_sides(tmp_path, monkeypatch):
    # Decisions live in the assistant's replies; the capture must include them.
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "Should we adopt the new schema?"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": "Yes — decision: migrate to schema v2."}},
        {"type": "user", "message": {"role": "user", "content": "Great, proceed"}},
    ]))
    hook = {"transcript_path": str(transcript), "session_id": "s10", "cwd": str(tmp_path)}
    captured = {}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: captured.setdefault("env", env) or Path("x"))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    content = captured["env"]["content"]
    assert "schema v2" in content          # assistant decision captured
    assert "adopt the new schema" in content  # user prompt captured
    assert captured["env"]["source"] == "session_end_hook"


def test_pre_compact_captures_with_tag(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "Investigate the failing migration"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Root cause: a stale cursor."}},
        {"type": "user", "message": {"role": "user", "content": "Fix it and re-run the cycle"}},
    ]))
    hook = {"transcript_path": str(transcript), "session_id": "s11", "trigger": "auto"}
    captured = {}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: captured.setdefault("env", env) or Path("x"))
    session_hooks.pre_compact(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert captured["env"]["kind"] == "ingest"
    assert captured["env"]["source"] == "pre_compact_hook"
    assert "pre-compact" in captured["env"]["tags"]
    assert "stale cursor" in captured["env"]["content"]


def test_pre_compact_skips_trivial(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps(
        {"type": "user", "message": {"role": "user", "content": "hi"}}))
    hook = {"transcript_path": str(transcript), "session_id": "s12", "trigger": "manual"}
    called = {"n": 0}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: called.update(n=called["n"] + 1))
    session_hooks.pre_compact(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert called["n"] == 0


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


def test_session_end_handles_list_content_blocks(tmp_path, monkeypatch):
    import io
    import json
    from pathlib import Path
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "Investigate the failing migration thoroughly please"}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "and then summarise what you changed in detail"}]}},
    ]))
    hook = {"transcript_path": str(transcript), "session_id": "s3", "cwd": str(tmp_path)}
    captured = {}
    monkeypatch.setattr(session_hooks, "write_capture",
                        lambda home, env: captured.setdefault("env", env) or Path("x"))
    session_hooks.session_end(str(tmp_path / "home"), stdin=io.StringIO(json.dumps(hook)))
    assert "migration" in captured["env"]["content"].lower()


def test_remedies_map_has_exact_strings():
    r = session_hooks._REMEDIES
    assert r["google"] == "Google sign-in expired → run: mcpbrain auth"
    assert r["claude"] == "Daemon/plugin not seen recently → run: mcpbrain doctor"
    assert r["clickup"] == "ClickUp key invalid → re-enter it in the mcpbrain wizard"
    assert r["backup"] == "Backup overdue → run: mcpbrain doctor"
    assert r["records"] == "Records repo problem → run: mcpbrain doctor"
    assert r["enrichment"] == (
        "Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix"
    )


def test_action_needed_single_google(monkeypatch):
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "needs_action", "detail": "", "last_verified": None},
        "claude": {"state": "ok", "detail": "", "last_verified": None},
        "clickup": {"state": "ok", "detail": "", "last_verified": None},
        "backup": {"state": "ok", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "ok", "detail": "", "last_verified": None},
    })
    block = session_hooks._action_needed("/some/home")
    assert "## ⚠️ Action needed" in block
    assert "Google sign-in expired → run: mcpbrain auth" in block
    # only one remedy line
    assert block.count("\n- ") == 1


def test_action_needed_ignores_not_started(monkeypatch):
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "ok", "detail": "", "last_verified": None},
        "claude": {"state": "ok", "detail": "", "last_verified": None},
        # never configured -> must NOT produce a line
        "clickup": {"state": "not_started", "detail": "", "last_verified": None},
        "backup": {"state": "not_started", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "ok", "detail": "", "last_verified": None},
    })
    assert session_hooks._action_needed("/some/home") == ""


def test_action_needed_empty_when_all_ok(monkeypatch):
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "ok", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    assert session_hooks._action_needed("/some/home") == ""


def test_action_needed_caps_at_three_in_priority_order(monkeypatch):
    # All six broken -> only the top 3 by priority survive: google, claude, records.
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "needs_action", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    block = session_hooks._action_needed("/some/home")
    lines = block.splitlines()
    assert lines[0] == "## ⚠️ Action needed"
    body = lines[1:]
    assert len(body) == 3  # capped
    assert body[0] == "- Google sign-in expired → run: mcpbrain auth"
    assert body[1] == "- Daemon/plugin not seen recently → run: mcpbrain doctor"
    assert body[2] == "- Records repo problem → run: mcpbrain doctor"
    # lower-priority remedies dropped
    assert "ClickUp key invalid" not in block
    assert "Enrichment stalled" not in block


def test_action_needed_orders_subset(monkeypatch):
    # Only enrichment + claude broken -> claude first (higher priority), enrichment second.
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "ok", "detail": "", "last_verified": None},
        "claude": {"state": "needs_action", "detail": "", "last_verified": None},
        "clickup": {"state": "ok", "detail": "", "last_verified": None},
        "backup": {"state": "ok", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "needs_action", "detail": "", "last_verified": None},
    })
    body = session_hooks._action_needed("/some/home").splitlines()[1:]
    assert body == [
        "- Daemon/plugin not seen recently → run: mcpbrain doctor",
        "- Enrichment stalled → open Claude so the hourly task can run, or run /mcpbrain-fix",
    ]


def test_session_start_appends_action_block_after_actions(tmp_path, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text(
        "# Hot\n- **2026-06-10:** shipped the thing\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        "google": {"state": "needs_action", "detail": "", "last_verified": None},
        "claude": {"state": "ok", "detail": "", "last_verified": None},
        "clickup": {"state": "ok", "detail": "", "last_verified": None},
        "backup": {"state": "ok", "detail": "", "last_verified": None},
        "records": {"state": "ok", "detail": "", "last_verified": None},
        "enrichment": {"state": "ok", "detail": "", "last_verified": None},
    })
    out = io.StringIO()
    session_hooks.session_start(str(tmp_path / "home"), out=out)
    text = out.getvalue()
    assert "## Open actions" in text
    assert "## ⚠️ Action needed" in text
    assert "Google sign-in expired → run: mcpbrain auth" in text
    # ordering: the action-needed block comes AFTER the open-actions heading
    assert text.index("## Open actions") < text.index("## ⚠️ Action needed")


def test_session_start_no_action_block_when_all_ok(tmp_path, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text("# Hot\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))
    monkeypatch.setattr(session_hooks.probes, "all_connections", lambda home, store=None: {
        name: {"state": "ok", "detail": "", "last_verified": None}
        for name in ("google", "claude", "clickup", "backup", "records", "enrichment")
    })
    out = io.StringIO()
    session_hooks.session_start(str(tmp_path / "home"), out=out)
    assert "Action needed" not in out.getvalue()


def test_session_start_survives_probe_exception(tmp_path, monkeypatch):
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "state" / "hot.md").write_text(
        "# Hot\n- **2026-06-10:** shipped the thing\n")
    monkeypatch.setattr(session_hooks.config, "records_dir", lambda home: str(repo))

    def boom(home, store=None):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(session_hooks.probes, "all_connections", boom)
    out = io.StringIO()
    # must NOT raise
    session_hooks.session_start(str(tmp_path / "home"), out=out)
    text = out.getvalue()
    # continuity + actions still printed
    assert "shipped the thing" in text
    assert "## Open actions" in text
    # no action block emitted
    assert "Action needed" not in text


def test_action_needed_returns_empty_on_exception(monkeypatch):
    def boom(home, store=None):
        raise RuntimeError("nope")

    monkeypatch.setattr(session_hooks.probes, "all_connections", boom)
    assert session_hooks._action_needed("/some/home") == ""
