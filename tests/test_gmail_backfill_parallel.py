"""A full-history attachment backfill must be parallel, single-writer and safe.

A1 — "a PDF emailed to the user is invisible to the brain, while the
byte-identical file in Drive is extracted normally" — is the largest finding in
the 2026-07-27 audit, and it needs a pass over the whole mailbox. Sequentially,
at ~1 network round trip per message plus one per attachment, that is hours.

The parallel shape is the one already proven in sync/drive.reingest_files:
workers FETCH, the main thread WRITES. The store is single-writer, and
googleapiclient's Resource wraps a stateful httplib2.Http that is not safe to
share across threads.
"""
import base64

import pytest

from mcpbrain.store import Store
from mcpbrain.sync.gmail import backfill_gmail


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _msg(mid: str, *, body: str = "Body text here.", attachment: bool = False) -> dict:
    payload = {
        "mimeType": "text/plain",
        "headers": [{"name": "Subject", "value": f"Subject {mid}"},
                    {"name": "From", "value": "a@b.com"},
                    {"name": "Date", "value": "Tue, 02 Jun 2026 16:30:01 +0800"}],
        "body": {"data": _b64(body)},
    }
    if attachment:
        payload = {
            "mimeType": "multipart/mixed",
            "headers": payload["headers"],
            "parts": [
                {"mimeType": "text/plain", "filename": "", "body": {"data": _b64(body)}},
                {"mimeType": "text/plain", "filename": f"{mid}.txt",
                 "body": {"attachmentId": f"att-{mid}", "size": 32}},
            ],
        }
    return {"id": mid, "threadId": f"t-{mid}", "labelIds": ["INBOX"], "payload": payload}


class _FakeGmail:
    """Minimal Gmail fake that records concurrency. `build_count` proves each
    worker got its OWN service rather than sharing one Resource."""

    build_count = 0

    def __init__(self, messages: dict, *, page_size: int = 100):
        _FakeGmail.build_count += 1
        self._messages = messages
        self._page_size = page_size
        self._pending = None

    # --- fluent chain -----------------------------------------------------
    def users(self):
        return self

    def messages(self):
        return self

    def attachments(self):
        self._attachments = True
        return self

    def list(self, userId=None, q=None, maxResults=None, pageToken=None):
        ids = sorted(self._messages)
        start = int(pageToken or 0)
        page = ids[start:start + self._page_size]
        nxt = start + self._page_size
        self._pending = {"messages": [{"id": i} for i in page]}
        if nxt < len(ids):
            self._pending["nextPageToken"] = str(nxt)
        return self

    def get(self, userId=None, id=None, messageId=None, format=None):
        if messageId is not None:            # attachments().get()
            self._pending = {"data": _b64("attachment body text"), "size": 32}
        else:
            self._pending = self._messages[id]
        return self

    def execute(self, num_retries=0):
        return self._pending


def _store(tmp_path) -> Store:
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_parallel_backfill_indexes_every_message(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:03d}": _msg(f"m{i:03d}") for i in range(50)}
    store = _store(tmp_path)

    n = backfill_gmail(_FakeGmail(msgs), store, after="1970/01/01",
                       max_workers=8,
                       service_factory=lambda: _FakeGmail(msgs))

    assert n == 50
    for mid in msgs:
        assert store.get_chunk(f"gmail-{mid}-body-0") is not None, f"{mid} missing"


def test_parallel_and_sequential_produce_identical_results(tmp_path, monkeypatch):
    """The discriminator for the whole change: parallelism must not alter WHAT
    gets written, only how fast."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:02d}": _msg(f"m{i:02d}", attachment=(i % 3 == 0)) for i in range(12)}

    (tmp_path / "seq").mkdir()
    (tmp_path / "par").mkdir()
    seq_store = _store(tmp_path / "seq")
    n_seq = backfill_gmail(_FakeGmail(msgs), seq_store, after="1970/01/01")

    par_store = _store(tmp_path / "par")
    n_par = backfill_gmail(_FakeGmail(msgs), par_store, after="1970/01/01",
                           max_workers=6,
                           service_factory=lambda: _FakeGmail(msgs))

    assert n_seq == n_par == 12

    def _ids(s):
        with s._connect() as db:
            return sorted(r["doc_id"] for r in db.execute("SELECT doc_id FROM chunks"))

    assert _ids(seq_store) == _ids(par_store)
    assert any("-att-" in d for d in _ids(par_store)), "attachments must be included"


def test_every_worker_builds_its_own_service(tmp_path, monkeypatch):
    """googleapiclient's Resource wraps a stateful httplib2.Http that is NOT
    thread-safe. Sharing one across workers corrupts responses under load —
    the reason reingest_files takes a service_factory rather than a service."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:02d}": _msg(f"m{i:02d}") for i in range(20)}
    store = _store(tmp_path)
    built: list = []

    backfill_gmail(_FakeGmail(msgs), store, after="1970/01/01", max_workers=4,
                   service_factory=lambda: built.append(1) or _FakeGmail(msgs))

    assert built, "service_factory was never called; workers shared one Resource"


