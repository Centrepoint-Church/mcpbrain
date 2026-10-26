"""Daily, idempotent repair of the owner's split actions (2026-10-22
ingestion-integrity spec §2, follow-up; rulings I18-I20).

Before graph_write resolved one owner identity (graph_write.OwnerClass), the
owner's open actions were written under several (owner, owner_entity_id)
pairs: the configured short name with the DERIVED id, the same name with the
REAL owner node's id, the node's own display name, and the short name with no
id. Write-time dedupe compared owners as strings, so two paraphrases of one
commitment from one thread, credited to two spellings of the owner, both
stayed open.

repair_owner_actions heals existing rows on every install with no attended run
(it runs in the daemon's daily action_hygiene cadence):

1. canonicalise: every OPEN action that is the owner (OwnerClass.is_owner) gets
   owner = owner.name and owner_entity_id = the owner class's id. The prior
   owner string goes to prior_owner and the prior id to prior_owner_entity_id
   (an existing value is never replaced). A row whose id names ANOTHER existing
   person is left alone (ruling I19): a name never overrides a resolution.
2. same-thread duplicates: open email actions of one logical source
   (thread_id) -- any owner, not only the owner's -- are grouped with the
   write-time rule: cosine >= ACTION_SIM_THRESHOLD on unit embeddings, the
   lexical rules when no model is configured. Groups never join two different
   named owners or two different non-empty deadlines. Per group the row with a
   deadline is kept (then a named owner, then the earliest), and only the rows
   that match the KEPT row directly are archived (star check, ruling I20 --
   write-time dedupe is pairwise, never transitive): auto_archived with
   resolved_by 'dedup_owner'.

Every changed value is logged to cleanup_log under step LOG_STEP
('owner_repair'), in the clean-up's own format (owner, owner_entity_id,
prior_owner, prior_owner_entity_id for canonicalised rows; status, resolved_by,
resolved_at for archived rows; old and new value). The step is undo-able
(`mcpbrain clean-ingestion --undo owner_repair --yes`) but is NOT one of
ingestion_cleanup.STEPS: the clean-up never runs it.

undo_owner_repair restores exactly the logged old values (NULL stays NULL),
only on rows still holding the logged new value; a drifted row is skipped and
its log row kept. Rows repaired BEFORE the log existed have no log rows and are
undone the old way: owner/owner_entity_id restored from the prior columns,
'dedup_owner' rows reopened. The old way never touches a row that has log rows
(a drifted row stays as it is).

The comparison needs the model, canonicalising does not: with the model still
loading or failed (skip_comparison=reason) the rows are still rewritten, the
comparison is skipped, and the watermark is never moved past uncompared threads.

Bounded and incremental: a watermark (sync_cursors 'action_owner_repair') is
the highest per-thread newest-open-action id already compared. Each run
compares at most `max_threads` threads whose newest open action is above it,
oldest first, and canonicalising a row in an older thread pulls the watermark
back so that thread is compared again. Nothing new -> no embedding, no write.
A model that fails mid-run skips the comparison and leaves the watermark where
it was (never the weaker lexical rules with a configured model).
"""
from __future__ import annotations

import difflib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

from mcpbrain import graph_write as gw

log = logging.getLogger("mcpbrain.action_owner")

CURSOR = "action_owner_repair"
RESOLVED_BY = "dedup_owner"
MAX_THREADS = 1000
LOG_STEP = "owner_repair"
_OWNER_COLS = ("owner", "owner_entity_id", "prior_owner", "prior_owner_entity_id")
_ARCHIVE_COLS = ("status", "resolved_by", "resolved_at")


def _log(db, aid, column, old, new, stamp) -> None:
    from mcpbrain import ingestion_cleanup as ic
    ic.log_change(db, LOG_STEP, "actions", {"id": aid}, column, old, new, stamp)


# ---------------------------------------------------------------------------
# Grouping (shared with ingestion_cleanup's dedup_embed step)
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


def _sim(vecs, a: str, b: str) -> float:
    return sum(x * y for x, y in zip(vecs[a], vecs[b]))


