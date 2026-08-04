"""Each thread must get its own httplib2.Http.

Google's own docs: "The httplib2.Http() objects are not thread-safe." mcpbrain
built ONE per service in auth.build_service and cached the services in
Daemon._services, then used them concurrently from the cycle thread, the
maintenance thread, backfill threads, and every ThreadingHTTPServer control-API
handler thread. Interleaved TLS records on a shared connection corrupt OpenSSL's
state, which showed up as:

    [SSL] record layer failure (_ssl.c:2580)
    SIGTRAP / libsystem_malloc: "BUG IN CLIENT OF LIBMALLOC: memory corruption
    of free block"
      CRYPTO_malloc -> tls_setup_write_buffer -> ssl3_dispatch_alert

Five such crashes on 2026-08-04, each restart-looped by launchd's KeepAlive. A
crash report caught two threads inside the SSL stack at the same instant
(thread #0 in _ssl__SSLSocket_read, thread #2 faulting while writing an alert).
"""
import threading

from mcpbrain import auth


class _FakeHttp:
    """Stands in for httplib2.Http, recording which instance served a request."""

    def __init__(self, timeout=None):
        self.timeout = timeout
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append(threading.get_ident())
        return ("resp", b"")


def _service_http(monkeypatch):
    """Build a service through the real build_service, returning the http it
    handed to googleapiclient's build()."""
    monkeypatch.setattr(auth.httplib2, "Http", _FakeHttp)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    captured = {}

    def _capture_build(api, version, http):
        captured["http"] = http
        return ("svc", api)

    monkeypatch.setattr(auth, "build", _capture_build)
    auth.build_service("drive", "v3", object())
    return captured["http"]


def test_two_threads_do_not_share_one_http(monkeypatch):
    """The crash cause: one Http object serving concurrent threads.

    Both threads are kept alive simultaneously via a barrier, and the serving
    objects are held in a list rather than compared by id(): a dead thread's
    thread-local storage is freed, and CPython will happily hand the next
    allocation the same address, so comparing ids across sequential threads can
    report a false match.
    """
    http = _service_http(monkeypatch)

    served = []
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def _run():
        http.request("https://example.invalid/x", method="GET")
        inner = http._get()
        barrier.wait()          # both threads alive at once, both objects live
        with lock:
            served.append(inner)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive(), "worker thread hung"

    assert len(served) == 2
    assert served[0] is not served[1], (
        "both threads were served by the same httplib2.Http — httplib2 is not "
        "thread-safe and concurrent use corrupts the shared TLS connection")
    # And each really did serve its own thread's request.
    assert served[0].calls and served[1].calls
    assert served[0].calls != served[1].calls


def test_one_thread_reuses_its_own_http(monkeypatch):
    """Per-thread, not per-call: a new connection per request would throw away
    keep-alive and pay a TLS handshake every time."""
    http = _service_http(monkeypatch)

    box = {}

    def _run():
        http.request("https://example.invalid/a", method="GET")
        first = id(http._get())
        http.request("https://example.invalid/b", method="GET")
        box["same"] = (first == id(http._get()))

    t = threading.Thread(target=_run)
    t.start()
    t.join(timeout=5)
    assert box["same"], "the same thread got a fresh Http per request"


def test_every_thread_http_excludes_308_and_keeps_the_timeout(monkeypatch):
    """The per-thread Http must still carry the settings build_service promises.

    A thread-local factory that skipped _google_http would silently reintroduce
    the httplib2 308 bug for every thread but the first, breaking resumable
    uploads. Timeout likewise: routine reads must not inherit the 600s upload
    timeout, nor uploads the short read timeout.
    """
    monkeypatch.setattr(auth.httplib2, "Http", _FakeHttp)
    monkeypatch.setattr(auth, "AuthorizedHttp", lambda creds, http: http)
    captured = {}
    monkeypatch.setattr(auth, "build",
                        lambda api, version, http: captured.setdefault("http", http))

    auth.build_service("drive", "v3", object(), timeout_s=600)
    http = captured["http"]

    seen = []

    def _run():
        http.request("https://example.invalid/x", method="GET")
        inner = http._get()
        seen.append((inner.timeout, 308 in inner.redirect_codes))

    for _ in range(2):
        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=5)

    assert len(seen) == 2
    for timeout, has_308 in seen:
        assert timeout == 600, f"per-thread Http lost the timeout: {timeout}"
        assert not has_308, "per-thread Http did not exclude 308 as a redirect"
