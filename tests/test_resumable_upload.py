"""The snapshot upload must be resumable, and 308 must survive the transport.

Live incident 2026-08-04. `backup._default_media` used `resumable=False`, so
googleapiclient took the multipart path: it read the whole artifact with
`getbytes(0, size)`, built the ENTIRE request body in one `io.BytesIO`
(discovery.py `g.flatten(msgRoot)` -> `fp.getvalue()`), and PUT it in a single
request. Google caps simple/multipart upload at **5 MB**; the snapshot was
**4.24 GB**. Every attempt burned GBs of RAM and died with a broken pipe.

Switching to resumable fixes both halves: googleapiclient streams the file in
chunks (`_StreamSlice`) instead of buffering it, and the request sizes are
legal. But resumable uploads only work if httplib2 lets Google's
"308 Resume Incomplete" reach googleapiclient, which is what the second test
here pins -- see `auth._google_http`.
"""
import http.server
import threading

import pytest

from mcpbrain import auth, backup


def test_snapshot_upload_is_resumable_and_streams_from_disk(tmp_path):
    """A multi-GB artifact cannot go up as one non-resumable multipart body."""
    artifact = tmp_path / "snapshot.enc"
    artifact.write_bytes(b"ciphertext" * 100)

    media = backup._default_media(artifact)

    assert media.resumable() is True, (
        "non-resumable upload is capped at 5MB by Google and buffers the whole "
        "artifact in memory")
    assert media.has_stream() is True, (
        "googleapiclient only streams (_StreamSlice) when the media exposes a "
        "stream; without it, it calls getbytes() and buffers the whole file")


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
