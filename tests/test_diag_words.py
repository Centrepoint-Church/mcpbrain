"""Every check doctor reports has plain-English wording for the console."""
from datetime import datetime, timezone

import pytest

from mcpbrain import connector, diag_words, doctor

# Things a terminal line carries that the console must never show.
_RAW = ("→", "—", "✅", "⚠", "❌", "➖", "⏳", "mcpbrain doctor", "mcpbrain connect",
        "mcpbrain setup", "/Users/", "C:\\", "bin/", "Error", "Exception", "  ")


def _assert_plain(w):
    assert w is not None
    for raw in _RAW:
        assert raw not in w["words"], (raw, w)
    assert w["words"].endswith((".", "!")), w


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(connector, "desktop_config_paths",
                        lambda: [tmp_path / "claude_desktop_config.json"])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: [{"version": "0.7.1"}, {"version": "0.7.2"}])


def _conns(**states):
    base = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
            for k in ("google", "claude", "backup", "records", "enrichment")}
    for name, state in states.items():
        base[name] = {"state": state, "detail": state, "last_verified": None}
    return base


def _doctor_lines(tmp_path, **kw):
    from mcpbrain.store import Store
    Store(tmp_path / "brain.sqlite3", dim=4).init()
    _, msg = doctor.run_doctor(str(tmp_path), model_present=lambda h: True,
                               repairs={}, **kw)
    return msg.splitlines()


@pytest.mark.parametrize("conns,status", [
    (_conns(), {"watchdog_exits": 0}),
    (_conns(google="needs_action", backup="not_started", enrichment="needs_action",
            claude="needs_action"),
     {"watchdog_exits": 4, "watchdog_limit_reached": True,
      "google_unavailable": {"since": "09:00", "attempts": 2, "affects": ["sync"]},
      "restore_pending": {"since": "09:00"}, "restore_error": "bad key",
      "prune_held": {"newest_bytes": 10, "largest_kept_bytes": 999}}),
])
def test_every_line_doctor_writes_is_worded(tmp_path, conns, status):
    seen = set()
    for line in _doctor_lines(tmp_path, conns=conns, daemon_status=status):
        w = diag_words.word(line)
        if w is None:
            continue
        assert w["worded"], f"no wording for: {line}"
        assert w["check"], f"no check name recognised in: {line}"
        _assert_plain(w)
        seen.add(w["check"])
    assert {"Daemon", "Google", "Connector", "MCP version", "Sync queue",
            "Integrity", "Reflow", "Clean-up", "Scheduled tasks"} <= seen


def test_every_check_has_a_name_and_an_entry():
    for label in diag_words._LABELS:
        assert label in diag_words.NAMES, label
    for label in diag_words.WORDS:
        assert label in diag_words.NAMES, label
    unworded = set(diag_words._LABELS) - set(diag_words.WORDS) - {"Repair state", "Invalidators"}
    assert not unworded, unworded


def test_variant_lines_are_worded(tmp_path):
    now = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)
    lines = [
        doctor.sync_queue_line(8, "2026-11-10T01:00:00+00:00", now=now),
        doctor.sync_queue_line(8, "2026-11-10T03:59:00+00:00", now=now),
        doctor.arch_line("riscv"),
        doctor.integrity_line(tmp_path, check=lambda h: ["row 4 missing from index i1"]),
        doctor.integrity_line(tmp_path, check=lambda h: (_ for _ in ()).throw(OSError("x"))),
        "⚠️  Tenant           NOT CONFIGURED — fleet, backup upload and auto-update are disabled",
        "❌ Reflow           HALTED: dangling ref — investigate, then `python bin/reflow.py resume --yes`",
        "⚠️ Reflow           BLOCKED: last seed no_recent_backup — 3 queued, 2 owner(s) waiting; x",
        "⚠️ Reflow           2 owner(s) held: ordinary path did not converge (retried once a day) — 0 queued",
        "⏳ Reflow           4 queued; 1 of 2 owners done (none yet); 0 chunks carried",
        "⏳ Clean-up         pending",
        "❌ Clean-up         failed at step 2",
        "⚠️  Backup           a damaged local backup was discarded 2h ago (x); a fresh full backup was started",
        "⚠️  Backup           3 old backup file(s) Drive will not let mcpbrain remove (check the backup folder's permissions)",
        "⚠️ Sync failures    2 items retrying — f1 (3 attempts: export timeout)",
        "⚠️  Infra drives     mcpbrain did not remove 4 item(s) from Acme Drive because x",
        "⚠️ Chunk window     12 chunk(s) longer than one window — re-embed pending (5 left)",
        "⚠️ content-free chunks: 3",
        "⚠️ Items awaiting re-chunk: 1",
        "⚠️ dangling invalidator pointers: 2",
        "⚠️  OCR              tesseract missing — scanned PDFs index with no text",
        "❌ Embedder         weights missing → re-download failed: boom (needs network)",
        "❌ Records          not created → creating... ✅ fixed",
        "➖ Baseline         skipped (RuntimeError: no store)",
        "❌ Daemon           no repair registered for 'daemon'",
    ]
    for line in lines:
        w = diag_words.word(line)
        assert w["worded"], line
        _assert_plain(w)


