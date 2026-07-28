"""Tests for thread_enrich — grouping the unenriched backlog by thread and
reassembling a thread's chunks into ordered messages.

These primitives are consumed by prepare.py's _group_unenriched_threads /
_reassemble_thread seams. The interface is locked: group_unenriched_threads
takes the keyword thread_cap, returns batch objects with .thread_id / .doc_ids /
.chunks; reassemble_thread returns message dicts with the per-message provenance
fields plus the body text.
"""

from mcpbrain.store import Store
from mcpbrain import thread_enrich


def _store(tmp_path):
    s = Store(tmp_path / "t.sqlite3", dim=4)
    s.init()
    return s


def _seed(store, doc_id, *, text="body", thread_id="", message_id="",
          chunk_index=0, sender="", subject="", date="", labels=""):
    """Insert one unenriched chunk (enriched defaults to 0)."""
    meta = {
        "source_type": "gmail",
        "message_id": message_id,
        "thread_id": thread_id,
        "subject": subject,
        "sender": sender,
        "date": date,
        "labels": labels,
        "content_type": "email_body",
        "chunk_index": chunk_index,
    }
    store.upsert_chunk(doc_id, text, f"hash-{doc_id}", meta)


def test_chunk_key_shared_precedence_matches_group_key_and_reassemble():
    # _group_key and reassemble_thread must not drift on the message-identity
    # portion of their key precedence (file_id, else message_id, else doc_id --
    # the part reassemble_thread actually uses). Both now delegate to the one
    # shared thread_enrich._chunk_key helper, so the three cases below resolve
    # identically at both call sites. thread_id is intentionally NOT part of
    # this shared portion: _group_key checks it first at the whole-backlog
    # (batch-forming) level, but reassemble_thread runs WITHIN an
    # already-thread-grouped batch, where thread_id is constant across every
    # message and would wrongly collapse distinct messages into one if applied
    # there too (see test_reassemble_thread_orders_messages_by_date, where four
    # chunks share one thread_id but must reassemble into two messages).
    cases = [
        ({"file_id": "f1", "message_id": "m1"}, "doc-1", "f1"),  # file_id wins
        ({"message_id": "m1"}, "doc-1", "m1"),  # message_id wins over doc_id
        ({}, "doc-1", "doc-1"),  # doc_id fallback
    ]
    for meta, doc_id, expected in cases:
        assert thread_enrich._chunk_key(meta, doc_id) == expected

        # _group_key call site: delegates to the same helper when thread_id is
        # absent from the chunk's metadata.
        chunk = {"doc_id": doc_id, "metadata": meta}
        assert thread_enrich._group_key(chunk) == expected

        # reassemble_thread call site: reassembling a single chunk with this
        # metadata emits a message keyed the same way.
        msgs = thread_enrich.reassemble_thread(
            [{"doc_id": doc_id, "metadata": meta, "text": "x"}])
        assert msgs[0]["message_id"] == expected


def test_group_unenriched_by_thread(tmp_path):
    store = _store(tmp_path)
    # Two threads, two chunks each.
    _seed(store, "gmail-a-body-0", thread_id="thread-A", message_id="a", chunk_index=0)
    _seed(store, "gmail-a-body-1", thread_id="thread-A", message_id="a", chunk_index=1)
    _seed(store, "gmail-b-body-0", thread_id="thread-B", message_id="b", chunk_index=0)
    _seed(store, "gmail-b-body-1", thread_id="thread-B", message_id="b", chunk_index=1)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=10)

    by_id = {b.thread_id: b for b in batches}
    assert set(by_id) == {"thread-A", "thread-B"}
    assert set(by_id["thread-A"].doc_ids) == {"gmail-a-body-0", "gmail-a-body-1"}
    assert set(by_id["thread-B"].doc_ids) == {"gmail-b-body-0", "gmail-b-body-1"}
    # .chunks are the raw chunk dicts (carry the parsed metadata).
    assert len(by_id["thread-A"].chunks) == 2
    assert all("metadata" in c for c in by_id["thread-A"].chunks)


def _seed_drive(store, doc_id, *, file_id, text="body", chunk_index=0):
    """Insert one unenriched Drive chunk: file_id set, no message_id/thread_id
    (mirrors real Drive chunk metadata)."""
    meta = {
        "source_type": "gdrive",
        "file_id": file_id,
        "content_subtype": "prose",
        "chunk_index": chunk_index,
    }
    store.upsert_chunk(doc_id, text, f"hash-{doc_id}", meta)


