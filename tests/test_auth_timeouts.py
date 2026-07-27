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


def test_build_google_services_applies_drive_timeout_to_drive_service(monkeypatch):
    """Verify that build_google_services' drive_timeout_s param is passed through."""
    timeouts_seen = {}

    class _Http:
        def __init__(self, timeout=None):
            self.timeout = timeout

    def _capture_http(timeout=None):
        timeouts_seen["http_timeout"] = timeout
        return _Http(timeout)

    def _build_service(api, version, creds, timeout_s=None):
        # Capture the timeout that build_service was called with for drive
        if api == "drive":
            timeouts_seen["drive_timeout"] = timeout_s
        return f"mock_{api}_service"

    monkeypatch.setattr(auth.httplib2, "Http", _capture_http)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    monkeypatch.setattr(auth, "build", lambda api, version, http: f"mock_{api}_service")
    monkeypatch.setattr(auth, "build_service", _build_service)
    monkeypatch.setattr(auth, "_granted_scopes", lambda creds, tf: None)
    monkeypatch.setattr(auth, "load_credentials", lambda **kw: object())

    # Call build_google_services with explicit drive_timeout_s
    services = auth.build_google_services(
        creds=object(),
        drive_timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S
    )

    # Verify the drive service was built with the long timeout
    assert timeouts_seen.get("drive_timeout") == auth.DEFAULT_HTTP_TIMEOUT_S
    assert services.get("drive_service") == "mock_drive_service"
