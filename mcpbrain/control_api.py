import json
import logging
import os
import re
import secrets
import tempfile
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

def _log_recall_exposures(store, results: list[dict], session_id: str) -> None:
    """Fire-and-forget: log exposure events for recalled chunks (S2 signal).

    Silently swallows all errors so recall is never disrupted by feedback I/O.
    """
    try:
        from mcpbrain.feedback import record_exposures
        doc_ids = [r["doc_id"] for r in results if r.get("doc_id")]
        record_exposures(store, doc_ids, session_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("_log_recall_exposures error (swallowed): %s", exc)


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

# GET route -> source_controls list function.
_CHUNK_LIST_ROUTES = {"/api/chunks/cold": "list_cold",
                      "/api/chunks/waiting": "list_waiting",
                      "/api/chunks/reflow-gave-up": "list_reflow_gave_up"}
_SYNC_SOURCES = ("gmail", "drive", "calendar", "anarlog")
_STATIC_TYPES = {".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8",
                 ".woff2": "font/woff2",
                 ".txt": "text/plain; charset=utf-8"}


def h_json(h, code, obj):
    b = json.dumps(obj).encode()
    h.send_response(code); h.send_header("Content-Type","application/json")
    h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)


def worded(reason: str, **extra) -> dict:
    """A refusal written for the page: console.js shows `reason` only when the
    body carries "worded": true (anything else gets its fixed sentence)."""
    return {"ok": False, **extra, "reason": reason, "worded": True}


def h_text(h, code, text):
    b = text.encode("utf-8")
    h.send_response(code); h.send_header("Content-Type", "text/plain; charset=utf-8")
    h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)


class RestoreHoldRefused(RuntimeError):
    """Daemon.call_tool refused a store-writing tool during the restore hold."""

