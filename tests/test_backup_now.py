"""`mcpbrain backup`: ask the daemon to back up now and watch the result."""
import json

from mcpbrain.backup_now import main
from mcpbrain.control_client import DaemonUnavailable


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Client:
    """Scripted daemon: `request_backup` answers `requested`; each status()
    poll pops the next backup_running value and runs `on_poll`."""

    def __init__(self, *, requested=None, running=(), on_poll=None, down=False,
                 old=False):
        self.requested = requested if requested is not None else {"requested": True}
        self.running = list(running)
        self.on_poll = on_poll
        self.down = down
        self.old = old
        self.polls = 0

    def request_backup(self):
        if self.down or self.old:
            raise DaemonUnavailable("x")
        return self.requested

    def status(self):
        if self.down:
            raise DaemonUnavailable("x")
        self.polls += 1
        if self.on_poll:
            self.on_poll(self.polls)
        return {"backup_running": self.running.pop(0) if self.running else None}


def _state(home, **kw):
    (home / "backup_state.json").write_text(json.dumps(kw))


def _run(home, client, argv=()):
    out = []
    clock = _Clock()

    def sleep(s):
        clock.t += s

    code = main(list(argv), client=client, home=home, sleep=sleep, clock=clock,
                out=out.append)
    return code, "\n".join(out)


def test_reports_progress_then_success(tmp_path):
    _state(tmp_path, last_attempt=100.0, consecutive_failures=3)

    def finish(n):
        if n == 3:
            _state(tmp_path, last_attempt=200.0, consecutive_failures=0)

    client = _Client(running=[
        {"phase": "snapshot"},
        {"phase": "upload", "sent": 384_000_000, "total": 768_000_000},
    ], on_poll=finish)
    code, out = _run(tmp_path, client)
    assert code == 0
    assert "snapshot" in out
    assert "384 MB of 768 MB (50%)" in out
    assert "Backup complete" in out


def test_reports_a_failure_with_its_error(tmp_path):
    _state(tmp_path, last_attempt=100.0, consecutive_failures=0)

    def fail(n):
        _state(tmp_path, last_attempt=200.0, consecutive_failures=1,
               last_error="The read operation timed out")

    code, out = _run(tmp_path, _Client(on_poll=fail))
    assert code == 1
    assert "The read operation timed out" in out


def test_daemon_not_running(tmp_path):
    code, out = _run(tmp_path, _Client(down=True))
    assert code == 1
    assert "isn't running" in out


def test_daemon_too_old_for_the_route(tmp_path):
    code, out = _run(tmp_path, _Client(old=True))
    assert code == 1
    assert "mcpbrain update" in out


def test_backup_not_configured(tmp_path):
    code, out = _run(tmp_path, _Client(requested={"error": "backup is not configured"}))
    assert code == 1
    assert "wizard" in out


def test_no_wait_returns_after_the_request(tmp_path):
    client = _Client()
    code, out = _run(tmp_path, client, ["--no-wait"])
    assert code == 0
    assert client.polls == 0