def _same(vecs, a: str, b: str) -> bool:
    if vecs is None:
        return lexical_same(a, b)
    return _sim(vecs, a, b) >= gw.ACTION_SIM_THRESHOLD


def _deadline(r) -> str:
    return (r.get("deadline") or "").strip()[:10]


def cluster(rows, vecs, owner_key) -> list[list[dict]]:
    """Single-link candidate groups at ACTION_SIM_THRESHOLD, strongest pairs
    first, never joining two different named owners (blank/'unclear' joins any)
    or two different non-empty deadlines (recurring instances). `owner_key(row)`
    gives the owner-compatibility key ('' = blank). vecs=None: lexical pairs.
    A group is a CANDIDATE set: dedupe_groups archives only the members that
    match the kept row directly."""
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
                for j in range(i + 1, n):
                    s = _sim(vecs, rows[i]["text"], rows[j]["text"])
                    if s >= gw.ACTION_SIM_THRESHOLD:
                        pairs.append((s, i, j))
    parent = list(range(n))
    owners = [{owner_key(r)} - {""} for r in rows]
    deadlines = [{_deadline(r)} - {""} for r in rows]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for _s, i, j in sorted(pairs, key=lambda p: (-p[0], p[1], p[2])):
        a, b = find(i), find(j)
        if (a == b or len(owners[a] | owners[b]) > 1
                or len(deadlines[a] | deadlines[b]) > 1):
            continue
        parent[b] = a
        owners[a] |= owners[b]
        deadlines[a] |= deadlines[b]
    groups: dict[int, list[dict]] = defaultdict(list)
    for i, r in enumerate(rows):
        groups[find(i)].append(r)
    return [g for g in groups.values() if len(g) > 1]


def dedupe_groups(rows, vecs, owner_key) -> list[tuple[dict, list[dict]]]:
    """[(kept row, rows to archive)] for one logical source's open actions.
    Keep: a deadline, then a named owner, then the earliest created_at, then
    the lowest id. Archive only the members that match the kept row directly
    (star check): a chain A~B~C never archives C through B."""
    out = []
    for g in cluster(rows, vecs, owner_key):
        keep = min(g, key=lambda a: (not _deadline(a), owner_key(a) == "",
                                     a.get("created_at") or "", a["id"]))
        drop = [a for a in g if a is not keep and _same(vecs, keep["text"], a["text"])]
        if drop:
            out.append((keep, drop))
    return out


# ---------------------------------------------------------------------------
# The repair
# ---------------------------------------------------------------------------

def _person_ids(db, ids) -> set:
    found: set = set()
    ids = sorted({i for i in ids if i})
    for k in range(0, len(ids), 500):
        part = ids[k:k + 500]
        qs = ",".join("?" * len(part))
        found |= {r[0] for r in db.execute(
            f"SELECT id FROM entities WHERE type='person' AND id IN ({qs})", part)}
    return found


def canonicalise(db, oc, *, stamp) -> list[str]:
    """Rewrite the owner of every OPEN action that is the owner. Returns the
    thread_ids of the rows changed (one entry per row)."""
    if not oc.name:
        return []
    want_eid = oc.entity_id or ""
    rows = db.execute(
        "SELECT id, owner, owner_entity_id, prior_owner, prior_owner_entity_id, "
        "thread_id FROM actions "
        "WHERE status='open' AND COALESCE(trim(owner),'') != ''").fetchall()
    persons = _person_ids(db, {r["owner_entity_id"] for r in rows} - set(oc.ids))
    touched: list[str] = []
    for r in rows:
        owner, eid = r["owner"] or "", r["owner_entity_id"] or ""
        if not oc.is_owner(owner, eid, persons):
            continue
        if owner == oc.name and eid == want_eid:
            continue
        new = {"owner": oc.name, "owner_entity_id": want_eid,
               "prior_owner": r["prior_owner"] if r["prior_owner"] is not None else owner,
               "prior_owner_entity_id": (r["prior_owner_entity_id"]
                                         if r["prior_owner_entity_id"] is not None
                                         else eid)}
        cur = db.execute(
            "UPDATE actions SET owner=?, owner_entity_id=?, "
            "prior_owner=COALESCE(prior_owner, ?), "
            "prior_owner_entity_id=COALESCE(prior_owner_entity_id, ?), updated_at=? "
            "WHERE id=? AND status='open'",
            (oc.name, want_eid, owner, eid, stamp, r["id"]))
        if cur.rowcount:
            for c in _OWNER_COLS:
                if r[c] != new[c]:
                    _log(db, r["id"], c, r[c], new[c], stamp)
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
    """{text: unit vector}; None when the embedder fails."""
    cache: dict = {}
    uniq = list(dict.fromkeys(texts))
    for i in range(0, len(uniq), 256):
        if not gw._embed_texts(embedder, uniq[i:i + 256], cache):
            return None
    return cache


