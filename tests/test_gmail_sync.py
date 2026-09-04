"""Tests for mcpbrain.sync.gmail — fake service, no network."""

import base64

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mcpbrain.store import Store
from mcpbrain.sync.gmail import discover_gmail, handle_gmail_item


# ---------------------------------------------------------------------------
# Helpers shared with test_normalise.py
# ---------------------------------------------------------------------------

def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def plain_msg(mid: str, subject: str, sender: str, body: str) -> dict:
    return {
        "id": mid,
        "threadId": "t-" + mid,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": b64(body)},
        },
    }


# ---------------------------------------------------------------------------
# Fake Gmail service
# ---------------------------------------------------------------------------

class _Req:
    """One pending request. `executes` is a shared list every fake in the
    service appends its `num_retries` to, so a test can assert the retry
    parameter reached every call site (see FakeService.execute_retries)."""

    def __init__(self, result, executes=None):
        self._r = result
        self._executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self._executes.append(num_retries)
        return self._r


class _History:
    def __init__(self, pages, raise_on_list=None, executes=None):
        # pages is a list of page dicts; pageToken "1","2",... indexes self._pages
        self._pages = pages
        self._raise = raise_on_list  # if set, raise this on list()
        self._executes = executes

    def list(self, **kw):
        if self._raise is not None:
            raise self._raise
        token = kw.get("pageToken")
        idx = 0 if token is None else int(token)
        return _Req(self._pages[idx], self._executes)


class _Messages:
    def __init__(self, by_id, executes=None):
        self._by_id = by_id
        self.get_call_count = {}  # mid -> count
        self._executes = executes

    def get(self, userId, id, format):
        self.get_call_count[id] = self.get_call_count.get(id, 0) + 1
        result = self._by_id[id]
        if isinstance(result, Exception):
            raise result
        return _Req(result, self._executes)


class _Users:
    def __init__(self, profile_hid, history, messages, executes=None):
        self._p = profile_hid
        self._h = history
        self._m = messages
        self._executes = executes

    def getProfile(self, userId):
        return _Req({"historyId": self._p, "emailAddress": "test@example.com"},
                    self._executes)

    def history(self):
        return self._h

    def messages(self):
        return self._m


class FakeService:
    def __init__(self, profile_hid="1000", pages=None, messages=None, raise_on_list=None):
        # Shared across every sub-fake: `execute_retries` ends up holding one
        # entry per .execute() call, in call order.
        self.execute_retries: list = []
        msgs = _Messages(messages or {}, self.execute_retries)
        self._users = _Users(
            profile_hid,
            _History(pages or [], raise_on_list=raise_on_list,
                     executes=self.execute_retries),
            msgs, executes=self.execute_retries)
        self._messages = msgs  # expose for call-count assertions

    def users(self):
        return self._users


def _make_page(msg_ids, history_id, next_page_token=None):
    """Build a history.list response page."""
    history = [
        {
            "id": f"h-{mid}",
            "messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX"]}}],
        }
        for mid in msg_ids
    ]
    page = {"history": history, "historyId": history_id}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bootstrap_sets_cursor_no_messages(tmp_path):
    """First run: no cursor stored. discover_gmail should read historyId from
    profile, store it, return 0, and enqueue nothing."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    svc = FakeService(profile_hid="1000")
    result = discover_gmail(svc, store)

    assert result == 0
    assert store.get_cursor("gmail") == "1000"
    assert store.sync_queue_pending() == 0


def test_handle_gmail_item_fetches_and_upserts(tmp_path):
    """Work one queued Gmail message: fetch the full message, normalise, and
    upsert its chunk -- the fetch/write half discover_gmail no longer does
    itself (it only lists and enqueues)."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    msg_m1 = plain_msg("m1", "Budget update", "alice@example.com",
                       "The quarterly budget review is scheduled for next week.")
    svc = FakeService(profile_hid="1000", messages={"m1": msg_m1})

    handle_gmail_item(svc, store, {"ref_id": "m1", "version": "", "event": "upsert",
                                   "modified_at": "2026-09-04T00:00:00"})

    chunk = store.get_chunk("gmail-m1-body-0")
    assert chunk is not None
    assert "budget" in chunk["text"].lower()