def test_writes_only_happen_on_the_calling_thread(tmp_path, monkeypatch):
    """SQLite here is single-writer. Every upsert must land on the thread that
    called backfill_gmail, never in a fetch worker."""
    import threading

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:02d}": _msg(f"m{i:02d}", attachment=True) for i in range(15)}
    store = _store(tmp_path)
    main = threading.get_ident()
    offenders: list = []
    orig = store.upsert_chunk

    def _spy(*a, **kw):
        if threading.get_ident() != main:
            offenders.append(threading.get_ident())
        return orig(*a, **kw)

    store.upsert_chunk = _spy

    backfill_gmail(_FakeGmail(msgs), store, after="1970/01/01", max_workers=8,
                   service_factory=lambda: _FakeGmail(msgs))

    assert offenders == [], f"{len(offenders)} chunk writes happened off-thread"


def test_attachment_skips_are_aggregated_not_written_per_attachment(tmp_path, monkeypatch):
    """fetch_and_normalise recorded one change_log row PER skipped attachment.
    change_log is pruned to 500 rows and doubles as the user-facing change
    digest, so a full-history pass over a mailbox full of images and .zips would
    evict the entire audit trail — and in a worker thread those writes also break
    the single-writer rule. Same defect class already fixed for the Drive path in
    bin/repair.py.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # 30 messages, each with an attachment type nothing can extract.
    msgs = {}
    for i in range(30):
        mid = f"m{i:02d}"
        m = _msg(mid)
        m["payload"] = {
            "mimeType": "multipart/mixed",
            "headers": m["payload"]["headers"],
            "parts": [{"mimeType": "application/zip", "filename": f"{mid}.zip",
                       "body": {"attachmentId": f"a-{mid}", "size": 64}}],
        }
        msgs[mid] = m
    store = _store(tmp_path)

    backfill_gmail(_FakeGmail(msgs), store, after="1970/01/01", max_workers=6,
                   service_factory=lambda: _FakeGmail(msgs))

    with store._connect() as db:
        rows = db.execute(
            "SELECT summary FROM change_log WHERE change_type='ingest_skip'"
        ).fetchall()
    assert len(rows) <= 3, (
        f"{len(rows)} skip rows for 30 skipped attachments — must be aggregated, "
        "or a full-history pass evicts the 500-row change_log"
    )
    assert any("zip" in r["summary"] for r in rows), (
        f"aggregation lost WHICH type was skipped: {[r['summary'] for r in rows]}"
    )


def test_one_failing_message_does_not_end_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:02d}": _msg(f"m{i:02d}") for i in range(10)}
    store = _store(tmp_path)

    class _Flaky(_FakeGmail):
        def execute(self, num_retries=0):
            pending = self._pending
            if isinstance(pending, dict) and pending.get("id") == "m05":
                raise RuntimeError("transient explosion")
            return pending

    n = backfill_gmail(_Flaky(msgs), store, after="1970/01/01", max_workers=4,
                       service_factory=lambda: _Flaky(msgs))

    assert n == 9, f"expected 9 of 10 indexed, got {n}"
    assert store.get_chunk("gmail-m00-body-0") is not None


def test_a_404_message_is_skipped_quietly(tmp_path, monkeypatch):
    """A message deleted between list and get is normal, not an error."""
    from googleapiclient.errors import HttpError

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:02d}": _msg(f"m{i:02d}") for i in range(6)}
    store = _store(tmp_path)

    class _Resp:
        status = 404
        reason = "Not Found"

    class _Gone(_FakeGmail):
        def execute(self, num_retries=0):
            pending = self._pending
            if isinstance(pending, dict) and pending.get("id") == "m03":
                raise HttpError(_Resp(), b"gone")
            return pending

    n = backfill_gmail(_Gone(msgs), store, after="1970/01/01", max_workers=4,
                       service_factory=lambda: _Gone(msgs))

    assert n == 5
    assert store.get_chunk("gmail-m03-body-0") is None


def test_max_messages_still_bounds_a_parallel_run(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:03d}": _msg(f"m{i:03d}") for i in range(40)}
    store = _store(tmp_path)

    n = backfill_gmail(_FakeGmail(msgs, page_size=10), store, after="1970/01/01",
                       max_messages=15, max_workers=5,
                       service_factory=lambda: _FakeGmail(msgs, page_size=10))

    assert n <= 20, f"limit 15 overshot to {n} (one page of slack is acceptable)"
    assert n >= 15


def test_sequential_path_is_unchanged_when_no_factory_is_given(tmp_path, monkeypatch):
    """max_workers>1 without a service_factory must stay sequential rather than
    silently sharing one non-thread-safe Resource across workers."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:02d}": _msg(f"m{i:02d}") for i in range(8)}
    store = _store(tmp_path)

    n = backfill_gmail(_FakeGmail(msgs), store, after="1970/01/01", max_workers=8)

    assert n == 8


@pytest.mark.parametrize("workers", [1, 4])
def test_pagination_is_followed_in_both_modes(tmp_path, monkeypatch, workers):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    msgs = {f"m{i:03d}": _msg(f"m{i:03d}") for i in range(25)}
    store = _store(tmp_path)

    n = backfill_gmail(_FakeGmail(msgs, page_size=10), store, after="1970/01/01",
                       max_workers=workers,
                       service_factory=lambda: _FakeGmail(msgs, page_size=10))

    assert n == 25, "later pages were dropped"
