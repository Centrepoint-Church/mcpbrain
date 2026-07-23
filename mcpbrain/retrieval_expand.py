"""Read-side small-to-big expansion for recall. Pure functions over ranked hits
+ a store; called last in daemon.search (after ranking/rerank) so
expansion never blunts the reranker or triggers lost-in-the-middle."""


def parent_key(meta: dict, doc_id: str) -> tuple[str, str]:
    """(kind, key) for grouping a chunk to its parent: thread > file > chunk."""
    if meta.get("thread_id"):
        return ("thread", meta["thread_id"])
    if meta.get("file_id"):
        return ("file", meta["file_id"])
    return ("chunk", doc_id)


def group_by_parent(hits: list[dict]) -> list[dict]:
    """Group ranked hits by parent, preserving best (first-seen) rank order.
    Each hit dict carries doc_id, score, metadata."""
    groups: dict[tuple, dict] = {}
    for rank, h in enumerate(hits):
        meta = h.get("metadata") or {}
        kind, key = parent_key(meta, h["doc_id"])
        g = groups.get((kind, key))
        if g is None:
            g = {"kind": kind, "key": key, "rank": rank, "hit_indices": [],
                 "rep_doc_id": h["doc_id"], "score": h.get("score", 0.0)}
            groups[(kind, key)] = g
        idx = (meta or {}).get("chunk_index")
        if idx is not None:
            g["hit_indices"].append(int(idx))
    return sorted(groups.values(), key=lambda g: g["rank"])


_JOIN = "\n\n"
_GAP = "\n\n[…]\n\n"  # marks a break between non-adjacent span-stitch runs


def _by_date(chunks: list[dict]) -> list[dict]:
    return sorted(chunks, key=lambda c: (c.get("metadata") or {}).get("date", "") or "")


def _stitch_with_gaps(chunks: list[dict]) -> str:
    """Join chunks (already sorted by idx) with _JOIN between adjacent indices
    and a visible _GAP marker between non-adjacent runs, so a window-stitched
    large file never presents disjoint spans as if they were contiguous."""
    parts: list[str] = []
    prev_idx = None
    for c in chunks:
        if prev_idx is not None:
            parts.append(_JOIN if c["idx"] == prev_idx + 1 else _GAP)
        parts.append(c["text"])
        prev_idx = c["idx"]
    return "".join(parts)


def expand_parent(store, group: dict, *, window_n: int, short_doc_max_chunks: int) -> str:
    kind, key = group["kind"], group["key"]
    if kind == "thread":
        chunks = _by_date(store.thread_chunks(key))
        return _JOIN.join(c["text"] for c in chunks)
    if kind == "file":
        chunks = store.chunks_for_file(key)  # already sorted by idx
        if len(chunks) <= short_doc_max_chunks:
            return _JOIN.join(c["text"] for c in chunks)
        # large file: contiguous span-stitch around each hit index
        wanted: set[int] = set()
        for hi in group["hit_indices"]:
            wanted.update(range(hi - window_n, hi + window_n + 1))
        kept = [c for c in chunks if c["idx"] in wanted]
        return _stitch_with_gaps(kept)
    # bare chunk: no parent context available
    return ""


def _attach_metadata(store, hits: list[dict]) -> list[dict]:
    out = []
    for h in hits:
        c = store.get_chunk(h["doc_id"])
        out.append({**h, "metadata": (c or {}).get("metadata", {})})
    return out


def _head_tail(items: list) -> list:
    """Reorder by rank so the top passages sit at head AND tail (lost-in-the-middle)."""
    if len(items) <= 2:
        return items
    head, tail = [], []
    for i, it in enumerate(items):
        (head if i % 2 == 0 else tail).append(it)
    return head + tail[::-1]


def expand_hits(store, hits: list[dict], *, window_n: int = 3,
                short_doc_max_chunks: int = 15, max_parents: int = 5,
                char_budget: int = 4000) -> list[dict]:
    """Attach metadata, group by parent, cap to max_parents, expand each within
    a single char budget, order head-and-tail LAST.

    Selection happens first, in rank order: each parent's own text is bound to
    char_budget (so even the FIRST parent is truncated rather than admitted
    whole — a 27k-char single result is no longer possible), then accumulated
    until the budget is spent; a lower-ranked parent that no longer fits is
    dropped. Only once the final set is chosen is it `_head_tail`-reordered,
    so the consumer (prompt_recall) receives an already-budgeted set it never
    needs to re-truncate.
    """
    if not hits:
        return hits
    with_meta = _attach_metadata(store, hits)
    groups = group_by_parent(with_meta)[:max_parents]
    by_doc = {h["doc_id"]: h for h in hits}
    results, used = [], 0
    for g in groups:
        text = expand_parent(store, g, window_n=window_n,
                             short_doc_max_chunks=short_doc_max_chunks)
        if not text:
            text = by_doc[g["rep_doc_id"]].get("text", "")
        text = text[:char_budget]  # bind every parent's own text to the budget
        cost = len(text)
        if used + cost > char_budget:
            continue  # budget exhausted; drop this (lower-ranked) parent
        used += cost
        base = by_doc[g["rep_doc_id"]]
        results.append({"doc_id": base["doc_id"], "score": base.get("score", 0.0),
                        "distance": base.get("distance", 0.0), "text": text})
    return _head_tail(results)


def maybe_expand(store, hits, *, home, expand):
    """Apply small-to-big expansion ONLY when a consumer asks (expand=True) AND
    the retrieval_expand flag is on. brain_search never sets expand → flat hits.
    Degrades to the input hits on any error — recall must never raise."""
    if not expand:
        return hits
    from mcpbrain import config
    if not config.retrieval_expand_enabled(home):
        return hits
    try:
        return expand_hits(store, hits, **config.expand_params(home))
    except Exception:  # noqa: BLE001
        return hits