def repair_owner_actions(store, owner, *, embedder=None, owner_email: str = "",
                         as_of: str | None = None,
                         max_threads: int = MAX_THREADS,
                         skip_comparison: str | None = None) -> dict:
    """Canonicalise the owner's open actions, then collapse same-thread
    duplicates. Returns {'canonicalised': n, 'deduped': n, 'threads': n
    compared}, plus 'skipped': 'embedder_failed' when the model failed (the
    comparison is then left for the next run). embedder=None: lexical rules.
    skip_comparison=reason (the model is loading / failed): canonicalise only,
    return skipped=reason; the watermark is only ever pulled back."""
    now = (datetime.now(timezone.utc) if as_of is None
           else datetime.fromisoformat(as_of.replace("Z", "+00:00")))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    oc = gw.resolve_owner_class(store, owner, owner_email)
    limit = max(1, int(max_threads))

    try:
        wm = int(store.get_cursor(CURSOR) or 0)
    except (TypeError, ValueError):
        wm = 0
    start_wm = wm

    # 1. canonicalise (one write transaction)
    with store._connect(write=True) as db:
        touched = canonicalise(db, oc, stamp=stamp)
        if touched:
            back = _thread_max_ids(db, touched)
            if back:
                wm = min(wm, min(back.values()) - 1)

    if skip_comparison:
        if wm != start_wm:
            store.set_cursor(CURSOR, str(wm))
        return {"canonicalised": len(touched), "deduped": 0, "threads": 0,
                "skipped": skip_comparison}

    # 2. which threads changed since the watermark (oldest first, bounded),
    # read in ONE read transaction so the page and the watermark agree.
    with store._connect() as db:
        db.execute("BEGIN")
        sel = db.execute(
            f"SELECT thread_id, MAX(id) AS m, COUNT(*) AS n FROM actions "
            f"WHERE {_THREAD_ROWS} GROUP BY thread_id HAVING MAX(id) > ? "
            f"ORDER BY m LIMIT ?", (wm, limit)).fetchall()
        if len(sel) < limit:
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
        persons = _person_ids(db, {r["owner_entity_id"] for r in rows} - set(oc.ids))
        db.execute("COMMIT")

    result = {"canonicalised": len(touched), "deduped": 0, "threads": len(threads)}
    vecs = None
    if rows and embedder is not None:
        vecs = _vectors(embedder, [r["text"] for r in rows])
        if vecs is None:
            # Never the weaker lexical rules with a configured model, and the
            # watermark stays (a pulled-back one is kept) for the next run.
            if wm != start_wm:
                store.set_cursor(CURSOR, str(wm))
            log.warning("action_owner_repair: embedder failed; same-thread "
                        "comparison left for the next run")
            return {**result, "threads": 0, "skipped": "embedder_failed"}

    deduped = 0
    if rows:
        by_thread: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_thread[r["thread_id"]].append(r)

        def key(r):
            return oc.key(r["owner"], r["owner_entity_id"], persons)

        drop = [a["id"] for g in by_thread.values()
                for _keep, ds in dedupe_groups(g, vecs, key) for a in ds]
        if drop:
            with store._connect(write=True) as db:
                for aid in drop:
                    old = db.execute(
                        "SELECT status, resolved_by, resolved_at FROM actions "
                        "WHERE id=?", (aid,)).fetchone()
                    n = db.execute(
                        "UPDATE actions SET status='auto_archived', resolved_by=?, "
                        "resolved_at=?, updated_at=? WHERE id=? AND status='open'",
                        (RESOLVED_BY, stamp, stamp, aid)).rowcount
                    if n:
                        for c, v in zip(_ARCHIVE_COLS,
                                        ("auto_archived", RESOLVED_BY, stamp)):
                            _log(db, aid, c, old[c], v, stamp)
                    deduped += n

    if new_wm != start_wm:
        store.set_cursor(CURSOR, str(new_wm))
    if touched or deduped:
        log.info("action_owner_repair: canonicalised=%d deduped=%d threads=%d",
                 len(touched), deduped, len(threads))
    return {**result, "deduped": deduped}