def test_group_drive_chunks_by_file_id(tmp_path):
    store = _store(tmp_path)
    # Three chunks of ONE Drive file (no thread_id/message_id) + one email thread.
    fid = "1Tj2fbHCq5CN5d4uAZXjE0Is3zptIbu1P"
    _seed_drive(store, f"gdrive-{fid}-0", file_id=fid, chunk_index=0)
    _seed_drive(store, f"gdrive-{fid}-1", file_id=fid, chunk_index=1)
    _seed_drive(store, f"gdrive-{fid}-2", file_id=fid, chunk_index=2)
    _seed(store, "gmail-a-body-0", thread_id="thread-A", message_id="a", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=10)

    by_id = {b.thread_id: b for b in batches}
    # The whole Drive doc is ONE batch keyed on file_id, not three per-chunk ones.
    assert fid in by_id
    assert set(by_id[fid].doc_ids) == {
        f"gdrive-{fid}-0", f"gdrive-{fid}-1", f"gdrive-{fid}-2"}
    # Email grouping is untouched.
    assert "thread-A" in by_id


def test_group_missing_thread_id_falls_back_to_message_then_doc(tmp_path):
    store = _store(tmp_path)
    # No thread_id, but a message_id -> singleton keyed on message_id.
    _seed(store, "gmail-m1-body-0", thread_id="", message_id="m1", chunk_index=0)
    # No thread_id and no message_id -> singleton keyed on doc_id.
    _seed(store, "gmail-m2-body-0", thread_id="", message_id="", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=10)
    keys = {b.thread_id for b in batches}
    assert keys == {"m1", "gmail-m2-body-0"}
    # Each is a singleton.
    assert all(len(b.doc_ids) == 1 for b in batches)


def test_thread_cap_limits_threads(tmp_path):
    store = _store(tmp_path)
    # 5 distinct threads, one chunk each. Seed in a deterministic order so the
    # cap is stable: group_unenriched_threads preserves first-appearance order,
    # which mirrors the rowid order unenriched_chunks returns.
    for i in range(5):
        _seed(store, f"gmail-t{i}-body-0", thread_id=f"thread-{i}",
              message_id=f"t{i}", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=2)
    # The cap counts THREADS, not chunks. First two by newest-synced order (rowid DESC).
    assert len(batches) == 2
    assert [b.thread_id for b in batches] == ["thread-4", "thread-3"]


def test_reassemble_thread_orders_messages_by_date(tmp_path):
    store = _store(tmp_path)
    # One thread, two messages, two body chunks each. m-late is seeded first to
    # prove ordering is by date, not insertion order.
    _seed(store, "gmail-late-body-0", thread_id="thread-X", message_id="m-late",
          chunk_index=0, text="late part one", sender="b@x.com",
          subject="Re: hello", date="2026-06-02T10:00:00Z", labels="INBOX")
    _seed(store, "gmail-late-body-1", thread_id="thread-X", message_id="m-late",
          chunk_index=1, text="late part two", sender="b@x.com",
          subject="Re: hello", date="2026-06-02T10:00:00Z", labels="INBOX")
    _seed(store, "gmail-early-body-0", thread_id="thread-X", message_id="m-early",
          chunk_index=0, text="early part one", sender="a@x.com",
          subject="hello", date="2026-06-01T09:00:00Z", labels="INBOX,IMPORTANT")
    _seed(store, "gmail-early-body-1", thread_id="thread-X", message_id="m-early",
          chunk_index=1, text="early part two", sender="a@x.com",
          subject="hello", date="2026-06-01T09:00:00Z", labels="INBOX,IMPORTANT")

    batch = thread_enrich.group_unenriched_threads(store, thread_cap=10)[0]
    messages = thread_enrich.reassemble_thread(batch.chunks)

    assert [m["message_id"] for m in messages] == ["m-early", "m-late"]
    early = messages[0]
    assert early["sender"] == "a@x.com"
    assert early["subject"] == "hello"
    assert early["date"] == "2026-06-01T09:00:00Z"
    assert early["labels"] == "INBOX,IMPORTANT"
    # Body chunks concatenated in chunk_index order with a blank line.
    assert early["text"] == "early part one\n\nearly part two"
    assert messages[1]["text"] == "late part one\n\nlate part two"
    # Provenance contract: every message carries the locked fields.
    for m in messages:
        assert set(m) >= {"message_id", "sender", "date", "labels", "subject", "text"}