def test_duplicate_message_id_enqueues_once(tmp_path):
    """m1 appears in two messagesAdded entries within one page. sync_queue's
    PRIMARY KEY (source, ref_id) collapses them to one queued row, not two --
    the durable equivalent of the old in-call dedup set."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    page = {
        "history": [
            {"id": "h1", "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]},
            {"id": "h2", "messagesAdded": [{"message": {"id": "m1", "labelIds": ["INBOX"]}}]},
        ],
        "historyId": "1010",
    }
    svc = FakeService(profile_hid="1000", pages=[page])

    discover_gmail(svc, store)

    assert store.sync_queue_pending("gmail") == 1


def test_expired_historyid_rebootstraps(tmp_path):
    """history().list() raises 404 INVALID_HISTORY_ID -> re-bootstrap to fresh historyId, return 0."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    # Pre-seed an old cursor to trigger the delta path
    store.set_cursor("gmail", "1000")

    error = HttpError(httplib2.Response({"status": 404}), b"INVALID_HISTORY_ID")
    # profile_hid = "5000" is what getProfile returns during re-bootstrap
    svc = FakeService(profile_hid="5000", raise_on_list=error)

    result = discover_gmail(svc, store)

    assert result == 0
    assert store.get_cursor("gmail") == "5000"


def test_expired_historyid_410_rebootstraps(tmp_path):
    """history().list() raises 410 -> also triggers re-bootstrap."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "2000")

    error = HttpError(httplib2.Response({"status": 410}), b"Sync token expired")
    svc = FakeService(profile_hid="6000", raise_on_list=error)

    result = discover_gmail(svc, store)

    assert result == 0
    assert store.get_cursor("gmail") == "6000"


def test_non_404_httperror_propagates(tmp_path):
    """history().list() raises HttpError with status 500 -> propagates, not swallowed."""
    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    error = HttpError(httplib2.Response({"status": 500}), b"Internal Server Error")
    svc = FakeService(profile_hid="1000", raise_on_list=error)

    with pytest.raises(HttpError):
        discover_gmail(svc, store)

    # Cursor must be unchanged
    assert store.get_cursor("gmail") == "1000"


# ---------------------------------------------------------------------------
# Retry coverage for the discovery/work path
#
# _fetch_one / backfill_gmail's messages.list already passed num_retries;
# discover_gmail's bootstrap getProfile, history.list pagination, and
# 404/410-recovery getProfile, plus handle_gmail_item's messages.get, are the
# calls the delta path -- the path that runs every cycle -- makes.
# ---------------------------------------------------------------------------

def test_bootstrap_get_profile_passes_num_retries(tmp_path):
    from mcpbrain.sync.gmail import _NUM_RETRIES

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    svc = FakeService(profile_hid="1000")
    discover_gmail(svc, store)

    assert svc.execute_retries == [_NUM_RETRIES]


def test_discover_and_handle_pass_num_retries(tmp_path):
    from mcpbrain.sync.gmail import _NUM_RETRIES

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg("m1", "Budget update", "alice@example.com",
                       "The quarterly budget review is scheduled for next week.")
    pages = [_make_page(["m1"], history_id="1005")]
    svc = FakeService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    assert discover_gmail(svc, store) == 1
    handle_gmail_item(svc, store, {"ref_id": "m1", "version": "", "event": "upsert",
                                   "modified_at": "2026-09-04T00:00:00"})

    # One history.list (discover_gmail) + one messages.get (handle_gmail_item),
    # both retried.
    assert svc.execute_retries == [_NUM_RETRIES, _NUM_RETRIES]


def test_expired_historyid_recovery_get_profile_passes_num_retries(tmp_path):
    """The 404/410 re-bootstrap getProfile is the one call that runs when the
    mailbox's history window has already been lost -- a transient failure here
    leaves the cursor stale for another whole cycle."""
    from mcpbrain.sync.gmail import _NUM_RETRIES

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.set_cursor("gmail", "1000")

    error = HttpError(httplib2.Response({"status": 410}), b"Sync token expired")
    svc = FakeService(profile_hid="5000", raise_on_list=error)

    assert discover_gmail(svc, store) == 0

    # history.list raised before .execute(), so the only recorded call is the
    # recovery getProfile.
    assert svc.execute_retries == [_NUM_RETRIES]


# ---------------------------------------------------------------------------
# Task 5 (attachment wiring), now against handle_gmail_item -- the work-loop
# handler that fetches/normalises/upserts one queued message.
# ---------------------------------------------------------------------------

def test_handle_gmail_item_ingests_attachments(tmp_path, monkeypatch):
    """Wiring test: the attachment path must be reached from the real work
    handler, not merely be callable in isolation. normalise_gmail has never
    called it, which is why A1 went unnoticed."""
    from mcpbrain.sync import attachments
    from mcpbrain.sync.normalise import Chunk

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    seen: list = []

    def _fake_fetch(service, raw, store=None):
        seen.append(raw["id"])
        return [Chunk(doc_id=f"gmail-{raw['id']}-att-0-0", text="Total due: 4,200.00",
                      content_hash="h1",
                      metadata={"source_type": "gmail",
                                "content_type": "email_attachment",
                                "message_id": raw["id"]})]

    monkeypatch.setattr(attachments, "fetch_and_normalise", _fake_fetch)
    svc = FakeService(profile_hid="1000",
                      messages={"m1": plain_msg("m1", "Invoice", "a@b.com",
                                                "See attached.")})

    handle_gmail_item(svc, store, {"ref_id": "m1", "version": "", "event": "upsert",
                                   "modified_at": "2026-09-04T00:00:00"},
                      fetch_attachments=True)

    assert seen == ["m1"], "handle_gmail_item never reached the attachment path"
    assert store.get_chunk("gmail-m1-att-0-0") is not None


def test_handle_gmail_item_skips_attachments_when_the_flag_is_off(tmp_path, monkeypatch):
    from mcpbrain.sync import attachments

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    called: list = []
    monkeypatch.setattr(attachments, "fetch_and_normalise",
                        lambda *a, **kw: called.append(1) or [])

    handle_gmail_item(
        FakeService(profile_hid="1000",
                   messages={"m1": plain_msg("m1", "s", "a@b.com", "body")}),
        store,
        {"ref_id": "m1", "version": "", "event": "upsert",
         "modified_at": "2026-09-04T00:00:00"},
        fetch_attachments=False)

    assert called == []


def test_backfill_gmail_can_narrow_the_query(tmp_path):
    """A full-history attachment backfill must fetch ONLY attachment-bearing
    mail. Gmail's `has:attachment` is a server-side filter, so the backfill costs
    one list page per hundred matches instead of re-walking the whole mailbox —
    the difference between a targeted repair and re-ingesting everything."""
    from mcpbrain.sync.gmail import backfill_gmail

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    seen: dict = {}

    class _Svc:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **params):
            seen["q"] = params.get("q")
            return self

        def execute(self, num_retries=0):
            return {"messages": []}

    assert backfill_gmail(_Svc(), store, after="1970/01/01",
                          q_extra="has:attachment") == 0
    assert seen["q"] == "after:1970/01/01 has:attachment"


def test_backfill_gmail_without_q_extra_is_unchanged(tmp_path):
    from mcpbrain.sync.gmail import backfill_gmail

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    seen: dict = {}

    class _Svc:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **params):
            seen["q"] = params.get("q")
            return self

        def execute(self, num_retries=0):
            return {"messages": []}

    backfill_gmail(_Svc(), store, after="2026/01/01", before="2026/02/01")

    assert seen["q"] == "after:2026/01/01 before:2026/02/01"


# ---------------------------------------------------------------------------
# Task 8: reingest_messages -- re-chunk stale threads under the current
# chunker version, mirroring sync/drive.py's reingest_files.
# ---------------------------------------------------------------------------

def test_reingest_messages_rechunks_a_stale_thread(tmp_path):
    """A thread with a chunk stamped chunker_version=1 gets re-fetched and
    re-chunked; the new chunk carries the current CHUNKER_VERSION and the
    freshly-fetched text."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old short content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    msg_m1 = plain_msg("m1", "Re: test", "a@b.com", "new content")
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 0}
    chunk = store.get_chunk("gmail-m1-body-0")
    assert chunk is not None
    assert chunk["metadata"]["chunker_version"] == CHUNKER_VERSION
    assert "new content" in chunk["text"]


