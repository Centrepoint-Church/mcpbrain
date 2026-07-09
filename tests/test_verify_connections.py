import json
import google.oauth2.credentials as _gcreds
from mcpbrain import probes


def test_verify_writes_cache(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({
        "owner_name": "S", "owner_email": "s@x", "orgs": [{"name": "O"}],
        "timezone": "UTC"}))
    home = str(tmp_path)
    monkeypatch.setattr(probes, "_verify_google", lambda h: {"state": "ok", "detail": "token ok", "last_verified": "t"})
    probes.verify_connections(home, store=None)
    cache = json.loads((tmp_path / "connections.json").read_text())
    assert cache["google"]["detail"] == "token ok"
    assert "clickup" not in cache  # ClickUp is no longer a surfaced connection


def _patch_verify(monkeypatch, *, expired, refresh_raises):
    """Force _verify_google down its network branch with a fake credential whose
    refresh() raises `refresh_raises` (or None). Local probe is forced 'ok'."""
    monkeypatch.setattr(probes, "probe_google",
                        lambda h: {"state": "ok", "detail": "Connected", "last_verified": "t"})

    class FakeCreds:
        def __init__(self): self.expired = expired; self.refresh_token = "r"
        @classmethod
        def from_authorized_user_file(cls, path, scopes): return cls()
        def refresh(self, req):
            if refresh_raises is not None:
                raise refresh_raises
    monkeypatch.setattr(_gcreds, "Credentials", FakeCreds)


def test_verify_transient_network_error_keeps_local_state(tmp_path, monkeypatch):
    # A transient failure (offline/timeout) must NOT downgrade to "expired" —
    # it falls back to the locally-valid 'Connected'. This is the sticky-false-
    # expired bug (Trigger B).
    _patch_verify(monkeypatch, expired=True, refresh_raises=ConnectionError("network down"))
    out = probes._verify_google(str(tmp_path))
    assert out["state"] == "ok"
    assert out["detail"] != "Sign-in expired — reconnect"


def test_verify_refresh_error_is_expired(tmp_path, monkeypatch):
    # A genuine auth failure (invalid_grant → RefreshError) DOES surface as expired.
    from google.auth.exceptions import RefreshError
    _patch_verify(monkeypatch, expired=True, refresh_raises=RefreshError("invalid_grant"))
    out = probes._verify_google(str(tmp_path))
    assert out["state"] == "needs_action" and "expired" in out["detail"].lower()


def test_verify_success_is_verified(tmp_path, monkeypatch):
    _patch_verify(monkeypatch, expired=False, refresh_raises=None)  # not expired → no refresh
    out = probes._verify_google(str(tmp_path))
    assert out["state"] == "ok" and out["detail"] == "Verified"


def test_verify_skips_network_when_local_token_broken(tmp_path, monkeypatch):
    # If the local token is already broken, return that without a network call.
    monkeypatch.setattr(probes, "probe_google",
                        lambda h: {"state": "needs_action", "detail": "Access expired — reconnect", "last_verified": None})
    def _boom(*a, **k): raise AssertionError("must not touch the network")
    monkeypatch.setattr(_gcreds, "Credentials", type("C", (), {"from_authorized_user_file": staticmethod(_boom)}))
    out = probes._verify_google(str(tmp_path))
    assert out["state"] == "needs_action"
