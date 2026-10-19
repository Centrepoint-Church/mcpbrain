"""Drive resumable upload over a fake Drive server.

The fake keeps Drive's server-side state per session URI (bytes persisted,
completion) and answers the real protocol -- initiate POST, chunk PUT with
`Content-Range: bytes a-b/N`, status query `bytes */N`, 308 + `Range`, 404 for
a session it forgot -- so these tests prove resume behaviour against what the
server actually holds, not a scripted call sequence.
"""
import hashlib
import json
import os

import pytest
from googleapiclient.errors import HttpError

from mcpbrain import drive_resumable as dr
from mcpbrain.drive_resumable import Session, UploadMismatch, upload_file, verify

MiB = 1024 * 1024
QUANTUM = 256 * 1024


class FakeDrive:
    """Drive's server side: sessions keyed by URI, files keyed by id."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.files: dict[str, bytes] = {}
        self._n = 0

    def expire(self, uri):
        self.sessions.pop(uri, None)

    def initiate(self, body, headers):
        meta = json.loads(body)
        assert headers["X-Upload-Content-Type"] == "application/octet-stream"
        self._n += 1
        uri = f"https://upload.example/session/{self._n}"
        self.sessions[uri] = {"data": bytearray(), "total": int(headers["X-Upload-Content-Length"]),
                              "meta": meta, "id": None}
        return 200, {"location": uri}, b""

    def _final(self, s):
        data = bytes(s["data"])
        return 200, {}, json.dumps({"id": s["id"], "size": str(len(data)),
                                    "md5Checksum": hashlib.md5(data).hexdigest()}).encode()

    def _progress(self, s):
        if not s["data"]:
            return 308, {}, b""
        return 308, {"range": f"bytes=0-{len(s['data']) - 1}"}, b""

    def put(self, uri, body, headers, persist_limit=None):
        s = self.sessions.get(uri)
        if s is None:
            return 404, {}, b'{"error": {"code": 404}}'
        rng = headers["Content-Range"]
        assert int(headers["Content-Length"]) == len(body)
        if rng.startswith("bytes */"):
            assert int(rng[len("bytes */"):]) == s["total"]
            return self._final(s) if s["id"] else self._progress(s)
        span, total = rng[len("bytes "):].split("/")
        first, last = (int(x) for x in span.split("-"))
        assert int(total) == s["total"] and last - first + 1 == len(body)
        if first != len(s["data"]):
            return 400, {}, b'{"error": "offset mismatch"}'
        if last + 1 != s["total"] and len(body) % QUANTUM:
            return 400, {}, b'{"error": "non-final chunk not a multiple of 256 KiB"}'
        keep = body if persist_limit is None else body[:persist_limit]
        s["data"] += keep
        if len(s["data"]) == s["total"]:
            s["id"] = f"file-{uri.rsplit('/', 1)[1]}"
            self.files[s["id"]] = bytes(s["data"])
            return self._final(s)
        return self._progress(s)


class FakeTransport:
    """A client bound to a FakeDrive. `hook(n, method, headers, body)` runs
    before the request is handled (n counts this transport's requests from 0);
    it may raise, return a (status, headers, body) override, return
    ("persist", k) to have the server keep only k bytes and then drop the
    connection, or return None to proceed normally."""

    def __init__(self, server, hook=None):
        self.server, self.hook = server, hook
        self.calls: list[tuple[str, str, dict, int]] = []

    def request(self, uri, method, body, headers):
        n = len(self.calls)
        self.calls.append((method, uri, dict(headers), len(body or b"")))
        if self.hook is not None:
            out = self.hook(n, method, headers, body)
            if isinstance(out, tuple) and out and out[0] == "persist":
                self.server.put(uri, body, headers, persist_limit=out[1])
                raise TimeoutError("read timed out")
            if out is not None:
                return out
        if method == "POST":
            assert "uploadType=resumable" in uri and "supportsAllDrives=true" in uri
            return self.server.initiate(body, headers)
        return self.server.put(uri, body, headers)

    def chunk_sizes(self):
        return [size for m, _, h, size in self.calls
                if m == "PUT" and not h["Content-Range"].startswith("bytes */")]


def _file(tmp_path, size, name="seg"):
    p = tmp_path / name
    p.write_bytes(os.urandom(size))
    return p


def _md5(p):
    return hashlib.md5(p.read_bytes()).hexdigest()


def _upload(transport, path, *, session=None, saves=None, deadline=1e18, clock=None,
            sleeps=None):
    saves = [] if saves is None else saves
    sleeps = [] if sleeps is None else sleeps
    return upload_file(transport, path, name="seg", parent_id="folder-1", session=session,
                       save=lambda s: saves.append(s.to_json()), deadline=deadline,
                       clock=clock or (lambda: 0.0), sleep=sleeps.append)


def test_uploads_in_chunks_and_returns_metadata(tmp_path):
    p = _file(tmp_path, 70 * MiB)
    server = FakeDrive()
    t = FakeTransport(server)
    meta = _upload(t, p)
    assert t.chunk_sizes() == [32 * MiB, 32 * MiB, 6 * MiB]
    assert meta["md5Checksum"] == _md5(p)
    assert int(meta["size"]) == 70 * MiB
    assert server.files[meta["id"]] == p.read_bytes()
    post = t.calls[0]
    assert post[0] == "POST"
    session = next(iter(server.sessions.values()))
    assert session["meta"] == {"name": "seg", "parents": ["folder-1"]}


def test_deadline_stops_between_chunks_and_saves_session(tmp_path):
    p = _file(tmp_path, 70 * MiB)
    server = FakeDrive()
    now = [0.0]
    saves = []

    def save_hook(s):
        saves.append(s.to_json())
        if s.offset > 0:
            now[0] = 100.0

    out = upload_file(FakeTransport(server), p, name="seg", parent_id="folder-1",
                      session=None, save=save_hook, deadline=100.0,
                      clock=lambda: now[0], sleep=lambda s: None)
    assert out is None
    last = Session.from_json(saves[-1])
    assert last.offset == 32 * MiB
    assert len(next(iter(server.sessions.values()))["data"]) == 32 * MiB


def test_resume_after_new_process_uses_saved_session(tmp_path):
    p = _file(tmp_path, 70 * MiB)
    server = FakeDrive()
    now = [0.0]
    saves = []

    def save_hook(s):
        saves.append(s.to_json())
        if s.offset > 0:
            now[0] = 100.0

    first = FakeTransport(server)
    assert upload_file(first, p, name="seg", parent_id="folder-1", session=None,
                       save=save_hook, deadline=100.0, clock=lambda: now[0],
                       sleep=lambda s: None) is None

    # A new process: fresh transport, the session read back from the journal.
    second = FakeTransport(server)
    meta = _upload(second, p, session=Session.from_json(saves[-1]))
    method, uri, headers, size = second.calls[0]
    assert (method, headers["Content-Range"], size) == ("PUT", f"bytes */{70 * MiB}", 0)
    assert not any(c[0] == "POST" for c in second.calls)
    assert sum(first.chunk_sizes()) + sum(second.chunk_sizes()) == 70 * MiB
    assert meta["md5Checksum"] == _md5(p)


def test_dropped_chunk_resumes_from_server_offset(tmp_path):
    p = _file(tmp_path, 40 * MiB)
    server = FakeDrive()
    dropped = []

    def hook(n, method, headers, body):
        if method == "PUT" and body and not dropped:
            dropped.append(True)
            return ("persist", len(body) // 2)   # server keeps 16 MiB, then the link drops
        return None

    t = FakeTransport(server, hook)
    sleeps = []
    meta = _upload(t, p, sleeps=sleeps)
    assert server.files[meta["id"]] == p.read_bytes()
    # After the drop the client asks the server rather than guessing.
    after = t.calls[2]
    assert after[2]["Content-Range"] == f"bytes */{40 * MiB}"
    # It resumed at the server's 16 MiB, not at 0 and not at 32 MiB.
    assert t.calls[3][2]["Content-Range"].startswith(f"bytes {16 * MiB}-")
    assert sleeps == [5]


def test_chunk_halves_on_failure_and_regrows(tmp_path):
    # 32 (dropped, nothing persisted) -> 16, 16, 16 clean -> back to 32.
    p = _file(tmp_path, 3 * 16 * MiB + 32 * MiB)
    server = FakeDrive()
    failed = []

    def hook(n, method, headers, body):
        if method == "PUT" and body and not failed:
            failed.append(True)
            raise TimeoutError("timed out")
        return None

    t = FakeTransport(server, hook)
    meta = _upload(t, p)
    assert [s // MiB for s in t.chunk_sizes()] == [32, 16, 16, 16, 32]
    assert server.files[meta["id"]] == p.read_bytes()


def test_chunk_never_shrinks_below_8_mib(tmp_path):
    p = _file(tmp_path, 9 * MiB)
    server = FakeDrive()
    fails = [0]

    def hook(n, method, headers, body):
        if method == "PUT" and body and fails[0] < 4:
            fails[0] += 1
            raise ConnectionResetError("reset")
        return None

    t = FakeTransport(server, hook)
    _upload(t, p)
    sizes = [s // MiB for s in t.chunk_sizes()]
    # min(chunk, remaining): chunk 32 -> 16 -> 8 -> 8 -> 8, then the 1 MiB tail.
    assert sizes == [9, 9, 8, 8, 8, 1]


def test_transient_status_backs_off_and_resumes(tmp_path):
    p = _file(tmp_path, 1 * MiB)
    server = FakeDrive()
    seen = []

    def hook(n, method, headers, body):
        if method == "PUT" and body and not seen:
            seen.append(True)
            return 503, {}, b"backend error"
        return None

    sleeps = []
    meta = _upload(FakeTransport(server, hook), p, sleeps=sleeps)
    assert meta["md5Checksum"] == _md5(p)
    assert sleeps == [5]


def test_six_failures_without_progress_raise(tmp_path):
    p = _file(tmp_path, 1 * MiB)

    def hook(n, method, headers, body):
        if method == "PUT":
            raise TimeoutError("down")
        return None

    sleeps = []
    with pytest.raises(TimeoutError):
        _upload(FakeTransport(FakeDrive(), hook), p, sleeps=sleeps)
    assert sleeps == [5, 10, 20, 40, 80]   # the 6th failure raises instead of sleeping


def test_backoff_caps_at_240_seconds():
    assert [dr._backoff_s(k) for k in range(1, 8)] == [5, 10, 20, 40, 80, 160, 240]


def test_backoff_that_would_cross_the_deadline_saves_and_stops(tmp_path):
    p = _file(tmp_path, 1 * MiB)

    def hook(n, method, headers, body):
        if method == "PUT" and body:
            raise TimeoutError("down")
        return None

    saves, sleeps = [], []
    out = _upload(FakeTransport(FakeDrive(), hook), p, saves=saves, sleeps=sleeps,
                  deadline=3.0)
    assert out is None and sleeps == []
    assert Session.from_json(saves[-1]).chunk == 16 * MiB


def test_expired_session_restarts_file(tmp_path):
    p = _file(tmp_path, 40 * MiB)
    server = FakeDrive()
    now = [0.0]
    saves = []

    def save_hook(s):
        saves.append(s.to_json())
        if s.offset > 0:
            now[0] = 100.0

    upload_file(FakeTransport(server), p, name="seg", parent_id="folder-1", session=None,
                save=save_hook, deadline=100.0, clock=lambda: now[0], sleep=lambda s: None)
    old = Session.from_json(saves[-1])
    assert old.offset == 32 * MiB
    server.expire(old.uri)   # offline for more than a week

    t = FakeTransport(server)
    meta = _upload(t, p, session=old)
    assert t.calls[0][2]["Content-Range"] == f"bytes */{40 * MiB}"
    assert t.calls[1][0] == "POST"
    assert t.calls[2][2]["Content-Range"].startswith("bytes 0-")
    assert sum(t.chunk_sizes()) == 40 * MiB
    assert server.files[meta["id"]] == p.read_bytes()


def test_permanent_403_raises_immediately(tmp_path):
    p = _file(tmp_path, 1 * MiB)
    body = json.dumps({"error": {"code": 403, "errors": [
        {"reason": "insufficientFilePermissions"}]}}).encode()
    t = FakeTransport(FakeDrive(), lambda n, m, h, b: (403, {}, body))
    sleeps = []
    with pytest.raises(HttpError) as ei:
        _upload(t, p, sleeps=sleeps)
    assert ei.value.resp.status == 403
    assert len(t.calls) == 1 and sleeps == []


def test_rate_limit_403_is_transient(tmp_path):
    p = _file(tmp_path, 1 * MiB)
    body = json.dumps({"error": {"code": 403, "errors": [
        {"reason": "userRateLimitExceeded"}]}}).encode()
    hit = []

    def hook(n, method, headers, body_):
        if method == "POST" and not hit:
            hit.append(True)
            return 403, {}, body
        return None

    sleeps = []
    meta = _upload(FakeTransport(FakeDrive(), hook), p, sleeps=sleeps)
    assert meta["md5Checksum"] == _md5(p) and sleeps == [5]


def test_empty_file_is_refused(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    with pytest.raises(ValueError):
        _upload(FakeTransport(FakeDrive()), p)


def test_session_json_round_trip():
    s = Session(uri="https://u/1", offset=3, chunk=8 * MiB, clean=2)
    assert Session.from_json(s.to_json()) == s


def test_verify_rejects_size_or_md5_mismatch(tmp_path):
    p = _file(tmp_path, 1000)
    good = {"size": "1000", "md5Checksum": _md5(p)}
    verify(good, p)
    with pytest.raises(UploadMismatch):
        verify({**good, "size": "999"}, p)
    with pytest.raises(UploadMismatch):
        verify({**good, "md5Checksum": "0" * 32}, p)
    with pytest.raises(UploadMismatch):
        verify({"id": "x"}, p)


def test_authorized_transport_normalises_response(monkeypatch):
    import httplib2

    class FakeAuthed:
        def __init__(self, creds, http=None):
            self.http = http

        def request(self, uri, method, body=None, headers=None):
            resp = httplib2.Response({"status": "308", "Range": "bytes=0-9"})
            return resp, b""

    monkeypatch.setattr(dr, "AuthorizedHttp", FakeAuthed)
    t = dr.authorized_transport(object(), timeout_s=5)
    assert 308 not in t._http.http.redirect_codes
    status, headers, body = t.request("https://u", "PUT", b"", {})
    assert status == 308 and headers["range"] == "bytes=0-9" and body == b""
