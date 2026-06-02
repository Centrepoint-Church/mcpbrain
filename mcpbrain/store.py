import json
import sqlite3
from pathlib import Path

import sqlite_vec


def _open_db(path, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection to the derived store with sqlite-vec loaded.

    read_only=True uses a mode=ro URI (the MCP server's read path); read_only=False
    is the daemon's write path. row_factory is sqlite3.Row and the sqlite-vec
    extension is loaded so vec0 virtual tables resolve on connect. Shared by
    Store._connect and the backup checkpoint path.
    """
    if read_only:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)  # vec0 tables need the extension even on a read-only conn
    db.enable_load_extension(False)
    return db


class Store:
    def __init__(self, path: Path, dim: int, read_only: bool = False):
        self.path = Path(path)
        self.dim = dim
        self.read_only = read_only  # MCP server opens read-only; daemon is the sole writer

    def _connect(self) -> sqlite3.Connection:
        return _open_db(self.path, self.read_only)

    def init(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")  # concurrent reader (MCP) + one writer (daemon)
            db.execute("""CREATE TABLE IF NOT EXISTS chunks(
                rowid INTEGER PRIMARY KEY,
                doc_id TEXT UNIQUE, text TEXT, content_hash TEXT,
                metadata TEXT, embedded INTEGER DEFAULT 0,
                enriched INTEGER DEFAULT 0)""")
            db.execute(f"""CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks
                USING vec0(embedding float[{self.dim}])""")
            db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks
                USING fts5(text)""")
            db.execute("""CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)""")
            db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dim',?)", (str(self.dim),))
            db.execute("""CREATE TABLE IF NOT EXISTS sync_cursors(
                source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT)""")

            # --- enrichment graph tables (Task 4.2) -----------------------
            db.execute("""CREATE TABLE IF NOT EXISTS entities(
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL,
                org         TEXT DEFAULT '',
                first_seen  TEXT DEFAULT '',
                last_seen   TEXT DEFAULT '',
                mentions    INTEGER DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ent_type ON entities(type)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ent_org  ON entities(org)")

            db.execute("""CREATE TABLE IF NOT EXISTS entity_relations(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a      TEXT NOT NULL,
                relation      TEXT NOT NULL,
                entity_b      TEXT NOT NULL,
                source_doc_id TEXT DEFAULT '',
                UNIQUE(entity_a, relation, entity_b))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_a ON entity_relations(entity_a)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_b ON entity_relations(entity_b)")

            db.execute("""CREATE TABLE IF NOT EXISTS graph_actions(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                text          TEXT NOT NULL,
                owner         TEXT DEFAULT '',
                deadline      TEXT DEFAULT '',
                status        TEXT DEFAULT 'open',
                source_doc_id TEXT DEFAULT '',
                thread_id     TEXT DEFAULT '',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP)""")

            db.execute("""CREATE TABLE IF NOT EXISTS graph_decisions(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                text          TEXT NOT NULL,
                decided_on    TEXT DEFAULT '',
                source_doc_id TEXT DEFAULT '',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP)""")

            # --- entity merge audit (R4) ----------------------------------
            # IF NOT EXISTS so init() also back-fills the table on existing stores.
            db.execute("""CREATE TABLE IF NOT EXISTS entity_merge_log(
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                winner_id   TEXT NOT NULL,
                loser_id    TEXT NOT NULL,
                loser_name  TEXT DEFAULT '',
                method      TEXT NOT NULL,
                at          TEXT DEFAULT CURRENT_TIMESTAMP)""")

            # --- migration: add chunks.enriched to pre-existing stores --------
            # The real ~/.mcpbrain store predates the enriched column. Idempotent:
            # CREATE TABLE IF NOT EXISTS above won't alter an existing table, so
            # check PRAGMA table_info and ALTER if the column is missing.
            cols = {row["name"] for row in db.execute("PRAGMA table_info(chunks)").fetchall()}
            if "enriched" not in cols:
                db.execute("ALTER TABLE chunks ADD COLUMN enriched INTEGER DEFAULT 0")

    def upsert_chunk(self, doc_id, text, content_hash, metadata) -> None:
        with self._connect() as db:
            cur = db.execute("SELECT rowid, content_hash FROM chunks WHERE doc_id=?", (doc_id,))
            row = cur.fetchone()
            if row and row["content_hash"] == content_hash:
                return  # idempotent: unchanged
            if row:
                db.execute(
                    "UPDATE chunks SET text=?,content_hash=?,metadata=?,embedded=0,enriched=0 WHERE doc_id=?",
                    (text, content_hash, json.dumps(metadata), doc_id),
                )
            else:
                db.execute(
                    "INSERT INTO chunks(doc_id,text,content_hash,metadata) VALUES(?,?,?,?)",
                    (doc_id, text, content_hash, json.dumps(metadata)),
                )

    def mark_all_unembedded(self) -> None:
        with self._connect() as db:
            db.execute("UPDATE chunks SET embedded=0")

    def chunk_count(self) -> int:
        """Total number of rows in the chunks table."""
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def unembedded_chunks(self) -> list[dict]:
        with self._connect() as db:
            cur = db.execute(
                "SELECT rowid,doc_id,text,metadata FROM chunks WHERE embedded=0"
            )
            return [
                {
                    "rowid": r["rowid"],
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in cur.fetchall()
            ]

    def unenriched_chunks(self, limit: int | None = None) -> list[dict]:
        """Chunks not yet enriched into the graph (enriched=0).

        Independent of embedding: gated on enriched=0 only, not embedded.
        Shape mirrors unembedded_chunks(): {rowid, doc_id, text, metadata}.

        limit caps how many rows are returned (LIMIT in SQL, so the backlog is
        not fully loaded then sliced). limit=None returns every unenriched
        chunk — the no-arg behaviour existing callers/tests rely on.
        """
        sql = "SELECT rowid,doc_id,text,metadata FROM chunks WHERE enriched=0"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._connect() as db:
            cur = db.execute(sql, params)
            return [
                {
                    "rowid": r["rowid"],
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in cur.fetchall()
            ]

    def mark_enriched(self, doc_ids: list[str]) -> None:
        """Set enriched=1 for the given doc_ids (single connection, parameterised)."""
        if not doc_ids:
            return
        with self._connect() as db:
            db.executemany(
                "UPDATE chunks SET enriched=1 WHERE doc_id=?",
                [(d,) for d in doc_ids],
            )

    def write_embedding(self, rowid: int, vector: list[float]) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM vec_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES(?,?)",
                       (rowid, sqlite_vec.serialize_float32(vector)))
            db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO fts_chunks(rowid, text) "
                       "SELECT rowid, text FROM chunks WHERE rowid=?", (rowid,))
            db.execute("UPDATE chunks SET embedded=1 WHERE rowid=?", (rowid,))

    def vec_knn(self, query_vec: list[float], k: int) -> list[tuple[str, float]]:
        with self._connect() as db:
            cur = db.execute(
                "SELECT c.doc_id, v.distance FROM vec_chunks v "
                "JOIN chunks c ON c.rowid=v.rowid "
                "WHERE v.embedding MATCH ? AND k=? ORDER BY v.distance",
                (sqlite_vec.serialize_float32(query_vec), k))
            return [(r["doc_id"], r["distance"]) for r in cur.fetchall()]

    def fts_search(self, query: str, k: int) -> list[tuple[str, float]]:
        with self._connect() as db:
            cur = db.execute(
                "SELECT c.doc_id, bm25(fts_chunks) AS rank FROM fts_chunks "
                "JOIN chunks c ON c.rowid=fts_chunks.rowid "
                "WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                (query, k))
            return [(r["doc_id"], r["rank"]) for r in cur.fetchall()]

    def get_chunk(self, doc_id: str) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT doc_id,text,metadata FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
            return {"doc_id": r["doc_id"], "text": r["text"], "metadata": json.loads(r["metadata"])} if r else None

    def get_cursor(self, source: str) -> str | None:
        with self._connect() as db:
            r = db.execute("SELECT cursor FROM sync_cursors WHERE source=?", (source,)).fetchone()
            return r["cursor"] if r else None

    def set_cursor(self, source: str, cursor: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO sync_cursors(source, cursor, updated_at) VALUES(?,?,datetime('now')) "
                "ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, updated_at=datetime('now')",
                (source, cursor))

    # --- generic meta accessors -------------------------------------------

    def set_meta(self, k: str, v: str) -> None:
        """Insert or replace a key/value pair in the meta table."""
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))

    def get_meta(self, k: str) -> str | None:
        """Return the value for key k, or None if absent."""
        with self._connect() as db:
            r = db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
            return r["v"] if r else None

    # --- enrichment graph writers (Task 4.2) ------------------------------

    def upsert_entity(self, ent_id, name, entity_type, org="", seen="") -> bool:
        """Insert or merge an entity. Returns True if a NEW row was created,
        False if an existing entity was merged into."""
        # Single-writer invariant: this SELECT + upsert is race-free only because
        # the daemon is the sole writer. Do not call concurrently.
        with self._connect() as db:
            existed = db.execute(
                "SELECT 1 FROM entities WHERE id=?", (ent_id,)).fetchone() is not None
            db.execute(
                """INSERT INTO entities(id, name, type, org, first_seen, last_seen, mentions)
                   VALUES(?,?,?,?,?,?,1)
                   ON CONFLICT(id) DO UPDATE SET
                       first_seen = CASE WHEN entities.first_seen = '' THEN excluded.first_seen ELSE entities.first_seen END,
                       last_seen  = MAX(last_seen, excluded.last_seen),
                       mentions   = mentions + 1,
                       name       = CASE WHEN name = '' THEN excluded.name ELSE name END,
                       org        = CASE WHEN org  = '' THEN excluded.org  ELSE org  END,
                       type       = CASE WHEN type = 'unknown' THEN excluded.type ELSE type END""",
                (ent_id, name, entity_type, org, seen, seen))
            return not existed

    def add_relation(self, entity_a, relation, entity_b, source_doc_id="") -> bool:
        """Insert a relation triple. Returns True if a new row was inserted,
        False if the (entity_a, relation, entity_b) triple already existed."""
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO entity_relations(entity_a, relation, entity_b, source_doc_id) "
                "VALUES(?,?,?,?)",
                (entity_a, relation, entity_b, source_doc_id))
            return cursor.rowcount == 1

    def merge_entities(self, loser_id, winner_id, *, canonical_name=None,
                       method="deterministic") -> None:
        """Fold loser into winner, keeping the winner's id stable.

        The winner id is never re-slugged: relations reference ids, so changing
        it would orphan them. Relations are repointed onto the winner with
        UPDATE OR IGNORE (the UNIQUE(entity_a,relation,entity_b) constraint drops
        rows that would duplicate an existing winner triple); leftover loser rows
        and any self-loops the repoint created are then deleted. Scalars take the
        winner's value unless it's a stub (org "" / "unknown", type "unknown"),
        in which case the loser's value wins; mentions are summed. One
        transaction. Loser==winner or a missing id is a no-op.
        """
        if loser_id == winner_id:
            return
        with self._connect() as db:
            loser = db.execute(
                "SELECT name,type,org,mentions FROM entities WHERE id=?", (loser_id,)).fetchone()
            win = db.execute(
                "SELECT name,type,org,mentions FROM entities WHERE id=?", (winner_id,)).fetchone()
            if loser is None or win is None:
                return
            # Repoint relations onto the winner; UPDATE OR IGNORE drops rows that
            # would collide with an existing winner triple (the UNIQUE index).
            db.execute("UPDATE OR IGNORE entity_relations SET entity_a=? WHERE entity_a=?",
                       (winner_id, loser_id))
            db.execute("UPDATE OR IGNORE entity_relations SET entity_b=? WHERE entity_b=?",
                       (winner_id, loser_id))
            # Rows still on the loser are the ignored duplicates -> delete them.
            db.execute("DELETE FROM entity_relations WHERE entity_a=? OR entity_b=?",  # admin-delete-ok
                       (loser_id, loser_id))
            # Drop any self-loop the merge produced. Scoped to the winner: the
            # only self-loops a repoint can create have winner on both sides, so
            # this never sweeps unrelated rows.
            db.execute(
                "DELETE FROM entity_relations WHERE entity_a=entity_b AND entity_a=?",  # admin-delete-ok
                (winner_id,),
            )

            new_org = win["org"] if win["org"] not in ("", "unknown") else loser["org"]
            new_type = win["type"] if win["type"] != "unknown" else loser["type"]
            new_name = canonical_name or win["name"]
            new_mentions = (win["mentions"] or 0) + (loser["mentions"] or 0)
            db.execute("UPDATE entities SET name=?,type=?,org=?,mentions=? WHERE id=?",
                       (new_name, new_type, new_org, new_mentions, winner_id))
            db.execute(
                "INSERT INTO entity_merge_log(winner_id,loser_id,loser_name,method) "
                "VALUES(?,?,?,?)",
                (winner_id, loser_id, loser["name"], method))
            db.execute("DELETE FROM entities WHERE id=?", (loser_id,))  # admin-delete-ok

    def add_action(self, text, owner="", deadline="", status="open",
                   source_doc_id="", thread_id="") -> int:
        # No dedup: callers must ensure each doc is processed once (retries would duplicate).
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO graph_actions(text, owner, deadline, status, source_doc_id, thread_id) "
                "VALUES(?,?,?,?,?,?)",
                (text, owner, deadline, status, source_doc_id, thread_id))
            return cur.lastrowid

    def add_decision(self, text, decided_on="", source_doc_id="") -> int:
        # No dedup: callers must ensure each doc is processed once (retries would duplicate).
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO graph_decisions(text, decided_on, source_doc_id) VALUES(?,?,?)",
                (text, decided_on, source_doc_id))
            return cur.lastrowid

    # --- enrichment graph readers -----------------------------------------

    def get_entity(self, ent_id) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT * FROM entities WHERE id=?", (ent_id,)).fetchone()
            return dict(r) if r else None

    def find_entity(self, query: str) -> dict | None:
        """Resolve an entity by id, then by slug of a display name, then by name.

        Tries the literal query as an id first, then slugify(query) as an id
        (handles "Taryn Hamilton" -> "taryn-hamilton"), then a case-insensitive
        name match. Returns the entity dict or None.
        """
        hit = self.get_entity(query)
        if hit:
            return hit
        from mcpbrain.chunking import slugify  # chunking is dependency-free; avoids store->enrich coupling
        slug = slugify(query)
        if slug and slug != query:
            hit = self.get_entity(slug)
            if hit:
                return hit
        with self._connect() as db:
            r = db.execute(
                "SELECT * FROM entities WHERE lower(name)=lower(?) LIMIT 1", (query,)
            ).fetchone()
            return dict(r) if r else None

    def relations_for(self, ent_id: str) -> list[dict]:
        """All relations touching ent_id (as entity_a or entity_b)."""
        with self._connect() as db:
            cur = db.execute(
                "SELECT entity_a, relation, entity_b, source_doc_id FROM entity_relations "
                "WHERE entity_a=? OR entity_b=? ORDER BY id",
                (ent_id, ent_id),
            )
            return [dict(r) for r in cur.fetchall()]

    def actions_for_owner(self, owner: str) -> list[dict]:
        """All graph_actions owned by `owner` (case-insensitive)."""
        with self._connect() as db:
            cur = db.execute(
                "SELECT * FROM graph_actions WHERE lower(owner)=lower(?) ORDER BY id",
                (owner,),
            )
            return [dict(r) for r in cur.fetchall()]

    def list_entities(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM entities ORDER BY id").fetchall()]

    def list_relations(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM entity_relations ORDER BY id").fetchall()]

    def entities_for_resolution(self) -> list[dict]:
        """All entities as {id,name,type,org,mentions} dicts — input for the resolver."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT id,name,type,org,mentions FROM entities ORDER BY id").fetchall()]

    def list_entity_merges(self) -> list[dict]:
        """Merge audit rows in insertion order."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT winner_id,loser_id,loser_name,method,at FROM entity_merge_log "
                "ORDER BY id").fetchall()]

    def list_actions(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM graph_actions ORDER BY id").fetchall()]

    def list_decisions(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM graph_decisions ORDER BY id").fetchall()]

    def thread_chunks(self, thread_id: str) -> list[dict]:
        """Return all chunks whose metadata.thread_id matches thread_id.

        Each result is {doc_id, text, metadata} with metadata parsed from JSON.
        Order is not guaranteed — callers sort by date if needed.
        """
        with self._connect() as db:
            # json_extract over metadata is a full table scan; acceptable at
            # current corpus size. Add a generated-column index on thread_id if
            # thread queries grow slow.
            cur = db.execute(
                "SELECT doc_id, text, metadata FROM chunks "
                "WHERE json_extract(metadata, '$.thread_id') = ?",
                (thread_id,),
            )
            return [
                {
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": json.loads(r["metadata"]),
                }
                for r in cur.fetchall()
            ]