class ControlServer:
    def __init__(self, daemon, home, store=None):
        self.daemon = daemon; self.home = Path(home)
        self.store = store  # may be None if dashboard not enabled
        self.token = secrets.token_urlsafe(32); self._httpd = None; self.port = None
        from mcpbrain import system_ops
        self.diagnostics = system_ops.Diagnostics()   # one background doctor run

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
            def _restoring(self):
                # A restored snapshot is being swapped in (Daemon._restore_gate):
                # answer every authenticated route 503 for that brief window
                # rather than touch a store mid-replace.
                probe = getattr(server.daemon, "is_restoring", None)
                if callable(probe) and probe() is True:
                    h_json(self, 503, {"error": "restoring"})
                    return True
                return False
            def do_GET(self):
                # Served before the auth gate on purpose: a browser with no token yet
                # must load the wizard page, which then injects the token into the HTML.
                if self.path == "/": return server._serve_wizard(self)   # Task 2.3
                if self.path == "/dashboard": return server._serve_dashboard(self)
                if self.path == "/settings": return server._serve_settings(self)
                # Served before the auth gate: a browser <img> tag carries no
                # token, so any /img/ request must be answered here. The whole
                # /img/ prefix is claimed (not just a matching name) so an
                # unknown or traversal-shaped name 404s rather than slipping
                # through to the auth gate and confusing the browser with a 401.
                if self.path.startswith("/img/"):
                    m = re.match(r"^/img/([A-Za-z0-9._-]+\.png)$", self.path)
                    return server._serve_image(self, m.group(1) if m else "")
                # The graph page's state lives in its query string
                # (?type=, ?entity=, ?show=unlinked, ?view=circles, ?since=, ?rel=).
                if self.path.split("?")[0] == "/graph": return server._serve_graph(self)
                if self.path.startswith("/vendor/"):
                    m = re.match(r"^/vendor/([A-Za-z0-9._-]+\.js)$", self.path)
                    return server._serve_vendor(self, m.group(1) if m else "")
                # The console's shared stylesheet, script and fonts. Before the
                # auth gate for the same reason as /vendor/: <link> and
                # <script src> carry no token. The whole /static/ prefix is
                # claimed so anything else 404s, and the token is never
                # injected into these files.
                if self.path.startswith("/static/"):
                    m = re.match(r"^/static/(fonts/)?([A-Za-z0-9._-]+\.(?:css|js|woff2|txt))$",
                                 self.path)
                    return server._serve_static(self, (m.group(1) or "", m.group(2))
                                                if m else ("", ""))
                if not self._auth_ok(): return
                if self._restoring(): return
                if self.path == "/api/status": return h_json(self, 200, server.daemon.status())
                if self.path == "/api/model/status":
                    return h_json(self, 200, server.daemon.model_status())
                if self.path == "/api/core":
                    # B2: return the always-injected core block for prompt_recall.py
                    if server.store is None:
                        return h_json(self, 200, {"core_block": ""})
                    try:
                        from mcpbrain.memory_tier import get_core_block
                        block = get_core_block(server.store, str(server.home))
                        return h_json(self, 200, {"core_block": block})
                    except Exception:
                        return h_json(self, 200, {"core_block": ""})
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
                if self.path == "/api/dashboard/stats":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    # Same JSON-error contract as the other dashboard reads: a
                    # raise here would drop the connection and the page could only
                    # say "Could not load data" with no cause.
                    try:
                        from mcpbrain import dashboard as dash
                        return h_json(self, 200, dash.stats(
                            server.store, str(server.home), server.daemon.status()))
                    except Exception as exc:
                        log.exception("dashboard stats failed")
                        return h_json(self, 500, {"error": str(exc)})
                if self.path.split("?")[0] in ("/api/changes", "/api/changes/added"):
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import change_undo
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    try:
                        hours = int(q.get("hours", ["24"])[0])
                    except (TypeError, ValueError):
                        return h_json(self, 400, {"error": "hours must be an integer"})
                    if not 1 <= hours <= 720:
                        return h_json(self, 400, {"error": "hours must be between 1 and 720"})
                    try:
                        if self.path.split("?")[0] == "/api/changes":
                            items = change_undo.feed(server.store, hours=hours)
                        else:
                            items = change_undo.added_since(server.store, hours=hours)
                        return h_json(self, 200, {"items": items})
                    except Exception as exc:
                        log.exception("changes feed failed")
                        return h_json(self, 500, {"error": str(exc)})
                if self.path == "/api/settings":
                    from mcpbrain import settings_api
                    return h_json(self, 200, settings_api.get_settings(server.home))
                if self.path == "/api/system":
                    from mcpbrain import system_ops
                    return h_json(self, 200, system_ops.system_info(server.daemon, server.home))
                if self.path == "/api/update/check":
                    from mcpbrain import system_ops
                    return h_json(self, 200, system_ops.check_update())
                if self.path == "/api/diagnostics":
                    return h_json(self, 200, server.diagnostics.result())
                m = re.match(r"^/api/logs/([a-z]+)$", self.path.split("?")[0])
                if m:
                    from mcpbrain import system_ops
                    try:
                        return h_text(self, 200, system_ops.tail_log(server.home, m.group(1)))
                    except ValueError:
                        return h_json(self, 404, {"error": "unknown log"})
                if self.path == "/api/support/report":
                    from mcpbrain import system_ops
                    try:
                        st = server.daemon.status()
                    except Exception:  # noqa: BLE001 — a report must still come out
                        log.exception("support report: status failed")
                        st = {}
                    return h_text(self, 200, system_ops.support_report(
                        server.home, st, diagnostics=server.diagnostics))
                if self.path.split("?")[0] in _CHUNK_LIST_ROUTES:
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import source_controls
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    try:
                        page = int(q.get("page", ["1"])[0])
                        size = int(q.get("page_size", [str(source_controls.PAGE_SIZE)])[0])
                    except (TypeError, ValueError):
                        return h_json(self, 400, {"error": "page and page_size must be integers"})
                    if page < 1 or size < 1:
                        return h_json(self, 400, {"error": "page and page_size must be at least 1"})
                    fn = getattr(source_controls, _CHUNK_LIST_ROUTES[self.path.split("?")[0]])
                    try:
                        return h_json(self, 200, fn(server.store, server.home,
                                                    page=page, page_size=size))
                    except Exception as exc:
                        log.exception("%s failed", self.path)
                        return h_json(self, 500, {"error": str(exc)})
                if self.path == "/api/maintenance":
                    # Settings > Advanced > Maintenance history: the two
                    # one-off passes, as status and doctor see them.
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        from mcpbrain import ingestion_cleanup
                        return h_json(self, 200, {
                            "reflow": server.store.reflow_status(),
                            "ingestion_cleanup": ingestion_cleanup.status(server.store)})
                    except Exception:  # noqa: BLE001 -- logged; the page gets words
                        log.exception("%s failed", self.path)
                        return h_json(self, 500, {"error": "The maintenance history "
                                                           "couldn't be read. Try again in a moment."})
                if self.path in ("/api/pending", "/api/findings"):
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        if self.path == "/api/pending":
                            from mcpbrain import auto_changes
                            items = auto_changes.list_pending(server.store)
                        else:
                            from mcpbrain import dashboard as dash
                            items = dash.findings_view(server.store)
                        return h_json(self, 200, {"items": items})
                    except Exception as exc:
                        log.exception("%s failed", self.path)
                        return h_json(self, 500, {"error": str(exc)})
                if self.path.split("?")[0] == "/api/graph/canvas":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        from mcpbrain import graph_view
                        qs = urllib.parse.urlparse(self.path).query
                        q = urllib.parse.parse_qs(qs)
                        def _int(name, default):
                            try:
                                return int(q.get(name, [default])[0])
                            except (TypeError, ValueError):
                                return default
                        result = graph_view.graph_canvas(
                            server.store,
                            min_conn=_int("min_conn", 7),
                            org=q.get("org", [""])[0],
                            community=q.get("community", [""])[0],
                            types=q.get("type", []),
                            recency_days=_int("recency_days", 0),
                            max_links=_int("max_links", 5000),
                            unlinked=q.get("unlinked", [""])[0] in ("1", "true"),
                            since_hours=max(0, _int("since_hours", 0)),
                        )
                        if isinstance(result, dict) and result.get("error") == "too_large":
                            return h_json(self, 413, result)
                        return h_json(self, 200, result)
                    except Exception as exc:
                        log.exception("graph canvas failed")
                        return h_json(self, 500, {"error": str(exc)})
                if self.path.split("?")[0] == "/api/graph/types":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import contract, graph_view
                    try:
                        return h_json(self, 200, {"types": graph_view.graph_types(server.store),
                                                  "classified": sorted(contract.ENTITY_TYPES),
                                                  "hidden": graph_view.hidden_count(server.store)})
                    except Exception as exc:
                        log.exception("graph types failed")
                        return h_json(self, 500, {"error": str(exc)})
                if self.path.split("?")[0] == "/api/graph/suppressed":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    try:
                        return h_json(self, 200, {"items": graph_view.list_suppressed(server.store)})
                    except Exception as exc:
                        log.exception("hidden entities list failed")
                        return h_json(self, 500, {"error": str(exc)})
                if self.path.split("?")[0] == "/api/graph/unlinked":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    try:
                        page = int(q.get("page", ["1"])[0])
                    except (TypeError, ValueError):
                        page = 0
                    # A page beyond a million is never a real list; past it
                    # the OFFSET would overflow SQLite's integer.
                    if page < 1 or page > graph_view.UNLINKED_PAGE_LAST:
                        return h_json(self, 400, worded(
                            "That page isn't in the list. Pages are numbered from 1."))
                    try:
                        page_size = int(q.get("page_size", ["50"])[0])
                    except (TypeError, ValueError):
                        return h_json(self, 400, worded(
                            "The number of entities a page shows has to be a whole number."))
                    try:
                        return h_json(self, 200, graph_view.list_unlinked(
                            server.store, page=page, page_size=page_size,
                            kinds=tuple(t for t in q.get("type", []) if t),
                            q=q.get("q", [""])[0]))
                    except Exception as exc:
                        log.exception("unlinked list failed")
                        return h_json(self, 500, {"error": str(exc)})
                m = re.match(r"^/api/graph/entity/([^/?]+)$", self.path.split("?")[0])
                if m:
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    d = graph_view.entity_detail(server.store, urllib.parse.unquote(m.group(1)))
                    return h_json(self, 200, d) if d else h_json(self, 404, {"error": "not found"})
                if self.path.split("?")[0] == "/api/graph/search":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                    return h_json(self, 200, graph_view.search_entities(server.store, q))
                if self.path.split("?")[0] == "/api/corrections/pending":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain.graph_corrections import _payload_entity_ids, describe
                    pending = []
                    rows = server.store.pending_corrections()
                    # Ids alone are unreadable on the card: resolve every
                    # entity id the pending payloads name, in one query.
                    ids = set()
                    for c in rows:
                        ids |= {i for i in _payload_entity_ids(c["payload"]) if isinstance(i, str)}
                    ents = server.store.get_entities(ids) if ids else {}
                    for c in rows:
                        # describe() indexes required payload keys; a pending row
                        # always has them (submit() validated before staging), but
                        # a hand-crafted/legacy row missing one must not 500 the
                        # dashboard -- fall back to the bare op name.
                        try:
                            summary = describe(c["op"], c["payload"])
                        except KeyError:
                            summary = c["op"]
                        names = {i: ents[i]["name"] for i in sorted(
                            i for i in _payload_entity_ids(c["payload"]) if i in ents)}
                        pending.append({"id": c["id"], "op": c["op"], "summary": summary,
                                        "reason": c["reason"], "created_at": c["created_at"],
                                        "names": names})
                    return h_json(self, 200, {"pending": pending})
                if self.path.split("?")[0] == "/api/graph/merge/preview":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    out = graph_view.merge_preview(server.store, q.get("loser", [""])[0], q.get("winner", [""])[0])
                    if out.get("ok"): return h_json(self, 200, out)
                    code = 404 if out.get("error") == "not_found" else 409
                    return h_json(self, code, {**out, "reason": out.get("message")
                                               or "Those two can't be merged.", "worded": True})
                if self.path.split("?")[0] == "/api/graph/ego":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    eid = q.get("id", [""])[0]
                    if not eid:
                        return h_json(self, 400, {"error": "missing id"})
                    try:
                        hops = int(q.get("hops", ["1"])[0])
                    except (TypeError, ValueError):
                        hops = 1
                    result = graph_view.graph_ego(server.store, eid, hops=hops,
                                                  relation=q.get("relation", [""])[0])
                    if result is None:
                        return h_json(self, 404, {"error": "not found"})
                    return h_json(self, 200, result)
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
                if self.path.split("?")[0] == "/api/resources/entities":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        from mcpbrain import entity_resource
                        return h_json(self, 200, {"entities": entity_resource.top_entities(server.store)})
                    except Exception as exc:
                        log.exception("entity resources list failed")
                        return h_json(self, 500, {"error": str(exc)})
                m = re.match(r"^/api/resources/entity/([^/?]+)$", self.path.split("?")[0])
                if m:
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        from mcpbrain import entity_resource
                        d = entity_resource.render_markdown(server.store, urllib.parse.unquote(m.group(1)))
                        return h_json(self, 200, d) if d else h_json(self, 404, {"error": "not found"})
                    except Exception as exc:
                        log.exception("entity resource render failed")
                        return h_json(self, 500, {"error": str(exc)})
                if self.path.split("?")[0] == "/api/resources/reply-needed":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        from mcpbrain import entity_resource
                        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                        return h_json(self, 200, {"ids": entity_resource.reply_needed_ids(server.store, q)})
                    except Exception as exc:
                        log.exception("reply-needed lookup failed")
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
                if self._restoring(): return
                return server._handle_post(self)                        # Task 2.2
            def do_DELETE(self):
                if not self._auth_ok(): return
                if self._restoring(): return
                m = re.match(r"^/api/graph/entity/([^/?]+)$", self.path.split("?")[0])
                if m:
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    eid = urllib.parse.unquote(m.group(1))
                    if server.store.get_entity(eid) is None:
                        return h_json(self, 404, {"error": "not found"})
                    return h_json(self, 200, graph_view.suppress_entity(server.store, eid))
                self.send_response(404); self.end_headers()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._httpd.server_address[1]
        self.home.mkdir(parents=True, exist_ok=True)
        for name, val in (("control_token", self.token), ("control_port", str(self.port))):
            _write_private(str(self.home / name), val)
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()

    def stop(self):
        if self._httpd: self._httpd.shutdown()

    def _serve_page(self, h, name):
        """Serve wizard/<name> with the control token injected."""
        p = Path(__file__).parent / "wizard" / name
        if not p.exists():
            b = f"wizard/{name} not found (packaging error)".encode()
            h.send_response(500); h.send_header("Content-Type","text/plain")
            h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)
            return
        html = p.read_text(encoding="utf-8").replace("__MCPBRAIN_TOKEN__", self.token).encode()
        h.send_response(200); h.send_header("Content-Type","text/html")
        h.send_header("Content-Length", str(len(html))); h.end_headers(); h.wfile.write(html)

    def _serve_wizard(self, h):
        # Setup until the install is configured; after that `/` is the Overview.
        # The redirect carries no token (the Overview injects its own).
        from mcpbrain import config as _config
        try:
            configured = _config.is_configured(str(self.home))
        except Exception:  # noqa: BLE001 — an unreadable config still gets setup
            log.exception("is_configured failed; serving setup")
            configured = False
        if configured:
            h.send_response(302); h.send_header("Location", "/dashboard")
            h.send_header("Content-Length", "0"); h.end_headers()
            return
        self._serve_page(h, "index.html")

    def _serve_dashboard(self, h):
        self._serve_page(h, "dashboard.html")

    def _serve_settings(self, h):
        self._serve_page(h, "settings.html")

    def _serve_image(self, h, name):
        root = (Path(__file__).parent / "wizard" / "img").resolve()
        p = (root / name).resolve()
        if root not in p.parents or not p.is_file():
            h.send_response(404); h.end_headers(); return
        data = p.read_bytes()
        h.send_response(200); h.send_header("Content-Type", "image/png")
        h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)

    def _serve_graph(self, h):
        p = Path(__file__).parent / "wizard" / "graph.html"
        if not p.exists():
            b = b"wizard/graph.html not found (packaging error)"
            h.send_response(500); h.send_header("Content-Type", "text/plain")
            h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)
            return
        html = p.read_text(encoding="utf-8").replace("__MCPBRAIN_TOKEN__", self.token).encode()
        h.send_response(200); h.send_header("Content-Type", "text/html")
        h.send_header("Content-Length", str(len(html))); h.end_headers(); h.wfile.write(html)

    def _serve_vendor(self, h, name):
        root = (Path(__file__).parent / "wizard" / "vendor").resolve()
        p = (root / name).resolve()
        if root not in p.parents or not p.is_file() or p.suffix != ".js":
            h.send_response(404); h.end_headers(); return
        data = p.read_bytes()
        h.send_response(200); h.send_header("Content-Type", "text/javascript")
        h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)

    def _serve_static(self, h, where):
        sub, name = where
        root = (Path(__file__).parent / "wizard").resolve()
        base = (root / "fonts").resolve() if sub else root
        p = (base / name).resolve() if name else base
        mime = _STATIC_TYPES.get(p.suffix)
        # Top-level: only console.css / console.js. fonts/: woff2 and OFL.txt.
        allowed = {".woff2", ".txt"} if sub else {".css", ".js"}
        # Resolved and required to sit directly in its folder: a symlink or a
        # `..` that lands anywhere else is refused.
        if (not name or p.parent != base or p.suffix not in allowed
                or mime is None or not p.is_file()):
            h.send_response(404); h.end_headers(); return
        data = p.read_bytes()
        h.send_response(200); h.send_header("Content-Type", mime)
        h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)

    def _close_finding(self, h, finding_id: int, *, dismiss: bool):
        """POST /api/findings/<id>/fixed|dismiss. Acts on the finding and every
        open finding grouped with it (same type and summary). A review item
        (pending change, proposed correction) is declined instead: its own
        queue is where it lives."""
        from mcpbrain import auto_changes, dashboard as dash
        from mcpbrain import graph_corrections as gc
        from mcpbrain.change_undo import revert_json
        f = self.store.get_finding(finding_id)
        if f and not f["resolved_at"] and str(f["ref_id"]).isdigit():
            if f["finding_type"] == auto_changes.FINDING_TYPE:
                out = auto_changes.decide(self.store, str(self.home), int(f["ref_id"]),
                                          approve=False)
                if not out["ok"]:
                    return h_json(h, 409, worded(out["reason"], declined=True))
                return h_json(h, 200, {"ok": True, "declined": True})
            if f["finding_type"] == gc.FINDING_TYPE:
                ok = gc.decline(self.store, int(f["ref_id"]))["status"] == "declined"
                if not ok:
                    return h_json(h, 409, worded("That correction isn't waiting any more.",
                                                 declined=True))
                return h_json(h, 200, {"ok": True, "declined": True})
        with self.store._connect() as db:
            row = db.execute("SELECT summary FROM proactive_findings WHERE id=?",
                             (finding_id,)).fetchone()
        label = ((row["summary"] if row else "") or (f or {}).get("finding_type") or "finding")
        ids = dash.close_finding_group(self.store, finding_id, dismiss=dismiss)
        if not ids:
            return h_json(h, 404, worded("That finding is already closed.",
                                         error="finding not found or already closed"))
        if len(ids) > 1:
            label += f" (x{len(ids)})"
        if dismiss:
            self.store.record_change(
                "finding_dismissed", ref_id=str(finding_id), source="dashboard",
                summary=f"Dismissed: {label}",
                revert_ref=revert_json({"ids": ids, "verdict": None},
                                       {"ids": ids, "verdict": "dismissed_by_human"}))
        else:
            self.store.record_change("finding_fixed", ref_id=str(finding_id),
                                     summary=f"Marked fixed: {label}", source="dashboard")
        return h_json(h, 200, {"ok": True, "closed": len(ids)})

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
            if h.path == "/api/sync-now":
                # Wake the daemon for an immediate sync->drain->prepare cycle. The
                # backfill fan-out uses this to advance to the next batch in seconds
                # instead of waiting out the normal interval. With {"source": X}
                # the next cycle syncs only that source (console "Sync" per row).
                source = body.get("source")
                if source is None:
                    d.sync_now(); return h_json(h, 200, {"woken": True})
                if source not in _SYNC_SOURCES:
                    return h_json(h, 400, {"error": "source must be one of "
                                                    + ", ".join(_SYNC_SOURCES)})
                from mcpbrain import config as _config
                if not _config.source_enabled(str(self.home), source):
                    label = {"gmail": "Gmail", "drive": "Drive", "calendar": "Calendar",
                             "anarlog": "Meetings"}.get(source, "That source")
                    return h_json(h, 409, worded(f"{label} is switched off. Turn it on under "
                                                 "Sources in Settings to sync it.",
                                                 error=f"{source} is switched off"))
                d.sync_now(source)
                return h_json(h, 200, {"woken": True, "source": source})
            if h.path == "/api/restart":
                # Reply first: the restart stops this server along with the
                # daemon, and the new one listens on a new port and token.
                from mcpbrain import system_ops
                # A second click while one restart runs is answered, not repeated.
                if not system_ops.claim_restart():
                    return h_json(h, 202, {"restarting": True, "already": True})
                try:
                    h_json(h, 202, {"restarting": True})
                    system_ops.request_restart(d, home=self.home)
                except Exception:  # noqa: BLE001 -- the client left, or it never started
                    # Nothing is restarting: free the claim so the next click works.
                    log.exception("restart: did not start")
                    system_ops.release_restart()
                return None
            if h.path == "/api/system/login":
                on = body.get("on")
                if not isinstance(on, bool):
                    return h_json(h, 400, {"error": "on must be true or false"})
                from mcpbrain import system_ops
                out = system_ops.set_start_at_login(self.home, on)
                return h_json(h, 200 if out.get("ok") else 500, out)
            if h.path == "/api/update/apply":
                from mcpbrain import system_ops
                try:
                    system_ops.apply_update(d)
                except system_ops.UpToDate:
                    return h_json(h, 409, worded("mcpbrain is already up to date.",
                                                 error="already up to date"))
                return h_json(h, 202, {"updating": True, "version": d._pending_update})
            if h.path == "/api/diagnostics/run":
                try:
                    st = d.status()
                except Exception:  # noqa: BLE001 — doctor reports the daemon side itself
                    st = {}
                started = self.diagnostics.start(self.home, st)
                return h_json(h, 202, {"started": started, "running": True})
            if h.path == "/api/model/ensure":
                d.ensure_model(); return h_json(h, 202, {"started": True})
            if h.path == "/api/connect-desktop":
                from mcpbrain import connector, desktop, setup as _setup
                # Write the connector while Claude Desktop is DOWN: it rewrites
                # its own config on quit, so an entry written first is discarded.
                mcpbrain_bin = _setup._mcpbrain_bin()
                return h_json(h, 200, desktop.relaunch_claude_desktop(
                    on_quit=lambda: connector.register_connector(mcpbrain_bin=mcpbrain_bin)))
            if h.path == "/api/recall":
                # Semantic recall for the UserPromptSubmit hook. Loopback +
                # bearer-token gated like every other handler here. Empty query
                # short-circuits to no results.
                q = (body.get("query") or "").strip()
                limit = max(1, min(int(body.get("limit") or 5), 10))
                expand = bool(body.get("expand"))
                results = d.search(q, limit, expand=expand) if q else []
                # Answer FIRST. The exposure INSERT waits on SQLite's single
                # write lock, which a long bulk write can hold for longer than
                # the hook's timeout. h_json writes the whole response (headers
                # + Content-Length body) to the unbuffered socket file before
                # returning, so the client has its answer; the exposure log
                # (S2 feedback signal) then runs on this handler thread. Errors
                # are swallowed inside _log_recall_exposures.
                h_json(h, 200, {"results": results})
                if results and self.store is not None:
                    session_id = (body.get("session_id") or "").strip()
                    _log_recall_exposures(self.store, results, session_id)
                return
            if h.path == "/api/tool":
                # Execute a Store-touching MCP tool in THIS process (the thin
                # adapter's daemon half). Argument validation happens at the MCP
                # protocol boundary; Daemon.call_tool re-checks defensively and
                # raises ValueError, which is a 400 rather than the generic 500
                # below because it means the two halves disagree about the
                # advertised schema -- a bug in us, not a fault in the handler.
                # A handler that genuinely raises falls through to the 500 +
                # log.exception, so a real fault keeps its traceback.
                name = body.get("name")
                arguments = body.get("arguments") or {}
                confirmation = body.get("confirmation")
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    return h_json(h, 400, {"error": "name must be a string and "
                                                    "arguments an object"})
                if confirmation is not None and not isinstance(confirmation, dict):
                    return h_json(h, 400, {"error": "confirmation must be an object"})
                # Passed only when present, like ControlClient.call_tool's own
                # body -- so a two-argument daemon stand-in (every existing
                # test double, and any tool but brain_graph_correct) keeps
                # working unchanged.
                kwargs = {"confirmation": confirmation} if confirmation is not None else {}
                try:
                    return h_json(h, 200, {"result": d.call_tool(name, arguments, **kwargs)})
                except RestoreHoldRefused as exc:
                    # A store-writing tool during the restore hold: a clean,
                    # readable refusal (ToolExecutionError on the MCP side).
                    # `error` is the MCP server's contract (it reaches Claude,
                    # not a page); a page only ever shows the worded reason.
                    log.info("control API /api/tool held for restore: %s", exc)
                    return h_json(h, 409, worded("mcpbrain is restoring a backup. Try again "
                                                 "in a moment.", error=str(exc)))
                except ValueError as exc:
                    log.error("control API /api/tool refused %r: %s", name, exc)
                    return h_json(h, 400, worded("That request wasn't accepted.", error=str(exc)))
            if h.path == "/api/recall-feedback":
                # The accept signal (S2/S4/S5 keystone): the hook reports doc_ids
                # that stayed relevant across the session (recalled again) as 'used'.
                # Fire-and-forget; never fails the caller.
                if self.store is not None:
                    ev = (body.get("event") or "used").strip() or "used"
                    sid = (body.get("session_id") or "").strip()
                    ids = [str(d_) for d_ in (body.get("doc_ids") or []) if d_]
                    if ids:
                        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        try:
                            self.store.record_recall_feedback_batch(
                                [(d_, sid, ev, ts) for d_ in ids])
                        except Exception:  # noqa: BLE001
                            pass
                return h_json(h, 200, {"recorded": True})
            # /api/config carries the Gemini key. The control API is loopback-only
            # over plain HTTP by design, so the key travels in cleartext on
            # localhost. HTTPS-on-loopback is deliberately avoided: the self-signed
            # cert UX is worse than the threat it removes. The bearer token plus the
            # 127.0.0.1 bind are the protections.
            if h.path == "/api/config":  d.apply_config(body); return h_json(h, 200, {"ok": True})
            if h.path == "/api/auth/start":
                threading.Thread(target=d.start_auth, daemon=True).start()
                return h_json(h, 202, {"started": True})
            if h.path == "/api/bootstrap-baseline":
                # Import the org snapshot + shared-drive ingest caches before the
                # first sync, or re-run on demand (wizard / doctor / `mcpbrain
                # bootstrap`). Synchronous like /api/backup/auto; returns the
                # summary. force=True because an explicit call means "run it now".
                return h_json(h, 200,
                              d.bootstrap_baseline_once(force=True) or {"status": "skipped"})
            if h.path == "/api/records/scaffold":
                from mcpbrain import records
                return h_json(h, 200, {"scaffolded": records.scaffold_records(str(self.home))})

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
                    return h_json(h, 404, worded("That action isn't open any more.",
                                                 error="action not found or not open"))
                return h_json(h, 200, {"snoozed": True})

            m = re.match(r"^/api/changes/(\d+)/undo$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import change_undo
                out = change_undo.undo(self.store, str(self.home), int(m.group(1)))
                if out["ok"]:
                    return h_json(h, 200, {"ok": True})
                code = 404 if out["reason"] == change_undo.NOT_FOUND else 409
                return h_json(h, code, {**out, "worded": True})

            m = re.match(r"^/api/circles/(\d+)/title$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                title = body.get("title")
                if not isinstance(title, str) or not title.strip():
                    return h_json(h, 400, {"error": "title must be a non-empty string"})
                from mcpbrain.community_synth import set_community_title
                if not set_community_title(self.store, int(m.group(1)), title.strip()[:200],
                                           source="dashboard"):
                    return h_json(h, 404, worded("That circle no longer exists.",
                                                 error="circle not found"))
                return h_json(h, 200, {"ok": True})

            m = re.match(r"^/api/corrections/(\d+)/(apply|decline)$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_corrections as gc
                cid = int(m.group(1))
                if m.group(2) == "apply":
                    out = gc.approve(self.store, cid, via="dashboard")
                    if out["status"] == "applied":
                        return h_json(h, 200, out)
                    log.info("correction %s not applied: %s", cid, out.get("error"))
                    return h_json(h, 409, worded(
                        "That correction couldn't be applied. It may no longer fit the graph.",
                        status=out["status"], correction_id=cid))
                out = gc.decline(self.store, cid)
                if out["status"] == "declined":
                    return h_json(h, 200, out)
                return h_json(h, 404, worded("That correction isn't waiting any more.",
                                             status=out["status"], correction_id=cid))

            if h.path in ("/api/chunks/reinclude", "/api/chunks/prioritise",
                          "/api/chunks/reflow-retry"):
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                ids = body.get("doc_ids")
                if not isinstance(ids, list) or not ids or \
                        not all(isinstance(i, str) and i for i in ids):
                    return h_json(h, 400, {"error": "doc_ids must be a non-empty list of strings"})
                if len(ids) > 1000:
                    return h_json(h, 400, {"error": "at most 1000 doc_ids per request"})
                if h.path == "/api/chunks/reflow-retry":
                    # The gave-up list's action: un-stamp the chosen owners so
                    # the reflow selector re-queues them on its next seed.
                    out = self.store.retry_reflow_gave_up(doc_ids=ids)
                    return h_json(h, 200, {"ok": True, **out})
                if h.path == "/api/chunks/reinclude":
                    # Derived chunks are refused and counted as skipped.
                    from mcpbrain import source_controls
                    out = self.store.reinclude_chunks(
                        ids, rules=source_controls.rules_for(self.store, self.home))
                    return h_json(h, 200, {"ok": True, **out})
                changed = self.store.prioritise_chunks(ids)
                return h_json(h, 200, {"ok": True, "changed": changed})

            if h.path == "/api/settings":
                from mcpbrain import settings_api
                out = settings_api.apply_settings(self.home, body)
                if not out["ok"]:
                    # `error` names the key for API callers; the page gets words.
                    return h_json(h, 400, worded("That setting couldn't be saved. Reload the "
                                                 "page and try again.", error=out["error"]))
                return h_json(h, 200, out)

            m = re.match(r"^/api/pending/(\d+)/(apply|decline|clear)$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import auto_changes
                if m.group(2) == "clear":
                    # An interrupted change the owner has checked.
                    out = auto_changes.clear(self.store, int(m.group(1)))
                else:
                    out = auto_changes.decide(self.store, str(self.home), int(m.group(1)),
                                              approve=(m.group(2) == "apply"))
                if out["ok"]:
                    return h_json(h, 200, {"ok": True})
                return h_json(h, 404 if out["reason"] == auto_changes.NOT_FOUND else 409,
                              {**out, "worded": True})

            m = re.match(r"^/api/findings/(\d+)/(fixed|dismiss)$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                return self._close_finding(h, int(m.group(1)), dismiss=(m.group(2) == "dismiss"))

            m = re.match(r"^/api/dashboard/findings/(\d+)/dismiss$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                finding_id = int(m.group(1))
                finding = self.store.get_finding(finding_id)
                from mcpbrain import graph_corrections as gc
                if finding and finding["finding_type"] == gc.FINDING_TYPE and \
                        not finding["resolved_at"] and str(finding["ref_id"]).isdigit():
                    # Dismissing a proposed correction declines it: left pending
                    # it would keep counting against the pending cap, unseen.
                    # decline() also resolves this finding.
                    if gc.decline(self.store, int(finding["ref_id"]))["status"] == "declined":
                        self.store.record_change("finding_dismissed", ref_id=str(finding_id))
                        return h_json(h, 200, {"dismissed": True, "correction": "declined"})
                from mcpbrain import auto_changes
                if finding and finding["finding_type"] == auto_changes.FINDING_TYPE and \
                        not finding["resolved_at"] and str(finding["ref_id"]).isdigit():
                    # Same for a change held for review: dismissing declines it,
                    # and decide() resolves this finding. On failure the finding
                    # stays open with its row: closing it here would strand the
                    # row, queued but with no Needs-you item.
                    out = auto_changes.decide(self.store, str(self.home),
                                              int(finding["ref_id"]), approve=False)
                    if out["ok"]:
                        return h_json(h, 200, {"dismissed": True, "change": "declined"})
                    return h_json(h, 409, worded(out["reason"], dismissed=False))
                from mcpbrain.agent_errs import FINDING_TYPE as _AGENT_STDERR_FINDING_TYPE
                verdict = (None if finding and finding["finding_type"] == _AGENT_STDERR_FINDING_TYPE
                           else "dismissed_by_human")
                ok = self.store.resolve_finding(finding_id, verdict=verdict)
                if not ok:
                    return h_json(h, 404, worded("That finding is already closed.",
                                                 error="finding not found or already dismissed"))
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
                    context_hash=body.get("context_hash", ""),
                )
                return h_json(h, 200, {"ok": True})

            if h.path == "/api/session/ingest":
                title = body.get("title", "").strip()
                content = body.get("content", "").strip()
                if not title or not content:
                    return h_json(h, 400, {"error": "title and content required"})
                from mcpbrain.capture import write_capture
                from mcpbrain.session_hooks import (
                    is_non_interactive_entrypoint, is_maintenance_text)
                # Same rules as the transcript hooks: subagent, headless/SDK
                # and scheduled-task runs are not captured at all.
                if body.get("is_sidechain") is True:
                    return h_json(h, 200, {"queued": False, "skipped": "sidechain"})
                if is_non_interactive_entrypoint(body.get("entrypoint")):
                    return h_json(h, 200, {"queued": False, "skipped": "non_interactive"})
                if is_maintenance_text(content):
                    return h_json(h, 200, {"queued": False, "skipped": "scheduled_task"})
                tags = [t.strip() for t in str(body.get("tags") or "session").split(",")
                        if t.strip()]
                if "session" not in {t.lower() for t in tags}:
                    tags.insert(0, "session")
                envelope = {
                    "kind": "ingest",
                    "source": "stop_hook",
                    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "title": title,
                    "content": content,
                    "tags": ",".join(tags),
                    "observation_type": "note",
                }
                sid = str(body.get("claude_session_id") or "").strip()
                if sid:
                    envelope["claude_session_id"] = sid
                p = write_capture(str(self.home), envelope)
                return h_json(h, 200, {"queued": True, "path": str(p)})

            if h.path == "/api/backup/run":
                # Back up now: due on the daemon's next loop iteration, past the
                # interval and the failure backoff. `mcpbrain backup` polls
                # /api/status (backup_running) and backup_state.json for the result.
                if not d.request_backup():
                    return h_json(h, 409, worded("Backup isn't set up yet. Set it up in Settings.",
                                                 error="backup is not configured"))
                return h_json(h, 202, {"requested": True})
            if h.path == "/api/backup/enable":
                try:
                    from mcpbrain import backup_setup, auth as _auth, config as _config
                    creds = _auth.load_credentials()
                    svc = _auth.build_service("drive", "v3", creds,
                                              timeout_s=_auth.DEFAULT_HTTP_TIMEOUT_S)
                    owner = _config.owner_email(str(self.home)) or "unknown"
                    cfg = backup_setup.enable_backup(str(self.home), drive_service=svc, user_id=owner)
                    return h_json(h, 200, {"enabled": True, "shared_drive_id": cfg["backup"]["shared_drive_id"]})
                except Exception as exc:
                    log.exception("backup/enable failed")
                    return h_json(h, 500, {"error": str(exc)})

            if h.path == "/api/backup/auto":
                # Automatic, no-choice recovery (called by the wizard once Google is
                # connected): if a backup exists for this account AND the local store
                # is empty, restore the whole brain; otherwise just ensure encrypted
                # backup is on. We never clobber a populated local brain — a returning
                # user who really wants to overwrite STOPS the daemon (launchctl
                # bootout; `mcpbrain restore` refuses while it runs, --force or not)
                # and then runs `mcpbrain restore --force`.
                try:
                    from mcpbrain import (restore as _restore, backup_setup,
                                          auth as _auth, config as _config)
                    # Resolve the account email as owner_email → connected Google
                    # account → legacy backup.user_id, so auto-restore fires right
                    # after Google sign-in instead of waiting for the user to fill
                    # and save the profile step (the escrow is keyed by the Google
                    # email, which is already known at sign-in).
                    owner = _restore.account_email(str(self.home))
                    if not owner:
                        return h_json(h, 200, {"action": "pending", "reason": "no account yet"})
                    creds = _auth.load_credentials()
                    svc = _auth.build_service("drive", "v3", creds,
                                              timeout_s=_auth.DEFAULT_HTTP_TIMEOUT_S)
                    info = _restore.detect_restorable(str(self.home), svc)
                    # Detection could not tell (a TRANSIENT lookup failure): never
                    # fall through to enable_backup on that -- a backup may well
                    # exist, and enabling would start a new one over an empty
                    # store. The wizard retries on 503 (R27b/R29). A definitive
                    # "key but no restore point" proceeds: enable_backup adopts
                    # the Drive key, never replaces it (R27a).
                    if info.get("retry"):
                        reason = info.get("reason") or "restore detection failed"
                        log.warning("backup/auto: restore detection inconclusive, "
                                    "not enabling backup: %s", reason)
                        return h_json(h, 503, {"error": reason, "reason": reason,
                                               "retry": True})
                    # The daemon initializes brain.sqlite3 (schema only) before the
                    # wizard runs, so file size can't distinguish a fresh install
                    # from a populated brain — check for real content (chunks rows).
                    # force=True because the file already exists (run_restore_auto's
                    # own size guard would otherwise block restoring over it).
                    if info.get("available") and not _restore.store_has_content(_config.store_path()):
                        # Never in place: decrypt to a staging file, then swap it
                        # in behind the daemon's restoring gate after a busy-checked
                        # checkpoint (Daemon.swap_restored_store). A busy store is
                        # transient -- the wizard is told to retry.
                        # run_restore_auto restores the NEWEST restore point
                        # across every backup location (backup_restore), with
                        # the records repo + config.json.
                        body_out = {"action": "restored",
                                    "snapshot_id": info.get("snapshot_id"),
                                    "restore_point": info.get("restore_point"),
                                    "source": info.get("source")}
                        try:
                            result = _restore.run_restore_auto(
                                str(self.home), drive_service=svc, force=True,
                                swap=getattr(d, "swap_restored_store", None))
                        except _restore.PartialRestore as exc:
                            # The store is restored (it has content now); only the
                            # records/config were not placed. Not a failure to
                            # retry: the empty-store rule would refuse it. Record
                            # it where /api/status and doctor see it.
                            log.error("backup/auto: restore partly done: %s", exc)
                            note = getattr(d, "note_restore_error", None)
                            if note is not None:
                                note(f"store restored, but the records repo / "
                                     f"config.json were not: {exc}")
                            return h_json(h, 200, {**body_out, "partial": True,
                                                   "error": str(exc)})
                        used = getattr(result, "used", None)
                        if used is not None:
                            # The point actually restored (after any fallback).
                            body_out.update(snapshot_id=used.file_id,
                                            **_restore.point_info(svc, used))
                        # The restored bundle carries the escrow key + backup config,
                        # so backup is already on after a restore.
                        return h_json(h, 200, body_out)
                    cur_backup = _config.read_config(str(self.home)).get("backup") or {}
                    if not cur_backup.get("escrow_key"):
                        backup_setup.enable_backup(str(self.home), drive_service=svc, user_id=owner)
                    return h_json(h, 200, {"action": "backup_enabled",
                                           "restorable": bool(info.get("available"))})
                except Exception as exc:
                    from mcpbrain.backup import RestoreBusyError
                    from mcpbrain.store import StoreRestoringError
                    if isinstance(exc, (RestoreBusyError, StoreRestoringError)):
                        log.warning("backup/auto: store busy, restore refused: %s", exc)
                        return h_json(h, 503, {"error": str(exc), "retry": True})
                    log.exception("backup/auto failed")
                    return h_json(h, 500, {"error": str(exc)})

            m = re.match(r"^/api/graph/entity/([^/?]+)/facts$", h.path)
            if m:
                if self.store is None: return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_view
                out = graph_view.add_fact(self.store, urllib.parse.unquote(m.group(1)),
                                          body.get("attribute"), body.get("value"))
                if out.get("ok"):
                    return h_json(h, 200, out)
                if out.get("error") == "not_found":
                    return h_json(h, 404, worded("That entity isn't in the graph any more."))
                if out.get("error") == "hidden":
                    return h_json(h, 409, worded(out["reason"]))
                return h_json(h, 400, worded(out.get("reason") or "Not saved."))
            m = re.match(r"^/api/graph/entity/([^/?]+)/restore$", h.path)
            if m:
                if self.store is None: return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_view
                out = graph_view.restore_entity(self.store, urllib.parse.unquote(m.group(1)))
                if out.get("ok"):
                    return h_json(h, 200, out)
                if out.get("error") == "not_found":
                    return h_json(h, 404, worded("That entity isn't in the graph any more."))
                return h_json(h, 409, worded("It isn't hidden any more."))
            m = re.match(r"^/api/graph/entity/([^/?]+)$", h.path)
            if m:
                if self.store is None: return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_view
                eid = urllib.parse.unquote(m.group(1))
                d = graph_view.update_entity(
                    self.store, eid,
                    name=body.get("name"), org=body.get("org"),
                    email_addr=body.get("email_addr"), notes=body.get("notes"))
                return h_json(h, 200, d) if d else h_json(h, 404, {"error": "not found"})
            if h.path == "/api/graph/merge":
                if self.store is None: return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_view
                out = graph_view.merge_entities(self.store, body.get("loser_id", ""),
                                                body.get("winner_id", ""),
                                                name_override=body.get("name"))
                if out.get("ok"): return h_json(h, 200, out)
                code = 404 if out.get("error") == "not_found" else 409
                return h_json(h, code, {**out, "reason": out.get("message")
                                        or "Those two can't be merged.", "worded": True})

        except Exception as exc:
            log.exception("control API POST %s failed", h.path)
            return h_json(h, 500, {"error": str(exc)})
        return h_json(h, 404, {"error": "not found"})
