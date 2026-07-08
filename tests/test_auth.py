"""Tests for mcpbrain.auth — all fully offline (no browser, no network)."""

import datetime
import json
import sys

import pytest

from mcpbrain import auth, config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def test_identity_scopes_requested_but_not_required():
    # Identity scopes (for name/email prefill) are requested at consent
    # (CONSENT_SCOPES) but NOT in the required validation set (SCOPES), so
    # existing tokens stay valid and are not forced to re-consent.
    for s in auth.IDENTITY_SCOPES:
        assert s not in auth.SCOPES
    assert auth.CONSENT_SCOPES == auth.SCOPES + auth.IDENTITY_SCOPES
    assert any("userinfo.profile" in s for s in auth.IDENTITY_SCOPES)


def test_fetch_google_name_degrades_to_empty(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("token lacks userinfo.profile")
    monkeypatch.setattr(auth, "build_service", _boom)
    assert auth.fetch_google_name(object()) == ""


def test_fetch_google_name_reads_userinfo(monkeypatch):
    class _UI:
        def userinfo(self): return self
        def get(self): return self
        def execute(self): return {"name": "Josh Kemp", "email": "josh.k@x.com"}
    monkeypatch.setattr(auth, "build_service", lambda *a, **k: _UI())
    assert auth.fetch_google_name(object()) == "Josh Kemp"


# Minimal authorized-user token info that produces a VALID (non-expired) creds
# object when loaded via from_authorized_user_info.
def _future_expiry() -> str:
    future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
    return future.strftime("%Y-%m-%dT%H:%M:%SZ")


def _base_token_info(expiry: str | None = None) -> dict:
    info = {
        "token": "ya29.valid-token",
        "refresh_token": "1//refresh-token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "test-client-id.apps.googleusercontent.com",
        "client_secret": "test-secret",
        "scopes": SCOPES,
    }
    if expiry is not None:
        info["expiry"] = expiry
    return info


def _write_token(path, info: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(info))


# ---------------------------------------------------------------------------
# Test 1 — token_path is under app_dir and named correctly
# ---------------------------------------------------------------------------

def test_token_path_under_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    p = auth.token_path()
    assert p.name == "google_token.json"
    assert str(p).startswith(str(config.app_dir()))


# ---------------------------------------------------------------------------
# Test 2 — valid (non-expired) token loads without calling refresh
# ---------------------------------------------------------------------------

def test_load_valid_token_returns_without_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    token_file = auth.token_path()
    _write_token(token_file, _base_token_info(expiry=_future_expiry()))

    refresh_called = []

    def fake_refresh(self, request):
        refresh_called.append(True)

    monkeypatch.setattr(auth.Credentials, "refresh", fake_refresh)

    creds = auth.load_credentials(scopes=SCOPES)
    assert creds is not None
    assert not refresh_called, "refresh should NOT be called for a valid token"


# ---------------------------------------------------------------------------
# Test 3 — expired token with refresh_token is refreshed and rewritten
# ---------------------------------------------------------------------------

def test_load_expired_token_refreshes_and_rewrites(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    token_file = auth.token_path()
    _write_token(token_file, _base_token_info(expiry="2020-01-01T00:00:00Z"))

    def fake_refresh(self, request):
        # Simulate what a real refresh does: updates token and clears expiry.
        self.token = "refreshed-xyz"
        self.expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    monkeypatch.setattr(auth.Credentials, "refresh", fake_refresh)

    creds = auth.load_credentials(scopes=SCOPES)
    assert creds.token == "refreshed-xyz"
    saved = json.loads(token_file.read_text())
    assert saved["token"] == "refreshed-xyz"


# ---------------------------------------------------------------------------
# Test 4 — missing token file raises RuntimeError mentioning consent flow
# ---------------------------------------------------------------------------

def test_load_missing_token_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    # Ensure token file does not exist
    token_file = auth.token_path()
    assert not token_file.exists()

    with pytest.raises(RuntimeError, match="consent"):
        auth.load_credentials(scopes=SCOPES)


# ---------------------------------------------------------------------------
# Test 5 — run_consent_flow writes token file (browser/network mocked)
# ---------------------------------------------------------------------------

def test_run_consent_flow_writes_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    token_file = auth.token_path()

    expected_json = json.dumps({"token": "consent-token", "source": "flow"})

    class FakeCreds:
        def to_json(self):
            return expected_json

    class FakeFlow:
        def run_local_server(self, port=0):
            return FakeCreds()

    # Use a dummy path — from_client_secrets_file is fully mocked
    dummy_secrets = tmp_path / "client_secret.json"
    dummy_secrets.write_text("{}")

    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: FakeFlow()),
    )

    creds = auth.run_consent_flow(scopes=SCOPES, client_secrets=dummy_secrets)
    assert isinstance(creds, FakeCreds)
    assert token_file.exists()
    assert token_file.read_text() == expected_json
    # The connection-status cache is refreshed inline so a fresh sign-in isn't
    # masked by a stale "expired" entry: connections.json now carries google.
    conn = json.loads((token_file.parent / "connections.json").read_text())
    assert "google" in conn


