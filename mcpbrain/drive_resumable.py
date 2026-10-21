"""Drive's resumable upload protocol, spoken directly over an authorised HTTP
client, with a session the caller persists.

Why not googleapiclient's MediaFileUpload: its resumable session lives only in
the `HttpRequest` object. A lid close, reboot or daemon restart throws that
object away, so the next attempt starts the file from byte 0 -- on a phone
hotspot that meant four daily backups in a row lost a 768 MB upload (live
2026-10-15..18). Here the session URI and Drive's confirmed offset are a plain
`Session` the caller writes to its journal after every confirmed chunk
(`save`), and `upload_file` given that Session resumes by asking Drive how much
it holds.

Protocol (https://developers.google.com/drive/api/guides/manage-uploads):
  * initiate: POST .../upload/drive/v3/files?uploadType=resumable with the
    JSON metadata and `X-Upload-Content-Length`; the session URI comes back in
    `Location`.
  * chunk: PUT `Content-Range: bytes a-b/N`; 308 with `Range: bytes=0-x`
    means Drive holds x+1 bytes (no Range = none yet); 200/201 carries the
    finished file resource.
  * status query: PUT `Content-Range: bytes */N` with an empty body, answered
    like a chunk.
  * a session Drive forgot (404/410; sessions live about a week) restarts the
    file from zero.

After ANY failed chunk the next request is a status query, never a guess: a
PUT that died mid-flight may have persisted part of its body, and sending
from our own offset again would be rejected (or, guessing high, skip bytes).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Callable, Protocol

import httplib2
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.errors import HttpError

from mcpbrain import auth, backup

log = logging.getLogger(__name__)

_INITIATE_URL = ("https://www.googleapis.com/upload/drive/v3/files"
                 "?uploadType=resumable&supportsAllDrives=true&fields=id,size,md5Checksum")

# Drive requires every non-final chunk to be a multiple of 256 KiB.
_QUANTUM = 256 * 1024
# Start large (fewer round trips on a good link), halve after a failed chunk so
# a flaky link loses less per drop, never below 2 MiB (a hotspot that drops
# every few MiB still lands chunks), regrow after
# _REGROW_AFTER clean chunks in a row.
CHUNK_MAX = 32 * 1024 * 1024
CHUNK_MIN = 2 * 1024 * 1024
_REGROW_AFTER = 3

# Consecutive failures with no byte confirmed in between before giving up. The
# count resets whenever Drive's offset moves, so a link that drops every few
# chunks still finishes; one that is down raises after ~2.5 min of backoff.
_MAX_FAILURES = 6
_BACKOFF_BASE_S = 5
_BACKOFF_CAP_S = 240

# A session Drive keeps answering 404/410 for even when brand new is not an
# expired session but a broken upload target: bounded so it cannot spin.
_MAX_RESTARTS = 3

# Drive answers a rate limit with 403 as well as 429; the reason tells them
# apart from a real permission refusal.
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class UploadMismatch(Exception):
    """Drive's record of an uploaded file disagrees with the local file."""


class Transport(Protocol):
    def request(self, uri: str, method: str, body: bytes | None,
                headers: dict) -> tuple[int, dict, bytes]:
        """Send one request; return (status, lower-case headers, body)."""


class _AuthorizedTransport:
    """`Transport` over google-auth's AuthorizedHttp (token refresh and the
    one 401 retry included). httplib2 is not thread-safe: one per thread."""

    def __init__(self, http):
        self._http = http

    def request(self, uri, method, body, headers):
        resp, content = self._http.request(uri, method, body=body, headers=headers)
        return (int(resp.status), {str(k).lower(): v for k, v in resp.items()},
                content or b"")


def authorized_transport(creds, timeout_s: float = auth.DEFAULT_HTTP_TIMEOUT_S) -> Transport:
    """A Transport for `creds`. `auth._google_http` un-registers 308 as a
    redirect; without that httplib2 raises RedirectMissingLocation on Drive's
    first "308 Resume Incomplete"."""
    return _AuthorizedTransport(AuthorizedHttp(creds, http=auth._google_http(timeout_s)))


