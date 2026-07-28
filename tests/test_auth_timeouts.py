"""Routine API reads must not inherit the 600s backup-upload timeout."""
from mcpbrain import auth


def test_read_timeout_is_much_smaller_than_the_upload_timeout():
    """Restored after a round-1 deletion that was too broad: this file's other
    three tests exercise real behavior (they mock build_service's HTTP layer
    and assert the resulting timeout), but NOTHING else in the suite pins
    DEFAULT_READ_TIMEOUT_S's actual value -- without this, it could regress
    all the way back to 600 (DEFAULT_HTTP_TIMEOUT_S, i.e. the exact Task 6
    defect this module's docstring exists to prevent: routine API reads
    inheriting the 600s backup-upload timeout) and the full suite would stay
    green. The `<= 120` bound is an independent sanity ceiling, not a
    self-comparison -- it fails if DEFAULT_READ_TIMEOUT_S is ever raised to
    something no longer "read-sized" even while technically still less than
    DEFAULT_HTTP_TIMEOUT_S.
    """
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
    """Verify that build_google_services drive_timeout_s reaches actual Http layer.

    Calls the REAL build_google_services with drive_timeout_s parameter and mocks
    only the lowest level (httplib2.Http, AuthorizedHttp, build) to verify that
    the drive service gets DEFAULT_HTTP_TIMEOUT_S while other services get
    DEFAULT_READ_TIMEOUT_S. Does NOT mock build_service or build_google_services
    to ensure the real parameter-threading code runs end-to-end.
    """
    http_timeouts = {}  # key: service_name (from build's return), value: timeout_s

    class _Http:
        def __init__(self, timeout=None):
            self.timeout = timeout

    monkeypatch.setattr(auth.httplib2, "Http", _Http)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)

    def _capture_build(api, version, http):
        # Capture the timeout that reached this service
        http_timeouts[api] = http.timeout
        return f"mock_{api}_service"

    monkeypatch.setattr(auth, "build", _capture_build)
    # Return all scopes as granted so all services are built
    monkeypatch.setattr(auth, "_granted_scopes", lambda creds, tf: auth.SCOPES)

    # Call the REAL build_google_services with explicit drive_timeout_s
    services = auth.build_google_services(
        creds=object(),
        drive_timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S
    )

    # Verify drive service got the long timeout and others got the read timeout
    assert http_timeouts.get("drive") == auth.DEFAULT_HTTP_TIMEOUT_S
    assert http_timeouts.get("gmail") == auth.DEFAULT_READ_TIMEOUT_S
    assert http_timeouts.get("calendar") == auth.DEFAULT_READ_TIMEOUT_S
    assert services.get("drive_service") == "mock_drive_service"
    assert services.get("gmail_service") == "mock_gmail_service"
    assert services.get("calendar_service") == "mock_calendar_service"
