"""Daily, idempotent repair of the owner's split actions (2026-10-22
ingestion-integrity spec §2, follow-up).

Before graph_write resolved one owner identity (graph_write.OwnerClass), the
owner's open actions were written under several (owner, owner_entity_id)
pairs: the configured short name with the DERIVED id, the same name with the
REAL owner node's id, the node's own display name, and the short name with no
id. Write-time dedupe compared owners as strings, so two paraphrases of one
commitment from one thread, credited to two spellings of the owner, both
stayed open.

repair_owner_actions heals existing rows on every install with no attended run
(it runs in the daemon's daily action_hygiene cadence):

1. canonicalise: every OPEN action that is the owner under rule 1 gets
   owner = owner.name and owner_entity_id = the owner class's id; the prior
   owner string is kept in prior_owner (an existing value is never replaced);
2. dedupe: open email actions of one logical source (thread_id) are clustered
   with the write-time rule -- cosine >= ACTION_SIM_THRESHOLD on unit
   embeddings, the lexical rules when there is no embedder -- never joining two
   different named owners; per cluster the row with a deadline is kept, else
   the earliest, and the rest become auto_archived with resolved_by
   'dedup_owner' (reversible: reopen with set_action_status).

Bounded and incremental: a watermark (sync_cursors 'action_owner_repair') is
the highest per-thread newest-open-action id already compared. Each run
compares at most `max_threads` threads whose newest open action is above it,
oldest first, and canonicalising a row in an older thread pulls the watermark
back so that thread is compared again. Nothing new -> no embedding, no write.
"""
from __future__ import annotations

import difflib
import logging
from collections import defaultdict
from datetime import datetime, timezone

from mcpbrain import graph_write as gw

log = logging.getLogger("mcpbrain.action_owner")

CURSOR = "action_owner_repair"
RESOLVED_BY = "dedup_owner"
MAX_THREADS = 1000


# ---------------------------------------------------------------------------
# Clustering (shared with ingestion_cleanup's dedup_embed step)
# ---------------------------------------------------------------------------

def lexical_same(a: str, b: str) -> bool:
    """graph_write's lexical action-dedupe rules (its fallback when the
    embedder fails): normalised-title equality, then -- length-gated -- token
    subset, Jaccard >= 0.85, SequenceMatcher >= 0.82."""
    na, nb = gw._normalise_title_for_dedup(a), gw._normalise_title_for_dedup(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) / max(len(na), len(nb)) < 0.6:
        return False
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and (ta <= tb or tb <= ta):
        return True
    return (gw._token_jaccard(na, nb) >= 0.85
            or difflib.SequenceMatcher(None, na, nb).ratio() >= 0.82)