@dataclass
class Session:
    """One file's upload, as the caller journals it. `offset` is the byte
    count Drive has CONFIRMED; `chunk`/`clean` carry the adaptive chunk size
    across processes so a bad link does not restart at 32 MiB every wake."""
    uri: str
    offset: int
    chunk: int
    clean: int

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "Session":
        return cls(uri=str(data["uri"]), offset=int(data["offset"]),
                   chunk=int(data["chunk"]), clean=int(data["clean"]))


def _align(chunk: int) -> int:
    """`chunk` clamped to [CHUNK_MIN, CHUNK_MAX] and rounded down to 256 KiB,
    so a Session from a hand-edited or older journal cannot send a non-final
    chunk Drive rejects."""
    chunk = max(CHUNK_MIN, min(CHUNK_MAX, int(chunk)))
    return chunk - chunk % _QUANTUM


def _backoff_s(failures: int) -> int:
    """Wait after the `failures`-th consecutive failure: 5 s doubling, 240 s cap."""
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * 2 ** (failures - 1))


def _http_error(status: int, body: bytes, uri: str) -> HttpError:
    """A status-coded failure as googleapiclient's HttpError, so callers and
    `backup._is_transient_upload_error` classify it exactly like a failure
    from any other Drive call."""
    return HttpError(httplib2.Response({"status": str(status)}), body or b"", uri=uri)


def _is_rate_limit(body: bytes) -> bool:
    try:
        errors = json.loads(body or b"{}").get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return False
    return any(isinstance(e, dict) and e.get("reason") in _RATE_LIMIT_REASONS
               for e in errors)


def _is_transient_status(status: int, body: bytes) -> bool:
    """5xx and 429 are Drive's to fix (for a resumable upload Google documents
    every 5xx, 500 included, as "resume"); a 403 is transient only when it
    is a rate limit, otherwise it is a permission refusal and permanent."""
    return status >= 500 or status == 429 or (status == 403 and _is_rate_limit(body))


def _confirmed(headers: dict) -> int:
    """Bytes Drive holds, from a 308's `Range: bytes=0-<last>`; none = 0."""
    rng = headers.get("range")
    if not rng:
        return 0
    return int(rng.rsplit("-", 1)[1]) + 1


def _initiate(transport: Transport, *, name: str, parent_id: str, size: int) -> str:
    body = json.dumps({"name": name, "parents": [parent_id]}).encode()
    status, headers, content = transport.request(_INITIATE_URL, "POST", body, {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Type": "application/octet-stream",
        "X-Upload-Content-Length": str(size),
    })
    if status != 200:
        raise _http_error(status, content, _INITIATE_URL)
    uri = headers.get("location")
    if not uri:
        raise _http_error(502, b"resumable initiate returned no Location", _INITIATE_URL)
    return uri


