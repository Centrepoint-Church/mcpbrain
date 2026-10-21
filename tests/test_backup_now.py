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
                 old=False, requested_polls=0, pending=lambda n: 0):
        self.requested = requested if requested is not None else {"requested": True}
        self.running = list(running)
        self.on_poll = on_poll
        self.down = down
        self.old = old
        self.polls = 0
        self.requested_polls = requested_polls
        self.pending = pending

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
        return {"backup_running": self.running.pop(0) if self.running else None,
                "backup_requested": self.polls <= self.requested_polls,
                "backup": {"pending": self.pending(self.polls)}}


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


def test_backup_now_waits_for_pending_zero(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0, consecutive_failures=3)

    def finish(n):
        if n == 4:
            _state(tmp_path, last_attempt=200.0, last_success=200.0,
                   restore_point=200.0, consecutive_failures=0)

    # Success is recorded at poll 4, but 2 items are still waiting until poll 5:
    # the command must not report done while anything is pending.
    client = _Client(
        requested_polls=1,
        running=[None, {"phase": "upload", "sent": 384_000_000, "total": 768_000_000}],
        pending=lambda n: 2 if n < 5 else 0, on_poll=finish)
    code, out = _run(tmp_path, client)
    assert code == 0
    assert out.splitlines()[0] == "Capturing changes…"
    assert "Uploading: 384 of 768 MB (50%) · 2 waiting" in out
    assert "✅ Backed up. Restore point " in out
    assert client.polls == 5


def test_waits_for_the_requested_capture(tmp_path):
    """last_success moved but the capture has not been taken yet: keep waiting."""
    _state(tmp_path, last_attempt=100.0, last_success=90.0)
    client = _Client(requested_polls=2, on_poll=lambda n: _state(
        tmp_path, last_attempt=200.0, last_success=200.0, consecutive_failures=0)
        if n >= 1 else None)
    code, _ = _run(tmp_path, client)
    assert code == 0
    assert client.polls == 3


def test_reports_a_failure_with_its_error(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0, consecutive_failures=0)

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


def test_outbox_full_is_reported_as_a_failure(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0, consecutive_failures=0)

    def full(n):
        _state(tmp_path, last_attempt=200.0, last_success=90.0, consecutive_failures=1,
               last_error="outbox full: 2000 MB waiting to upload")

    code, out = _run(tmp_path, _Client(pending=lambda n: 3, on_poll=full))
    assert code == 1
    assert "outbox full" in out