def test_reassemble_orders_chunks_out_of_order(tmp_path):
    store = _store(tmp_path)
    # Chunk 1 seeded before chunk 0 -> reassemble must sort by chunk_index.
    _seed(store, "gmail-m-body-1", thread_id="thread-Y", message_id="m",
          chunk_index=1, text="second", date="2026-06-01T00:00:00Z")
    _seed(store, "gmail-m-body-0", thread_id="thread-Y", message_id="m",
          chunk_index=0, text="first", date="2026-06-01T00:00:00Z")

    batch = thread_enrich.group_unenriched_threads(store, thread_cap=10)[0]
    messages = thread_enrich.reassemble_thread(batch.chunks)
    assert len(messages) == 1
    assert messages[0]["text"] == "first\n\nsecond"


def test_reassemble_thread_empty_returns_empty(tmp_path):
    # No chunks in, no messages out.
    assert thread_enrich.reassemble_thread([]) == []


def test_group_unenriched_thread_cap_zero_returns_empty(tmp_path):
    store = _store(tmp_path)
    # A backlog of several threads, but a zero cap admits none of them.
    for i in range(3):
        _seed(store, f"gmail-t{i}-body-0", thread_id=f"thread-{i}",
              message_id=f"t{i}", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=0)
    assert batches == []


def test_a_hole_in_a_message_is_marked():
    """B8: group_unenriched_threads iterates unenriched_chunks and
    reassemble_thread joins only those. If part of a document was already
    enriched — or cold-marked, excluded at store.py:1264 — the model received a
    partial document with no indication anything was missing."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": "gmail-m1-body-0", "text": "First half.",
         "metadata": {"message_id": "m1", "chunk_index": 0, "chunk_total": 3,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}},
        {"doc_id": "gmail-m1-body-2", "text": "Third part.",
         "metadata": {"message_id": "m1", "chunk_index": 2, "chunk_total": 3,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}},
    ]

    messages = list(reassemble_thread(chunks))

    assert len(messages) == 1
    assert "[…]" in messages[0]["text"], (
        "a gap between chunk 0 and chunk 2 must be visible to the model; "
        "silently concatenating them presents a partial document as whole"
    )


def test_a_truncated_tail_is_marked():
    """The other half of B8, which can occur alone: indices 0 and 1 present, but
    chunk_total says 5."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": f"gmail-m1-body-{i}", "text": f"part {i}",
         "metadata": {"message_id": "m1", "chunk_index": i, "chunk_total": 5,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}}
        for i in (0, 1)
    ]

    assert "[…]" in list(reassemble_thread(chunks))[0]["text"]


