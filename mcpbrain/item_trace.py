"""Where an item is in the pipeline, and why (console: "Not in yet", "Find an item").

Before this, the only way to learn why an email or file was not in the graph
was to read the code and query the store: the sync queue showed up as one
doctor sentence, and nothing joined an item's stages together. Two read-only
views, both answering in codes the console words (dashboard.html word maps):

- `sync_queue`: items found in a source but not yet written to the store, with
  a state per row: `due` (next in line), `waiting` (held for a reason that is
  not the item's fault, e.g. an enrichment batch is working on it; no attempt
  counted) or `retrying` (it failed, and backs off).
- `trace`: find items by id or by words and follow each through every stage:
  still in the sync queue, its pieces (chunks) in the graph / kept out (with the
  rule) / waiting for enrichment, in an enrichment batch right now, re-chunking
  gave up.

An "item" is what the owner thinks of as one thing: a Drive file, an email
thread, a calendar event, a meeting, a note.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mcpbrain import source_controls

PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_ITEMS = 8
_QUERY_MAX = 200

# The metadata key that names an item, most specific first. A Gmail chunk has
# both thread_id and message_id; the thread is the item.
_OWNER_KEYS = ("file_id", "session_id", "event_id", "thread_id", "note_id")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def queue_state(row: dict, now: str) -> str:
    """`retrying` once an attempt failed; else `waiting` while held to a later
    time; else `due`. (A held row counts no attempt: Store.defer_sync_item.)"""
    if int(row.get("attempts") or 0) > 0:
        return "retrying"
    nxt = row.get("next_attempt_at")
    if nxt and str(nxt) > now:
        return "waiting"
    return "due"


def _queue_kind(source: str) -> str:
    """drive / gmail / calendar / anarlog; `reflow` for a re-chunking row."""
    head = (source or "").split(":", 1)[0]
    return head or "other"


def _queue_item(row: dict, now: str, title: str = "") -> dict:
    return {"source": _queue_kind(row["source"]), "ref_id": row["ref_id"],
            "title": title, "event": row.get("event") or "",
            "state": queue_state(row, now), "attempts": int(row.get("attempts") or 0),
            "next_at": row.get("next_attempt_at") or "",
            "error": row.get("last_error") or "",
            "discovered_at": row.get("discovered_at") or ""}


def _title(meta: dict) -> str:
    return source_controls._first(meta, "subject", "file_name", "title",
                                  "meeting_title", "summary")


def _known_title(store, ref_id: str) -> str:
    """The title of an item already in the store under this id, if any."""
    for key in ("file_id", "message_id", "event_id", "session_id"):
        rows = store.chunks_by_meta_key(key, ref_id, limit=1)
        if rows:
            return _title(rows[0]["metadata"])
    return ""


def sync_queue(store, home=None, *, page: int = 1, page_size: int = PAGE_SIZE) -> dict:
    """Items found in a source but not yet in the store, oldest first."""
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    rows = store.sync_queue_rows(limit=100_000)
    now = _now_iso()
    window = rows[(page - 1) * page_size: page * page_size]
    items = [_queue_item(r, now, _known_title(store, r["ref_id"])) for r in window]
    counts = {"due": 0, "waiting": 0, "retrying": 0}
    for r in rows:
        counts[queue_state(r, now)] += 1
    return {"items": items, "total": len(rows), "page": page, "counts": counts}


# ---- trace ------------------------------------------------------------------

def _is_digest(doc_id: str) -> bool:
    """An enrichment summary (`enriched-<cluster>`): synthetic, not an item.
    It carries its source's id under thread_id, so it must never name one."""
    return doc_id.startswith("enriched-")


def _owner(row: dict) -> tuple[str, str]:
    meta = row["metadata"]
    for key in _OWNER_KEYS:
        v = meta.get(key)
        if v:
            return key, str(v)
    return "doc_id", row["doc_id"]


def _owner_rows(store, key: str, value: str) -> list[dict]:
    if key == "doc_id":
        return store.chunk_rows([value])
    return store.chunks_by_meta_key(key, value)


def _looks_like_id(q: str) -> bool:
    """One token: tried as an exact id first (a word that is no id falls
    through to keyword search)."""
    return bool(q) and not any(c.isspace() for c in q)


