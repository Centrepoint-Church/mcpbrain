# mcpbrain/retrieval.py
import email.utils
import json
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


# Default RRF constant and per-ranker fusion weights. Tunable via the eval
# harness (see tests/eval/run_eval.py). Equal weights = the historical
# behaviour; vec_weight/kw_weight scale each ranker's contribution before sum.
_RRF_K = 60
_VEC_WEIGHT = 1.0
_KW_WEIGHT = 1.0


def _rrf(rankings: list[list[str]], k: int = _RRF_K,
         vec_weight: float = _VEC_WEIGHT,
         kw_weight: float = _KW_WEIGHT) -> dict[str, float]:
    """Weighted Reciprocal Rank Fusion.

    rankings is [semantic_ranking, keyword_ranking] (the order hybrid_search
    passes). The two weights scale each ranker's reciprocal-rank contribution
    so the fusion can be tuned without changing call sites. A missing third+
    ranking falls back to weight 1.0.
    """
    weights = [vec_weight, kw_weight]
    scores: dict[str, float] = {}
    for idx, ranking in enumerate(rankings):
        w = weights[idx] if idx < len(weights) else 1.0
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + w / (k + rank + 1)
    return scores


def _three_axis_boost(chunk: dict, *,
                      recency_weight: float = 0.0,
                      importance_weight: float = 0.0,
                      decay_weight: float = 0.0,
                      recency_alpha: float = 0.01) -> float:
    """Additive boost from recency + importance + decay for the three-axis ranker.

    recency_weight and importance_weight default 0.0 so existing callers that
    don't pass them get identical scores (safe no-op). Both must be set > 0
    (via config flags) for the axes to affect ranking.

    The boost is ADDITIVE to the normalised RRF score (0–1) so it can push a
    highly-important recent hit above a relevance-only top hit, but cannot
    swamp the relevance signal at default weights.
    """
    boost = 0.0

    if recency_weight > 0.0 or decay_weight > 0.0:
        from mcpbrain.importance import recency_decay as _rd
        meta = chunk.get("metadata") or {}
        rd = _rd(meta, alpha=recency_alpha)
        boost += recency_weight * rd
        # decay_weight uses the same recency curve when no decay factor is supplied
        # by the caller; the caller can override by passing pre-computed decay via
        # chunk["_decay_factor"] (set by decay.update_on_recall path).
        df = chunk.get("_decay_factor")
        if df is not None:
            boost += decay_weight * float(df)
        elif decay_weight > 0.0:
            boost += decay_weight * rd

    if importance_weight > 0.0:
        # salience is stored on the chunk dict by _enrich_with_salience below;
        # fall back to the structural scorer when absent.
        salience = chunk.get("salience")
        if salience is None:
            from mcpbrain.importance import score_structural as _ss
            salience = _ss(chunk.get("metadata") or {})
        boost += importance_weight * (float(salience) / 10.0)

    return boost


def hybrid_search(store, embedder, query: str, limit: int = 10, *,
                  rrf_k: int = _RRF_K, vec_weight: float = _VEC_WEIGHT,
                  kw_weight: float = _KW_WEIGHT, query_vec: list | None = None,
                  recency_weight: float = 0.0, importance_weight: float = 0.0,
                  decay_weight: float = 0.0, recency_alpha: float = 0.01,
                  exclude_cold: bool = False) -> list[dict]:
    """Hybrid RRF search with optional three-axis reranking.

    New keyword-only params (all default to off so existing callers are unaffected):
      recency_weight  — additive recency boost weight (B3)
      importance_weight — additive importance/salience boost weight (B3)
      decay_weight    — additive decay-factor boost weight (B5)
      recency_alpha   — exp decay rate for the recency term (0.01 → ~69d half-life)
      exclude_cold    — when True, skip memory_tier='cold' chunks (B2)

    query_vec lets a caller that already embedded the query (e.g. the recall
    distance gate in daemon.search) reuse it, avoiding a second embed_query —
    the slow part of a search. Identical results either way.
    """
    qv = query_vec if query_vec is not None else embedder.embed_query(query)
    sem = [d for d, _ in store.vec_knn(qv, limit * 2)]
    kw = [d for d, _ in store.fts_search(query, limit * 2)]
    fused = _rrf([sem, kw], k=rrf_k, vec_weight=vec_weight, kw_weight=kw_weight)
    ordered = sorted(fused, key=lambda d: -fused[d])
    # `score` is an INTRA-QUERY confidence: each fused score divided by this
    # query's top fused score, so the strongest hit is 1.0 and weaker hits trail
    # below it. It is NOT comparable across queries (every query's best hit is
    # 1.0 regardless of absolute match quality) and, because RRF contributions
    # are ~1/(k+rank), hits present in both rankers cluster near 1.0 while
    # single-ranker hits sit lower — treat it as "rank confidence within this
    # result set", not an absolute relevance scale. Computed over the FULL fused
    # set (before expiry filtering) so dropping an expired top hit does not
    # silently rescale the survivors.
    top = fused[ordered[0]] if ordered else 0.0
    use_three_axis = (recency_weight > 0.0 or importance_weight > 0.0
                      or decay_weight > 0.0)

    candidates = []
    for d in ordered:
        c = store.get_chunk(d)
        if not c:
            continue
        meta = c.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if meta.get("expired"):
            continue
        if exclude_cold and c.get("memory_tier") == "cold":
            continue
        rrf_score = (fused[d] / top) if top > 0 else 0.0
        c["score"] = rrf_score
        # Attach salience so _three_axis_boost can read it without a second DB call.
        c["salience"] = store.get_chunk_salience(d) if use_three_axis else 0.0
        candidates.append(c)

    if use_three_axis and candidates:
        for c in candidates:
            boost = _three_axis_boost(
                c,
                recency_weight=recency_weight,
                importance_weight=importance_weight,
                decay_weight=decay_weight,
                recency_alpha=recency_alpha,
            )
            c["score"] = c["score"] + boost
        # Re-sort by the boosted score.
        candidates.sort(key=lambda x: -x["score"])
        # Re-normalise so the top hit is still ~1.0.
        new_top = candidates[0]["score"] if candidates else 1.0
        if new_top > 0:
            for c in candidates:
                c["score"] = round(c["score"] / new_top, 4)

    results = []
    for c in candidates:
        results.append(c)
        if len(results) == limit:
            break
    return results
