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


def _dedupe_by_content(hits: list[dict]) -> list[dict]:
    """Keep one hit per distinct `content_hash`, best-ranked first.

    54% of the live store is redundant copies. The content-free purge removes
    68,193 of the 106,357, but ~38,164 remain and are genuine duplicate FILES —
    the fixed asset register exists three times in Drive (two identical names
    plus a '(1)' copy), each chunked independently. Three identical hits
    consuming three of ten slots is a real recall loss.

    Deleting the duplicate chunks instead would be wrong: doc_ids are positional
    (gdrive-<file_id>-<i>) and cited as graph provenance, so removing one file's
    copy makes THAT file unfindable by name, folder or file_id and orphans its
    rows. Crowding is a ranking problem, so it is fixed in the ranker —
    reversibly, with nothing lost.

    Hits are assumed already ordered best-first; a hit with no content_hash
    passes through untouched (not every producer sets it, and collapsing on a
    missing key would silently drop rows).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for hit in hits:
        h = hit.get("content_hash")
        if h:
            if h in seen:
                continue
            seen.add(h)
        out.append(hit)
    return out


def _doc_root(doc_id: str) -> str:
    """Positional root of a doc_id with its trailing '-<n>' chunk index
    stripped (gdrive-<fid>-3 -> gdrive-<fid>). Mirrors the same convention
    tests/eval/run_eval.py's `_doc_key` uses to group a chunk back to its
    document for document-level recall scoring. A doc_id with no recognisable
    '-<n>' suffix is its own singleton root."""
    m = re.match(r"^(.*)-\d+$", doc_id)
    return m.group(1) if m else doc_id


def _cluster_key(chunk: dict) -> str | None:
    """Identity of the information-cluster a chunk belongs to: the whole
    thread/file/calendar-event it's part of. Shared by a thread's raw message
    chunks AND its own digest (`enriched-<thread_id>`) chunk, since both are
    stamped with the same `thread_id`/`file_id`/`event_id` metadata.

    Normalises a legacy inconsistency found on the live store: one old digest
    had `thread_id` stamped as a raw chunk's full doc_id shape
    (`gdrive-<file_id>-<n>`) instead of the bare file_id current writers use —
    stripping the same 'gdrive-' prefix and trailing '-<n>' suffix as
    `_doc_root` makes it collapse to the same key a plain file_id would.
    """
    meta = chunk.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    key = meta.get("thread_id") or meta.get("file_id")
    if not key:
        event_id = meta.get("event_id")
        if event_id:
            key = f"cal-{event_id}"
    if not key:
        return None
    key = re.sub(r"^gdrive-", "", key)
    key = re.sub(r"-\d+$", "", key)
    return key


def _dedupe_by_cluster(hits: list[dict]) -> list[dict]:
    """Drop a digest (`enriched-<cluster>`) when its own cluster's raw chunk
    is also in the pool.

    A digest is a synthesized summary of its own thread/file's raw chunks —
    pure derived output, never independent information. When both are in the
    results it is redundant, and because a digest is synthetic text it can
    never itself match a query's actual expected source document — so
    letting it occupy a slot ahead of its own raw sibling only pushes the
    real answer down a rank for no benefit (confirmed on the gold set: a
    digest outranking its own thread's raw chunk by one position turned a
    correct #1 hit into a #2 miss).

    Kept unconditionally when no raw sibling of its cluster is in THIS pool
    (filtered out, beyond the retrieval boundary, etc.) — dropping it then
    would be content loss, not duplicate removal. Two raw chunks of the same
    cluster are never collapsed against each other; only a digest-vs-raw
    pair is redundant.
    """
    raw_clusters: set[str] = set()
    for hit in hits:
        if hit.get("doc_id", "").startswith("enriched-"):
            continue
        key = _cluster_key(hit)
        if key:
            raw_clusters.add(key)

    out: list[dict] = []
    for hit in hits:
        if hit.get("doc_id", "").startswith("enriched-"):
            key = _cluster_key(hit)
            if key and key in raw_clusters:
                continue
        out.append(hit)
    return out


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

    # Drop a digest that's redundant with its own thread/file's raw chunk
    # BEFORE the content-hash dedup below: a digest's content_hash never
    # matches its raw sibling's (it's synthesized text, not a copy), so the
    # content-hash pass can't see this redundancy on its own.
    candidates = _dedupe_by_cluster(candidates)

    # Dedup by content_hash AFTER ranking (the order above must be preserved so
    # the best-ranked copy of a duplicate survives) and BEFORE the limit
    # truncation below, so a freed slot goes to genuinely different content
    # instead of shortening the result set. Safe to do here because `candidates`
    # is still the full fused-and-filtered pool (bounded by the limit*2 vec/kw
    # retrieval above, not by `limit` itself) — truncating to `limit` first and
    # deduping after would just make the list shorter (the same class of bug
    # the 0.7.103 expansion fix and 0.7.110 open-actions fix each had to undo).
    #
    # A shared content_hash surfaces two different real-world shapes and only
    # one of them is safe to collapse: a genuinely duplicate FILE (another
    # document's WHOLE chunk-hash-set matches this one, however many chunks
    # each copy has — the shape the gold eval's asset-register case models,
    # and confirmed on the live store too: two "Copy of X" / re-export
    # duplicate pairs, every one of their chunks matching pairwise) versus two
    # DIFFERENT documents that merely share ONE boilerplate/template chunk
    # while the rest of each document's own chunks diverge. A per-chunk
    # sibling COUNT alone can't tell the two shapes apart (a genuine
    # multi-chunk duplicate file has count > 1 on every one of its own chunks
    # too — protecting on count alone leaves that FILE just as crowded as
    # before, reproducing the very problem this dedup exists to fix).
    # Comparing each candidate's WHOLE document hash SET can: root_hashes
    # below is fetched ONCE per search (batched, index-backed — see
    # doc_root_content_hashes), and union-find groups roots into a cluster
    # wherever two roots' full sets are equal and non-empty — transitively,
    # so 3+ mutual copies of the same file (the asset-register shape) merge
    # into one cluster. A root that only ever shares ONE hash with a root
    # whose FULL set differs never joins that root's cluster, so it is never
    # collapsed on that basis (protects two genuinely different documents
    # that happen to share a boilerplate/template chunk).
    roots = {_doc_root(c["doc_id"]) for c in candidates if c.get("content_hash")}
    root_hashes = store.doc_root_content_hashes(list(roots))

    parent = {r: r for r in roots}

    def _find(r: str) -> str:
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        return r

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    hash_to_roots: dict[str, list[str]] = {}
    for r in roots:
        for h in (root_hashes.get(r) or ()):
            hash_to_roots.setdefault(h, []).append(r)
    for co_roots in hash_to_roots.values():
        anchor = co_roots[0]
        for other in co_roots[1:]:
            if root_hashes.get(anchor) == root_hashes.get(other):
                _union(anchor, other)

    # Measured on the live gold set: BOTH observed collision pairs turned out
    # to be genuine duplicates (a "Copy of X" re-upload and a double-extension
    # re-export), so this isn't a "should we collapse" question but a "which
    # copy survives" one. Byte-identical content offers no relevance signal to
    # choose between the copies, so the tie-break is recency (metadata
    # date/modified — the more likely CURRENT copy of a duplicated file), with
    # the ranker's own order as the fallback when dates are unavailable or
    # tie — this recovered both live gold cases (each time, the gold-expected
    # copy was the more recently modified one).
    from mcpbrain.importance import recency_decay as _recency
    best: dict[str, tuple[float, int]] = {}
    primary_for: dict[str, str] = {}
    for i, c in enumerate(candidates):
        if not c.get("content_hash"):
            continue
        root = _doc_root(c["doc_id"])
        cluster = _find(root)
        key = (_recency(c.get("metadata") or {}), -i)
        if cluster not in best or key > best[cluster]:
            best[cluster] = key
            primary_for[cluster] = root

    # Keep one hit per (cluster, content_hash) pair PRESENT IN THIS POOL — not
    # one hit per cluster.
    #
    # Cluster membership is computed from the WHOLE database
    # (doc_root_content_hashes reads every chunk under each root), but "what the
    # surviving primary root contributes" is limited to the CANDIDATE POOL being
    # deduped here. Those two sets are not the same: `metadata.expired`
    # filtering, exclude_cold cold-marking (independent per chunk — 109,694
    # chunks are cold on the live store) and the limit*2 retrieval boundary can
    # all put a cluster-mate's chunk in the pool while the primary root's chunk
    # with that same content_hash is absent from it. Dropping by cluster
    # membership alone then makes that content vanish from the results
    # ENTIRELY, even though nothing in the pool actually duplicates it — a
    # silent content loss, not a duplicate removal.
    #
    # So a non-primary-root hit is dropped only when the primary root
    # contributes the SAME content_hash somewhere in this pool (`primary_pairs`
    # — the genuine duplicate-copy case, where the primary/recency tie-break
    # above decides which copy survives); otherwise it is kept, and it becomes
    # the pair's representative so further copies of that same hash within the
    # cluster still collapse. Hits with no content_hash pass through untouched.
    primary_pairs = {
        (_find(_doc_root(c["doc_id"])), c["content_hash"]) for c in candidates
        if c.get("content_hash")
        and primary_for.get(_find(_doc_root(c["doc_id"]))) == _doc_root(c["doc_id"])
    }
    kept_pairs: set[tuple[str, str]] = set()
    kept: list[dict] = []
    for c in candidates:
        h = c.get("content_hash")
        if not h:
            kept.append(c)
            continue
        root = _doc_root(c["doc_id"])
        pair = (_find(root), h)
        is_primary = primary_for.get(pair[0]) == root
        if not is_primary and pair in primary_pairs:
            continue        # the surviving copy contributes this exact content
        if pair in kept_pairs:
            continue        # already represented in the results
        kept_pairs.add(pair)
        kept.append(c)
    candidates = kept

    results = []
    for c in candidates:
        results.append(c)
        if len(results) == limit:
            break
    return results
