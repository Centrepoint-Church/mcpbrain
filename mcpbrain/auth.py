"""Google OAuth helpers for mcpbrain — installed-app (user credentials) flow.

Scopes are read-only for Gmail, Calendar, and Drive.

Typical usage:
    creds = load_credentials()          # load from token file; auto-refreshes if expired
    service = build_service("gmail", "v1", creds)

First-time setup (interactive, requires a browser):
    creds = run_consent_flow()          # opens browser, writes token file
"""

import json
import logging
import os
import sys
from pathlib import Path

import httplib2
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from mcpbrain import config

# httplib2's default socket timeout is very short — long enough for small reads
# but not for large resumable uploads (the encrypted backup snapshot is ~750MB)
# or occasionally-slow Gmail/Drive reads, which surfaced as intermittent
# "read operation timed out" errors in the daemon sync log. A generous timeout
# only ever prevents a premature cut-off; fast calls are unaffected.
DEFAULT_HTTP_TIMEOUT_S = 600

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    # drive.file: write access limited to files/folders this app creates — the
    # least privilege needed for the encrypted-snapshot backup upload
    # (backup.upload_snapshot creates a per-user folder on a Shared Drive and
    # uploads into it). Read access stays via drive.readonly above.
    "https://www.googleapis.com/auth/drive.file",
]