def test_reingest_messages_stamps_version_on_a_missing_message(tmp_path):
    """A 404'd message's existing chunks get stamped to the current
    chunker_version anyway -- the convergence guard that stops
    store.stale_chunker_ids from re-selecting the same dead thread forever
    (mirrors reingest_files' missing/empty stamping)."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    error = HttpError(httplib2.Response({"status": 404}), b"not found")
    svc = FakeService(messages={"m1": error})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 0, "missing": 1, "empty": 0, "failed": 0}
    chunk = store.get_chunk("gmail-m1-body-0")
    assert chunk is not None
    assert chunk["metadata"]["chunker_version"] == CHUNKER_VERSION
    assert chunk["metadata"]["reextract_missing"] is True
    # Stamping touches metadata only -- the existing content is left alone.
    assert chunk["text"] == "old content"


def test_reingest_messages_one_bad_message_does_not_end_the_run(tmp_path):
    """A non-404 failure on one thread is isolated (counted as `failed`) and
    does not prevent the next thread's message from being re-chunked."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})
    store.upsert_chunk("gmail-m2-body-0", "old content 2", "h2",
                       {"source_type": "gmail", "thread_id": "t2",
                        "message_id": "m2", "chunker_version": 1})

    error = HttpError(httplib2.Response({"status": 500}), b"boom")
    msg_m2 = plain_msg("m2", "Re: test 2", "a@b.com", "fresh content")
    msg_m2["threadId"] = "t2"
    svc = FakeService(messages={"m1": error, "m2": msg_m2})

    summary = reingest_messages(svc, store, ["t1", "t2"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 1}
    # t1's chunk is untouched -- a transient/non-404 failure must not stamp
    # the convergence guard, or a retryable error would wrongly converge.
    assert store.get_chunk("gmail-m1-body-0")["metadata"]["chunker_version"] == 1
    assert store.get_chunk("gmail-m2-body-0")["metadata"]["chunker_version"] == CHUNKER_VERSION


def test_reingest_messages_post_fetch_failure_is_isolated(tmp_path, monkeypatch):
    """An exception AFTER a successful fetch (in normalise/upsert/patch) must
    also be caught -- not just a fetch failure. Simulates a write-path error
    (e.g. a SQLite write failure) on one message; it must be counted `failed`
    and must not abort processing of the next thread_id in the same batch."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})
    store.upsert_chunk("gmail-m2-body-0", "old content 2", "h2",
                       {"source_type": "gmail", "thread_id": "t2",
                        "message_id": "m2", "chunker_version": 1})

    msg_m1 = plain_msg("m1", "Re: test", "a@b.com", "new content 1")
    msg_m1["threadId"] = "t1"
    msg_m2 = plain_msg("m2", "Re: test 2", "a@b.com", "new content 2")
    msg_m2["threadId"] = "t2"
    svc = FakeService(messages={"m1": msg_m1, "m2": msg_m2})

    real_upsert = store.upsert_chunk

    def _boom(doc_id, text, content_hash, metadata):
        if doc_id == "gmail-m1-body-0":
            raise RuntimeError("simulated sqlite write failure")
        return real_upsert(doc_id, text, content_hash, metadata)

    monkeypatch.setattr(store, "upsert_chunk", _boom)

    summary = reingest_messages(svc, store, ["t1", "t2"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 1}
    # t1's chunk is untouched by the failed write -- still the pre-existing
    # row, not stamped, since the failure is retryable, not a convergent one.
    t1_chunk = store.get_chunk("gmail-m1-body-0")
    assert t1_chunk["text"] == "old content"
    assert t1_chunk["metadata"]["chunker_version"] == 1
    # t2 is still re-chunked despite t1's write-path failure.
    t2_chunk = store.get_chunk("gmail-m2-body-0")
    assert t2_chunk["metadata"]["chunker_version"] == CHUNKER_VERSION
    assert "new content 2" in t2_chunk["text"]


def test_reingest_messages_stamps_version_on_empty_normalise_result(tmp_path):
    """A message that fetches successfully but normalises to zero chunks
    (here, a body too short to survive extract_body_with_signature's >10-char
    threshold) still gets its existing chunks stamped to the current
    chunker_version -- otherwise store.stale_chunker_ids re-selects this
    message on every future repair run forever, the identical non-convergence
    bug class the missing-message guard exists to prevent."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    msg_m1 = plain_msg("m1", "Re: test", "a@b.com", "hi")
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 0, "missing": 0, "empty": 1, "failed": 0}
    chunk = store.get_chunk("gmail-m1-body-0")
    assert chunk is not None
    assert chunk["metadata"]["chunker_version"] == CHUNKER_VERSION
    assert chunk["metadata"]["reextract_empty"] is True
    # Stamping touches metadata only -- the existing content is left alone.
    assert chunk["text"] == "old content"


