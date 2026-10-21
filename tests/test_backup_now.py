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
                 old=False, requested_polls=0, pending=lambda n: 0,
                 info=lambda n: {}, flaky=lambda n: False):
        self.requested = requested if requested is not None else {"requested": True}
        self.running = list(running)
        self.on_poll = on_poll
        self.down = down
        self.old = old
        self.polls = 0
        self.requested_polls = requested_polls
        self.pending = pending
        self.info = info
        self.flaky = flaky
        self.calls = 0

    def request_backup(self):
        if self.down or self.old:
            raise DaemonUnavailable("x")
        return self.requested

    def status(self):
        if self.down:
            raise DaemonUnavailable("x")
        self.calls += 1
        if self.flaky(self.calls):
            raise DaemonUnavailable("x")
        self.polls += 1
        if self.on_poll:
            self.on_poll(self.polls)
        return {"backup_running": self.running.pop(0) if self.running else None,
                "backup_requested": self.polls <= self.requested_polls,
                "backup": {"pending": self.pending(self.polls), **self.info(self.polls)}}


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


_PAUSED = {"last_error": "capture paused: 2,000 MB waiting to upload; the next "
                         "backup starts when they finish",
           "pending_bytes": 2_000_000_000}


def test_paused_by_full_outbox_tells_once_then_succeeds_after_drain(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0, consecutive_failures=1)

    def drain(n):
        if n == 6:
            _state(tmp_path, last_attempt=300.0, last_success=300.0,
                   restore_point=300.0, consecutive_failures=0)

    client = _Client(
        requested_polls=5, on_poll=drain,
        running=[None, None, None,
                 {"phase": "upload", "sent": 1_000_000_000, "total": 2_000_000_000}],
        pending=lambda n: 3 if n < 6 else 0,
        info=lambda n: _PAUSED if n < 6 else {})
    code, out = _run(tmp_path, client)
    assert code == 0
    assert out.count("Earlier changes (2,000 MB) are still uploading; this backup "
                     "will start when they finish.") == 1
    assert "Uploading: 1,000 of 2,000 MB (50%) · 3 waiting" in out
    assert "✅ Backed up." in out
    assert "Backup failed" not in out


def test_paused_until_the_deadline_prints_the_still_running_message(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0, consecutive_failures=1)
    client = _Client(requested_polls=10**9, pending=lambda n: 3, info=lambda n: _PAUSED)
    code, out = _run(tmp_path, client)
    assert code == 0
    assert out.count("Earlier changes") == 1
    assert "Still running after a long time" in out
    assert "keeps retrying" not in out


def test_failure_message_wording(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0, consecutive_failures=0)
    code, out = _run(tmp_path, _Client(on_poll=lambda n: _state(
        tmp_path, last_attempt=200.0, last_success=90.0, consecutive_failures=1,
        last_error="boom")))
    assert code == 1
    assert "mcpbrain keeps retrying in the background" in out
    assert "within the hour" not in out


def test_lost_contact_mid_wait_exits_cleanly(tmp_path):
    _state(tmp_path, last_attempt=100.0, last_success=90.0)
    client = _Client(requested_polls=10**9, flaky=lambda c: c >= 2)
    code, out = _run(tmp_path, client)
    assert code == 0
    assert "Lost contact with mcpbrain" in out
    assert "Backed up" not in out


def test_progress_prints_across_files_of_one_item_and_resets_per_item(tmp_path):
    def run(item, sent):
        return {"phase": "upload", "item": item, "sent": sent, "total": 100_000_000}

    client = _Client(
        requested_polls=1,
        running=[None, run("a", 0), run("a", 50_000_000), run("a", 100_000_000),
                 run("b", 0), run("b", 40_000_000)],
        pending=lambda n: 2 if n < 8 else 0,
        on_poll=lambda n: _state(tmp_path, last_attempt=200.0, last_success=200.0,
                                 restore_point=200.0, consecutive_failures=0)
        if n == 8 else None)
    _state(tmp_path, last_attempt=100.0, last_success=90.0)
    code, out = _run(tmp_path, client)
    assert code == 0
    lines = out.splitlines()
    assert "Uploading: 50 of 100 MB (50%) · 2 waiting" in lines
    assert "Uploading: 100 of 100 MB (100%) · 2 waiting" in lines
    assert "Uploading the next set of changes (2 waiting)" in lines
    # Item b starts again from 0% and prints at its own steps.
    assert lines.index("Uploading: 0 of 100 MB (0%) · 2 waiting",
                       lines.index("Uploading the next set of changes (2 waiting)")) > 0
    assert "Uploading: 40 of 100 MB (40%) · 2 waiting" in lines
