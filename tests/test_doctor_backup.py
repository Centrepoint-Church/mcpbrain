"""Doctor's Backup lines: read from local files only (daemon down is fine)."""
import json
import time

import pytest

from mcpbrain import backup_outbox, doctor, probes


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from mcpbrain import connector
    monkeypatch.setattr(connector, "desktop_config_paths",
                        lambda: [tmp_path / "claude_desktop_config.json"])
    monkeypatch.setattr(connector, "code_config_path", lambda: tmp_path / ".claude.json")


def _home(tmp_path, backup_cfg=None, **state):
    (tmp_path / "config.json").write_text(json.dumps({"backup": backup_cfg or {"dest": "x"}}))
    if state:
        (tmp_path / "backup_state.json").write_text(json.dumps(state))
    return tmp_path


def _doctor(home):
    conns = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
             for k in ("google", "claude", "records", "enrichment")}
    conns["backup"] = probes.probe_backup(home)
    return doctor.run_doctor(home, conns=conns, repairs={}, daemon_status={},
                             model_present=lambda _h: True,
                             agent_installed=lambda *_a: True)


def _outbox(monkeypatch, n, size):
    monkeypatch.setattr(backup_outbox, "pending_items", lambda _h: [object()] * n)
    monkeypatch.setattr(backup_outbox, "outbox_bytes", lambda _h: size)


def test_doctor_backup_line_shows_waiting_uploads(tmp_path, monkeypatch):
    home = _home(tmp_path, restore_point=time.time() - 2 * 3600,
                 last_success=time.time() - 2 * 3600, consecutive_failures=0)
    _outbox(monkeypatch, 2, 24_000_000)
    code, msg = _doctor(home)
    assert ("✅ Backup           restore point 2h ago · 2 waiting to upload (24 MB)"
            in msg)
    assert code == 0


def test_doctor_warns_when_outbox_full(tmp_path, monkeypatch):
    home = _home(tmp_path, {"dest": "x", "outbox_max_bytes": 1_000_000},
                 restore_point=time.time() - 3600, last_success=time.time() - 3600,
                 consecutive_failures=0)
    _outbox(monkeypatch, 5, 2_000_000)
    code, msg = _doctor(home)
    assert "capture paused: 2 MB waiting to upload" in msg
    assert "Run: mcpbrain backup" in msg
    assert code == 1


def test_doctor_shows_a_recent_discard(tmp_path):
    home = _home(tmp_path, restore_point=time.time(), last_success=time.time(),
                 last_discard={"at": time.time() - 2 * 3600, "item": "x",
                               "reason": "unreadable"})
    _, msg = _doctor(home)
    assert ("⚠️  Backup           a damaged local backup was discarded 2h ago "
            "(unreadable); a fresh full backup was started") in msg


def test_doctor_hides_an_old_discard(tmp_path):
    home = _home(tmp_path, restore_point=time.time(), last_success=time.time(),
                 last_discard={"at": time.time() - 8 * 86400, "item": "x",
                               "reason": "unreadable"})
    _, msg = _doctor(home)
    assert "damaged local backup" not in msg


def test_doctor_shows_files_drive_will_not_let_us_remove(tmp_path):
    home = _home(tmp_path, restore_point=time.time(), last_success=time.time())
    (tmp_path / "backup").mkdir()
    (tmp_path / "backup" / "journal.json").write_text(json.dumps({
        "delete_failures": {"a": {"count": 3, "error": "403"},
                            "b": {"count": 9, "error": "403"},
                            "c": {"count": 1, "error": "403"}}}))
    _, msg = _doctor(home)
    assert ("2 old backup file(s) Drive will not let mcpbrain remove "
            "(check the backup folder's permissions)") in msg


def test_doctor_backup_lines_work_with_daemon_down(tmp_path):
    home = _home(tmp_path)  # configured, no state file at all
    _, msg = _doctor(home)
    assert "No backup yet" in msg
