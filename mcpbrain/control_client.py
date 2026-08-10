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


class ToolExecutionError(Exception):
    """The daemon WAS reached and it refused or failed a tool call.

    Deliberately not a DaemonUnavailable: "the daemon is not running" is an
    environment condition a caller can explain to a user, while this is the
    remote analogue of a local handler raising. Collapsing the two would report
    a genuine daemon-side fault as an absent daemon and lose the real cause --
    the message here is the endpoint's own error text, and the daemon has
    already logged the full traceback on its side.
    """


# Statuses the control API emits BEFORE (or INSTEAD OF) reaching a tool handler.
# Every one of them means "this request never ran as a tool call", so they belong
# to DaemonUnavailable's meaning ("could not talk to the daemon") and not to
# ToolExecutionError's ("the handler ran and failed"):
#   401 -- the auth gate rejected the bearer token. Seen for real when a daemon
#          restart rewrites control_port/control_token non-atomically and a call
#          lands mid-window with the new port and the stale token.
#   403 -- the non-loopback guard.
#   404 -- no such route: version skew, e.g. an MCP server on a newer wheel
#          calling a still-old daemon that has no /api/tool yet.
#   413 -- the request body exceeded the control API's 1 MiB cap, so it was
#          refused unread.
# Note 400 is deliberately NOT here: /api/tool answers 400 for a Daemon.call_tool
# ValueError (unknown tool / arguments the two halves disagree about), which is a
# real, named, actionable refusal of the call -- a ToolExecutionError, so it is
# reported as itself instead of being disguised as an absent daemon.
_TRANSPORT_STATUSES = frozenset({401, 403, 404, 413})


class ControlClient:
    # Tool calls get their own, far longer timeout than the 5s the tray's
    # status/pause/resume calls use. Measured on the live store, brain_graph is
    # 6.3s median / 8.3s p95 and brain_draft_context can invoke a ~30s critique
    # subprocess, so the tray default would report the fleet's slowest tools as
    # an absent daemon on every call. This is a ceiling for a wedged daemon, not
    # a target.
    TOOL_CALL_TIMEOUT_S = 120.0

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

    def _request(self, path: str, method: str = "GET", body: dict | None = None,
                 *, timeout: float | None = None, error_body: bool = False):
        """Call the control API. Raises DaemonUnavailable for anything that means
        "could not talk to the daemon".

        `error_body=True` opts INTO reading a 4xx/5xx response's JSON body and
        returning it, instead of collapsing it into DaemonUnavailable -- except
        for the _TRANSPORT_STATUSES, which stay DaemonUnavailable because they are
        raised before any handler runs. Only call_tool wants the body at all (see
        ToolExecutionError); every other method keeps the original behaviour
        untouched, because the tray's callers act on "reachable or not" and
        nothing else.
        """
        base, token = self._endpoint()
        req = urllib.request.Request(base + path, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        if method == "POST":
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(body or {}).encode()
        try:
            with urllib.request.urlopen(
                    req, timeout=self._timeout if timeout is None else timeout) as resp:
                raw = resp.read()
        # HTTPError FIRST: it subclasses both URLError and OSError, so the
        # broader handler below would otherwise swallow it and its body.
        except urllib.error.HTTPError as exc:
            # Transport/auth/routing failures stay DaemonUnavailable even when the
            # caller asked for the error body: they did not come from a handler,
            # so presenting them as a tool failure would both misattribute the
            # fault and (for call_tool) turn a recoverable "daemon unreachable"
            # isError result into an uncaught raise. Checked BEFORE reading the
            # body because most of them have none -- the auth gate sends bare
            # headers, and `exc.read() or b"{}"` would parse that empty body into
            # a clean `{}` that looks exactly like a handler error with no text.
            if not error_body or exc.code in _TRANSPORT_STATUSES:
                raise DaemonUnavailable(str(exc)) from exc
            try:
                parsed = json.loads(exc.read() or b"{}")
            except (ValueError, OSError) as read_exc:
                # No readable JSON body (a truncated response, an HTML error page
                # from something that is not our daemon): the caller has nothing
                # to act on, so this is indistinguishable from not reaching the
                # daemon at all.
                raise DaemonUnavailable(str(exc)) from read_exc
            if not isinstance(parsed, dict):
                # Valid JSON, but not the {"result"/"error": ...} envelope every
                # endpoint speaks. Same conclusion: whatever answered is not the
                # control API mid-tool-call.
                raise DaemonUnavailable(f"{exc}: unexpected response body")
            return parsed
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

    def recall(self, query: str, limit: int = 10) -> list[dict]:
        """Semantic search via the daemon (embeds server-side). [] if daemon down."""
        try:
            r = self._request("/api/recall", method="POST",
                              body={"query": query, "limit": limit})
        except DaemonUnavailable:
            return []
        return r.get("results", [])

    def call_tool(self, name: str, arguments: dict):
        """Execute a Store-touching MCP tool IN the daemon and return its result.

        The thin-adapter path: the MCP server holds the protocol, the daemon
        holds the Store. Two failure modes, kept apart on purpose --
        DaemonUnavailable (not running, unreachable, or rejected before a handler
        ran: auth, routing, body cap -- which the MCP server turns into a readable
        isError result) and ToolExecutionError (a handler ran and failed, or the
        executor refused a tool/arguments it does not recognise).

        Returns the handler's return value verbatim, including None: `"result"`
        is present in every success body, so absence -- not falsiness -- is what
        marks an error.
        """
        r = self._request("/api/tool", method="POST",
                          body={"name": name, "arguments": arguments},
                          timeout=self.TOOL_CALL_TIMEOUT_S, error_body=True)
        if "result" not in r:
            raise ToolExecutionError(r.get("error") or f"tool call failed: {name}")
        return r["result"]

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

    def model_status(self) -> dict:
        return self._request("/api/model/status")

    def ensure_model(self) -> dict:
        return self._request("/api/model/ensure", method="POST")

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
