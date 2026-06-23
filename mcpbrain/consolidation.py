"""B4 — RAPTOR-style consolidation pass (sleep-time compute).

Clusters recent episodic chunks by topic similarity, then LLM-summarises each
cluster into a cited semantic note stored as a new chunk with
memory_type='semantic' and memory_tier='hot'.

Trigger: accumulated_importance (sum of salience of un-consolidated episodic
chunks) exceeds CONSOLIDATION_THRESHOLD. This is checked nightly.

The LLM call goes through the `claude` CLI (config.find_claude), so no API key
is needed. Skip cleanly when claude is absent.

Public API:
  should_consolidate(store, home) -> bool
  consolidate(store, home, *, cap, threshold) -> dict
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
from datetime import datetime, timezone

log = logging.getLogger("mcpbrain.consolidation")

CONSOLIDATION_THRESHOLD = 50.0   # accumulated salience to trigger a pass
_MAX_CLUSTER_CHARS = 3000        # max text per cluster fed to the LLM
_MIN_CLUSTER_SIZE = 3            # skip tiny clusters (likely noise)
_CONSOLIDATION_CAP = 5           # max clusters per nightly pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Trigger check
# ---------------------------------------------------------------------------

def _unconsolidated_salience(store) -> float:
    """Sum of salience for episodic chunks not yet promoted to hot/core."""
    with store._connect() as db:
        row = db.execute(
            "SELECT COALESCE(SUM(salience), 0.0) FROM chunks "
            "WHERE memory_type = 'episodic' "
            "  AND COALESCE(memory_tier, '') NOT IN ('hot', 'core') "
            "  AND embedded = 1"
        ).fetchone()
    return float(row[0] if row else 0.0)


def should_consolidate(store, home: str) -> bool:
    """True when enough unconsolidated salience has accumulated to run a pass."""
    from mcpbrain import config
    if not config.consolidation_enabled(home):
        return False
    return _unconsolidated_salience(store) >= CONSOLIDATION_THRESHOLD


# ---------------------------------------------------------------------------
# Clustering (simple TF-IDF word-overlap — no extra dep needed)
# ---------------------------------------------------------------------------

def _token_set(text: str) -> set[str]:
    import re
    words = re.findall(r"\b[a-z]{4,}\b", (text or "").lower())
    return set(words)


def _similarity(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cluster_chunks(chunks: list[dict], *, threshold: float = 0.12) -> list[list[dict]]:
    """Greedy single-link clustering by Jaccard overlap on 4-char+ words.

    Two chunks share a cluster if their word overlap exceeds `threshold`.
    Returns a list of clusters (each a list of chunk dicts), largest first.
    """
    token_sets = [_token_set(c.get("text") or "") for c in chunks]
    labels = list(range(len(chunks)))

    for i in range(len(chunks)):
        for j in range(i + 1, len(chunks)):
            if _similarity(token_sets[i], token_sets[j]) >= threshold:
                root_i = labels[i]
                root_j = labels[j]
                if root_i != root_j:
                    # Merge: reassign the smaller label to the larger
                    old, new = max(root_i, root_j), min(root_i, root_j)
                    labels = [new if lb == old else lb for lb in labels]

    by_label: dict[int, list] = {}
    for i, lb in enumerate(labels):
        by_label.setdefault(lb, []).append(chunks[i])

    clusters = sorted(by_label.values(), key=len, reverse=True)
    return [c for c in clusters if len(c) >= _MIN_CLUSTER_SIZE]


# ---------------------------------------------------------------------------
# LLM call (via claude CLI)
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a memory consolidator. Below are related email/note excerpts. \
Write a concise durable semantic note (3-6 sentences) that captures the \
key facts, decisions, and relationships. Cite source IDs in the note \
using [id] notation. Return ONLY the note text, no preamble.

Source IDs and excerpts:
{excerpts}
"""


def _call_claude(prompt: str, timeout: int = 60) -> str:
    """Call the claude CLI with the given prompt. Returns output text or ''."""
    from mcpbrain import config
    try:
        claude = config.find_claude()
    except RuntimeError:
        log.warning("consolidation: claude CLI not found — skipping LLM summarise")
        return ""
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            log.warning("consolidation: claude returned %d: %s",
                        result.returncode, result.stderr[:200])
            return ""
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.warning("consolidation: claude timed out after %ds", timeout)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("consolidation: claude call failed: %s", exc)
        return ""


def _build_prompt(cluster: list[dict]) -> str:
    excerpts = []
    total = 0
    for c in cluster:
        doc_id = c.get("doc_id", "?")
        text = (c.get("text") or "").strip()[:400]
        if total + len(text) > _MAX_CLUSTER_CHARS:
            break
        excerpts.append(f"[{doc_id}]\n{text}")
        total += len(text)
    return _PROMPT_TEMPLATE.format(excerpts="\n\n".join(excerpts))


# ---------------------------------------------------------------------------
# Write consolidated note
# ---------------------------------------------------------------------------

