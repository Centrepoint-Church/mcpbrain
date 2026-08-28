"""Thread grouping + reassembly primitives for the enrichment pipeline.

Phase 1, Task 9.1. Two pure functions over the store:

  group_unenriched_threads(store, *, thread_cap)
      Pull the un-enriched chunk backlog and group it into per-thread batches,
      capped at thread_cap distinct threads (first-appearance order).

  reassemble_thread(chunks)
      Turn one thread's raw chunks back into ordered message dicts.

prepare.py consumes both through its _group_unenriched_threads /
_reassemble_thread seams. The interface is locked there: batches expose
.thread_id / .doc_ids / .chunks; messages carry the per-message provenance
fields (message_id, sender, date, labels, subject) plus the body text.

The over-long-thread split is NOT done here — that is prepare's responsibility
(prepare._split_long_thread, spec Integration seam 1). reassemble_thread always
returns every message in date order; prepare decides how to chunk them.
"""

import re
from dataclasses import dataclass, field


# Chunking split bodies on blank lines (chunking.chunk_text splits and rejoins
# on "\n\n"), so reassembly rejoins a message's body chunks with the same
# separator.
_CHUNK_JOIN = "\n\n"


@dataclass
class ThreadBatch:
    """One thread's worth of un-enriched chunks.

    thread_id : grouping key (real threadId, or a message_id / doc_id fallback
                when the chunk metadata carries no thread_id).
    doc_ids   : every chunk doc_id in the thread — passed to store.mark_enriched.
    chunks    : the raw chunk dicts ({rowid, doc_id, text, metadata}) — passed
                to reassemble_thread.
    """

    thread_id: str
    doc_ids: list[str] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)


# One email attachment's chunks: doc_id gmail-<message_id>-att-<n>-<chunk_index>.
# The attachment index is read off the doc_id rather than metadata so this works
# for chunks written before attachments carried any extra field.
_ATTACHMENT_INDEX = re.compile(r"-att-(\d+)-\d+$")


def _chunk_key(meta: dict, doc_id: str) -> str:
    """Message-identity key shared by _group_key and reassemble_thread: file_id,
    else ``cal-<event_id>``, else message_id, else doc_id.

    This is the value reassemble_thread emits as a message's `message_id`, so
    every branch must be resolvable by store.doc_ids_for_messages — drain
    recovers the chunks an extraction covers through it, and a key that resolves
    to nothing means the extraction is discarded and its chunks re-queue (the
    0.7.98 Drive defect). The event_id branch is I6, and store._doc_ids_query has
    a matching arm for it, exactly as file_id does.

    The calendar branch emits the PREFIXED `cal-<event_id>`, not the bare id,
    because that is the identity namespace every existing calendar row in the
    store already uses (sync/calendar.py writes doc_id `cal-<eid>` /
    `cal-<eid>-<i>` and evidence/source_doc_id `cal-<eid>`; a single-chunk event's
    key is therefore literally its own doc_id, exactly as before I6). Emitting
    the bare id instead would fork a second namespace for the same event and
    silently break four things: store.meeting_series_for_old filters candidate
    series on `email_entities.message_id LIKE 'cal-%'`; semantic.build_semantic_doc
    labels a calendar digest `calendar_enriched_v2` only when the thread_id starts
    `cal-`; and graph_write keys the synthesised digest chunk `enriched-<thread_id>`
    and stores actions with that thread_id, so a re-extraction (routine — any
    content edit resets enriched=0 and calendar re-syncs on a rolling window)
    would write a duplicate digest/context/actions set under the bare-id
    namespace beside the `cal-`-prefixed original, with nothing cleaning it up
    and its actions no longer closeable. Prefixing solves I6's real problem — the
    `cal-<eid>-0..N` chunks of one split event grouping together instead of
    fragmenting into singletons — with no namespace change at all.

    This is the portion of the precedence chain that genuinely means the same
    thing at both call sites — "which message/document does this chunk belong
    to" — so it lives in one place rather than as separately-maintained
    fallback chains that could silently drift apart (reassemble_thread used to
    check file_id first with its own copy of this exact chain).

    thread_id is deliberately NOT part of this shared helper: _group_key checks
    it itself, one level up, because it answers a different question ("which
    THREAD does this chunk belong to", for grouping the whole unenriched
    backlog into per-thread batches). reassemble_thread runs WITHIN an
    already-thread-grouped batch, where thread_id is constant across every
    message in a multi-message thread — folding it into this shared key would
    wrongly collapse those distinct messages into one.

    The semantic digest chunk (doc_id "enriched-<thread_id>") is special-cased
    to its own doc_id regardless of what else is in its metadata. Since C3 it
    carries `message_id` set to the real lead message's id (for provenance —
    "which message did this fact come from"), which is the SAME id the lead
    message's own raw chunk carries. Falling through the message_id branch for
    both would resolve them to one identity key, merging the synthesized
    People:/Actions:/Topics: digest into the raw email body reassemble_thread
    hands back to the model for re-extraction on the stale-reextract path
    (mark_thread_unenriched resets both chunks to enriched=0 together). The
    digest chunk must stay its own singleton group; the field is written for
    provenance reads, not for grouping.
    """
    if doc_id.startswith("enriched-"):
        return doc_id
    if meta.get("file_id"):
        return meta["file_id"]
    if meta.get("event_id"):
        return f"cal-{meta['event_id']}"
    return meta.get("message_id") or doc_id


