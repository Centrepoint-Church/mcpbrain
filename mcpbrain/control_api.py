import json, logging, os, re, secrets, tempfile, threading
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
                if not self._auth_ok(): return
                if self.path == "/api/status": return h_json(self, 200, server.daemon.status())
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
            if h.path == "/api/register": return h_json(h, 200, {"config_path": d.register()})

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
                ok = dash.snooze(self.store, action_id, until)
                return h_json(h, 200, {"snoozed": ok})

        except Exception as exc:
            log.exception("control API POST %s failed", h.path)
            return h_json(h, 500, {"error": str(exc)})
        return h_json(h, 404, {"error": "not found"})