# Basic identity scopes — let the wizard prefill the user's name + email from
# their Google account. Requested at consent time (CONSENT_SCOPES) but NOT part
# of the REQUIRED set above: load_credentials/status validate against SCOPES, so
# existing tokens (granted without these) stay valid and are NOT forced to
# re-consent. New consents include them, enabling the prefill; if a token lacks
# them, name resolution simply degrades to "". These are non-sensitive scopes
# (no verification escalation for an Internal Workspace consent screen).
IDENTITY_SCOPES = [
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
CONSENT_SCOPES = SCOPES + IDENTITY_SCOPES

# Per-service (api, version) keyed by the scope that grants it.
_SERVICE_SPECS = [
    ("gmail_service", "gmail", "v1", "https://www.googleapis.com/auth/gmail.readonly"),
    ("calendar_service", "calendar", "v3", "https://www.googleapis.com/auth/calendar.readonly"),
    ("drive_service", "drive", "v3", "https://www.googleapis.com/auth/drive.readonly"),
]


def token_path() -> Path:
    """Path to the stored OAuth token (in the per-OS app dir)."""
    return config.app_dir() / "google_token.json"


def client_secrets_path() -> Path:
    """Path to the OAuth client secrets file (in the per-OS app dir)."""
    return config.app_dir() / "client_secret.json"


def load_credentials(scopes: list[str] = SCOPES, token_file: Path | None = None) -> Credentials:
    """Load user credentials from *token_file*, refreshing if expired.

    Args:
        scopes: OAuth scopes to validate against the stored token.
        token_file: Path to the authorized-user JSON. Defaults to token_path().

    Returns:
        A valid Credentials object.

    Raises:
        RuntimeError: If no valid token exists and a consent flow is required.
    """
    if token_file is None:
        token_file = token_path()

    creds: Credentials | None = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_file.write_text(creds.to_json())
        _secure_token_file(token_file)
        return creds

    raise RuntimeError(
        f"No valid credentials found at {token_file}. "
        "Run the consent flow to authorise access: "
        "from mcpbrain.auth import run_consent_flow; run_consent_flow()"
    )


def _secure_token_file(token_file: Path) -> None:
    """Restrict the token file to owner read/write (0600).

    A refresh token is long-lived and sensitive. The default umask often
    leaves new files world-readable (0644), so tighten after each write.
    No-op on platforms where POSIX file modes aren't meaningful.
    """
    if sys.platform == "win32":
        return
    try:
        token_file.chmod(0o600)
    except OSError as exc:  # pragma: no cover — best-effort hardening
        logger.warning("Could not chmod token file %s to 0600: %s", token_file, exc)


def _bundled_client_path() -> Path:
    """Path to the OAuth client config bundled next to this module."""
    return Path(__file__).resolve().parent / "google_oauth_client.json"


def embedded_client_config() -> dict | None:
    """Return the bundled installed-app OAuth client config dict, or None.

    Resolution order:
      1. env MCPBRAIN_GOOGLE_CLIENT — path to a client config JSON (override/testing).
      2. a bundled file next to this module: auth's dir / "google_oauth_client.json".
    Returns the parsed {"installed": {...}} dict, or None if neither exists.
    The desktop client secret is non-confidential (PKCE), so bundling is intended.
    """
    env = os.getenv("MCPBRAIN_GOOGLE_CLIENT")
    if env:
        p = Path(env)
        if p.exists():
            return _load_client_config(p)
        logger.warning(
            "MCPBRAIN_GOOGLE_CLIENT set to %s but not found; falling back", p
        )

    bundled = _bundled_client_path()
    if bundled.exists():
        return _load_client_config(bundled)

    return None


def _load_client_config(p: Path) -> dict:
    """Parse an OAuth client config JSON, raising a clear error on malformed JSON."""
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid OAuth client config at {p}: {exc}") from exc


def run_consent_flow(
    scopes: list[str] = CONSENT_SCOPES,
    client_secrets: Path | None = None,
    token_file: Path | None = None,
) -> Credentials:
    """Run the interactive OAuth consent flow and write the token file.

    Opens a local browser window to complete authorisation.  Should only be
    called once per machine, or whenever the refresh token is revoked.

    Prefers the bundled/embedded OAuth client (see embedded_client_config).
    An explicit client_secrets path overrides the embedded client, for orgs
    using their own client.

    Args:
        scopes: OAuth scopes to request.
        client_secrets: Path to client_secret.json. Overrides the embedded
            client. Defaults to None (use embedded, else client_secrets_path()).
        token_file: Where to write the resulting token. Defaults to token_path().

    Returns:
        The authorised Credentials object.
    """
    if token_file is None:
        token_file = token_path()

    # Requesting userinfo scopes makes Google also return `openid`, which
    # oauthlib otherwise rejects as a scope mismatch ("Scope has changed").
    # Relaxing lets the returned (super)set through.
    import os as _os
    _os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    embedded = embedded_client_config()
    if embedded is not None and client_secrets is None:
        flow = InstalledAppFlow.from_client_config(embedded, scopes)
    else:
        cs = client_secrets or client_secrets_path()
        if not Path(cs).exists():
            raise RuntimeError(
                "No OAuth client available. Ship a bundled client "
                "(mcpbrain/google_oauth_client.json or $MCPBRAIN_GOOGLE_CLIENT) "
                f"or place a client_secret.json at {cs}. See docs/INSTALL.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(cs), scopes)

    creds = flow.run_local_server(port=0)

    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())
    _secure_token_file(token_file)
    # Refresh the connection-status cache immediately so a successful re-auth
    # doesn't keep showing a stale "Sign-in expired" (connections.json otherwise
    # only updates on the daemon's periodic network verify, so the token file is
    # valid but the UI lags). token_file.parent is the home dir. Lazy import
    # avoids the probes<->auth import cycle; never let a cache hiccup break auth.
    try:
        from mcpbrain import probes
        probes.refresh_connection_cache(token_file.parent, "google")
    except Exception:  # noqa: BLE001 — status cache is best-effort
        pass
    return creds


def fetch_google_name(creds: Credentials) -> str:
    """The Google account's display name via the userinfo API, or "" on any
    error (e.g. the token wasn't granted the userinfo.profile scope). Best-effort:
    used only to prefill the wizard's name field, so it must never raise."""
    try:
        info = build_service("oauth2", "v2", creds).userinfo().get().execute()
        return (info.get("name") or "").strip()
    except Exception:  # noqa: BLE001 — prefill nicety; degrade silently
        return ""


def build_service(api: str, version: str, creds: Credentials,
                  *, timeout_s: float = DEFAULT_HTTP_TIMEOUT_S):
    """Build a Google API service client with an explicit socket timeout.

    Args:
        api: API name, e.g. "gmail".
        version: API version, e.g. "v1".
        creds: Valid Credentials object.
        timeout_s: socket timeout for every request made through this service.

    Returns:
        A googleapiclient Resource object.

    Uses an AuthorizedHttp over httplib2.Http(timeout=...) instead of the
    default credentials= path so the timeout actually applies (the library's
    default is too short for large resumable uploads / slow reads).
    """
    authed_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=timeout_s))
    return build(api, version, http=authed_http)