def test_a_complete_message_gets_no_gap_marker():
    """The discriminator: a marker on every message would train the model to
    ignore it."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": f"gmail-m1-body-{i}", "text": f"part {i}",
         "metadata": {"message_id": "m1", "chunk_index": i, "chunk_total": 2,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s"}}
        for i in (0, 1)
    ]

    assert "[…]" not in list(reassemble_thread(chunks))[0]["text"]


def test_a_message_with_no_chunk_total_gets_no_tail_marker():
    """chunk_total only exists on chunks written after this plan's C1 change.
    On older chunks the tail check must simply not fire."""
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [{"doc_id": "gmail-m1-body-0", "text": "only part",
               "metadata": {"message_id": "m1", "chunk_index": 0,
                            "date": "2026-06-01", "sender": "a@b.com",
                            "subject": "s"}}]

    assert "[…]" not in list(reassemble_thread(chunks))[0]["text"]


def test_the_semantic_digest_chunk_does_not_merge_with_the_message_it_summarises():
    """Regression: C3 stamps `message_id` on the semantic digest chunk
    (doc_id enriched-<thread_id>) for provenance, set to the SAME message_id
    the raw lead-message chunk carries. _chunk_key's fallback chain
    (file_id or message_id or doc_id) treated that as a GROUPING key, so once
    mark_thread_unenriched (store.py, called from stale_reextract.py) resets
    both chunks to enriched=0 together, reassemble_thread merged them into one
    "message" — blending the synthesized People:/Actions:/Topics: digest text
    into the raw email body sent back to the model for re-extraction. The
    digest chunk must stay its own singleton group; message_id is written for
    provenance reads, not for grouping.
    """
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": "gmail-m1-body-0", "text": "Raw lead message body.",
         "metadata": {"message_id": "m1", "chunk_index": 0,
                      "date": "2026-06-01", "sender": "a@b.com", "subject": "s",
                      "thread_id": "t1"}},
        {"doc_id": "enriched-t1", "text": "People: Sam\nActions:\n- Do the thing",
         "metadata": {"thread_id": "t1", "message_id": "m1"}},
    ]

    messages = reassemble_thread(chunks)

    assert len(messages) == 2, "the digest chunk must not merge into the raw message's group"
    texts = [m["text"] for m in messages]
    assert "Raw lead message body." in texts
    assert "People: Sam\nActions:\n- Do the thing" in texts
    # Neither text should contain the other, concatenated.
    raw_text = next(t for t in texts if t.startswith("Raw lead message body."))
    digest_text = next(t for t in texts if t.startswith("People:"))
    assert "People:" not in raw_text
    assert "Raw lead message body." not in digest_text


def test_an_attachment_does_not_merge_into_its_parent_message():
    """C2: attachment chunks deliberately carry their PARENT's message_id
    (sync/attachments.py), so _chunk_key's fallback chain resolved an attachment
    and the parent's body to the same identity. reassemble_thread merged them
    into one "message" and interleaved the two by chunk_index —
    'BODY0 […] ATT0 BODY1 […] ATT1' — so every email with an attachment handed
    the graph extractor a garbled, falsely gap-marked document.
    """
    from mcpbrain.thread_enrich import reassemble_thread

    body_meta = {"source_type": "gmail", "message_id": "m1", "thread_id": "t1",
                 "sender": "a@b.com", "subject": "Budget", "date": "2026-06-01",
                 "content_type": "email_body"}
    att_meta = {"source_type": "gmail", "message_id": "m1", "thread_id": "t1",
                "sender": "a@b.com", "subject": "Budget", "date": "2026-06-01",
                "content_type": "email_attachment",
                "attachment_name": "budget.xlsx"}
    chunks = [
        {"doc_id": "gmail-m1-body-0", "text": "BODY0",
         "metadata": {**body_meta, "chunk_index": 0, "chunk_total": 2}},
        {"doc_id": "gmail-m1-body-1", "text": "BODY1",
         "metadata": {**body_meta, "chunk_index": 1, "chunk_total": 2}},
        {"doc_id": "gmail-m1-att-0-0", "text": "ATT0",
         "metadata": {**att_meta, "chunk_index": 0, "chunk_total": 2}},
        {"doc_id": "gmail-m1-att-0-1", "text": "ATT1",
         "metadata": {**att_meta, "chunk_index": 1, "chunk_total": 2}},
    ]

    messages = reassemble_thread(chunks)

    assert len(messages) == 2, "the attachment must be its own message"
    body = next(m for m in messages if "BODY0" in m["text"])
    att = next(m for m in messages if "ATT0" in m["text"])
    assert body["text"] == "BODY0\n\nBODY1"
    assert att["text"] == "ATT0\n\nATT1"
    assert "ATT" not in body["text"]
    assert "BODY" not in att["text"]
    # Both halves are complete, so nothing may be marked as elided.
    assert "[…]" not in body["text"]
    assert "[…]" not in att["text"]
    # The EMITTED id stays the parent message id on both, because that is what
    # store.doc_ids_for_messages resolves back to these chunks (an id derived
    # from the attachment index would resolve to nothing and drain would discard
    # the extraction — the 0.7.98 Drive defect).
    assert [m["message_id"] for m in messages] == ["m1", "m1"]


def test_two_attachments_on_one_message_stay_separate():
    """The split is per ATTACHMENT, not "attachments vs body"."""
    from mcpbrain.thread_enrich import reassemble_thread

    att_meta = {"source_type": "gmail", "message_id": "m1", "thread_id": "t1",
                "content_type": "email_attachment", "date": "2026-06-01"}
    chunks = [
        {"doc_id": "gmail-m1-att-0-0", "text": "FIRST",
         "metadata": {**att_meta, "chunk_index": 0, "chunk_total": 1}},
        {"doc_id": "gmail-m1-att-1-0", "text": "SECOND",
         "metadata": {**att_meta, "chunk_index": 0, "chunk_total": 1}},
    ]

    texts = [m["text"] for m in reassemble_thread(chunks)]

    assert sorted(texts) == ["FIRST", "SECOND"]


def test_a_split_calendar_event_reassembles_into_one_message():
    """I6: calendar chunk metadata has no file_id and no message_id, so a long
    agenda split into cal-<eid>-0..3 gave _chunk_key nothing but each chunk's own
    doc_id — four singleton "messages", each one additionally stamped with a
    truncated-tail `[…]` marker because a singleton sees itself as a fragment of
    chunk_total. event_id is now in the chain, mirroring Drive's file_id.

    The emitted id asserted below was `evt9` (the BARE event_id) when I6 landed.
    That was wrong, not merely stylistic: it forked a SECOND identity namespace
    for calendar events beside the `cal-`-prefixed one every existing row uses,
    breaking store.meeting_series_for_old's `LIKE 'cal-%'` filter,
    semantic.build_semantic_doc's `calendar_enriched_v2` labelling, and
    graph_write's `enriched-<thread_id>` digest / action thread_id keying (a
    routine re-extraction would write a duplicate, un-closeable set). Tightened
    to the prefixed form, which is what the chunks' own doc_ids already use.
    """
    from mcpbrain.thread_enrich import reassemble_thread

    chunks = [
        {"doc_id": f"cal-evt9-{i}", "text": f"agenda part {i}",
         "metadata": {"source_type": "calendar", "event_id": "evt9",
                      "summary": "Leadership day", "start": "2026-06-01T09:00",
                      "chunk_index": i, "chunk_total": 4}}
        for i in range(4)
    ]

    messages = reassemble_thread(chunks)

    assert len(messages) == 1, "a split calendar event is ONE document"
    assert messages[0]["text"] == (
        "agenda part 0\n\nagenda part 1\n\nagenda part 2\n\nagenda part 3")
    assert "[…]" not in messages[0]["text"], (
        "the full chunk set is present — a gap marker here is a lie the model "
        "cannot check"
    )
    assert messages[0]["message_id"] == "cal-evt9", (
        "the emitted id must stay in the cal- namespace every existing calendar "
        "row uses — a bare event_id forks a second namespace"
    )


def test_a_split_calendar_event_groups_into_one_batch(tmp_path):
    """The other half of I6: _group_key also delegates to _chunk_key, so the four
    chunks must form ONE batch rather than four."""
    store = _store(tmp_path)
    for i in range(4):
        store.upsert_chunk(f"cal-evt9-{i}", f"agenda part {i}", f"h{i}",
                           {"source_type": "calendar", "event_id": "evt9",
                            "chunk_index": i, "chunk_total": 4})

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=10)

    assert len(batches) == 1
    assert batches[0].thread_id == "cal-evt9", (
        "same namespace as the emitted message_id — see the reassembly test")
    assert len(batches[0].doc_ids) == 4


def test_a_calendar_event_id_resolves_back_to_every_chunk_of_the_event(tmp_path):
    """The message_id reassemble_thread emits for a split calendar event
    (`cal-<event_id>`) MUST resolve in store.doc_ids_for_messages, or drain
    discards the extraction and the chunks re-queue forever — the 0.7.98 Drive
    defect. Hence the event_id arm in _doc_ids_query, bound with the `cal-`
    prefix stripped (no chunk's doc_id is the bare `cal-<eid>` once split)."""
    store = _store(tmp_path)
    for i in range(4):
        store.upsert_chunk(f"cal-evt9-{i}", f"agenda part {i}", f"h{i}",
                           {"source_type": "calendar", "event_id": "evt9",
                            "chunk_index": i, "chunk_total": 4})

    assert store.doc_ids_for_messages(["cal-evt9"]) == [
        "cal-evt9-0", "cal-evt9-1", "cal-evt9-2", "cal-evt9-3"]


def test_the_emitted_calendar_id_is_exactly_what_resolves_back(tmp_path):
    """End-to-end round trip, the property drain actually depends on: whatever
    _chunk_key emits for a calendar chunk set must resolve to that same chunk
    set. Asserting it as a round trip (rather than against a hardcoded literal on
    each side) is what catches a namespace fork like the bare-event_id one."""
    store = _store(tmp_path)
    for i in range(3):
        store.upsert_chunk(f"cal-evtRT-{i}", f"part {i}", f"hrt{i}",
                           {"source_type": "calendar", "event_id": "evtRT",
                            "chunk_index": i, "chunk_total": 3})
    chunks = list(store.unenriched_chunks())

    emitted = thread_enrich.reassemble_thread(chunks)[0]["message_id"]

    assert emitted.startswith("cal-"), (
        "graph_write/semantic/meeting_series_for_old all key off the cal- prefix")
    assert store.doc_ids_for_messages([emitted]) == [
        "cal-evtRT-0", "cal-evtRT-1", "cal-evtRT-2"]


def test_a_single_chunk_calendar_event_keeps_its_doc_id_as_its_identity(tmp_path):
    """An unsplit event's doc_id IS `cal-<eid>` (sync/calendar.py), so the
    prefixed key coincides with it — the pre-I6 behaviour, and the doc_id
    fallback arm resolves it even without the event_id arm."""
    store = _store(tmp_path)
    store.upsert_chunk("cal-solo", "short agenda", "hs",
                       {"source_type": "calendar", "event_id": "solo",
                        "chunk_index": 0, "chunk_total": 1})

    messages = thread_enrich.reassemble_thread(list(store.unenriched_chunks()))

    assert messages[0]["message_id"] == "cal-solo"
    assert store.doc_ids_for_messages(["cal-solo"]) == ["cal-solo"]
