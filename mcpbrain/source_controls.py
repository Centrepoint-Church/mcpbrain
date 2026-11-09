"""The console's skipped and waiting chunk lists (spec 7, "Pipeline stages").

Three read-only lists the owner can act on from the Overview: chunks kept out
of the graph (cold) with the rule that did it, chunks waiting for enrichment
(in queue order), and chunks whose reflow gave up. Acting on them is
Store.reinclude_chunks / Store.prioritise_chunks.

Each item is {doc_id, title, source, date, reason}; each list is
{"items", "total", "page"}.
"""
from __future__ import annotations

from mcpbrain import source_class as sc

PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# Gmail category labels prepare.should_enrich keeps out, named for the owner.
_CATEGORY_REASONS = (("CATEGORY_PROMOTIONS", "promotions"),
                     ("CATEGORY_SOCIAL", "social"),
                     ("CATEGORY_FORUMS", "forums"))


def _labels(meta: dict) -> set:
    raw = meta.get("labels") or ""
    if isinstance(raw, (list, tuple)):
        return {str(x).strip().upper() for x in raw if str(x).strip()}
    return {x.strip().upper() for x in str(raw).split(",") if x.strip()}


def cold_reason(meta, enrich_state, *, rules=None, doc_id: str = "") -> str:
    """The rule that kept a cold chunk out of the graph, in the owner's words.

    Returns "" for a chunk that is not cold. Checks follow the gates' own
    order (prepare: derived gate, then salience gate), so the reason named is
    the one that actually applied: superseded draft, consolidated note, session
    note, summary, draft, own automated mail, own automated document, Drive
    folder, table, transcript, bulk, promotions / social / forums, spreadsheet,
    short document; "other" when none of those explains it (a clean-up pass or
    a rule since changed). `rules` (source_class.Rules) carries the owner's
    subject prefixes and Drive folders; without it those two never match."""
    if (enrich_state or "") != "cold":
        return ""
    from mcpbrain.prepare import _COLD_DRIVE_MIMES
    meta = meta if isinstance(meta, dict) else {}
    if meta.get("superseded"):
        return "superseded draft"
    cls = sc.classify(doc_id or "", meta, rules if rules is not None else sc.Rules())
    if cls is sc.SourceClass.CONSOLIDATED:
        return "consolidated note"
    if cls is sc.SourceClass.SESSION:
        return "session note"
    if cls is sc.SourceClass.DIGEST:
        return "summary"
    if cls is sc.SourceClass.DRAFT:
        return "draft"
    if cls is sc.SourceClass.BRIEF:
        if meta.get("source_type") == "gdrive":
            if str(meta.get("app_generated")) == "1":
                return "own automated document"
            return "Drive folder"
        return "own automated mail"
    subtype = str(meta.get("content_subtype") or "").lower()
    if subtype in ("table", "transcript"):
        return subtype
    if meta.get("source_type") == "gmail" or meta.get("thread_id"):
        if meta.get("bulk"):
            return "bulk"
        labels = _labels(meta)
        for label, reason in _CATEGORY_REASONS:
            if label in labels:
                return reason
    if meta.get("source_type") == "gdrive" or meta.get("file_id"):
        if str(meta.get("mime_type") or "").lower() in _COLD_DRIVE_MIMES:
            return "spreadsheet"
        return "short document"
    return "other"


def _waiting_reason(meta: dict, priority: int) -> str:
    if priority > 0:
        return "prioritised"
    if meta.get("force_enrich") is True:
        return "re-included"
    return "queued"


def _source(doc_id: str, meta: dict) -> str:
    st = str(meta.get("source_type") or "")
    if st == "gdrive":
        return "drive"
    if st in ("gmail", "calendar", "anarlog"):
        return st
    if doc_id.startswith("cal-") or meta.get("event_id"):
        return "calendar"
    if meta.get("source") == "note" or doc_id.startswith("note-"):
        return "note"
    return st or "other"


def _first(meta: dict, *keys) -> str:
    for k in keys:
        v = meta.get(k)
        if v:
            return str(v)
    return ""


def _item(row: dict, reason: str) -> dict:
    meta = row["metadata"]
    return {"doc_id": row["doc_id"],
            "title": _first(meta, "subject", "file_name", "title", "meeting_title",
                            "summary"),
            "source": _source(row["doc_id"], meta),
            "date": _first(meta, "date", "modified", "start", "started_at",
                           "captured_at"),
            "reason": reason}


def _page(store, kind: str, page: int, page_size: int, reason_of) -> dict:
    page = max(1, int(page))
    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    rows, total = store.chunk_list_page(kind, offset=(page - 1) * page_size,
                                        limit=page_size)
    return {"items": [_item(r, reason_of(r)) for r in rows], "total": total, "page": page}


def _rules(store, home) -> sc.Rules:
    return sc.Rules.from_config(
        str(home), generated_label_ids=sc.cached_generated_label_ids(store))


def list_cold(store, home, *, page: int = 1, page_size: int = PAGE_SIZE) -> dict:
    """Chunks kept out of the graph, newest first, each with its rule."""
    rules = _rules(store, home)
    return _page(store, "cold", page, page_size,
                 lambda r: cold_reason(r["metadata"], r["enrich_state"],
                                       rules=rules, doc_id=r["doc_id"]))


def list_waiting(store, home, *, page: int = 1, page_size: int = PAGE_SIZE) -> dict:
    """Chunks waiting for enrichment, in the order the queue takes them."""
    return _page(store, "waiting", page, page_size,
                 lambda r: _waiting_reason(r["metadata"], r["priority"]))


def list_reflow_gave_up(store, home, *, page: int = 1,
                        page_size: int = PAGE_SIZE) -> dict:
    """Chunks of owners whose reflow gave up after repeated failures."""
    return _page(store, "reflow_gave_up", page, page_size,
                 lambda r: "reflow gave up")