# ---------------------------------------------------------------------------
# Final-review C1/I1: the unchanged-content_hash guard, and attachments
# ---------------------------------------------------------------------------

def _att_fake(chunks, *, skips=None):
    """A fake attachments.fetch_and_normalise matching the real signature
    _fetch_one calls it with (report=..., no store)."""
    seen: list = []

    def _fetch(service, raw, *, store=None, report=None):
        seen.append(raw["id"])
        if skips and report is not None:
            for key, count in skips.items():
                report[key] = report.get(key, 0) + count
        return list(chunks)

    return _fetch, seen


def test_reingest_messages_stamps_version_on_a_byte_identical_rechunk(tmp_path):
    """The NORMAL case for a Gmail sweep, not a corner one.

    CHUNKER_VERSION 2->3 changed sync/tabular.py only -- `chunk_text` (which
    chunks every Gmail body) is untouched -- so a re-fetched prose message
    re-chunks BYTE-IDENTICALLY. store.upsert_chunk short-circuits on an
    unchanged content_hash and writes nothing at all, metadata included, so
    without the patch_chunk_metadata fallback the chunk never acquires the new
    chunker_version, store.stale_chunker_ids re-selects the thread forever,
    and `bin/repair.py reingest-stale` burns Gmail quota on the same threads
    every run while reporting success.
    """
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.gmail import reingest_messages
    from mcpbrain.sync.normalise import normalise_gmail

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()

    msg_m1 = plain_msg("m1", "Re: test", "a@b.com",
                       "This body is stored exactly as it re-chunks.")
    msg_m1["threadId"] = "t1"
    # Seed EXACTLY what the re-fetch will produce, only on the old version.
    expected = normalise_gmail(msg_m1)
    assert expected, "fixture must produce at least one body chunk"
    for c in expected:
        store.upsert_chunk(c.doc_id, c.text, c.content_hash,
                           {**c.metadata, "chunker_version": 1})

    svc = FakeService(messages={"m1": msg_m1})
    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 0}
    for c in expected:
        chunk = store.get_chunk(c.doc_id)
        assert chunk["metadata"]["chunker_version"] == CHUNKER_VERSION, c.doc_id
        assert chunk["text"] == c.text          # unchanged, as expected


