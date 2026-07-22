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


_JOIN = "\n\n"


def _by_date(chunks: list[dict]) -> list[dict]:
    return sorted(chunks, key=lambda c: (c.get("metadata") or {}).get("date", "") or "")


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
        return _JOIN.join(c["text"] for c in kept)
    # bare chunk: no parent context available
    return ""
