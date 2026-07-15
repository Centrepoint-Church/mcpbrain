from mcpbrain.daemon import Daemon


def _bare_daemon():
    d = Daemon.__new__(Daemon)
    d._embedder_obj = None
    d._model_downloading = False
    d._model_error = None
    return d


def test_model_status_reports_not_cached(monkeypatch):
    monkeypatch.setattr("mcpbrain.embed.model_weights_cached", lambda: False)
    d = _bare_daemon()
    st = d.model_status()
    assert st == {"cached": False, "downloading": False, "error": None}


def test_model_status_reports_cached(monkeypatch):
    monkeypatch.setattr("mcpbrain.embed.model_weights_cached", lambda: True)
    d = _bare_daemon()
    assert d.model_status()["cached"] is True


class _ThreadSpy:
    """Stand-in for threading.Thread: records constructor calls and never
    actually runs `target` (start() is a no-op). This lets the tests below
    assert whether ensure_model() spawned a worker at all, without ever
    running the real (network-hitting) embedder build."""

    instances = []

    def __init__(self, *args, target=None, daemon=None, **kwargs):
        self.target = target
        self.daemon = daemon
        _ThreadSpy.instances.append(self)

    def start(self):
        pass


def test_ensure_model_is_idempotent_while_downloading(monkeypatch):
    _ThreadSpy.instances = []
    monkeypatch.setattr("mcpbrain.daemon.threading.Thread", _ThreadSpy)
    d = _bare_daemon()
    d._model_downloading = True  # simulate an in-flight download

    d.ensure_model()

    assert _ThreadSpy.instances == []  # no second worker spawned


def test_ensure_model_spawns_one_thread_and_returns_without_blocking(monkeypatch):
    _ThreadSpy.instances = []
    monkeypatch.setattr("mcpbrain.daemon.threading.Thread", _ThreadSpy)
    d = _bare_daemon()

    d.ensure_model()

    # Exactly one worker handed off; ensure_model() returned here without
    # ever invoking the spied thread's target (start() is a no-op above), so
    # the actual embedder build never ran inline.
    assert len(_ThreadSpy.instances) == 1
    assert d._model_downloading is True
    assert d._model_error is None
