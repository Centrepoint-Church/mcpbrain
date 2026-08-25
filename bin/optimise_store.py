"""Attended, backup-gated store rebuild. NEVER run automatically.

Six of the target optimisations each require rewriting the whole file
(page_size, STRICT, contentless FTS5, the partial indexes, FK constraints,
and dropping the dead columns), so they are applied together in ONE
out-of-place pass instead of six sequential rewrites of a 2.6 GB file.

Usage (attended — this is a CLI a human runs, following bin/consolidate.py):

    # 1. stop the daemon, then:
    uv run python bin/optimise_store.py                  # orphan/schema report only
    uv run python bin/optimise_store.py --yes            # rebuild to <store>.new
    uv run python bin/optimise_store.py --swap --yes     # promote it, retaining the old file

Nothing here is wired into a daemon cadence, a cron, or any automatic
trigger, and no invocation ever overwrites the live store: `--swap` moves the
old file aside under a timestamped name and never deletes it.

FREE SPACE, for ONE invocation: ~2.4x the store. On the 2.62 GB live store,
peak concurrent usage is 3.48 GB (the encrypted snapshot -- Fernet base64 costs
4/3 over the plaintext) + 2.62 GB (the snapshot's transient cleartext, both
during the snapshot and again while verifying it) + 1.49 GB (the rebuild)
= 7.6 GB. make_encrypted_snapshot refuses up front rather than filling the disk
part-way, so a short check fails cleanly at gate 2.

But running the DOCUMENTED PROCEDURE -- which is what an operator actually does
-- peaks HIGHER than any single invocation, because gate 2 fires on every
non-`--swap`/non-`--rollback` run (the report-only step included), writes a
TIMESTAMPED artifact, and never deletes it: two retained snapshots overlap.
Budget ~12-13 GB and see `docs/RELEASE-RUNBOOK.md` section 7 for the full
per-step arithmetic; do not size the disk from the single-invocation figure
above.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (child table, child column) -> parent table. entity_observations already
# DECLARES REFERENCES entities(id), but foreign_keys was OFF so it never
# enforced anything.
_REFS = [
    ("entity_relations", "entity_a", "entities", "id"),
    ("entity_relations", "entity_b", "entities", "id"),
    ("email_entities", "entity_id", "entities", "id"),
    ("entity_observations", "entity_id", "entities", "id"),
    ("entity_communities", "entity_id", "entities", "id"),
    # Task 7's self-referential FK. Nullable and with no writer today (0
    # non-null values on the live store), but listed anyway so the reporter
    # covers every REFERENCES clause init() actually creates rather than a
    # frozen five — the next FK to be added must show up here or in a failing
    # test, not in a foreign_key_check after the rebuild.
    ("entity_observations", "invalidated_by_observation_id",
     "entity_observations", "id"),
    # entity_relations' bitemporal sibling of the above, and the one that
    # actually MATTERS: it is written and read for real (graph_write
    # ._invalidate_relation / _supersede, maintenance.graph_cleanup), and it is
    # added by ALTER TABLE with NO `REFERENCES` clause -- so
    # `PRAGMA foreign_key_check` is structurally blind to it and gate 5 can
    # never catch a dangling one. The rebuild drops 8 orphan entity_relations
    # rows on the live store; any surviving row pointing at one of those 8
    # would be left dangling with nothing to detect it.
    ("entity_relations", "invalidated_by_relation_id",
     "entity_relations", "id"),
]

# Dangling NULLABLE references are repaired by NULLing the pointer, not by
# dropping the row: that is exactly what entity_observations' declared
# ON DELETE SET NULL means, and dropping a row because whatever invalidated it
# went away would lose good data (and cascade further). Same repair for
# entity_relations, whose column declares no FK at all: a NULL there reads as
# "invalidated, invalidator unknown", which is true and harmless, whereas a
# pointer to a row that no longer exists is neither.
#
# This runs on the DESTINATION, after the referential filter -- which is the
# only place it can work. A pointer into a row that exists in the source but
# is DROPPED as an orphan is not an orphan in the source, so report_orphans
# cannot see it; only the post-copy state can.
_NULLIFY = [
    ("entity_observations", "invalidated_by_observation_id",
     "entity_observations", "id"),
    ("entity_relations", "invalidated_by_relation_id", "entity_relations", "id"),
]


def report_orphans(path) -> dict[str, int]:
    """Count rows whose declared parent row does not exist, per FK column.

    Read-only. The `IS NOT NULL` guard matters for NULLABLE FK columns: a
    LEFT JOIN on a NULL child value also yields a NULL parent, so without it
    every unset pointer would be counted as an orphan (all 19,778
    entity_observations rows, on the live store). It is a no-op for the
    NOT NULL columns, which is why it can be applied uniformly.
    """
    db = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        out = {}
        for child, col, parent, pcol in _REFS:
            try:
                n = db.execute(
                    f'SELECT count(*) FROM "{child}" c '
                    f'LEFT JOIN "{parent}" p ON p."{pcol}" = c."{col}" '
                    f'WHERE p."{pcol}" IS NULL AND c."{col}" IS NOT NULL'
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            out[f"{child}.{col}"] = n
        return out
    finally:
        db.close()


# FTS5 and vec0 SHADOW tables are internal storage. Copying shadow rows
# straight across would corrupt them (and copying fts_chunks_content would
# defeat the whole point of the contentless rebuild), so every shadow table is
# skipped: fts_chunks is re-derived from chunks, and vec_chunks is copied
# through its own virtual-table interface (see _copy_vectors) instead.
# `sqlite_` covers sqlite_stat1/4 (re-derived by ANALYZE) and sqlite_sequence
# (maintained automatically by the copied AUTOINCREMENT rows).
_SKIP_PREFIXES = ("fts_chunks", "vec_chunks", "sqlite_")

# Referential filter per table -- keep only rows whose parent still exists.
_KEEP = {
    "entity_relations": "entity_a IN (SELECT id FROM entities) "
                        "AND entity_b IN (SELECT id FROM entities)",
    "email_entities": "entity_id IN (SELECT id FROM entities)",
    "entity_observations": "entity_id IN (SELECT id FROM entities)",
    "entity_communities": "entity_id IN (SELECT id FROM entities)",
}


def _store_dim(src) -> int:
    """The vector width recorded in the source store's meta table.

    Store(path, dim) has no dim default on purpose — a store's vector width is
    not guessable — and the destination MUST match the source or every copied
    embedding is the wrong shape. Read it rather than assume it.
    """
    db = sqlite3.connect(f"file:{Path(src)}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT v FROM meta WHERE k='dim'").fetchone()
    finally:
        db.close()
    if not row or not str(row[0]).strip():
        raise ValueError(f"{src}: meta has no 'dim' row; refusing to guess the "
                         "vector width of a store being rebuilt")
    return int(row[0])


class UnmigratedStore(RuntimeError):
    """The source still carries a pre-migration table shape.

    `init()` performs two RENAME migrations (store.py: graph_actions /
    graph_decisions -> *_legacy behind the meta.actions_migrated flag;
    enrich_payloads -> enrich_payloads_legacy when it is still doc_id-keyed).
    A store that has not run them yet is a valid input to init() -- that is
    the whole point of those branches -- but it is NOT a valid input to a
    rebuild, and both cases fail in ways that pass every gate:

    * graph_actions: the fresh destination's own init() drains its (empty)
      graph_actions and renames it, so the destination has
      graph_actions_legacy and no graph_actions. The carry rule then sees the
      source's graph_actions as an unmanaged table and recreates it, while the
      copied `meta` (which has no actions_migrated row) wipes the destination's
      flag. All seven gates pass, and then the FIRST init() on the rebuilt
      store tries the rename again and dies:
      `OperationalError: there is already another table or index with this
      name: graph_actions_legacy` -- an unopenable store.
    * enrich_payloads: the source's table is doc_id-keyed, the destination's is
      file_id-keyed, so the column intersection drops doc_id and every INSERT
      hits `NOT NULL constraint failed: enrich_payloads.file_id` -- an
      IntegrityError mid-rebuild leaving a stuck partial .new file.

    Making the rebuild rename-aware would mean re-implementing both
    migrations inside it (and the enrich_payloads one is a re-KEY that needs
    Store.migrate_enrich_payloads_batch, a background cadence, not an inline
    step). Refusing with an actionable message is the honest answer: let the
    daemon finish migrating, then rebuild.
    """


def check_migrations(src) -> list[str]:
    """Reasons the source is not rebuildable yet. Empty list == good to go."""
    db = sqlite3.connect(f"file:{Path(src)}?mode=ro", uri=True)
    try:
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        reasons = []
        for legacy in ("graph_actions", "graph_decisions"):
            if legacy in tables:
                reasons.append(
                    f"{legacy} still exists (pre-Task-1.7 shape). Start the "
                    "daemon once: init() drains it into `actions` and renames "
                    f"it to {legacy}_legacy, then rebuild.")
        if "enrich_payloads" in tables:
            cols = {r[1] for r in db.execute("PRAGMA table_info(enrich_payloads)")}
            if "doc_id" in cols:
                reasons.append(
                    "enrich_payloads is still doc_id-keyed. Start the daemon "
                    "and let the enrich_payload_migration cadence drain "
                    "enrich_payloads_legacy, then rebuild.")
        return reasons
    finally:
        db.close()


def embedded_without_vectors(src) -> int:
    """Chunks flagged embedded=1 whose vector does not resolve.

    Pre-checked because `backup.snapshot` -> `_verify_artifact` REFUSES such a
    store outright (it samples the first embedded chunks and raises), so gate 2
    cannot take a snapshot of it and the rebuild cannot start. That is also why
    the daemon's own backups are already failing on such a store, so this is
    worth fixing regardless of the rebuild.
    """
    from mcpbrain.store import _open_db
    db = _open_db(src, read_only=True)
    try:
        return db.execute(
            "SELECT count(*) FROM chunks WHERE embedded=1 AND rowid NOT IN "
            "(SELECT rowid FROM vec_chunks)").fetchone()[0]
    finally:
        db.close()


def _load_meta(raw) -> dict:
    """chunks.metadata -> dict, tolerantly.

    Every writer stores json.dumps() text, but one unparseable row must not
    abort a multi-hour rebuild — the FTS contextual prefix simply has nothing
    to add for that chunk.
    """
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = bytes(raw).decode("utf-8", "replace")
    try:
        val = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return val if isinstance(val, dict) else {}


def rebuild(src, dst, *, page_size: int = 8192) -> dict:
    """Rebuild `src` into a fresh `dst` in ONE pass.

    page_size MUST be set before any table exists, so it comes first, and it is
    materialised with a VACUUM: the pragma only takes effect when page 1 is
    written, and init()'s first statement switches the file to WAL, which
    refuses a later page-size change. Without the VACUUM the destination stays
    at the 4096-byte default and nothing complains.

    Everything the destination's schema does NOT define is preserved, not
    dropped: unmanaged tables are recreated from the source's own DDL, and any
    source column with no destination counterpart is reported with its
    non-null row count. The return dict is the audit record — no skip, drop, or
    truncation happens anywhere in here without a line in it.
    """
    from mcpbrain.store import Store, _open_db
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise FileExistsError(dst)
    # Checked here as well as in the CLI's report, so a direct caller cannot
    # produce the unopenable store described on UnmigratedStore.
    if reasons := check_migrations(src):
        raise UnmigratedStore("; ".join(reasons))

    dim = _store_dim(src)

    db = sqlite3.connect(dst)
    try:
        db.execute(f"PRAGMA page_size={int(page_size)}")
        db.execute("VACUUM")  # writes page 1, fixing the page size for good
    finally:
        db.close()

    Store(str(dst), dim=dim).init()   # single source of schema truth -- Tasks 5-7

    dropped = report_orphans(src)
    copy = _copy_all(src, dst)
    fts_rows = _rederive_fts(dst, dim)
    # Statistics for the planner, once, on the finished file. Through _open_db
    # so the vec0 extension is loaded -- ANALYZE walks sqlite_master.
    # bulk=True: a single connection scanning the whole schema (PR #25 finding 5).
    d = _open_db(dst, bulk=True)
    try:
        d.execute("ANALYZE")
        d.commit()
    finally:
        d.close()
    return {"copied": copy["copied"], "dropped": dropped,
            "dropped_rows": copy["dropped_rows"],
            "dropped_columns": copy["dropped_columns"],
            "carried": copy["carried"], "skipped": copy["skipped"],
            "nullified": copy["nullified"],
            "requeued_embeddings": copy["requeued_embeddings"],
            "vectors": copy["vectors"],
            "fts_rows": fts_rows,
            "dim": dim,
            "src_bytes": src.stat().st_size, "dst_bytes": dst.stat().st_size}


def _copy_all(src, dst, *, batch: int = 5000) -> dict:
    """Copy every source table into the freshly-init()'d destination.

    Returns an audit dict, not just row counts, because three things here can
    lose data and none of them may do so silently:

    * `carried`  — tables the current schema no longer creates (areas,
      projects, bandit_arms, doc_context, suppressed_entities,
      enrich_payloads_legacy on the live store). The brief's version INSERTed
      into the destination for every source table and would have raised
      `no such table` on the first one. They are recreated from the source's
      OWN DDL and copied verbatim: `bandit_arms` holds live learned bandit
      state, `enrich_payloads_legacy` holds payloads a daemon cadence is still
      draining, and `areas`/`projects` hold 42 rows of real user data from a
      removed feature. A rebuild is not the place to decide any of that is
      rubbish; they come across, and the report names them.
    * `dropped_columns` — source columns the destination schema has no
      counterpart for (entity_relations.normalised_strength and .since on the
      live store: 80,577 and 61,512 non-null values, no reader or writer left
      in the codebase). Dropping dead columns is part of what the rebuild is
      FOR, but the counts are reported so the human consents to it.
    * `dropped_rows` — orphans removed by the `_KEEP` referential filter.
      Per-TABLE, unlike report_orphans' per-COLUMN counts: one
      entity_relations row can be orphaned on both entity_a and entity_b, so
      the column counts are an upper bound on rows, never an equality.
    """
    from mcpbrain.store import _open_db
    # bulk=True on both sides: this is the actual bulk-copy pass -- every
    # table, potentially hundreds of thousands of rows (PR #25 finding 5).
    s_db = _open_db(src, read_only=True, bulk=True)
    # Stamped into dst's own meta table (below, AFTER the generic copy loop --
    # meta is itself a managed table the loop DELETEs-then-refills from src,
    # which would wipe a stamp written any earlier) so _swap can tell whether
    # src kept changing after THIS read of it, no matter how long dst then
    # sits waiting for --swap.
    freshness = _freshness_snapshot(s_db)
    d_db = _open_db(dst, bulk=True)
    # Parents may land after children (and entity_observations references
    # itself), so enforcement is off for the copy; the CLI's explicit
    # foreign_key_check on the finished file is what actually verifies it.
    d_db.execute("PRAGMA foreign_keys=OFF")
    # Bulk load, on a file nothing else can see yet: a torn destination is
    # thrown away and rebuilt, so per-commit fsyncs buy nothing.
    d_db.execute("PRAGMA synchronous=OFF")
    copied, carried, skipped = {}, {}, {}
    dropped_rows, dropped_columns, nullified = {}, {}, {}
    try:
        tables = [r[0] for r in s_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        dst_tables = {r[0] for r in d_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        # entities first so the FK targets exist before dependents.
        tables.sort(key=lambda t: 0 if t == "entities" else 1)
        for t in tables:
            if t.startswith(_SKIP_PREFIXES):
                skipped[t] = _row_count(s_db, t)
                continue
            src_cols = [r[1] for r in s_db.execute(f'PRAGMA table_info("{t}")')]
            if not src_cols:
                continue
            managed = t in dst_tables
            if not managed:
                _recreate_from_source_ddl(s_db, d_db, t)
                cols = src_cols
            else:
                dst_cols = [r[1] for r in d_db.execute(f'PRAGMA table_info("{t}")')]
                cols = [c for c in src_cols if c in dst_cols]
                for gone in (c for c in src_cols if c not in dst_cols):
                    dropped_columns[f"{t}.{gone}"] = s_db.execute(
                        f'SELECT count(*) FROM "{t}" '
                        f'WHERE "{gone}" IS NOT NULL').fetchone()[0]
                # init() seeds a few rows of its own (meta.dim, and
                # meta.actions_migrated via the graph_actions migration), which
                # would collide with the source's on a PRIMARY KEY. The source
                # is authoritative for CONTENT, so clear first -- a no-op on
                # every other table, since the destination is brand new.
                d_db.execute(f'DELETE FROM "{t}"')
            collist = ",".join(f'"{c}"' for c in cols)
            ph = ",".join("?" * len(cols))
            where = f" WHERE {_KEEP[t]}" if t in _KEEP else ""
            cur = s_db.execute(f'SELECT {collist} FROM "{t}"{where}')
            n = 0
            while True:
                rows = [tuple(r) for r in cur.fetchmany(batch)]
                if not rows:
                    break
                d_db.executemany(
                    f'INSERT INTO "{t}"({collist}) VALUES({ph})', rows)
                d_db.commit()
                n += len(rows)
            (copied if managed else carried)[t] = n
            if t in _KEEP and (lost := _row_count(s_db, t) - n):
                dropped_rows[t] = lost
        # vec0 rows: the ONLY thing in this store that cannot be re-derived
        # (every other virtual table is rebuilt from chunks). The brief skipped
        # the whole vec_chunks prefix and never put the vectors back, which on
        # the live store would have left 170,695 chunks flagged embedded=1 with
        # no vector at all -- semantic recall silently returning nothing, and no
        # way back short of re-embedding the corpus.
        vectors = _copy_vectors(s_db, d_db, batch=batch)
        # Whatever the vectors did, embedded=1 must MEAN "has a vector". If any
        # chunk lost its vector the honest state is embedded=0 (the daemon
        # re-embeds it) -- never a flag that lies.
        d_db.execute("UPDATE chunks SET embedded=0 WHERE embedded=1 AND rowid "
                     "NOT IN (SELECT rowid FROM vec_chunks)")
        requeued = d_db.execute("SELECT changes()").fetchone()[0]
        # Dangling nullable self-references, repaired per the column's declared
        # ON DELETE SET NULL rather than by dropping the row.
        for child, col, parent, pcol in _NULLIFY:
            d_db.execute(
                f'UPDATE "{child}" SET "{col}"=NULL WHERE "{col}" IS NOT NULL '
                f'AND "{col}" NOT IN (SELECT "{pcol}" FROM "{parent}")')
            k = d_db.execute("SELECT changes()").fetchone()[0]
            if k:
                nullified[f"{child}.{col}"] = k
        # Force the FTS rebuild to consider every row. Copying
        # fts_context_version across verbatim would make the re-derive think
        # the work was already done and leave the index EMPTY.
        d_db.execute("UPDATE chunks SET fts_context_version=0")
        # Written LAST, after the generic loop's own copy of meta (from src)
        # has already landed -- see the comment where `freshness` is computed.
        d_db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
                     (_FRESHNESS_META_KEY, json.dumps(freshness)))
        d_db.commit()
    finally:
        s_db.close()
        d_db.close()
    return {"copied": copied, "carried": carried, "skipped": skipped,
            "dropped_rows": dropped_rows, "dropped_columns": dropped_columns,
            "nullified": nullified, "requeued_embeddings": int(requeued or 0),
            "vectors": vectors}


def _row_count(db, table: str) -> int | None:
    try:
        return db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return None  # a shadow table can be uncountable through its own module


# Tables a real daemon session actually grows -- chunks (ingest), entities/
# entity_relations (graph writes), actions (extraction). Any of these moving
# between the rebuild's read of src and a later --swap means src kept
# changing after the rebuild was taken -- most concretely: the daemon
# restarted and ran for a while before someone got around to --swap.
_FRESHNESS_TABLES = ("chunks", "entities", "entity_relations", "actions")
_FRESHNESS_META_KEY = "optimise_rebuild_freshness"


def _freshness_snapshot(db) -> dict[str, int]:
    return {t: (_row_count(db, t) or 0) for t in _FRESHNESS_TABLES}


def _recreate_from_source_ddl(s_db, d_db, table: str) -> None:
    """Create `table` on the destination from the source's own CREATE TABLE.

    Used only for tables the current schema no longer defines, so init() is not
    an option and there is nothing to inherit STRICT/typing from. Copying the
    DDL verbatim keeps the rebuild lossless; the report flags the table so a
    later, deliberate schema cleanup can retire it on purpose.
    """
    row = s_db.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name=?", (table,)).fetchone()
    if not row or not row[0]:
        raise ValueError(f"{table}: no CREATE statement to carry across")
    d_db.execute(row[0])
    for idx in s_db.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                            "AND tbl_name=? AND sql IS NOT NULL", (table,)):
        d_db.execute(idx[0])
    d_db.commit()


def _copy_vectors(s_db, d_db, *, batch: int = 5000) -> int:
    """Copy vec_chunks through the vec0 virtual table, preserving rowids.

    Reading `embedding` back out of vec0 yields the same float32 blob
    serialize_float32 wrote, so the round-trip is byte-exact and no embedder is
    involved. Going through the virtual table (rather than its
    vec_chunks_rowids / vec_chunks_vector_chunks00 shadow tables) is what makes
    this safe: the shadows encode chunk-internal layout that a different vec0
    build is free to change.
    """
    n = 0
    last = -1
    while True:
        rows = s_db.execute(
            "SELECT rowid, embedding FROM vec_chunks WHERE rowid > ? "
            "ORDER BY rowid LIMIT ?", (last, batch)).fetchall()
        if not rows:
            return n
        d_db.executemany("INSERT INTO vec_chunks(rowid, embedding) VALUES(?,?)",
                         [(r[0], r[1]) for r in rows])
        d_db.commit()
        last = rows[-1][0]
        n += len(rows)


def _rederive_fts(dst, dim: int, *, batch: int = 5000) -> int:
    """Rebuild fts_chunks from chunks, reusing Store._fts_text.

    The contextual prefix comes from the same _fts_text the write path uses, so
    there is never a second copy of that logic. What this does NOT reuse is
    reindex_fts_batch's row SELECTION, and deliberately: that method picks rows
    by `fts_context_version < FTS_CONTEXT_VERSION` and re-stamps version 0 for
    a row written WITHOUT the prefix (correct for it — such a row must be
    revisited if contextual_retrieval later flips ON). Driven in a loop over a
    freshly-zeroed store with the flag OFF, that selection never converges: the
    same first `cap` rows come back forever and the remaining ~168k chunks
    never get an FTS row at all. A rowid cursor terminates under either flag
    state and covers every row exactly once.

    Only embedded=1 rows are indexed, matching the write path — an unembedded
    chunk gets its FTS row when its vector lands.
    """
    from mcpbrain.store import Store
    store = Store(str(dst), dim=dim)
    total, last = 0, 0
    while True:
        with store._connect(write=True, bulk=True) as db:
            rows = db.execute(
                "SELECT rowid, text, metadata FROM chunks "
                "WHERE embedded=1 AND rowid > ? ORDER BY rowid LIMIT ?",
                (last, batch)).fetchall()
            if not rows:
                return total
            for r in rows:
                fts_text, applied = Store._fts_text(r["text"] or "",
                                                    _load_meta(r["metadata"]))
                db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)",
                           (r["rowid"], fts_text))
                db.execute("UPDATE chunks SET fts_context_version=? WHERE rowid=?",
                           (Store.FTS_CONTEXT_VERSION if applied else 0,
                            r["rowid"]))
            last = rows[-1]["rowid"]
            total += len(rows)


# --------------------------------------------------------------------------
# CLI. Attended only.
# --------------------------------------------------------------------------

def _integrity(path, *, check_fk: bool = True) -> tuple[bool, str]:
    """(ok, detail) from PRAGMA integrity_check, plus foreign_key_check.

    check_fk=False for verifying a snapshot of the PRE-rebuild store: that
    store has dangling references by definition — removing them is what the
    rebuild is for, 256 of them on the live store — so a faithful snapshot of
    it must fail foreign_key_check. What "verified" means for a rollback
    artifact is "decrypts to a structurally sound, openable SQLite file", and
    that is integrity_check. The FK gate belongs on the REBUILT file, where a
    violation means the rebuild is wrong.
    """
    from mcpbrain.store import _open_db
    db = _open_db(path, read_only=True)
    try:
        ic = [r[0] for r in db.execute("PRAGMA integrity_check")]
        fk = db.execute("PRAGMA foreign_key_check").fetchall() if check_fk else []
    finally:
        db.close()
    ok = ic == ["ok"] and not fk
    detail = f"integrity_check={ic[:5]}"
    if check_fk:
        detail += f" foreign_key_check={len(fk)} violation(s)"
        if fk:
            detail += f" first={tuple(fk[0])}"
    return ok, detail


def _acquire_exclusive(src: Path):
    """Refuse unless nothing else can be writing `src`.

    For the LIVE store that is the daemon's own SingleWriterLock: taking it
    proves no daemon holds it, and holding it for the whole rebuild is the
    bulk-lock guarantee. For any other file (a scratch copy, which is how this
    is dry-run) no daemon is involved, so a lock beside that file keeps two
    rebuilds from racing each other. The choice is made on the RESOLVED path,
    so passing the live store in via --src cannot route around the daemon gate.

    The live lockfile is derived from the live store's OWN directory, never
    from --home: a --home that disagrees with MCPBRAIN_HOME would otherwise
    make this take an unrelated lockfile and report exclusivity it does not
    have while the real daemon keeps writing.
    """
    from mcpbrain import config
    from mcpbrain.daemon import AlreadyRunningError, SingleWriterLock
    live = Path(config.store_path()).resolve()
    is_live = src.resolve() == live
    lock_path = (live.parent / "daemon.lock") if is_live \
        else Path(f"{src}.rebuild.lock")
    lock = SingleWriterLock(lock_path)
    try:
        lock.acquire()
    except AlreadyRunningError:
        who = ("the daemon is running -- stop it first "
               "(launchctl bootout gui/$UID/com.mcpbrain)" if is_live
               else f"another rebuild holds {lock_path}")
        return None, is_live, who
    return lock, is_live, None


def _verified_snapshot(src: Path, home: Path) -> tuple[Path, Path | None]:
    """Encrypted snapshot of `src`, PROVEN restorable. Returns (artifact, key).

    An unverified snapshot is not a rollback, so this decrypts the artifact to
    a temp path and runs integrity_check on the result before returning; the
    cleartext is removed immediately either way. No records_dir/config_path is
    passed, so the artifact is a bare single-file sqlite snapshot -- the shape
    integrity_check can actually be run against.

    If config carries no escrow key one is generated and written next to the
    artifact at 0600 (opened via os.open with the mode set at creation, so
    there is no world-readable window between create and chmod), because an
    encrypted snapshot whose key is lost is not a rollback either.

    Named `<artifact>.key` -- ONE PER SNAPSHOT, never a fixed shared path.
    Snapshots are timestamped and retained across runs (main() never deletes
    an old one), so a shared `<store>.rebuild-key` meant a second run
    (e.g. the runbook's own step-2-then-step-3 sequence, or a second
    report-only invocation) silently generated a fresh random key and
    overwrote the only copy that could open the FIRST snapshot -- orphaning
    it while it stayed on disk looking like a valid rollback.
    """
    from mcpbrain import backup, config
    key_path = None
    key = ((config.read_config(str(home)).get("backup") or {})
           .get("escrow_key") or "")
    key = key.encode() if isinstance(key, str) else key
    out = Path(f"{src}.snapshot-{int(time.time())}.enc")
    if not key:
        key = backup.generate_escrow_key()
        # Same-second collision on `out` itself (two invocations producing an
        # identical epoch-seconds name) is an accepted, pre-existing edge
        # case for the .enc artifact too -- a real snapshot of a multi-GB
        # store takes measurably longer than a second, so this is not the
        # bug finding 3 describes. Deliberately O_CREAT|O_TRUNC (not O_EXCL):
        # an exact-same-name collision here means `out` collided too, so the
        # last write replaces both consistently, which is correct.
        key_path = Path(f"{out}.key")
        fd = os.open(key_path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
    backup.make_encrypted_snapshot(src, out, key)
    work = Path(tempfile.mkdtemp(prefix="mcpbrain-verify-"))
    try:
        plain = backup.decrypt_file(out, work / "verify.sqlite3", key)
        ok, detail = _integrity(plain, check_fk=False)
        if not ok:
            raise RuntimeError(f"snapshot {out} did not verify: {detail}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return out, key_path


def _schema_preflight(src: Path, dim: int) -> dict:
    """What the destination schema would drop, computed WITHOUT writing it.

    Built by init()'ing a throwaway store and diffing, so the orphan report the
    human consents to also names the tables and columns the rebuild would not
    carry forward. Consent before the numbers are known is not consent.
    """
    from mcpbrain.store import Store, _open_db
    work = Path(tempfile.mkdtemp(prefix="mcpbrain-preflight-"))
    try:
        probe = work / "probe.sqlite3"
        Store(str(probe), dim=dim).init()
        p_db, s_db = _open_db(probe, read_only=True), _open_db(src, read_only=True)
        try:
            fresh = {r[0] for r in p_db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            unmanaged, lost_cols = {}, {}
            for (t,) in s_db.execute("SELECT name FROM sqlite_master "
                                     "WHERE type='table' ORDER BY name"):
                if t.startswith(_SKIP_PREFIXES):
                    continue
                if t not in fresh:
                    unmanaged[t] = _row_count(s_db, t)
                    continue
                dst_cols = {r[1] for r in p_db.execute(f'PRAGMA table_info("{t}")')}
                for r in s_db.execute(f'PRAGMA table_info("{t}")'):
                    if r[1] not in dst_cols:
                        lost_cols[f"{t}.{r[1]}"] = s_db.execute(
                            f'SELECT count(*) FROM "{t}" '
                            f'WHERE "{r[1]}" IS NOT NULL').fetchone()[0]
        finally:
            p_db.close()
            s_db.close()
        return {"unmanaged_tables": unmanaged, "dropped_columns": lost_cols}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# A SQLite file is THREE paths, not one. `-wal` holds committed pages not yet
# checkpointed into the main file, and it is replayed on the next open with no
# error and no signal. So every move of a store must move (or deliberately
# retire) its sidecars with it: a `-wal` left beside a DIFFERENT main file is
# silently applied to it. That is not a theoretical hazard -- a WAL carries its
# own page size, so replaying the wrong one can rewrite the file's content AND
# flip its page_size while `PRAGMA integrity_check` still answers `ok`.
_SIDECARS = ("-wal", "-shm")


def _move_sidecars(frm, to) -> list[str]:
    moved = []
    for side in _SIDECARS:
        s = Path(f"{frm}{side}")
        if s.exists():
            os.replace(s, f"{to}{side}")
            moved.append(side)
    return moved


def _drop_sidecars(base) -> list[str]:
    """Remove a store's sidecars. ONLY safe after a clean TRUNCATE checkpoint,
    which is what proves the WAL holds nothing the main file does not."""
    gone = []
    for side in _SIDECARS:
        p = Path(f"{base}{side}")
        if p.exists():
            p.unlink()
            gone.append(side)
    return gone


def _checkpoint(path) -> tuple[bool, str]:
    """Fold committed WAL pages into the main file and truncate the WAL.

    Run on the rebuild immediately before it is promoted. Without it, anything
    that opened `<store>.new` write-capable between the rebuild and the swap
    leaves committed pages in `<store>.new-wal`, and `os.replace(dst, src)`
    moves only the main file -- silently dropping them. Checkpointing first
    makes the main file self-contained, so the swap cannot lose data even if
    relocating the sidecar afterwards fails. Best-effort: a failure here is not
    a reason to refuse, because the sidecar relocation below still preserves
    the pages.

    Returns (complete, detail). `complete` is True only when SQLite reports
    busy=0, i.e. every frame was applied -- the one condition under which the
    sidecars may then be deleted.
    """
    from mcpbrain.store import _open_db
    try:
        db = _open_db(path)
    except sqlite3.Error as exc:
        return False, f"could not open to checkpoint: {exc}"
    try:
        row = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        busy = row[0] if row else 1
        return not busy, f"wal_checkpoint(TRUNCATE)={tuple(row) if row else ()}"
    except sqlite3.Error as exc:
        return False, f"checkpoint failed: {exc}"
    finally:
        db.close()


def _swap(src: Path, dst: Path) -> int:
    """Promote dst over src, RETAINING src (and its sidecars) alongside."""
    if not dst.exists():
        print(f"[optimise] nothing to swap: {dst} does not exist")
        return 2
    # Freshness, not just integrity: has src moved since the rebuild that
    # produced dst last read it? A finished rebuild can sit for arbitrarily
    # long waiting for --swap -- nothing here expires one or re-checks src in
    # the meantime -- so without this, "rebuild, daemon restarts and ingests
    # for days, --swap --yes" would silently discard everything written
    # since, with every OTHER gate (integrity_check, foreign_key_check)
    # reporting clean.
    from mcpbrain.store import _open_db
    d_db = _open_db(dst, read_only=True)
    try:
        stamped_row = d_db.execute(
            "SELECT v FROM meta WHERE k=?", (_FRESHNESS_META_KEY,)).fetchone()
    finally:
        d_db.close()
    if stamped_row:
        stamped = json.loads(stamped_row[0])
        s_db = _open_db(src, read_only=True)
        try:
            current = _freshness_snapshot(s_db)
        finally:
            s_db.close()
        if current != stamped:
            moved = {t: {"now": current[t], "at_rebuild": stamped[t]}
                     for t in _FRESHNESS_TABLES if current[t] != stamped[t]}
            print(f"[optimise] REFUSING to swap: {src} has changed since the "
                  f"rebuild was taken -- row counts moved: {moved}. Re-run "
                  "the rebuild against the current store, or make sure "
                  "nothing writes to src between the rebuild and --swap "
                  "(stop the daemon for the whole window, not just at "
                  "--swap time).")
            return 1
    else:
        print(f"[optimise] {dst.name} carries no freshness stamp (built by "
              "an older version of this tool) -- cannot verify src has not "
              "moved since the rebuild. Proceeding, but re-running the "
              "rebuild is safer.")
    # Checkpoint BEFORE verifying, so integrity_check reads the content the
    # promoted file will actually have, not a pre-WAL view of it.
    print(f"[optimise] {dst.name}: {_checkpoint(dst)[1]}")
    ok, detail = _integrity(dst)
    print(f"[optimise] re-verify {dst.name}: {detail}")
    if not ok:
        print("[optimise] REFUSING to swap: the rebuilt file does not verify")
        return 1
    kept = Path(f"{src}.pre-rebuild-{int(time.time())}")
    if kept.exists():
        print(f"[optimise] REFUSING to swap: {kept} already exists")
        return 1
    # The old store's sidecars travel WITH it, so nothing of its is left beside
    # the promoted rebuild -- and so a later --rollback has them to restore.
    _move_sidecars(src, kept)
    os.replace(src, kept)
    os.replace(dst, src)
    # Belt and braces for the checkpoint above: if it could not truncate, the
    # rebuild's own committed pages are still in dst's WAL, and they must follow
    # the main file to its new name or they are dropped.
    _move_sidecars(dst, src)
    print(f"[optimise] swapped. OLD STORE RETAINED at {kept}\n"
          f"[optimise] roll back with (NOT a bare mv -- see below):\n"
          f"[optimise]   uv run python bin/optimise_store.py "
          f"--src {src} --rollback --yes\n"
          f"[optimise] delete the retained file only after the gold gate passes:\n"
          f"[optimise]   uv run python tests/eval/run_eval.py --gold --k 10")
    return 0


def _is_sidecar(p: Path) -> bool:
    """True for one of SQLite's own `-wal`/`-shm` sidecar names.

    ONE predicate, used both when `_find_retained` picks a candidate itself and
    when the operator names one with `--from`. Two independent checks would
    drift, and the automatic path having a filter the explicit path lacks is
    precisely the gap that let a sidecar through `--from`.
    """
    return p.name.endswith(_SIDECARS)


# A real store has a full schema and, on the live machine, ~640k pages. A fresh
# init() is already 114. So this rejects the degenerate cases -- a 0-byte file,
# which SQLite opens as a perfectly valid EMPTY database, and any stub -- while
# staying an order of magnitude below the smallest legitimate store.
_MIN_STORE_PAGES = 16


def _refuse_as_store(p: Path) -> str | None:
    """Why `p` must not be promoted over the live store, or None if it may be.

    `--rollback --from` promotes an arbitrary operator-supplied path over the
    live store, and the three retained generations (`.pre-rebuild-*`,
    `.rolled-back-*`, and their `-wal`/`-shm`) share a common prefix -- so one
    tab-completion too many lands on a 0-byte sidecar. SQLite opens that as a
    valid, empty, zero-table database and `integrity_check` answers `ok`, so
    every gate downstream passes and the tool reports success having installed
    an EMPTY BRAIN over a real one. This is the one operation a stressed human
    runs against the live store after something has already gone wrong, so it
    refuses in code like every other gate here rather than warning in prose.
    """
    if _is_sidecar(p):
        return (f"{p.name} is a SQLite {p.name[-4:]} sidecar, not a store. A "
                "0-byte sidecar opens as a valid EMPTY database, so promoting "
                "one would install an empty brain over the real store. Pass "
                "the main .pre-rebuild-* file (no -wal/-shm suffix).")
    try:
        db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"{p.name} will not open as SQLite: {exc}"
    try:
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    except sqlite3.DatabaseError as exc:
        return f"{p.name} will not read as SQLite: {exc}"
    finally:
        db.close()
    if pages < _MIN_STORE_PAGES:
        return (f"{p.name} holds {pages} page(s) -- a store has at least "
                f"{_MIN_STORE_PAGES} (a fresh one, 114). This is an empty or "
                "truncated file, not a store.")
    if "chunks" not in tables:
        return (f"{p.name} has no `chunks` table ({len(tables)} table(s) "
                "present), so it is not an mcpbrain store.")
    return None


def _find_retained(src: Path) -> Path | None:
    """Newest <src>.pre-rebuild-* (the main file, never a sidecar)."""
    kept = [p for p in src.parent.glob(f"{src.name}.pre-rebuild-*")
            if not _is_sidecar(p)]
    return max(kept, key=lambda p: p.name) if kept else None


def _rollback(src: Path, frm: Path | None) -> int:
    """Restore a retained pre-rebuild store, sidecars and all.

    This exists because `mv <kept> <src>` -- the instruction this tool used to
    print -- SILENTLY CORRUPTS the restored store. By the time anyone wants to
    roll back, the daemon has been running against the promoted rebuild, so
    `<src>-wal` on disk belongs to the NEW store. Moving only the main file
    back leaves that foreign WAL in place and SQLite replays it into the old
    file on the next open: no error, `integrity_check` still `ok`, the old
    content gone and the page size flipped to the new store's. The safety net
    for the highest-stakes step in the plan cannot be a command that does that.

    So: the current store is moved aside WITH its sidecars (retained, not
    deleted -- nothing here destroys a store), which is what clears the foreign
    WAL, and then the retained file is restored WITH the sidecars that are
    genuinely its own.

    The candidate -- whether found automatically or named with `--from` -- must
    pass `_refuse_as_store` first; see there for why integrity_check alone is
    not a sufficient gate on what gets promoted over the live store.
    """
    kept = frm or _find_retained(src)
    if kept is None:
        print(f"[optimise] nothing to roll back to: no {src.name}"
              ".pre-rebuild-* beside the store")
        return 2
    if not kept.exists():
        print(f"[optimise] no such retained store: {kept}")
        return 2
    # BEFORE integrity_check, because integrity_check cannot tell the difference:
    # a 0-byte sidecar is a structurally perfect empty database.
    if refusal := _refuse_as_store(kept):
        print(f"[optimise] REFUSING to roll back: {refusal}")
        return 2
    # check_fk=False: a pre-rebuild store has dangling references BY
    # DEFINITION (256 on the live store) -- removing them is what the rebuild
    # was for. Structural soundness is what matters for a restore.
    ok, detail = _integrity(kept, check_fk=False)
    print(f"[optimise] verify {kept.name}: {detail}")
    if not ok:
        print("[optimise] REFUSING to roll back: the retained store does not "
              "verify")
        return 1
    aside = Path(f"{src}.rolled-back-{int(time.time())}")
    if aside.exists():
        print(f"[optimise] REFUSING to roll back: {aside} already exists")
        return 1
    if src.exists():
        # Its WAL is the one that must not survive beside the restored file.
        moved = _move_sidecars(src, aside)
        os.replace(src, aside)
        print(f"[optimise] current store moved aside to {aside}"
              f"{' (+' + ','.join(moved) + ')' if moved else ''}")
    os.replace(kept, src)
    restored = _move_sidecars(kept, src)
    # Fold the restored store's OWN WAL into its main file and, once that is
    # complete, leave a single self-contained file behind. Not tidiness: it
    # means the post-rollback state is unambiguous. Otherwise a `-wal` beside
    # the store could be either the restored store's own or a leftover of the
    # one just rolled back from, and nothing on disk distinguishes them.
    complete, detail = _checkpoint(src)
    ok, integrity = _integrity(src, check_fk=False)
    # AFTER the verification, not before: opening the file re-creates an (empty)
    # -shm/-wal pair, so dropping first would leave them behind again. Gated on
    # a complete checkpoint AND a clean read -- the two things that prove the
    # main file holds everything, so there is nothing in a sidecar to lose.
    dropped = _drop_sidecars(src) if complete and ok else []
    print(f"[optimise] rolled back {kept.name} -> {src.name}"
          f"{' (+' + ','.join(restored) + ')' if restored else ''}\n"
          f"[optimise] {detail}"
          f"{'; sidecars folded in and removed' if dropped else ''}\n"
          f"[optimise] restored store: {integrity}")
    return 0 if ok else 1


def _print_report(r: dict) -> None:
    print(f"[optimise] dim={r['dim']}  {r['src_bytes']:,} B -> "
          f"{r['dst_bytes']:,} B  "
          f"({100 * r['dst_bytes'] / max(r['src_bytes'], 1):.1f}% of original)")
    print(f"[optimise] vectors copied: {r['vectors']}  "
          f"fts rows: {r['fts_rows']}")
    for label, key in (("dropped orphan rows", "dropped_rows"),
                       ("dropped dead columns (non-null values)", "dropped_columns"),
                       ("carried over verbatim (unmanaged tables)", "carried"),
                       ("nullified dangling self-refs", "nullified")):
        if r[key]:
            print(f"[optimise] {label}: {r[key]}")
    if r["requeued_embeddings"]:
        print(f"[optimise] WARNING: {r['requeued_embeddings']} chunks lost their "
              "vector and were reset to embedded=0 for re-embedding")


def _compare_counts(src: Path, dst: Path, r: dict) -> bool:
    """Every source row is copied, dropped as an orphan, or carried. No slack.

    Also reconciles the two DERIVED tables that are not row-copied, because
    that is precisely where a loss would go unnoticed: a skipped vec_chunks
    or a half-finished FTS re-derive shows up in no `copied` count at all.
    """
    from mcpbrain.store import _open_db
    db = _open_db(src, read_only=True)
    ok = True
    try:
        for table, n in sorted({**r["copied"], **r["carried"]}.items()):
            total = db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            drop = r["dropped_rows"].get(table, 0)
            if n + drop != total:
                print(f"[optimise] MISMATCH {table}: src={total} copied={n} "
                      f"dropped={drop}")
                ok = False
            if drop and table not in _KEEP:
                print(f"[optimise] MISMATCH {table}: rows dropped from a table "
                      "with no referential filter")
                ok = False
        for table, drop in r["dropped_rows"].items():
            bound = sum(v for k, v in r["dropped"].items()
                        if k.split(".")[0] == table)
            if drop > bound:
                print(f"[optimise] MISMATCH {table}: dropped {drop} rows but "
                      f"only {bound} orphan references were reported")
                ok = False
        src_vecs = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
        if r["vectors"] != src_vecs:
            print(f"[optimise] MISMATCH vec_chunks: src={src_vecs} "
                  f"copied={r['vectors']}")
            ok = False
    finally:
        db.close()
    out = _open_db(dst, read_only=True)
    try:
        embedded = out.execute(
            "SELECT count(*) FROM chunks WHERE embedded=1").fetchone()[0]
        out_vecs = out.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
        if r["fts_rows"] != embedded:
            print(f"[optimise] MISMATCH fts_chunks: {r['fts_rows']} rows "
                  f"re-derived for {embedded} embedded chunks")
            ok = False
        if out_vecs != r["vectors"]:
            print(f"[optimise] MISMATCH vec_chunks on dst: {out_vecs} rows, "
                  f"{r['vectors']} copied")
            ok = False
    finally:
        out.close()
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", default=None, help="store to rebuild (default: live)")
    ap.add_argument("--dst", default=None, help="output (default: <src>.new)")
    ap.add_argument("--home", default=None)
    ap.add_argument("--page-size", type=int, default=8192)
    ap.add_argument("--yes", action="store_true",
                    help="proceed past the orphan/schema report")
    ap.add_argument("--swap", action="store_true",
                    help="promote <src>.new over <src>, retaining the old file")
    ap.add_argument("--rollback", action="store_true",
                    help="restore the retained pre-rebuild store, sidecars and "
                         "all -- NEVER do this with a bare mv")
    ap.add_argument("--from", dest="frm", default=None,
                    help="--rollback: which retained store (default: newest)")
    ns = ap.parse_args(argv)

    from mcpbrain import config
    home = Path(ns.home) if ns.home else Path(config.app_dir())
    src = Path(ns.src) if ns.src else Path(config.store_path())
    dst = Path(ns.dst) if ns.dst else Path(f"{src}.new")
    if ns.swap and ns.rollback:
        print("[optimise] --swap and --rollback are opposite operations; "
              "pick one")
        return 2
    if not src.exists() and not ns.rollback:
        print(f"[optimise] no such store: {src}")
        return 2

    # GATE 1 -- nothing else may be writing this store.
    lock, is_live, refusal = _acquire_exclusive(src)
    if refusal:
        print(f"[optimise] REFUSING: {refusal}")
        return 2
    try:
        print(f"[optimise] exclusive on {src}"
              f"{'' if is_live else ' (not the live store)'}")

        if ns.swap or ns.rollback:
            what = "--swap" if ns.swap else "--rollback"
            if not ns.yes:
                print(f"[optimise] {what} replaces the live store. Re-run with "
                      f"{what} --yes.")
                return 2
            if ns.swap:
                return _swap(src, dst)
            return _rollback(src, Path(ns.frm) if ns.frm else None)

        # GATE 2 -- a VERIFIED encrypted snapshot, before anything is written.
        # Pre-checked, because backup._verify_artifact REFUSES a store whose
        # embedded chunks have no vector: without this the snapshot raises a
        # bare traceback and the operator is left guessing, in the middle of a
        # deliberately careful attended operation.
        if missing := embedded_without_vectors(src):
            print(f"[optimise] REFUSING: {missing} chunks are flagged "
                  "embedded=1 but have no vector, and NO snapshot of this "
                  "store can be taken (backup._verify_artifact rejects it) -- "
                  "so the daemon's own backups are already failing too. Fix "
                  "first, then rebuild:\n"
                  f"[optimise]   sqlite3 {src} \"UPDATE chunks SET embedded=0 "
                  "WHERE embedded=1 AND rowid NOT IN (SELECT rowid FROM "
                  "vec_chunks)\"\n"
                  "[optimise]   uv run python bin/repair.py embed-pending --apply")
            return 2
        try:
            snap, key_path = _verified_snapshot(src, home)
        except Exception as exc:  # noqa: BLE001 -- attended CLI: no tracebacks
            print(f"[optimise] REFUSING: could not take a VERIFIED snapshot, "
                  f"so there is no rollback and the rebuild must not start: "
                  f"{type(exc).__name__}: {exc}")
            return 1
        print(f"[optimise] verified snapshot: {snap} ({snap.stat().st_size / 1e9:.3f} GB)")
        if key_path:
            print(f"[optimise] escrow key written to {key_path} -- KEEP IT, the "
                  "snapshot is worthless without it")

        # GATE 3 -- the report, then explicit consent.
        dim = _store_dim(src)
        orphans = report_orphans(src)
        pre = _schema_preflight(src, dim)
        print(f"[optimise] orphans (rows with a missing parent): {orphans} "
              f"total={sum(orphans.values())}")
        print(f"[optimise] tables the new schema does not define (carried over "
              f"verbatim): {pre['unmanaged_tables']}")
        print(f"[optimise] columns the new schema does not define (DROPPED, "
              f"non-null counts): {pre['dropped_columns']}")
        # Not a consent matter -- a pre-migration source cannot be rebuilt at
        # all (see UnmigratedStore), so --yes does not unlock it.
        if reasons := check_migrations(src):
            print("[optimise] REFUSING: the source has not finished init()'s "
                  "rename migrations, and rebuilding it would produce a store "
                  "that passes every gate and then cannot be opened:")
            for reason in reasons:
                print(f"[optimise]   - {reason}")
            return 2
        if not ns.yes:
            print("[optimise] report only. Re-run with --yes to rebuild.")
            return 0

        # GATE 4 -- rebuild out of place. The live file is never touched.
        if dst.exists():
            print(f"[optimise] REFUSING: {dst} already exists; move it aside")
            return 2
        t0 = time.time()
        try:
            r = rebuild(src, dst, page_size=ns.page_size)
        except Exception as exc:  # noqa: BLE001 -- attended CLI: no tracebacks
            print(f"[optimise] REBUILD FAILED: {type(exc).__name__}: {exc}")
            print(f"[optimise] the live store is UNTOUCHED and {snap} is your "
                  "verified rollback. A partial "
                  f"{dst.name} may remain -- delete it before retrying.")
            return 1
        print(f"[optimise] rebuilt {dst} in {time.time() - t0:.0f}s")
        _print_report(r)

        # GATE 5 -- integrity + FK on the result.
        ok, detail = _integrity(dst)
        print(f"[optimise] {detail}")

        # GATE 6 -- row counts reconcile against the orphan report.
        counts_ok = _compare_counts(src, dst, r)
        print(f"[optimise] row-count reconciliation: "
              f"{'ok' if counts_ok else 'MISMATCH'}")

        if not (ok and counts_ok):
            print(f"[optimise] NOT SAFE TO SWAP. {dst} is retained for "
                  "inspection; the live store is untouched.")
            return 1

        # GATE 7 -- the swap is a separate, explicit invocation. Never here.
        print(f"[optimise] OK. Nothing was swapped. To promote it:\n"
              f"[optimise]   uv run python bin/optimise_store.py "
              f"--src {src} --swap --yes")
        return 0
    finally:
        if lock is not None:
            lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