def _write_note(store, cluster: list[dict], summary: str) -> str | None:
    """Write a consolidated semantic note chunk. Returns the new doc_id or None."""
    if not summary:
        return None

    source_ids = [c.get("doc_id", "") for c in cluster]
    ts = _now_iso()
    content_hash = hashlib.sha256(summary.encode()).hexdigest()[:16]
    doc_id = f"note-consolidated-{content_hash}"

    text = summary
    metadata = {
        "observation_type": "consolidated",
        "source_doc_ids": source_ids,
        "captured_at": ts,
        "title": "Consolidated note",
    }

    store.upsert_chunk(doc_id, text, content_hash, metadata)
    store.set_chunk_type(doc_id, "semantic")
    store.set_chunk_tier(doc_id, "hot")
    return doc_id


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def _episodic_chunks_for_consolidation(store, cap: int) -> list[dict]:
    """Fetch high-salience episodic chunks not yet consolidated."""
    import json as _json
    with store._connect() as db:
        rows = db.execute(
            "SELECT doc_id, text, metadata, salience FROM chunks "
            "WHERE memory_type = 'episodic' "
            "  AND COALESCE(memory_tier, '') NOT IN ('hot', 'core') "
            "  AND embedded = 1 "
            "  AND COALESCE(salience, 0.0) > 0.0 "
            "ORDER BY salience DESC "
            "LIMIT ?",
            (cap * 10,),
        ).fetchall()
    result = []
    for r in rows:
        try:
            meta = _json.loads(r["metadata"] or "{}")
        except Exception:
            meta = {}
        result.append({
            "doc_id": r["doc_id"],
            "text": r["text"],
            "metadata": meta,
            "salience": float(r["salience"] or 0.0),
        })
    return result


def _cluster_by_embedding(chunks: list[dict], embedder, *,
                          threshold: float = 0.55) -> list[list[dict]] | None:
    """Cluster chunks by cosine similarity on their bge embeddings (single-link).

    Reuses the same local embedder the store uses, so clusters reflect semantic
    similarity — far better than word-overlap for paraphrases / short notes. bge
    vectors are unit-normalised, so dot product == cosine. Returns None on any
    embedding failure so the caller falls back to the lexical clusterer.
    """
    texts = [c.get("text") or "" for c in chunks]
    try:
        vecs = embedder.embed_passages(texts)
    except Exception as exc:  # noqa: BLE001
        log.debug("consolidation: embedding clustering failed (%s); falling back", exc)
        return None
    n = len(chunks)
    labels = list(range(n))
    for i in range(n):
        vi = vecs[i]
        for j in range(i + 1, n):
            if sum(a * b for a, b in zip(vi, vecs[j])) >= threshold:
                ri, rj = labels[i], labels[j]
                if ri != rj:
                    old, new = max(ri, rj), min(ri, rj)
                    labels = [new if lb == old else lb for lb in labels]
    by_label: dict[int, list] = {}
    for i, lb in enumerate(labels):
        by_label.setdefault(lb, []).append(chunks[i])
    clusters = sorted(by_label.values(), key=len, reverse=True)
    return [c for c in clusters if len(c) >= _MIN_CLUSTER_SIZE]


def _cluster(chunks: list[dict], embedder=None) -> list[list[dict]]:
    """Cluster by embeddings when an embedder is available, else lexical Jaccard."""
    if embedder is not None:
        clusters = _cluster_by_embedding(chunks, embedder)
        if clusters is not None:
            return clusters
    return _cluster_chunks(chunks)


def consolidate(store, home: str, *, cap: int = _CONSOLIDATION_CAP,
                threshold: float = CONSOLIDATION_THRESHOLD, embedder=None) -> dict:
    """Run one consolidation pass.

    embedder (optional): the local bge embedder; when supplied, clustering is
    embedding-based (semantic) rather than lexical word-overlap.

    Returns {"clusters_found": N, "notes_written": M, "skipped": K}.
    """
    from mcpbrain import config
    if not config.consolidation_enabled(home):
        return {"clusters_found": 0, "notes_written": 0, "skipped": 0}

    acc = _unconsolidated_salience(store)
    if acc < threshold:
        log.debug("consolidation: accumulated salience %.1f < %.1f — skipping", acc, threshold)
        return {"clusters_found": 0, "notes_written": 0, "skipped": 1}

    chunks = _episodic_chunks_for_consolidation(store, cap)
    if not chunks:
        return {"clusters_found": 0, "notes_written": 0, "skipped": 0}

    clusters = _cluster(chunks, embedder)
    log.info("consolidation: %d chunks → %d clusters (%s)", len(chunks), len(clusters),
             "embedding" if embedder is not None else "lexical")

    written = 0
    for cluster in clusters[:cap]:
        prompt = _build_prompt(cluster)
        summary = _call_claude(prompt)
        if not summary:
            continue
        doc_id = _write_note(store, cluster, summary)
        if doc_id:
            written += 1
            # Mark source chunks as consolidated (promote to hot tier so they
            # aren't re-clustered next pass).
            for c in cluster:
                store.set_chunk_tier(c["doc_id"], "hot")
            log.info("consolidation: wrote semantic note %s from %d sources",
                     doc_id, len(cluster))

    store.record_change(
        "consolidation_pass",
        summary=f"Consolidated {written} semantic notes from {len(clusters)} clusters",
        source="consolidation",
    )
    return {"clusters_found": len(clusters), "notes_written": written, "skipped": 0}