def cluster(rows, vecs, owner_key) -> list[list[dict]]:
    """Single-link clusters at ACTION_SIM_THRESHOLD, strongest pairs first,
    never joining two different named owners (blank/'unclear' joins any).
    `owner_key(row)` gives the owner-compatibility key ('' = blank).
    vecs=None: lexical pairs (lexical_same) instead of cosine."""
    n = len(rows)
    if vecs is None:
        pairs = [(1.0, i, j) for i in range(n) for j in range(i + 1, n)
                 if lexical_same(rows[i]["text"], rows[j]["text"])]
    else:
        try:
            import numpy as np
            m = np.array([vecs[r["text"]] for r in rows], dtype="float32")
            sims = m @ m.T
            hits = np.argwhere(np.triu(sims >= gw.ACTION_SIM_THRESHOLD, 1))
            pairs = [(float(sims[i, j]), int(i), int(j)) for i, j in hits]
        except ImportError:  # numpy ships with fastembed; this is only a fallback
            pairs = []
            for i in range(n):
                vi = vecs[rows[i]["text"]]
                for j in range(i + 1, n):
                    s = sum(a * b for a, b in zip(vi, vecs[rows[j]["text"]]))
                    if s >= gw.ACTION_SIM_THRESHOLD:
                        pairs.append((s, i, j))
    parent = list(range(n))
    owners = [{owner_key(r)} - {""} for r in rows]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _s, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        a, b = find(i), find(j)
        if a == b or len(owners[a] | owners[b]) > 1:
            continue
        parent[b] = a
        owners[a] |= owners[b]
    groups: dict[int, list[dict]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[find(i)].append(r)
    return [g for g in groups.values() if len(g) > 1]


# ---------------------------------------------------------------------------
# The repair
# ---------------------------------------------------------------------------

def canonicalise(db, oc, *, stamp) -> list[str]:
    """Rewrite the owner of every OPEN action that is the owner under rule 1.
    Returns the thread_ids of the rows changed (one entry per row)."""
    if not oc.name:
        return []
    want_eid = oc.entity_id or ""
    touched: list[str] = []
    for r in db.execute(
            "SELECT id, owner, owner_entity_id, prior_owner, thread_id FROM actions "
            "WHERE status='open' AND COALESCE(trim(owner),'') != ''").fetchall():
        owner, eid = r["owner"] or "", r["owner_entity_id"] or ""
        if not oc.is_owner(owner, eid):
            continue
        if owner == oc.name and eid == want_eid:
            continue
        db.execute(
            "UPDATE actions SET owner=?, owner_entity_id=?, "
            "prior_owner=COALESCE(prior_owner, ?), updated_at=? "
            "WHERE id=? AND status='open'",
            (oc.name, want_eid, owner, stamp, r["id"]))
        touched.append(r["thread_id"] or "")
    return touched


_THREAD_ROWS = ("status='open' AND COALESCE(source,'email')='email' "
                "AND COALESCE(thread_id,'') != '' AND COALESCE(trim(text),'') != ''")


def _thread_max_ids(db, thread_ids) -> dict[str, int]:
    out: dict[str, int] = {}
    ids = sorted({t for t in thread_ids if t})
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        qs = ",".join("?" * len(part))
        for t, m in db.execute(
                f"SELECT thread_id, MAX(id) FROM actions WHERE {_THREAD_ROWS} "
                f"AND thread_id IN ({qs}) GROUP BY thread_id", part):
            out[t] = m
    return out


def _vectors(embedder, texts):
    """{text: unit vector}, or None (lexical) when there is no embedder or it
    fails -- dedupe must never abort the repair."""
    if embedder is None:
        return None
    cache: dict = {}
    uniq = list(dict.fromkeys(texts))
    for i in range(0, len(uniq), 256):
        if not gw._embed_texts(embedder, uniq[i:i + 256], cache):
            return None
    return cache


def repair_owner_actions(store, owner, *, embedder=None, owner_email: str = "",
                         as_of: str | None = None,
                         max_threads: int = MAX_THREADS) -> dict:
    """Canonicalise the owner's open actions, then collapse owner-split
    duplicates within each logical source. Returns
    {'canonicalised': n, 'deduped': n, 'threads': n compared}."""
    now = (datetime.now(timezone.utc) if as_of is None
           else datetime.fromisoformat(as_of.replace("Z", "+00:00")))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    oc = gw.resolve_owner_class(store, owner, owner_email)

    try:
        wm = int(store.get_cursor(CURSOR) or 0)
    except (TypeError, ValueError):
        wm = 0

    # 1. canonicalise (one write transaction)
    with store._connect(write=True) as db:
        touched = canonicalise(db, oc, stamp=stamp)
        if touched:
            back = _thread_max_ids(db, touched)
            if back:
                wm = min(wm, min(back.values()) - 1)

    # 2. which threads changed since the watermark (oldest first, bounded)
    with store._connect() as db:
        sel = db.execute(
            f"SELECT thread_id, MAX(id) AS m, COUNT(*) AS n FROM actions "
            f"WHERE {_THREAD_ROWS} GROUP BY thread_id HAVING MAX(id) > ? "
            f"ORDER BY m LIMIT ?", (wm, max(1, int(max_threads)))).fetchall()
        if len(sel) < max(1, int(max_threads)):
            top = db.execute("SELECT COALESCE(MAX(id), 0) FROM actions").fetchone()[0]
            new_wm = max(wm, top)
        else:
            new_wm = sel[-1]["m"]
        threads = [r["thread_id"] for r in sel if r["n"] > 1]
        rows: list[dict] = []
        for i in range(0, len(threads), 500):
            part = threads[i:i + 500]
            qs = ",".join("?" * len(part))
            rows += [dict(r) for r in db.execute(
                f"SELECT id, text, owner, owner_entity_id, deadline, created_at, "
                f"thread_id FROM actions WHERE {_THREAD_ROWS} "
                f"AND thread_id IN ({qs}) ORDER BY id", part)]

    deduped = 0
    if rows:
        vecs = _vectors(embedder, [r["text"] for r in rows])
        by_thread: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_thread[r["thread_id"]].append(r)
        drop: list[int] = []

        def key(r):
            return oc.key(r["owner"], r["owner_entity_id"])

        for g in by_thread.values():
            for c in cluster(g, vecs, key):
                keep = min(c, key=lambda a: (
                    not (a["deadline"] or "").strip(), key(a) == "",
                    a["created_at"] or "", a["id"]))
                drop += [a["id"] for a in c if a is not keep]
        if drop:
            with store._connect(write=True) as db:
                for aid in drop:
                    deduped += db.execute(
                        "UPDATE actions SET status='auto_archived', resolved_by=?, "
                        "resolved_at=?, updated_at=? WHERE id=? AND status='open'",
                        (RESOLVED_BY, stamp, stamp, aid)).rowcount

    if new_wm != wm or touched:
        store.set_cursor(CURSOR, str(new_wm))
    if touched or deduped:
        log.info("action_owner_repair: canonicalised=%d deduped=%d threads=%d",
                 len(touched), deduped, len(threads))
    return {"canonicalised": len(touched), "deduped": deduped,
            "threads": len(threads)}