# ---------------------------------------------------------------------------
# G1 — build_google_services
# ---------------------------------------------------------------------------

ALL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class _FakeCreds:
    def __init__(self, scopes):
        self.scopes = scopes


def _spy_build_service(monkeypatch):
    """Replace auth.build_service with a spy returning (api, version) sentinels."""
    calls = []

    def fake_build(api, version, creds):
        calls.append((api, version))
        return f"{api}:{version}"

    monkeypatch.setattr(auth, "build_service", fake_build)
    return calls


def test_build_google_services_all_scopes(monkeypatch):
    _spy_build_service(monkeypatch)
    creds = _FakeCreds(ALL_SCOPES)

    services = auth.build_google_services(creds=creds)

    assert services["gmail_service"] == "gmail:v1"
    assert services["calendar_service"] == "calendar:v3"
    assert services["drive_service"] == "drive:v3"
    assert len(services) == 3


def test_build_google_services_uses_passed_creds_not_load(monkeypatch):
    _spy_build_service(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("load_credentials must not be called when creds passed")

    monkeypatch.setattr(auth, "load_credentials", boom)
    creds = _FakeCreds(ALL_SCOPES)

    services = auth.build_google_services(creds=creds)
    assert set(services) == {"gmail_service", "calendar_service", "drive_service"}


def test_build_google_services_loads_when_creds_none(tmp_path, monkeypatch):
    # Isolate the token path so the helper doesn't read a real on-disk token;
    # with no file present it falls back to creds.scopes (ALL_SCOPES) -> 3.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    _spy_build_service(monkeypatch)
    loaded = []

    def fake_load(scopes=None, token_file=None):
        loaded.append(True)
        return _FakeCreds(ALL_SCOPES)

    monkeypatch.setattr(auth, "load_credentials", fake_load)

    services = auth.build_google_services()
    assert loaded, "load_credentials should be called when creds is None"
    assert len(services) == 3


def test_build_google_services_skips_absent_scope(monkeypatch):
    _spy_build_service(monkeypatch)
    # No calendar scope granted.
    creds = _FakeCreds([
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ])

    services = auth.build_google_services(creds=creds)

    assert "calendar_service" not in services
    assert services["gmail_service"] == "gmail:v1"
    assert services["drive_service"] == "drive:v3"


def test_build_google_services_per_service_build_error_omitted(monkeypatch):
    calls = []

    def fake_build(api, version, creds):
        calls.append((api, version))
        if api == "drive":
            raise RuntimeError("drive build failed")
        return f"{api}:{version}"

    monkeypatch.setattr(auth, "build_service", fake_build)
    creds = _FakeCreds(ALL_SCOPES)

    services = auth.build_google_services(creds=creds)

    assert "drive_service" not in services
    assert services["gmail_service"] == "gmail:v1"
    assert services["calendar_service"] == "calendar:v3"


def test_build_google_services_no_scopes_builds_all(monkeypatch):
    _spy_build_service(monkeypatch)
    # creds with no .scopes populated -> lenient, build all three.
    creds = _FakeCreds(None)

    services = auth.build_google_services(creds=creds)
    assert len(services) == 3


# ---------------------------------------------------------------------------
# G2 — embedded OAuth client
# ---------------------------------------------------------------------------

def _client_config_dict(cid="bundled.apps.googleusercontent.com"):
    return {
        "installed": {
            "client_id": cid,
            "project_id": "p",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_secret": "s",
            "redirect_uris": ["http://localhost"],
        }
    }


def test_embedded_client_config_none_when_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPBRAIN_GOOGLE_CLIENT", raising=False)
    # Point the bundled-file lookup at a tmp dir with nothing in it.
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: tmp_path / "nope.json")
    assert auth.embedded_client_config() is None