def _reassembly_key(meta: dict, doc_id: str) -> str:
    """Grouping key for reassemble_thread: _chunk_key, except that an email
    ATTACHMENT gets one group PER ATTACHMENT (C2).

    Attachment chunks deliberately carry their parent message's `message_id`
    (sync/attachments.py: it is what joins them to their thread for enrichment,
    expansion and doc_ids_for_messages), so _chunk_key resolves an attachment
    and its parent's body to the SAME key. reassemble_thread then merged them
    into one "message" and interleaved the two by chunk_index — body 0,
    attachment 0, body 1, attachment 1 — handing the extractor a garbled
    document with spurious `[…]` gap markers. Every email with an attachment hit
    this.

    Grouping is therefore finer than message identity here, while the EMITTED
    message_id stays _chunk_key (the parent message id): the attachment really
    is part of that message, and it is the id that resolves back to these chunks
    in store.doc_ids_for_messages. Two message dicts sharing one message_id is
    fine downstream — drain unions their resolutions, and graph_write's
    per-message loops key on sender, not id.
    """
    key = _chunk_key(meta, doc_id)
    if meta.get("content_type") == "email_attachment":
        m = _ATTACHMENT_INDEX.search(doc_id)
        return f"{key}#att-{m.group(1)}" if m else f"{key}#{doc_id}"
    return key


def _group_key(chunk: dict) -> str:
    """Grouping key for a chunk: thread_id, else file_id, else message_id, else
    doc_id.

    A chunk with no thread_id is a standalone message (or an un-threaded doc),
    so it forms its own singleton group keyed on message_id; with neither, the
    doc_id is the last-resort unique key.

    Drive docs carry a file_id but no message_id and split across many chunks
    (doc_id gdrive-<file_id>-<idx>). Keying them on file_id groups the whole
    document into one batch — matching reassemble_thread, which also groups
    Drive chunks by file_id — so the batch's message_id (=file_id, emitted by
    reassemble_thread) resolves back to exactly this batch's chunks via
    store.doc_ids_for_messages. Keying on doc_id instead made each chunk its own
    batch whose message_id (file_id) matched no chunk, so drain never applied it.
    """
    meta = chunk.get("metadata") or {}
    return meta.get("thread_id") or _chunk_key(meta, chunk["doc_id"])


def group_unenriched_threads(store, *, thread_cap: int) -> list[ThreadBatch]:
    """Group the un-enriched chunk backlog into per-thread batches.

    Grouping is over store.unenriched_chunks() so only the backlog is touched
    (not the whole corpus). Threads are kept in first-appearance order — which
    follows the rowid order unenriched_chunks returns — and the distinct-thread
    count is capped at thread_cap. The cap counts THREADS, not chunks: a thread
    already admitted keeps accumulating chunks even once the cap is reached.
    """
    batches: dict[str, ThreadBatch] = {}
    for chunk in store.unenriched_chunks():
        key = _group_key(chunk)
        batch = batches.get(key)
        if batch is None:
            if len(batches) >= thread_cap:
                continue  # cap reached; drop chunks for not-yet-seen threads
            batch = ThreadBatch(thread_id=key)
            batches[key] = batch
        batch.doc_ids.append(chunk["doc_id"])
        batch.chunks.append(chunk)
    return list(batches.values())


_GAP_MARKER = "\n\n[…]\n\n"


def _join_with_gaps(parts: list[dict]) -> tuple[str, bool]:
    """Join one message's chunks in index order, marking any missing piece.

    Returns ``(text, had_gap)``. ``had_gap`` is True when a _GAP_MARKER was
    inserted anywhere, and it is the signal prepare._split_message_at_seams keys
    on: a gap marker is not _CHUNK_JOIN, so with one present
    ``_CHUNK_JOIN.join(chunk_pieces)`` no longer reproduces this text and a seam
    split could not be proven lossless. Without a gap that equality holds
    exactly, which is what makes reconstructing a part's body from a subset of
    the pieces safe.

    B8: this function only ever sees the chunks its CALLER selected, and
    group_unenriched_threads selects UNENRICHED chunks while
    store.unenriched_chunks additionally excludes cold-marked ones
    (store.py:1264). A partially-enriched or partially-cold document therefore
    reached the model as a seamless body with pieces silently absent — and the
    model has no way to know, so it extracts confidently from a fragment.

    Two independent signals, because either can occur alone: a hole in the
    middle (indices 0, 2 — chunk 1 already enriched) and a truncated tail
    (indices 0, 1 of a chunk_total of 5). The tail check needs chunk_total,
    which only exists on chunks written after this plan's C1 change; on older
    chunks it is absent and the check simply does not fire, which is the correct
    degradation. `parts` is already sorted by chunk_index by the caller.
    """
    out: list[str] = []
    had_gap = False
    prev = None
    for p in parts:
        idx = int((p.get("metadata") or {}).get("chunk_index", 0) or 0)
        if prev is not None:
            if idx != prev + 1:
                out.append(_GAP_MARKER)
                had_gap = True
            else:
                out.append(_CHUNK_JOIN)
        out.append(p.get("text", ""))
        prev = idx
    if parts and prev is not None:
        total = int((parts[-1].get("metadata") or {}).get("chunk_total", 0) or 0)
        if total and prev < total - 1:
            out.append(_GAP_MARKER)
            had_gap = True
    return "".join(out), had_gap


