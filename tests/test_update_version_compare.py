from mcpbrain import update


def test_prerelease_sorts_below_final(monkeypatch):
    html = ('<a href="mcpbrain-0.3.0rc1-py3-none-any.whl">x</a>'
            '<a href="mcpbrain-0.3.0-py3-none-any.whl">x</a>'
            '<a href="mcpbrain-0.2.0-py3-none-any.whl">x</a>')
    monkeypatch.setattr(update, "_fetch", lambda u: html)
    assert update._latest_version("https://x/simple/") == "0.3.0"


def test_should_update_handles_prerelease():
    assert update._should_update("0.3.0rc1", "0.3.0") is True
    assert update._should_update("0.3.0", "0.3.0rc1") is False
