"""Routine API reads must not inherit the 600s backup-upload timeout."""
from mcpbrain import auth


def test_read_timeout_is_much_smaller_than_the_upload_timeout():
    assert auth.DEFAULT_READ_TIMEOUT_S < auth.DEFAULT_HTTP_TIMEOUT_S
    assert auth.DEFAULT_READ_TIMEOUT_S <= 120


def test_build_service_defaults_to_the_read_timeout(monkeypatch):
    seen = {}

    class _Http:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

    monkeypatch.setattr(auth.httplib2, "Http", _Http)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    monkeypatch.setattr(auth, "build", lambda api, version, http: ("svc", api))
    auth.build_service("drive", "v3", object())
    assert seen["timeout"] == auth.DEFAULT_READ_TIMEOUT_S


def test_backup_can_still_request_the_long_timeout(monkeypatch):
    seen = {}

    class _Http:
        def __init__(self, timeout=None):
            seen["timeout"] = timeout

    monkeypatch.setattr(auth.httplib2, "Http", _Http)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    monkeypatch.setattr(auth, "build", lambda api, version, http: ("svc", api))
    auth.build_service("drive", "v3", object(), timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S)
    assert seen["timeout"] == auth.DEFAULT_HTTP_TIMEOUT_S
