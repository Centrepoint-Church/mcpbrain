"""`mcpbrain restore` (the separate-process path, swap=None) must refuse while
the daemon runs: a daemon connection opened before the busy-checked checkpoint
that writes after os.replace silently rolls the restored store back. Detection
is a raw TCP connect to 127.0.0.1:<control_port>; --force does not bypass it,
and nothing is written when refused."""
import socket
from pathlib import Path

import pytest

from mcpbrain import config, restore


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    config.write_config(str(tmp_path), {
        "owner_email": "j@x.com",
        "fleet": {"escrow_folder_id": "ESC"},
        "backup": {"escrow_key": "K" * 44, "shared_drive_id": "SD", "user_id": "j@x.com"},
    })
    return tmp_path


@pytest.fixture
def listener(home):
    """A real TCP listener on 127.0.0.1:<ephemeral>, recorded as control_port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    (home / "control_port").write_text(f"{srv.getsockname()[1]}\n")
    yield srv
    srv.close()


@pytest.fixture
def drive(monkeypatch):
    """Drive + decrypt stand-ins that record every write."""
    calls = []
    monkeypatch.setattr("mcpbrain.auth.load_credentials", lambda: calls.append("creds") or object())
    monkeypatch.setattr("mcpbrain.auth.build_service",
                        lambda *a, **k: calls.append("build") or object())
    monkeypatch.setattr("mcpbrain.backup.find_latest_snapshot",
                        lambda *a: calls.append("find") or "SNAP")
    monkeypatch.setattr("mcpbrain.backup.find_latest_in_subfolder", lambda *a: "SNAP")
    monkeypatch.setattr(restore, "_download_escrow_key", lambda *a: b"KEYBYTES")
    monkeypatch.setattr("mcpbrain.backup.download_snapshot",
                        lambda svc, fid, dest: calls.append("download")
                        or Path(dest).write_bytes(b"enc") or Path(dest))

    def fake_restore(enc, dest, key, *, records_dir=None, config_path=None):
        calls.append("restore")
        Path(dest).write_bytes(b"store")
        Path(config_path).write_text("{}")          # the bundle's config.json
        return Path(dest)
    monkeypatch.setattr("mcpbrain.backup.restore", fake_restore)
    monkeypatch.setattr("mcpbrain.backup.swap_in_store",
                        lambda stage, dest: calls.append("swap")
                        or Path(dest).write_bytes(Path(stage).read_bytes()))
    return calls


def _snapshot(home):
    return {p.name: p.read_bytes() for p in Path(home).rglob("*") if p.is_file()}


def test_daemon_running_detects_a_listener(home, listener):
    assert restore.daemon_running(str(home)) is True


def test_daemon_running_false_without_port_file_or_listener(home):
    assert restore.daemon_running(str(home)) is False            # no control_port
    (home / "control_port").write_text("not-a-port")
    assert restore.daemon_running(str(home)) is False            # garbage
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                    # bound then freed: nothing listens
    (home / "control_port").write_text(str(port))
    assert restore.daemon_running(str(home)) is False


@pytest.mark.parametrize("force", [False, True])
def test_run_restore_refuses_while_daemon_runs_and_writes_nothing(home, listener, drive, force):
    before = _snapshot(home)
    with pytest.raises(restore.DaemonRunningError, match="launchctl bootout"):
        restore.run_restore(str(home), force=force)
    assert drive == []                                           # not even a Drive call
    assert _snapshot(home) == before


@pytest.mark.parametrize("force", [False, True])
def test_run_restore_auto_refuses_while_daemon_runs(home, listener, drive, force):
    before = _snapshot(home)
    with pytest.raises(restore.DaemonRunningError, match="launchctl bootstrap"):
        restore.run_restore_auto(str(home), force=force, drive_service=object())
    assert drive == []
    assert _snapshot(home) == before


def test_run_restore_proceeds_when_nothing_listens(home, drive):
    out = restore.run_restore(str(home))
    assert drive[-2:] == ["restore", "swap"]
    assert Path(out).read_bytes() == b"store"


def test_daemon_passed_swap_is_not_refused(home, listener, drive):
    # /api/backup/auto runs INSIDE the daemon and passes its gated swap.
    swapped = []
    restore.run_restore_auto(str(home), drive_service=object(), force=True,
                             swap=lambda stage: swapped.append(stage))
    assert len(swapped) == 1


def test_cli_exits_nonzero_with_the_bootout_message(home, listener, drive, capsys):
    for argv in (["--force"], ["--auto", "--force"], []):
        with pytest.raises(SystemExit) as e:
            restore.run_restore_main(argv)
        assert e.value.code == 1
        err = capsys.readouterr().err
        assert "daemon is running" in err
        assert "launchctl bootout gui/$(id -u)/com.mcpbrain" in err
        assert "launchctl stop" in err and "KeepAlive" in err
    assert drive == []