def _find_owners(store, q: str) -> list[tuple[str, str]]:
    """Items matching `q`: an exact id first (any indexed key, or a chunk id);
    otherwise keyword search, best match first."""
    owners: list[tuple[str, str]] = []

    def add(o):
        if o not in owners:
            owners.append(o)

    if _looks_like_id(q):
        for key in store.TRACE_KEYS:
            rows = [r for r in store.chunks_by_meta_key(key, q, limit=50)
                    if not _is_digest(r["doc_id"])]
            if rows:
                add(_owner(rows[0]))
        c = None if _is_digest(q) else store.get_chunk(q)
        if c:
            add(_owner({"doc_id": q, "metadata": c["metadata"]}))
        if owners:
            return owners[:MAX_ITEMS]
    hits = [doc_id for doc_id, _ in store.fts_search(q, 80) if not _is_digest(doc_id)]
    chunks = store.get_chunks(hits)
    for doc_id in hits:
        c = chunks.get(doc_id)
        if c:
            add(_owner({"doc_id": doc_id, "metadata": c["metadata"]}))
        if len(owners) >= MAX_ITEMS:
            break
    return owners


def _trace_one(store, key: str, value: str, rules, refs: set, now: str) -> dict | None:
    rows = _owner_rows(store, key, value)
    if not rows:
        return None
    meta = rows[0]["metadata"]
    pieces = [r for r in rows if not _is_digest(r["doc_id"])]
    cold = [r for r in pieces if r["enrich_state"] == "cold"]
    reasons: list[str] = []
    for r in cold:
        why = source_controls.cold_reason(r["metadata"], r["enrich_state"],
                                          rules=rules, doc_id=r["doc_id"])
        if why and why not in reasons:
            reasons.append(why)
    waiting = [r for r in pieces if not r["enriched"] and r["enrich_state"] != "cold"]
    in_graph = [r for r in pieces if r["enriched"]]
    gave_up = [r for r in pieces if r["metadata"].get("reflow_skipped") == "gave_up"]
    ids = {r["doc_id"] for r in rows} | {value}
    ids |= {str(r["metadata"].get("message_id")) for r in rows if r["metadata"].get("message_id")}
    in_batch = bool(refs & ids)
    queue_refs = {value} | {str(r["metadata"].get("message_id"))
                            for r in rows if r["metadata"].get("message_id")}
    queue = [_queue_item(q, now) for q in store.sync_queue_rows(ref_ids=sorted(queue_refs))]
    return {"key": key, "id": value, "title": _title(meta),
            "source": source_controls._source(rows[0]["doc_id"], meta),
            "date": source_controls._first(meta, "date", "modified", "start",
                                           "started_at", "captured_at"),
            "pieces": len(pieces), "in_graph": len(in_graph), "kept_out": len(cold),
            "kept_out_reasons": reasons, "waiting": len(waiting),
            "gave_up": len(gave_up), "in_batch": in_batch, "queue": queue}


def trace(store, home, q: str) -> dict:
    """Up to MAX_ITEMS items matching `q`, each followed through the pipeline.
    An id found only in the sync queue (not written yet) is returned as a
    queue-only item."""
    q = (q or "").strip()[:_QUERY_MAX]
    if not q:
        return {"query": "", "items": []}
    from mcpbrain import reflow
    rules = source_controls.rules_for(store, home)
    try:
        refs = set(reflow.pending_unit_refs(home))
    except Exception:  # noqa: BLE001 — an unreadable queue dir means "not in a batch"
        refs = set()
    now = _now_iso()
    items = [t for k, v in _find_owners(store, q)
             if (t := _trace_one(store, k, v, rules, refs, now)) is not None]
    if not items and _looks_like_id(q):
        queued = store.sync_queue_rows(ref_ids=[q])
        if queued:
            items = [{"key": "ref_id", "id": q, "title": "", "source":
                      _queue_kind(queued[0]["source"]), "date": "", "pieces": 0,
                      "in_graph": 0, "kept_out": 0, "kept_out_reasons": [],
                      "waiting": 0, "gave_up": 0, "in_batch": q in refs,
                      "queue": [_queue_item(r, now) for r in queued]}]
    return {"query": q, "items": items}
