"""308 must survive the transport.

Resumable uploads only work if httplib2 lets Google's "308 Resume Incomplete"
reach the caller -- see `auth._google_http`. (The 2026-08-04 incident that
made the old full-snapshot upload resumable is history: the incremental
layout uploads through drive_resumable.)
"""
import http.server
import threading

import pytest

from mcpbrain import auth


class _ResumeIncompleteHandler(http.server.BaseHTTPRequestHandler):
    """Reproduces Google's resumable-upload interim response.

    A "308 Resume Incomplete" carries a Range header and, crucially, NO
    Location header -- which is exactly what makes httplib2 raise
    RedirectMissingLocation when it treats 308 as a redirect.
    """

    protocol_version = "HTTP/1.1"

    def do_PUT(self):
        self.send_response(308)
        self.send_header("Range", "bytes=0-99")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture()
def resume_incomplete_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _ResumeIncompleteHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_port}/upload"
    finally:
        srv.shutdown()
        srv.server_close()


def test_308_resume_incomplete_reaches_the_caller(resume_incomplete_server):
    """httplib2 must hand 308 back, not try to follow it as a redirect.

    httplib2 puts 308 in REDIRECT_CODES and its redirect branch fires even for
    PUT (`response.status in (303, 308)`), so a Location-less 308 raises
    RedirectMissingLocation and every resumable upload dies on its first chunk.
    googleapiclient handles 308 itself in `HttpRequest._process_response`, so
    the transport must pass it through untouched.
    """
    resp, _ = auth._google_http(timeout_s=10).request(
        resume_incomplete_server, "PUT", body=b"chunk")

    assert resp.status == 308
