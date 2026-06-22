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


def _group_key(chunk: dict) -> str:
    """Grouping key for a chunk: thread_id, else message_id, else doc_id.

    A chunk with no thread_id is a standalone message (or an un-threaded doc),
    so it forms its own singleton group keyed on message_id; with neither, the
    doc_id is the last-resort unique key.
    """
    meta = chunk.get("metadata") or {}
    return meta.get("thread_id") or meta.get("message_id") or chunk["doc_id"]


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


def reassemble_thread(chunks: list[dict]) -> list[dict]:
    """Reassemble a thread's chunks into ordered message dicts.

    Chunks are grouped by a stable key:
    - Drive docs (chunks with ``file_id`` in metadata): grouped by ``file_id``
      so all chunks of the same document join into one body instead of
      appearing as N one-line stubs.
    - Email messages: grouped by ``message_id``.
    - Fallback: ``doc_id`` for chunks with neither.

    Within each group, body chunks are sorted by chunk_index and joined with
    the chunking separator. One message dict is emitted per group, ordered by
    date (string sort). Provenance fields are read from any chunk of the group
    (they share base_metadata).

    Splitting an over-long thread is prepare's job, not this function's; this
    always returns the full ordered message list.
    """
    by_message: dict[str, list[dict]] = {}
    order: list[str] = []
    for chunk in chunks:
        meta = chunk.get("metadata") or {}
        # Drive documents carry a file_id but no message_id. Group all chunks
        # of the same document under the file_id so a multi-chunk doc assembles
        # into one "message" instead of N one-line stubs.
        file_id = meta.get("file_id")
        if file_id:
            mid = file_id
        else:
            mid = meta.get("message_id") or chunk["doc_id"]
        if mid not in by_message:
            by_message[mid] = []
            order.append(mid)
        by_message[mid].append(chunk)

    messages = []
    for mid in order:
        parts = sorted(by_message[mid],
                       key=lambda c: (c.get("metadata") or {}).get("chunk_index", 0))
        meta = parts[0].get("metadata") or {}
        text = _CHUNK_JOIN.join(p.get("text", "") for p in parts)
        messages.append({
            "message_id": mid,
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
        })

    messages.sort(key=lambda m: m.get("date", ""))
    return messages