def test_reingest_messages_reingests_attachment_chunks_and_sweeps_orphans(
        tmp_path, monkeypatch):
    """Attachment chunks are the ones the tabular fix exists to repair.

    They are produced by sync/attachments.normalise_attachment (which routes
    spreadsheets through sync/tabular.render_chunks), so a body-only re-ingest
    would leave every emailed workbook on the old chunker_version and the
    thread would be re-selected forever. They are also the one shape that can
    SHRINK -- a phantom-column-inflated sheet collapses to fewer chunks -- so
    the surplus tail doc_ids must be deleted, not left behind as searchable
    garbage (B5).
    """
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync import attachments
    from mcpbrain.sync.gmail import reingest_messages
    from mcpbrain.sync.normalise import Chunk

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))

    store.upsert_chunk("gmail-m1-body-0", "old body", "hb",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})
    # Two attachment chunks today; the re-render produces only the first.
    store.upsert_chunk("gmail-m1-att-0-0", "Item: chair; Cost: 12", "ha0",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "content_subtype": "table",
                        "chunker_version": 1})
    store.upsert_chunk("gmail-m1-att-0-1", "phantom-column overflow", "ha1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "content_subtype": "table",
                        "chunker_version": 1})

    # Byte-identical to what is stored, so this ALSO exercises the C1 guard on
    # the attachment write path.
    fresh_att = Chunk(doc_id="gmail-m1-att-0-0", text="Item: chair; Cost: 12",
                      content_hash="ha0",
                      metadata={"source_type": "gmail", "thread_id": "t1",
                                "message_id": "m1", "content_subtype": "table",
                                "chunker_version": CHUNKER_VERSION})
    fetch, seen = _att_fake([fresh_att])
    monkeypatch.setattr(attachments, "fetch_and_normalise", fetch)

    msg_m1 = plain_msg("m1", "Invoice", "a@b.com", "See the attached sheet.")
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 0}
    assert seen == ["m1"], "reingest_messages never reached the attachment path"
    # The surviving attachment chunk acquired the current version even though
    # its text was byte-identical (the C1 guard on the attachment write).
    assert store.get_chunk("gmail-m1-att-0-0")["metadata"][
        "chunker_version"] == CHUNKER_VERSION
    # The orphaned tail chunk is gone.
    assert store.get_chunk("gmail-m1-att-0-1") is None
    # The body was re-chunked normally and is NOT swept.
    assert store.get_chunk("gmail-m1-body-0")["metadata"][
        "chunker_version"] == CHUNKER_VERSION


