"""ControlClient talks to a real ControlServer over the loopback API."""

import pytest

from mcpbrain.control_api import ControlServer
from mcpbrain.control_client import ControlClient, DaemonUnavailable


class FakeDaemon:
    def __init__(self):
        self.paused = False

    def status(self):
        return {"paused": self.paused, "chunk_count": 7, "google_connected": True,
                "granted_scopes": [], "enrich_enabled": False}

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def test_status_pause_resume_round_trip(tmp_path):
    srv = ControlServer(FakeDaemon(), home=str(tmp_path))
    srv.start()
    try:
        c = ControlClient(home=str(tmp_path))
        assert c.is_running() is True
        assert c.status()["chunk_count"] == 7
        c.pause()
        assert c.status()["paused"] is True
        c.resume()
        assert c.status()["paused"] is False
        assert c.wizard_url() == f"http://127.0.0.1:{srv.port}/"
    finally:
        srv.stop()


def test_unavailable_when_no_daemon(tmp_path):
    # No control_port/control_token files in this home: the daemon isn't running.
    c = ControlClient(home=str(tmp_path))
    assert c.is_running() is False
    assert c.wizard_url() == ""
    with pytest.raises(DaemonUnavailable):
        c.status()