def reassemble_thread(chunks: list[dict]) -> list[dict]:
    """Reassemble a thread's chunks into ordered message dicts.

    Chunks are grouped by a stable key:
    - Drive docs (chunks with ``file_id`` in metadata): grouped by ``file_id``
      so all chunks of the same document join into one body instead of
      appearing as N one-line stubs.
    - Calendar events (chunks with ``event_id``): grouped by ``cal-<event_id>``,
      same reasoning — a long agenda split into cal-<eid>-0..N used to become N
      singleton "messages", each one wrongly carrying a truncated-tail `[…]`
      marker (I6). The key keeps the `cal-` prefix so the emitted id stays in the
      namespace every existing calendar row uses — see _chunk_key.
    - Email messages: grouped by ``message_id``.
    - Email attachments: one group each, keyed finer than message identity —
      see _reassembly_key (C2).
    - Fallback: ``doc_id`` for chunks with neither.

    Within each group, body chunks are sorted by chunk_index and joined with
    the chunking separator. One message dict is emitted per group, ordered by
    date (string sort). Provenance fields are read from any chunk of the group
    (they share base_metadata).

    Splitting an over-long thread is prepare's job, not this function's; this
    always returns the full ordered message list.
    """
    by_group: dict[str, list[dict]] = {}
    order: list[str] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        # Drive documents carry a file_id but no message_id, and calendar events
        # an event_id. Group all chunks of the same document/event under it so a
        # multi-chunk doc assembles into one "message" instead of N one-line
        # stubs. Shared with _group_key via _chunk_key so the two cannot drift
        # on this precedence; _reassembly_key adds the attachment split, which
        # is reassembly-only.
        key = _reassembly_key(meta, chunk["doc_id"])
        if key not in by_group:
            by_group[key] = []
            order.append(key)
        by_group[key].append(chunk)

    messages = []
    for key in order:
        parts = sorted(by_group[key],
                       key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
        meta = parts[0].get("metadata") or {}
        text, chunk_has_gap = _join_with_gaps(parts)
        # Chunk-level provenance, ordered exactly as _join_with_gaps consumed
        # the pieces. prepare._split_long_thread splits an over-long message at
        # these seams and carries the covered ids as part_doc_ids, so drain can
        # mark exactly the chunks a part covered instead of the whole document.
        chunk_doc_ids = [p["doc_id"] for p in parts]
        # The REAL per-chunk text, parallel and same-length to chunk_doc_ids —
        # never re-derived by splitting `text` on _CHUNK_JOIN. chunking.chunk_text
        # PACKS several paragraphs into one chunk whenever they fit the budget
        # together, so a chunk's own stored text routinely contains internal
        # "\n\n"; re-splitting the join yields more pieces than there are chunks
        # (60 paragraphs / 8 chunks on a real document) and prepare's
        # length-mismatch guard then ships every ordinary document unsplit.
        # With chunk_has_gap False, _CHUNK_JOIN.join(chunk_pieces) == text
        # exactly, which is what makes a seam split provably lossless.
        chunk_pieces = [p.get("text", "") for p in parts]
        messages.append({
            # The GROUP key can be finer than message identity (attachments);
            # the emitted id must stay the resolvable one — see _reassembly_key.
            "message_id": _chunk_key(meta, parts[0]["doc_id"]),
            # Drive chunks store the file owner in "owner"; email chunks use
            # "sender". Fall through both so the assembled message always has
            # the best available attribution.
            "sender": meta.get("sender") or meta.get("owner", ""),
            # Four date sources: gmail → "date", calendar → "start",
            # drive → "modified", fallback → "".
            "date": (
                meta.get("date") or meta.get("start") or meta.get("modified") or ""
            ),
            "labels": meta.get("labels", ""),
            # Drive docs use "file_name" as the subject equivalent.
            "subject": meta.get("subject") or meta.get("file_name", ""),
            "text": text,
            "chunk_doc_ids": chunk_doc_ids,
            "chunk_pieces": chunk_pieces,
            "chunk_has_gap": chunk_has_gap,
        })

    messages.sort(key=lambda m: m.get("date", ""))
    return messages