def test_reingest_messages_orphan_sweep_skipped_after_an_attachment_failure(
        tmp_path, monkeypatch):
    """attachments.fetch_and_normalise is best-effort per attachment: a 404 or
    a failed extraction silently yields FEWER chunks. Deleting on that would
    destroy previously-good chunks over a transient error -- the same hazard
    drive.upsert_file_chunks' `partial` guard exists for."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync import attachments
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))

    store.upsert_chunk("gmail-m1-body-0", "old body", "hb",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})
    store.upsert_chunk("gmail-m1-att-0-0", "good rows", "ha0",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    fetch, _seen = _att_fake(
        [], skips={("attachment_fetch_failed", "application/pdf"): 1})
    monkeypatch.setattr(attachments, "fetch_and_normalise", fetch)

    msg_m1 = plain_msg("m1", "Invoice", "a@b.com", "See the attached sheet.")
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 0}
    assert store.get_chunk("gmail-m1-att-0-0") is not None, \
        "a transient attachment failure must never be read as a shrink"
    # Its version is deliberately NOT stamped either -- nothing re-rendered it.
    assert store.get_chunk("gmail-m1-att-0-0")["metadata"]["chunker_version"] == 1
    assert store.get_chunk("gmail-m1-body-0")["metadata"][
        "chunker_version"] == CHUNKER_VERSION


def test_reingest_messages_orphan_sweep_skipped_after_an_unsupported_attachment(
        tmp_path, monkeypatch):
    """attachment_unsupported is deterministic TODAY (an unsupported mime type
    stays unsupported), but that's only true as long as the supported set
    never shrinks. Treated as ambiguous like fetch_failed/empty so a future
    mime-support removal can't silently read as a shrink and delete
    previously-good chunks."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync import attachments
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))

    store.upsert_chunk("gmail-m1-body-0", "old body", "hb",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})
    store.upsert_chunk("gmail-m1-att-0-0", "good rows", "ha0",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    fetch, _seen = _att_fake(
        [], skips={("attachment_unsupported", "application/zip"): 1})
    monkeypatch.setattr(attachments, "fetch_and_normalise", fetch)

    msg_m1 = plain_msg("m1", "Invoice", "a@b.com", "See the attached sheet.")
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 0}
    assert store.get_chunk("gmail-m1-att-0-0") is not None, \
        "an unsupported-attachment skip must never be read as a shrink"
    assert store.get_chunk("gmail-m1-body-0")["metadata"][
        "chunker_version"] == CHUNKER_VERSION