def test_the_reported_run_reads_as_words(tmp_path):
    """The Diagnostics run from the 0.7.148 acceptance check, line for line."""
    home = "/Users/dana/Library/Application Support/mcpbrain"
    text = "\n".join([
        f"mcpbrain doctor — 2026-11-10 03:12 UTC   (home: {home})",
        "",
        "✅ Backup           restore point 64m ago · 1 waiting to upload (23 MB)",
        "✅ Tenant           Acme (acme)",
        "⚠️  Connector        claude_desktop_config.json points at /Users/dana/.local/bin/mcpbrain, "
        "which differs from the current install (/Users/dana/.local/share/uv/tools/mcpbrain/bin/"
        "mcpbrain) — run 'mcpbrain connect'",
        "➖ Connector        .claude.json not present (/Users/dana)",
        "⚠️  MCP version      4 live server(s) on 0.7.144, 0.7.146, installed is 0.7.148 — each "
        "keeps its start-time code until ITS OWN client restarts: quit Claude Desktop",
        "✅ Sync queue       8 pending, oldest 2 min old",
        "✅ Reflow           idle (13347 of 13347 owners done (carried 13111); 82024 chunks carried)",
        "",
        "0 fixed automatically, 0 need your action (see ↑).",
    ])
    shown = [diag_words.word(ln) for ln in text.splitlines()]
    shown = [w for w in shown if w]
    assert [(w["status"], w["words"]) for w in shown] == [
        ("ok", "Backups are working: restore point 64m ago, 1 waiting to upload (23 MB)."),
        ("ok", "Set up for Acme."),
        ("warn", "Claude Desktop isn't connected to mcpbrain. "
                 "Use Reconnect and restart under Connections."),
        ("info", "Claude Code isn't on this computer, or hasn't been opened yet."),
        ("warn", "4 open Claude sessions are still using an older mcpbrain. Quit Claude "
                 "Desktop completely and close any long-running Claude Code sessions."),
        ("ok", "8 new items being fetched now."),
        ("ok", "Background re-processing is finished."),
    ]


def test_an_unknown_check_gets_a_safe_sentence():
    w = diag_words.word("⚠️  Frobnicator     /Users/dana/x failed: KeyError('y') — run mcpbrain fix")
    assert w["worded"] is False and w["status"] == "warn"
    assert w["words"] == "One check: this needs a look. Details are in the mcpbrain log."
    w = diag_words.word("✅ Sync queue       x")
    assert w["words"] == "Nothing waiting to sync."


def test_an_entry_that_raises_falls_back(monkeypatch):
    monkeypatch.setitem(diag_words.WORDS, "OCR", lambda st, d: 1 / 0)
    w = diag_words.word("⚠️  OCR              tesseract missing")
    assert w["words"] == "Scanned PDFs: this needs a look. Details are in the mcpbrain log."


def test_header_summary_and_blank_lines_are_hidden():
    for line in ("mcpbrain doctor — 2026-11-10 03:12 UTC   (home: /x)", "",
                 "0 fixed automatically, 0 need your action (see ↑)."):
        assert diag_words.word(line) is None