def test_embedded_client_config_from_env(tmp_path, monkeypatch):
    cfg = tmp_path / "client.json"
    cfg.write_text(json.dumps(_client_config_dict("env-client")))
    monkeypatch.setenv("MCPBRAIN_GOOGLE_CLIENT", str(cfg))
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: tmp_path / "nope.json")

    out = auth.embedded_client_config()
    assert out["installed"]["client_id"] == "env-client"


def test_embedded_client_config_env_precedence_over_bundled(tmp_path, monkeypatch):
    env_cfg = tmp_path / "env.json"
    env_cfg.write_text(json.dumps(_client_config_dict("env-client")))
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps(_client_config_dict("bundled-client")))

    monkeypatch.setenv("MCPBRAIN_GOOGLE_CLIENT", str(env_cfg))
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: bundled)

    out = auth.embedded_client_config()
    assert out["installed"]["client_id"] == "env-client"


def test_embedded_client_config_from_bundled(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPBRAIN_GOOGLE_CLIENT", raising=False)
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps(_client_config_dict("bundled-client")))
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: bundled)

    out = auth.embedded_client_config()
    assert out["installed"]["client_id"] == "bundled-client"


def test_run_consent_flow_prefers_embedded(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    token_file = auth.token_path()
    expected_json = json.dumps({"token": "embedded-token"})

    class FakeCreds:
        def to_json(self):
            return expected_json

    class FakeFlow:
        def run_local_server(self, port=0):
            return FakeCreds()

    cfg = tmp_path / "client.json"
    cfg.write_text(json.dumps(_client_config_dict()))
    monkeypatch.setenv("MCPBRAIN_GOOGLE_CLIENT", str(cfg))

    from_config_calls = []
    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_config",
        classmethod(lambda cls, config, scopes: (from_config_calls.append(config), FakeFlow())[1]),
    )

    def boom(cls, path, scopes):
        raise AssertionError("from_client_secrets_file must not be used when embedded present")

    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_secrets_file", classmethod(boom)
    )

    creds = auth.run_consent_flow(scopes=SCOPES)
    assert isinstance(creds, FakeCreds)
    assert from_config_calls, "from_client_config should have been called"
    assert token_file.read_text() == expected_json


def test_run_consent_flow_falls_back_to_client_secrets_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("MCPBRAIN_GOOGLE_CLIENT", raising=False)
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: tmp_path / "nope.json")
    token_file = auth.token_path()
    expected_json = json.dumps({"token": "file-token"})

    class FakeCreds:
        def to_json(self):
            return expected_json

    class FakeFlow:
        def run_local_server(self, port=0):
            return FakeCreds()

    secrets = tmp_path / "client_secret.json"
    secrets.write_text(json.dumps(_client_config_dict()))

    file_calls = []
    monkeypatch.setattr(
        auth.InstalledAppFlow, "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: (file_calls.append(path), FakeFlow())[1]),
    )

    creds = auth.run_consent_flow(scopes=SCOPES, client_secrets=secrets)
    assert isinstance(creds, FakeCreds)
    assert file_calls
    assert token_file.read_text() == expected_json