def test_reingest_messages_orphan_sweep_skipped_when_attachments_are_off(
        tmp_path, monkeypatch):
    """With gmail_attachments off, att_chunks is empty for EVERY message --
    which is not evidence that anything shrank. Previously-ingested attachment
    chunks must survive untouched."""
    from mcpbrain.sync import attachments
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"gmail_attachments": false}')

    store.upsert_chunk("gmail-m1-body-0", "old body", "hb",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})
    store.upsert_chunk("gmail-m1-att-0-0", "good rows", "ha0",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    called: list = []
    monkeypatch.setattr(attachments, "fetch_and_normalise",
                        lambda *a, **kw: called.append(1) or [])

    msg_m1 = plain_msg("m1", "Invoice", "a@b.com", "See the attached sheet.")
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    reingest_messages(svc, store, ["t1"])

    assert called == [], "the flag is off; the attachment path must not run"
    assert store.get_chunk("gmail-m1-att-0-0") is not None


def test_reingest_messages_converges_a_body_that_normalises_to_nothing(
        tmp_path, monkeypatch):
    """Half-empty message: the body normalises to zero chunks (here, a body
    below extract_body_with_signature's >10-char threshold) but an attachment
    still produces one.

    Folding att_chunks into the emptiness check means the whole-message
    `empty` outcome no longer fires, so the surviving BODY doc_ids would keep
    their old chunker_version and store.stale_chunker_ids would re-select this
    thread forever -- the same non-convergence, narrowed to one half of the
    message."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync import attachments
    from mcpbrain.sync.gmail import reingest_messages
    from mcpbrain.sync.normalise import Chunk

    store = Store(tmp_path / "test.sqlite3", dim=4)
    store.init()
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))

    store.upsert_chunk("gmail-m1-body-0", "old body", "hb",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    fresh_att = Chunk(doc_id="gmail-m1-att-0-0", text="Item: chair; Cost: 12",
                      content_hash="ha0",
                      metadata={"source_type": "gmail", "thread_id": "t1",
                                "message_id": "m1",
                                "chunker_version": CHUNKER_VERSION})
    fetch, _seen = _att_fake([fresh_att])
    monkeypatch.setattr(attachments, "fetch_and_normalise", fetch)

    msg_m1 = plain_msg("m1", "Invoice", "a@b.com", "hi")   # body too short
    msg_m1["threadId"] = "t1"
    svc = FakeService(messages={"m1": msg_m1})

    summary = reingest_messages(svc, store, ["t1"])

    assert summary == {"messages": 1, "missing": 0, "empty": 0, "failed": 0}
    assert store.get_chunk("gmail-m1-att-0-0") is not None
    body = store.get_chunk("gmail-m1-body-0")
    assert body["metadata"]["chunker_version"] == CHUNKER_VERSION
    assert body["metadata"]["reextract_empty"] is True
    # Metadata only -- the existing body text is left alone.
    assert body["text"] == "old body"
