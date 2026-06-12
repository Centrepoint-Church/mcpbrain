import json
import logging
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

def _write_private(path, text):
    """Write text to path at mode 0600 with no world-readable window.

    The temp file is created 0600 via fchmod, written through the fd, then
    atomically renamed over the target with os.replace — so no reader ever
    sees it at a wider mode or half-written. Mirrors config.write_config.
    """
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    try:
        if hasattr(os, "fchmod"):  # POSIX-only; mkstemp is already owner-only on Windows
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def h_json(h, code, obj):
    b = json.dumps(obj).encode()
    h.send_response(code); h.send_header("Content-Type","application/json")
    h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)

class ControlServer:
    def __init__(self, daemon, home, store=None):
        self.daemon = daemon; self.home = Path(home)
        self.store = store  # may be None if dashboard not enabled
        self.token = secrets.token_urlsafe(32); self._httpd = None; self.port = None

    def start(self):
        server = self
        class H(BaseHTTPRequestHandler):
            # Bound a slow/stuck client (e.g. one that sends a Content-Length
            # header then no body): BaseHTTPRequestHandler applies this as the
            # request socket read timeout, freeing the handler thread instead of
            # tying it up forever. Comfortably above the wizard's normal fetches.
            timeout = 30
            def log_message(self, *a): pass
            def _auth_ok(self):
                if self.headers.get("Authorization") != f"Bearer {server.token}":
                    # Close on failure: a keep-alive client with an unread body
                    # must not reuse this connection with the stale body as the
                    # next request.
                    self.close_connection = True
                    self.send_response(401); self.end_headers(); return False
                # Defence-in-depth against DNS-rebinding aimed at the browser wizard
                # (Task 2.3 serves GET / to a real browser). Not the primary control:
                # the socket already binds 127.0.0.1 and the bearer token is the real gate.
                # Don't remove this as redundant.
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in ("127.0.0.1", "localhost"):
                    self.close_connection = True
                    self.send_response(403); self.end_headers(); return False
                return True
            def do_GET(self):
                # Served before the auth gate on purpose: a browser with no token yet
                # must load the wizard page, which then injects the token into the HTML.
                if self.path == "/": return server._serve_wizard(self)   # Task 2.3
                if self.path == "/dashboard": return server._serve_dashboard(self)
                # Served before the auth gate: a browser <img> tag carries no
                # token, so any /img/ request must be answered here. The whole
                # /img/ prefix is claimed (not just a matching name) so an
                # unknown or traversal-shaped name 404s rather than slipping
                # through to the auth gate and confusing the browser with a 401.
                if self.path.startswith("/img/"):
                    m = re.match(r"^/img/([A-Za-z0-9._-]+\.png)$", self.path)
                    return server._serve_image(self, m.group(1) if m else "")
                if not self._auth_ok(): return
                if self.path == "/api/status": return h_json(self, 200, server.daemon.status())
                if self.path == "/api/config":
                    return h_json(self, 200, server.daemon.config_profile())
                if self.path == "/api/timezones":
                    from mcpbrain import timezones
                    return h_json(self, 200,
                                  {"zones": timezones.zone_options(now=datetime.now(timezone.utc))})
                if self.path == "/api/auth/status":
                    st = server.daemon.status()
                    return h_json(self, 200, {"connected": st["google_connected"],
                                              "granted_scopes": st["granted_scopes"]})
                if self.path == "/api/dashboard/today":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    # Same JSON-error contract as _handle_post: a raise here would
                    # otherwise drop the connection and the page could only say
                    # "Could not load data" with no cause.
                    try:
                        from mcpbrain import dashboard as dash
                        return h_json(self, 200, dash.assemble(server.store, str(server.home)))
                    except Exception as exc:
                        log.exception("dashboard today failed")
                        return h_json(self, 500, {"error": str(exc)})
                m = re.match(r"^/api/meeting-packs/([^?]+)$", self.path)
                if m:
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    event_id = m.group(1)
                    pack = server.store.get_meeting_pack(event_id)
                    if pack is None:
                        return h_json(self, 404, {"error": "pack not found"})
                    return h_json(self, 200, pack)
                self.send_response(404); self.end_headers()
            def do_POST(self):
                if not self._auth_ok(): return
                return server._handle_post(self)                        # Task 2.2
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._httpd.server_address[1]
        self.home.mkdir(parents=True, exist_ok=True)
        for name, val in (("control_token", self.token), ("control_port", str(self.port))):
            _write_private(str(self.home / name), val)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd: self._httpd.shutdown()

    def _serve_wizard(self, h):
        p = Path(__file__).parent / "wizard" / "index.html"
        if not p.exists():
            b = b"wizard/index.html not found (packaging error)"
            h.send_response(500); h.send_header("Content-Type","text/plain")
            h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)
            return
        html = p.read_text().replace("__MCPBRAIN_TOKEN__", self.token).encode()
        h.send_response(200); h.send_header("Content-Type","text/html")
        h.send_header("Content-Length", str(len(html))); h.end_headers(); h.wfile.write(html)

    def _serve_dashboard(self, h):
        p = Path(__file__).parent / "wizard" / "dashboard.html"
        if not p.exists():
            b = b"wizard/dashboard.html not found (packaging error)"
            h.send_response(500); h.send_header("Content-Type","text/plain")
            h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)
            return
        html = p.read_text().replace("__MCPBRAIN_TOKEN__", self.token).encode()
        h.send_response(200); h.send_header("Content-Type","text/html")
        h.send_header("Content-Length", str(len(html))); h.end_headers(); h.wfile.write(html)

    def _serve_image(self, h, name):
        root = (Path(__file__).parent / "wizard" / "img").resolve()
        p = (root / name).resolve()
        if root not in p.parents or not p.is_file():
            h.send_response(404); h.end_headers(); return
        data = p.read_bytes()
        h.send_response(200); h.send_header("Content-Type", "image/png")
        h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)

    def _handle_post(self, h):
        # Clamp Content-Length to a non-negative int. A negative or unparseable
        # value would otherwise slip past the size cap below and turn the
        # rfile.read(length) into read(-N), which misbehaves; treat anything
        # invalid as an empty body.
        try:
            length = max(0, int(h.headers.get("Content-Length") or 0))
        except (TypeError, ValueError):
            length = 0
        # Cap the body before reading it. 1 MiB is ample for config/register
        # payloads and stops a client claiming a huge length from making us
        # buffer it all.
        if length > 1_048_576:
            return h_json(h, 413, {"error": "body too large"})
        try:
            body = json.loads(h.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            return h_json(h, 400, {"error": "invalid JSON body"})
        if not isinstance(body, dict):
            return h_json(h, 400, {"error": "body must be a JSON object"})
        d = self.daemon
        # A handler that raises would otherwise surface as an opaque 500 (or a
        # dropped connection) and the wizard could only say "Failed". Return the
        # error text as JSON so the cause is visible in the browser and the log.
        try:
            if h.path == "/api/pause":   d.pause();  return h_json(h, 200, {"paused": True})
            if h.path == "/api/resume":  d.resume(); return h_json(h, 200, {"paused": False})
            # /api/config carries the Gemini key. The control API is loopback-only
            # over plain HTTP by design, so the key travels in cleartext on
            # localhost. HTTPS-on-loopback is deliberately avoided: the self-signed
            # cert UX is worse than the threat it removes. The bearer token plus the
            # 127.0.0.1 bind are the protections.
            if h.path == "/api/config":  d.apply_config(body); return h_json(h, 200, {"ok": True})
            if h.path == "/api/auth/start":
                threading.Thread(target=d.start_auth, daemon=True).start()
                return h_json(h, 202, {"started": True})
            if h.path == "/api/enrich-backfill/start":
                threading.Thread(target=d.start_enrich_backfill, daemon=True).start()
                return h_json(h, 202, {"started": True})
            if h.path == "/api/enrich-backfill/cancel":
                d.cancel_enrich_backfill(); return h_json(h, 200, {"cancelled": True})
            if h.path == "/api/records/scaffold":
                from mcpbrain import records
                return h_json(h, 200, {"scaffolded": records.scaffold_records(str(self.home))})
            if h.path == "/api/hooks/install":
                from mcpbrain import hooks
                hooks.install_session_hooks()
                return h_json(h, 200, {"installed": True})

            m = re.match(r"^/api/dashboard/actions/(\d+)/done$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import dashboard as dash
                action_id = int(m.group(1))
                ok = dash.mark_done(self.store, action_id)
                return h_json(h, 200, {"done": ok})

            m = re.match(r"^/api/dashboard/actions/(\d+)/snooze$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import dashboard as dash
                action_id = int(m.group(1))
                until = body.get("until", "")
                # Bad date is a client error, not a server fault: validate before
                # the success/404 split so garbage returns 400, not 500.
                try:
                    ok = dash.snooze(self.store, action_id, until)
                except ValueError:
                    return h_json(h, 400, {"error": "invalid date"})
                if not ok:
                    # Mirrors the dismiss route: nothing to snooze (unknown id or
                    # already closed) is a 404, not a success-shaped 200.
                    return h_json(h, 404, {"error": "action not found or not open"})
                return h_json(h, 200, {"snoozed": True})

            m = re.match(r"^/api/dashboard/findings/(\d+)/dismiss$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                finding_id = int(m.group(1))
                ok = self.store.resolve_finding(finding_id)
                if not ok:
                    return h_json(h, 404, {"error": "finding not found or already dismissed"})
                self.store.record_change("finding_dismissed", ref_id=str(finding_id))
                return h_json(h, 200, {"dismissed": True})

            if h.path == "/api/meeting-packs/upsert":
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                event_id = body.get("event_id", "").strip()
                if not event_id:
                    return h_json(h, 400, {"error": "event_id required"})
                self.store.upsert_meeting_pack(
                    event_id=event_id,
                    event_title=body.get("event_title", ""),
                    event_date=body.get("event_date", ""),
                    pack_text=body.get("pack_text", ""),
                    attendees=body.get("attendees") or [],
                    cowork_session=body.get("cowork_session", ""),
                )
                return h_json(h, 200, {"ok": True})

            if h.path == "/api/session/ingest":
                title = body.get("title", "").strip()
                content = body.get("content", "").strip()
                if not title or not content:
                    return h_json(h, 400, {"error": "title and content required"})
                from mcpbrain.capture import write_capture
                envelope = {
                    "kind": "ingest",
                    "source": "stop_hook",
                    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "title": title,
                    "content": content,
                    "tags": str(body.get("tags") or "session"),
                    "observation_type": "note",
                }
                p = write_capture(str(self.home), envelope)
                return h_json(h, 200, {"queued": True, "path": str(p)})

        except Exception as exc:
            log.exception("control API POST %s failed", h.path)
            return h_json(h, 500, {"error": str(exc)})
        return h_json(h, 404, {"error": "not found"})
