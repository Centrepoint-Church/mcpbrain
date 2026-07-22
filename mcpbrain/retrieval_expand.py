"""Read-side small-to-big expansion for recall. Pure functions over ranked hits
+ a store; called last in daemon.search (after ranking/rerank/sufficiency) so
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