def undo_owner_repair(store, *, as_of: str | None = None) -> dict:
    """Reverse repair_owner_actions. Logged changes (cleanup_log step
    'owner_repair') are restored exactly, through the clean-up's own `undo`:
    old values back (NULL stays NULL). Drift is judged per ACTION ROW, by
    column group: if any logged owner column (owner, owner_entity_id,
    prior_owner, prior_owner_entity_id) of a row no longer holds the value the
    repair wrote, none of that row's owner columns is restored and all their
    log rows are kept (each counted as drifted) -- half an owner restored
    would pair an old name with a newer id. The dedup columns (status,
    resolved_by, resolved_at) are judged the same way, as their own group. Rows repaired
    BEFORE the log existed have no log rows and are undone the old way (owner /
    owner_entity_id from the prior columns, 'dedup_owner' rows reopened) -- but
    never a row that has log rows. The watermark is reset, so the next daily run
    would apply the repair again -- turn the cadence off first if that is not
    wanted. Returns {'restored': rows, 'reopened': rows, 'drifted': values}."""
    from mcpbrain import ingestion_cleanup as ic
    now = (datetime.now(timezone.utc) if as_of is None
           else datetime.fromisoformat(as_of.replace("Z", "+00:00")))
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with store._connect() as db:
        logged = [(r["id"], json.loads(r["row_key"])["id"], r["column_name"])
                  for r in db.execute(
                      "SELECT id, row_key, column_name FROM cleanup_log WHERE step=?",
                      (LOG_STEP,))]
    _restored, drifted = (ic.undo(store, LOG_STEP, dry=False,
                                  groups=(_OWNER_COLS, _ARCHIVE_COLS))
                          if logged else (0, 0))
    with store._connect() as db:
        left = {r[0] for r in db.execute(
            "SELECT id FROM cleanup_log WHERE step=?", (LOG_STEP,))}
    done = [(aid, col) for lid, aid, col in logged if lid not in left]
    logged_ids = sorted({aid for _l, aid, _c in logged})
    restored = {aid for aid, col in done if col in _OWNER_COLS}
    reopened = {aid for aid, col in done if col == "status"}
    skip = ",".join("?" * len(logged_ids))
    not_logged = f" AND id NOT IN ({skip})" if logged_ids else ""
    with store._connect(write=True) as db:
        restored |= {r[0] for r in db.execute(
            "SELECT id FROM actions WHERE prior_owner_entity_id IS NOT NULL"
            + not_logged, logged_ids)}
        db.execute(
            "UPDATE actions SET owner=COALESCE(prior_owner, owner), "
            "owner_entity_id=prior_owner_entity_id, prior_owner=NULL, "
            "prior_owner_entity_id=NULL, updated_at=? "
            "WHERE prior_owner_entity_id IS NOT NULL" + not_logged,
            (stamp, *logged_ids))
        reopened |= {r[0] for r in db.execute(
            "SELECT id FROM actions WHERE status='auto_archived' AND resolved_by=?"
            + not_logged, (RESOLVED_BY, *logged_ids))}
        db.execute(
            "UPDATE actions SET status='open', resolved_by='', resolved_at='', "
            "updated_at=? WHERE status='auto_archived' AND resolved_by=?"
            + not_logged, (stamp, RESOLVED_BY, *logged_ids))
        db.execute("DELETE FROM sync_cursors WHERE source=?", (CURSOR,))
    return {"restored": len(restored), "reopened": len(reopened),
            "drifted": drifted}