def upload_file(transport: Transport, path, *, name: str, parent_id: str,
                session: Session | None, save: Callable[[Session], None],
                deadline: float, clock=time.monotonic, sleep=time.sleep) -> dict | None:
    """Upload `path` to Drive folder `parent_id` as `name`, resuming `session`
    when given. Returns Drive's file resource (`id`, `size`, `md5Checksum`) once
    the last byte lands -- check it with `verify` -- or None when `deadline`
    (on `clock`) arrives first, after `save` has the session to resume from.

    `save(session)` is called after the session starts and after every
    confirmed chunk; the caller journals it. Transient failures (network,
    5xx, 429, a rate-limit 403) halve the chunk and back off; a backoff that
    would run past `deadline` saves and returns None instead of sleeping
    through the caller's budget. `_MAX_FAILURES` consecutive failures without
    progress raise the last error; any other HTTP error raises an HttpError
    at once. 404/410 on the session (Drive forgot it) starts a new one from 0.
    """
    size = os.path.getsize(path)
    if size == 0:
        raise ValueError(f"{path}: refusing to upload an empty file")
    if session is not None:
        session = Session(session.uri, session.offset, _align(session.chunk), session.clean)
    chunk = session.chunk if session is not None else CHUNK_MAX
    clean = session.clean if session is not None else 0
    # A resumed session's offset is only what we last journaled: ask Drive.
    need_query = session is not None
    failures = restarts = 0

    def _save():
        session.chunk, session.clean = chunk, clean
        save(session)

    with open(path, "rb") as fh:
        while True:
            if clock() >= deadline:
                if session is not None:
                    _save()
                return None

            sent = 0
            is_chunk = False
            uri = session.uri if session is not None else _INITIATE_URL
            try:
                if session is None:
                    session = Session(_initiate(transport, name=name, parent_id=parent_id,
                                                size=size), 0, chunk, clean)
                    _save()
                    continue
                if need_query:
                    status, headers, body = transport.request(session.uri, "PUT", b"", {
                        "Content-Length": "0", "Content-Range": f"bytes */{size}"})
                else:
                    want = min(chunk, size - session.offset)
                    fh.seek(session.offset)
                    data = fh.read(want)
                    if len(data) != want:
                        raise UploadMismatch(f"{path}: shrank during upload")
                    sent, is_chunk = want, True
                    last = session.offset + sent - 1
                    status, headers, body = transport.request(session.uri, "PUT", data, {
                        "Content-Length": str(sent),
                        "Content-Range": f"bytes {session.offset}-{last}/{size}"})
                if status >= 400 and status not in (404, 410):
                    raise _http_error(status, body, uri)
            except Exception as exc:
                transient = (_is_transient_status(exc.resp.status, exc.content)
                             if isinstance(exc, HttpError) and exc.resp is not None
                             else backup._is_transient_upload_error(exc))
                if not transient:
                    raise
                failures += 1
                if is_chunk:
                    chunk, clean = _align(chunk // 2), 0
                if failures >= _MAX_FAILURES:
                    raise
                delay = _backoff_s(failures)
                log.warning("Drive upload of %s failed (%s); retrying in %ss (%d/%d)",
                            name, exc, delay, failures, _MAX_FAILURES)
                if clock() + delay >= deadline:
                    if session is not None:
                        _save()
                    return None
                sleep(delay)
                need_query = session is not None
                continue

            if status in (200, 201):
                return json.loads(body)
            if status in (404, 410):
                restarts += 1
                if restarts > _MAX_RESTARTS:
                    raise _http_error(status, body, session.uri)
                log.info("Drive upload session for %s expired (%s); restarting from 0",
                         name, status)
                session, need_query = None, False
                continue
            if status != 308:
                raise _http_error(status, body, session.uri)

            confirmed = _confirmed(headers)
            if confirmed > session.offset:
                failures = restarts = 0
            if sent and confirmed == session.offset + sent:
                clean += 1
                if clean >= _REGROW_AFTER and chunk < CHUNK_MAX:
                    chunk, clean = _align(chunk * 2), 0
            session.offset = confirmed
            need_query = False
            _save()


def verify(meta: dict, path) -> None:
    """Raise UploadMismatch unless Drive's `size` and `md5Checksum` for the
    uploaded file equal the local file's: the upload is only done when Drive
    holds exactly these bytes."""
    local_size = os.path.getsize(path)
    md5 = hashlib.md5()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            md5.update(block)
    try:
        remote_size = int(meta["size"])
        remote_md5 = meta["md5Checksum"]
    except (KeyError, TypeError, ValueError) as exc:
        raise UploadMismatch(f"{path}: Drive returned no size/md5Checksum ({meta!r})") from exc
    if remote_size != local_size:
        raise UploadMismatch(f"{path}: Drive holds {remote_size} bytes, local {local_size}")
    if remote_md5 != md5.hexdigest():
        raise UploadMismatch(f"{path}: Drive md5 {remote_md5} != local {md5.hexdigest()}")
