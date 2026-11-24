"""A client hanging up must not print a traceback; a real handler error must."""
import pytest

from mcpbrain import control_api
from mcpbrain.control_api import ControlServer


class _Daemon:
    def status(self): return {}


@pytest.fixture
def server(tmp_path):
    s = ControlServer(_Daemon(), str(tmp_path))
    s.start()
    yield s
    s.stop()


def _handle(httpd, exc):
    try:
        raise exc
    except Exception:
        httpd.handle_error(None, ("127.0.0.1", 1))


def test_control_server_uses_the_quiet_server(server):
    assert isinstance(server._httpd, control_api._ControlHTTPServer)


@pytest.mark.parametrize("exc", [BrokenPipeError(32, "Broken pipe"),
                                 ConnectionResetError(54, "reset"),
                                 ConnectionAbortedError(53, "aborted")])
def test_client_disconnect_prints_nothing(server, capsys, exc):
    _handle(server._httpd, exc)
    assert capsys.readouterr().err == ""


def test_real_handler_error_still_prints_traceback(server, capsys):
    _handle(server._httpd, ValueError("handler bug"))
    err = capsys.readouterr().err
    assert "Traceback" in err and "ValueError: handler bug" in err