def test_run_consent_flow_raises_when_no_client_available(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("MCPBRAIN_GOOGLE_CLIENT", raising=False)
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: tmp_path / "nope.json")
    # client_secrets_path() resolves under MCPBRAIN_HOME; nothing there.

    with pytest.raises(RuntimeError, match="No OAuth client"):
        auth.run_consent_flow(scopes=SCOPES)


# ---------------------------------------------------------------------------
# Fix 1 — build_google_services uses GRANTED scopes from the token file,
# not the requested creds.scopes. Exercises the REAL file path: a real
# calendar-less token on disk must skip calendar even though creds.scopes
# (the requested set) reports all three.
# ---------------------------------------------------------------------------

GMAIL_DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


class _FakeCredsGranted:
    """Mimics the real on-disk case: creds.scopes == requested (all), but
    granted_scopes is empty and the file holds only the granted subset."""

    def __init__(self, scopes, granted_scopes=None):
        self.scopes = scopes
        self.granted_scopes = granted_scopes


def test_build_google_services_granted_from_token_file_skips_calendar(tmp_path, monkeypatch):
    # Real token file on disk: only gmail + drive granted (no calendar).
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({
        "token": "ya29.x",
        "refresh_token": "1//r",
        "scopes": GMAIL_DRIVE_SCOPES,
    }))

    calls = _spy_build_service(monkeypatch)

    # Mimic the real on-disk creds: from_authorized_user_file sets .scopes to
    # the REQUESTED set (all three) and leaves granted_scopes empty.
    fake_creds = _FakeCredsGranted(scopes=ALL_SCOPES, granted_scopes=None)

    def fake_load(scopes=None, token_file=None):
        return fake_creds

    monkeypatch.setattr(auth, "load_credentials", fake_load)

    services = auth.build_google_services(token_file=token_file)

    # Calendar must be OMITTED — its scope isn't in the file's granted set...
    assert "calendar_service" not in services
    assert ("calendar", "v3") not in calls
    # ...while gmail and drive ARE built.
    assert services["gmail_service"] == "gmail:v1"
    assert services["drive_service"] == "drive:v3"


def test_granted_scopes_prefers_granted_scopes_attr(tmp_path):
    # granted_scopes wins over both the file and creds.scopes.
    token_file = tmp_path / "google_token.json"
    token_file.write_text(json.dumps({"scopes": GMAIL_DRIVE_SCOPES}))
    creds = _FakeCredsGranted(scopes=ALL_SCOPES, granted_scopes=[
        "https://www.googleapis.com/auth/gmail.readonly",
    ])
    assert auth._granted_scopes(creds, token_file) == {
        "https://www.googleapis.com/auth/gmail.readonly"
    }


def test_granted_scopes_falls_back_to_creds_scopes_without_file():
    # No token_file passed -> file step skipped -> falls back to creds.scopes.
    creds = _FakeCreds(GMAIL_DRIVE_SCOPES)
    assert auth._granted_scopes(creds, None) == set(GMAIL_DRIVE_SCOPES)


