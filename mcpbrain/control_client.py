"""A thin client for the daemon's loopback control API.

The menu-bar tray (and any other local UI) is a separate process from the
launchd/systemd-managed daemon, so it cannot hold a Daemon object. It talks to
the daemon over the token-guarded loopback control API instead. This client
reads the ``control_port`` / ``control_token`` files the daemon writes into
MCPBRAIN_HOME and calls the API with stdlib urllib only.

Every call degrades gracefully: if the daemon is not running (no port file, or
the connection is refused) the methods raise ``DaemonUnavailable`` rather than a
raw socket error, so callers can show "daemon not running" instead of crashing.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from mcpbrain.config import app_dir


class DaemonUnavailable(Exception):
    """The control API could not be reached (daemon not running / no port)."""


class ControlClient:
    def __init__(self, home=None, timeout: float = 5.0):
        self._home = app_dir() if home is None else Path(home)
        self._timeout = timeout

    # -- connection details (re-read each call: port/token change per daemon run)
    def _endpoint(self):
        port_file = self._home / "control_port"
        token_file = self._home / "control_token"
        try:
            port = int(port_file.read_text().strip())
            token = token_file.read_text().strip()
        except (OSError, ValueError) as exc:
            raise DaemonUnavailable("control port/token not found") from exc
        return f"http://127.0.0.1:{port}", token

    def _request(self, path: str, method: str = "GET"):
        base, token = self._endpoint()
        req = urllib.request.Request(base + path, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if method == "POST":
            req.data = b"{}"
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except (urllib.error.URLError, OSError) as exc:
            raise DaemonUnavailable(str(exc)) from exc
        return json.loads(raw) if raw else {}

    def is_running(self) -> bool:
        try:
            self.status()
            return True
        except DaemonUnavailable:
            return False

    def status(self) -> dict:
        return self._request("/api/status")

    def pause(self) -> dict:
        return self._request("/api/pause", method="POST")

    def resume(self) -> dict:
        return self._request("/api/resume", method="POST")

    def reconnect_google(self) -> dict:
        """POST /api/auth/start (re-run the OAuth consent flow)."""
        return self._request("/api/auth/start", method="POST")

    def sync_now(self) -> dict:
        """Wake the daemon for an immediate sync->drain->prepare cycle."""
        return self._request("/api/sync-now", method="POST")

    def start_enrich_backfill(self) -> dict:
        return self._request("/api/enrich-backfill/start", method="POST")

    def cancel_enrich_backfill(self) -> dict:
        return self._request("/api/enrich-backfill/cancel", method="POST")

    def bootstrap_baseline(self) -> dict:
        """POST /api/bootstrap-baseline — import the org snapshot + shared-drive
        ingest caches (re-runnable; idempotent daemon-side)."""
        return self._request("/api/bootstrap-baseline", method="POST")

    def wizard_url(self) -> str:
        """The local setup-page URL, or '' if the daemon is not running."""
        try:
            base, _ = self._endpoint()
        except DaemonUnavailable:
            return ""
        return base + "/"

    def dashboard_url(self) -> str:
        """The local dashboard URL, or '' if the daemon is not running."""
        try:
            base, _ = self._endpoint()
        except DaemonUnavailable:
            return ""
        return base + "/dashboard"
