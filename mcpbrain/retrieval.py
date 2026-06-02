# mcpbrain/retrieval.py
import email.utils
import re
from datetime import timezone


# Single-word markers are matched with word boundaries (regex) to avoid
# substring false positives ("done" in "well done", "abandoned", etc).
_SINGLE_WORD_MARKERS = (
    "done",
    "resolved",
    "completed",
    "sorted",
    "handled",
)

# Multi-word / phrase markers are specific enough to match as substrings.
_PHRASE_MARKERS = (
    "taken care of",
    "sent through",
    "no longer needed",
    "all good",
    "received, thanks",
    "received with thanks",
)

_SINGLE_WORD_RE = re.compile(
    r"\b(" + "|".join(_SINGLE_WORD_MARKERS) + r")\b"
)

# Pragmatic exclusion guard for the highest-frequency false positives:
# forward-looking or negated uses of a marker that don't mean "resolved".
# Short and intentionally non-exhaustive.
_RESOLUTION_EXCLUSIONS = (
    "not done",
    "get it done",
    "get this done",
    "well done",
    "yet to be",
    "to be done",
    "isn't done",
    "still need",
)


def _text_signals_resolution(text_lower: str) -> bool:
    """True if the lowercased message text carries a genuine resolution signal.

    Single-word markers require word boundaries; phrase markers match as
    substrings. A short exclusion list suppresses common forward-looking or
    negated uses ("get it done", "well done") that aren't resolutions.
    """
    if any(excl in text_lower for excl in _RESOLUTION_EXCLUSIONS):
        return False
    if _SINGLE_WORD_RE.search(text_lower):
        return True
    return any(phrase in text_lower for phrase in _PHRASE_MARKERS)


def _parse_date(s):
    """Parse an RFC2822 date string to a UTC-aware datetime, or None on failure.
    RFC2822 '-0000' yields a naive datetime; treat it as UTC so comparisons never
    mix naive and aware datetimes."""
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def action_is_stale(store, action: dict) -> bool:
    """True if the action's thread contains a resolution signal in a message
    that is NOT the action's source message (and, when both dates parse, is
    NEWER than the source message). No thread_id -> False (can't determine).
    """
    thread_id = action.get("thread_id")
    if not thread_id:
        return False

    source_doc_id = action.get("source_doc_id")

    # Establish the anchor date from the source chunk's metadata.
    anchor_dt = None
    if source_doc_id:
        source_chunk = store.get_chunk(source_doc_id)
        if source_chunk:
            anchor_dt = _parse_date(source_chunk["metadata"].get("date", ""))

    for chunk in store.thread_chunks(thread_id):
        # Skip the source message itself.
        if chunk["doc_id"] == source_doc_id:
            continue

        text_lower = chunk["text"].lower()
        if not _text_signals_resolution(text_lower):
            continue

        # Marker found in a different message. Apply the newer-than gate when
        # both dates are parseable; if either is missing/unparseable, the
        # marker alone is sufficient to flag stale.
        chunk_dt = _parse_date(chunk["metadata"].get("date", ""))
        if chunk_dt is not None and anchor_dt is not None:
            if chunk_dt <= anchor_dt:
                continue  # resolution predates the request — ignore it

        return True

    return False


def annotate_action_freshness(store, actions: list[dict]) -> list[dict]:
    """Return copies of the actions with a 'freshness' field set to 'stale' or 'fresh'.

    Does NOT mutate the input dicts or write anything to the database.
    """
    # N+1: issues O(N) thread_chunks queries (one per action). Acceptable at
    # current scale; batch by thread_id if action lists grow large.
    return [
        {**a, "freshness": "stale" if action_is_stale(store, a) else "fresh"}
        for a in actions
    ]


def _rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return scores


def hybrid_search(store, embedder, query: str, limit: int = 10) -> list[dict]:
    qv = embedder.embed_query(query)
    sem = [d for d, _ in store.vec_knn(qv, limit * 2)]
    kw = [d for d, _ in store.fts_search(query, limit * 2)]
    fused = _rrf([sem, kw])
    top = sorted(fused, key=lambda d: -fused[d])[:limit]
    return [c for d in top if (c := store.get_chunk(d))]