# ---------------------------------------------------------------------------
# Fix 2 — token files are written 0600 (refresh path and consent path)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_refresh_writes_token_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    token_file = auth.token_path()
    _write_token(token_file, _base_token_info(expiry="2020-01-01T00:00:00Z"))

    def fake_refresh(self, request):
        self.token = "refreshed"
        self.expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)

    monkeypatch.setattr(auth.Credentials, "refresh", fake_refresh)

    auth.load_credentials(scopes=SCOPES)
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_consent_flow_writes_token_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    token_file = auth.token_path()

    class FakeCreds:
        def to_json(self):
            return json.dumps({"token": "consent"})

    class FakeFlow:
        def run_local_server(self, port=0):
            return FakeCreds()

    dummy_secrets = tmp_path / "client_secret.json"
    dummy_secrets.write_text("{}")
    monkeypatch.setattr(
        auth.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda cls, path, scopes: FakeFlow()),
    )

    auth.run_consent_flow(scopes=SCOPES, client_secrets=dummy_secrets)
    assert oct(token_file.stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# Fix 3 — malformed client config raises a clear ValueError naming the path
# ---------------------------------------------------------------------------

def test_embedded_client_config_malformed_json_raises(tmp_path, monkeypatch):
    bad = tmp_path / "client.json"
    bad.write_text("{ this is not valid json ")
    monkeypatch.setenv("MCPBRAIN_GOOGLE_CLIENT", str(bad))

    with pytest.raises(ValueError, match=str(bad)):
        auth.embedded_client_config()


# ---------------------------------------------------------------------------
# Fix 4 — env path set but missing logs a warning, then falls back to bundled
# ---------------------------------------------------------------------------

def test_embedded_client_config_env_missing_warns_and_falls_back(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "does-not-exist.json"
    monkeypatch.setenv("MCPBRAIN_GOOGLE_CLIENT", str(missing))
    bundled = tmp_path / "bundled.json"
    bundled.write_text(json.dumps(_client_config_dict("bundled-client")))
    monkeypatch.setattr(auth, "_bundled_client_path", lambda: bundled)

    with caplog.at_level("WARNING"):
        out = auth.embedded_client_config()

    assert out["installed"]["client_id"] == "bundled-client"
    assert any(str(missing) in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# G4 — `python -m mcpbrain.auth` entry point runs the consent flow
# ---------------------------------------------------------------------------

def test_auth_main_runs_consent_flow(tmp_path, monkeypatch, capsys):
    """auth.main([]) runs the consent flow and prints the token path. The flow
    itself is mocked (no browser, no network). With no --client-secrets,
    run_consent_flow is called with client_secrets=None (use the bundled client)."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    consent_calls = []

    def fake_consent(client_secrets=None):
        consent_calls.append(client_secrets)
        return object()

    monkeypatch.setattr(auth, "run_consent_flow", fake_consent)

    auth.main([])

    assert consent_calls, "auth.main should call run_consent_flow"
    assert consent_calls[0] is None, "no --client-secrets -> client_secrets None"
    out = capsys.readouterr().out
    assert "Authorised" in out
    assert str(auth.token_path()) in out


def test_auth_main_plumbs_client_secrets_override(tmp_path, monkeypatch, capsys):
    """auth.main(["--client-secrets", PATH]) plumbs the override through to
    run_consent_flow as a Path (so the documented org override actually works)."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path / "data"))
    cs_path = tmp_path / "cs.json"
    consent_calls = []

    def fake_consent(client_secrets=None):
        consent_calls.append(client_secrets)
        return object()

    monkeypatch.setattr(auth, "run_consent_flow", fake_consent)

    auth.main(["--client-secrets", str(cs_path)])

    assert consent_calls, "auth.main should call run_consent_flow"
    from pathlib import Path as _P
    assert consent_calls[0] == _P(cs_path), "the override path must be plumbed through"
    out = capsys.readouterr().out
    assert "Authorised" in out


def test_build_google_services_empty_granted_scopes_skips_all(monkeypatch):
    """An explicitly EMPTY granted_scopes ([]) means no scopes were granted:
    build_google_services must skip ALL services rather than leniently building
    everything (which is reserved for the truly-unknown None case)."""
    calls = _spy_build_service(monkeypatch)
    # Explicit empty granted set; no token_file -> file step skipped.
    creds = _FakeCredsGranted(scopes=ALL_SCOPES, granted_scopes=[])

    services = auth.build_google_services(creds=creds)

    assert services == {}, "granted=[] must build nothing"
    assert calls == [], "no service should be built when granted is empty"