def _granted_scopes(creds, token_file: Path | None = None) -> set[str] | None:
    """Determine the scopes actually GRANTED for *creds*, or None if unknown.

    google-auth's Credentials.from_authorized_user_file(file, scopes) sets
    creds.scopes to the PASSED scopes (used only for validation), not the
    server-granted set. So a calendar-less token still reports all requested
    scopes via creds.scopes. The granted set must come from a reliable source.

    Resolution order:
      1. creds.granted_scopes — server-confirmed, populated after a refresh.
      2. the token file's stored "scopes" list — written by to_json() at
         consent. Only consulted when *token_file* is resolvable (passed in,
         or the file creds was loaded from).
      3. creds.scopes — last resort (may be the requested set, not granted).
      4. None — caller treats this as lenient (build everything).
    """
    granted = getattr(creds, "granted_scopes", None)
    if granted is not None:
        # An explicitly empty list means "no scopes granted" — honour it
        # deterministically rather than falling through to the lenient case.
        return set(granted)

    if token_file is not None:
        try:
            if token_file.exists():
                data = json.loads(token_file.read_text())
                stored = data.get("scopes")
                if stored:
                    return set(stored)
        except (OSError, json.JSONDecodeError):
            pass

    scopes = getattr(creds, "scopes", None)
    if scopes:
        return set(scopes)

    return None


def build_google_services(creds=None, *, scopes=SCOPES, token_file=None) -> dict:
    """Build the Google API service clients for sync from user credentials.

    Loads credentials via load_credentials() if creds is None. Returns a dict
    suitable for run_sync_cycle / Daemon: {"gmail_service":..., "calendar_service":...,
    "drive_service":...}. A service whose scope the token lacks is OMITTED
    gracefully (e.g. a token without calendar.readonly -> no calendar_service),
    rather than failing the whole build.
    """
    if creds is None:
        # Resolve the on-disk path so granted scopes can be read from the
        # token file creds is loaded from (creds.scopes only reports the
        # requested set, not what the server actually granted).
        if token_file is None:
            token_file = token_path()
        creds = load_credentials(scopes=scopes, token_file=token_file)

    granted = _granted_scopes(creds, token_file)

    services: dict = {}
    for key, api, version, scope in _SERVICE_SPECS:
        # Lenient: if the granted scopes are unknown, build everything.
        if granted is not None and scope not in granted:
            continue
        try:
            services[key] = build_service(api, version, creds)
        except Exception as exc:  # noqa: BLE001 — one bad service must not abort the rest
            logger.warning("Skipping %s: build failed: %s", key, exc)

    return services


def main(argv=None) -> None:
    """CLI entry point: `python -m mcpbrain.auth` runs the consent flow.

    --client-secrets PATH points the flow at your own OAuth client_secret.json,
    overriding the bundled client (for orgs using their own client).
    """
    import argparse

    ap = argparse.ArgumentParser(prog="mcpbrain.auth")
    ap.add_argument(
        "--client-secrets", default=None,
        help="path to your own OAuth client_secret.json (overrides the bundled client)",
    )
    args = ap.parse_args(argv)
    cs = Path(args.client_secrets) if args.client_secrets else None
    run_consent_flow(client_secrets=cs)
    print(f"Authorised. Token written to {token_path()}")


if __name__ == "__main__":
    main()
