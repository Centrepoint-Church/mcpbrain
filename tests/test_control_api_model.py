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
