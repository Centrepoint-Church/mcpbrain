import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sqlite_vec

# chunking is dependency-free; no store->enrich coupling. action_fingerprint is
# the single source of truth shared with graph_write so a text rewrite produces
# a fingerprint the near-duplicate guard recognises.
from mcpbrain.chunking import action_fingerprint as _action_fingerprint, slugify


# Enrichment-logic version. Bump when extraction rules/guards/model materially
# improve: chunks enriched under an older version are re-flowed (reflow_outdated_chunks)
# so the existing corpus re-extracts itself under current logic over time. Chunks
# enriched before this stamp existed default to 0, so shipping at 1 schedules a
# one-time gradual re-extraction of the whole already-enriched corpus.
ENRICH_LOGIC_VERSION = 1


def _fts_match_query(query: str) -> str:
    """Turn an arbitrary user string into a safe FTS5 MATCH expression.

    FTS5 treats characters like '-', ':', '"', '*', '(', ')' as query operators,
    so a raw query such as 'VERIFY-CAP-001' is parsed as a column filter and
    raises 'no such column: CAP'. We split on whitespace and wrap each token in
    double quotes (escaping embedded quotes by doubling), turning every token
    into a literal phrase joined with spaces (implicit AND). This preserves the
    previous keyword-AND behaviour while never raising on punctuation. Tokens
    that tokenise to nothing (e.g. a lone '-') simply match nothing.

    Returns '' when the query has no usable tokens; callers should treat an
    empty result as "no keyword matches" rather than passing it to MATCH.
    """
    quoted = ['"' + tok.replace('"', '""') + '"' for tok in query.split() if tok]
    return " ".join(quoted)


def store_dim_from_path(path) -> int | None:
    """Read the vector dim a store was built with from its meta table.

    Returns None when the file does not exist or has no dim row.
    """
    from pathlib import Path as _Path
    import sqlite3 as _sqlite3
    p = _Path(path)
    if not p.exists():
        return None
    try:
        db = _sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        row = db.execute("SELECT v FROM meta WHERE k='dim'").fetchone()
        db.close()
        return int(row[0]) if row else None
    except Exception:  # noqa: BLE001
        return None


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
    # Explicit busy_timeout so concurrent writers (daemon + MCP draft handle) serialise
    # via WAL with a bounded wait instead of failing immediately on a locked DB. WAL is
    # set once at init() and is a file-level setting, so every connection inherits it.
    db.execute("PRAGMA busy_timeout=5000")
    db.enable_load_extension(True)
    sqlite_vec.load(db)  # vec0 tables need the extension even on a read-only conn
    db.enable_load_extension(False)
    return db


class Store:
    def __init__(self, path: Path, dim: int, read_only: bool = False):
        self.path = Path(path)
        self.dim = dim
        self.read_only = read_only  # daemon is sole writer for index/graph/ingest tables;
        # the MCP server also opens a writable handle for draft_records (serialised via WAL)

    @contextmanager
    def _connect(self):
        """Open a connection, commit-or-rollback on exit, and ALWAYS close it.

        sqlite3.Connection is its own context manager but only commits/rollbacks
        — it does NOT close the file handle. Callers that did `with store._connect()
        as db:` leaked one OS fd per call. The control API polls /api/status every
        few seconds, so a long-running daemon exhausted its fd budget and started
        failing with `sqlite3.OperationalError: unable to open database file`.
        This wrapper keeps the existing commit/rollback semantics (via the inner
        `with db:`) and adds an unconditional close in the finally block.
        """
        db = _open_db(self.path, self.read_only)
        try:
            with db:
                yield db
        finally:
            db.close()

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
                mentions    INTEGER DEFAULT 0,
                email_count INTEGER DEFAULT 0,
                degree      INTEGER DEFAULT 0,
                aliases     TEXT DEFAULT '',
                email_addr  TEXT DEFAULT '',
                notes       TEXT DEFAULT '')""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ent_type ON entities(type)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ent_org  ON entities(org)")
            # Back-fill email_count/degree on pre-existing stores (mirrors the
            # chunks.enriched pattern below). Phase 1 does NOT port Nexus's
            # degree triggers (memory_db.py:486-515): graph_write.upsert_relation
            # (Task 2) increments degree explicitly instead.
            ent_cols = {row["name"] for row in db.execute("PRAGMA table_info(entities)").fetchall()}
            if "email_count" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN email_count INTEGER DEFAULT 0")
            if "degree" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN degree INTEGER DEFAULT 0")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ent_degree ON entities(degree)")
            # Dedup-support columns for the graph_write upsert path (Task 2.2):
            # email_addr (email->entity dedup), aliases (alias-merge dedup),
            # notes (accumulated free-text). Back-filled on pre-existing stores.
            if "aliases" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN aliases TEXT DEFAULT ''")
            if "email_addr" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN email_addr TEXT DEFAULT ''")
            if "notes" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN notes TEXT DEFAULT ''")
            if "profile" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN profile TEXT DEFAULT ''")
            if "profile_updated_at" not in ent_cols:
                db.execute("ALTER TABLE entities ADD COLUMN profile_updated_at TEXT DEFAULT ''")
            if "org_valid_from" not in ent_cols:
                # Date (email valid_from) the current org was asserted, so a
                # newer-dated observation can overwrite a stale org under backfill.
                db.execute("ALTER TABLE entities ADD COLUMN org_valid_from TEXT DEFAULT ''")
            if "profile_audited_at" not in ent_cols:
                # When profile_audit last reviewed this profile, so audit re-runs
                # only after a change (and rotates) rather than re-doing the same
                # most-active profiles every cycle.
                db.execute("ALTER TABLE entities ADD COLUMN profile_audited_at TEXT DEFAULT ''")

            db.execute("""CREATE TABLE IF NOT EXISTS entity_relations(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a      TEXT NOT NULL,
                relation      TEXT NOT NULL,
                entity_b      TEXT NOT NULL,
                source_doc_id TEXT DEFAULT '',
                UNIQUE(entity_a, relation, entity_b))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_a ON entity_relations(entity_a)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_b ON entity_relations(entity_b)")
            # --- entity_relations bitemporal columns (Phase 1, Task 1.6) --
            # Keep the existing UNIQUE(entity_a,relation,entity_b) + source_doc_id
            # (add_relation/merge_entities rely on them). ALTER the bitemporal
            # columns on (Spec 7 shape, memory_db.py:455-471). SQLite ALTER ADD
            # COLUMN cannot use non-constant defaults, so each column is nullable
            # or carries a constant default. Idempotent via PRAGMA check.
            er_cols = {row["name"] for row in db.execute("PRAGMA table_info(entity_relations)").fetchall()}
            for col_name, col_def in (
                ("valid_from", "TEXT"),
                ("valid_to", "TEXT"),
                ("invalidated_at", "TEXT"),
                ("invalidated_by_relation_id", "INTEGER"),
                ("superseded_reason", "TEXT"),
                ("confidence", "REAL DEFAULT 1.0"),
                ("evidence", "TEXT"),
                ("strength", "INTEGER DEFAULT 1"),
                ("last_seen", "TEXT"),
            ):
                if col_name not in er_cols:
                    db.execute(f"ALTER TABLE entity_relations ADD COLUMN {col_name} {col_def}")
            # 5 temporal indexes ported from memory_db.py:473-478.
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_valid_now ON entity_relations(entity_a, entity_b, relation) "
                       "WHERE invalidated_at IS NULL AND valid_to IS NULL")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_a_rel       ON entity_relations(entity_a, relation)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_b_rel       ON entity_relations(entity_b, relation)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_invalidated ON entity_relations(invalidated_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_er_valid_range ON entity_relations(valid_from, valid_to)")

            # graph_actions/graph_decisions are only created PRE-migration. Once
            # the Task 1.7 migration has renamed them to *_legacy (meta flag set),
            # CREATE TABLE IF NOT EXISTS would otherwise resurrect empty orphan
            # tables on every re-init, so guard on the flag.
            already_migrated = db.execute(
                "SELECT 1 FROM meta WHERE k='actions_migrated'").fetchone() is not None
            if not already_migrated:
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

            # --- unified actions table (Phase 1, Task 1.7) ----------------
            # Single forward surface for actions + recorded decisions. The
            # legacy graph_actions/graph_decisions tables are migrated in once
            # (below) and renamed to *_legacy; all new code uses this table.
            db.execute("""CREATE TABLE IF NOT EXISTS actions(
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                text             TEXT NOT NULL,
                owner            TEXT DEFAULT '',
                owner_entity_id  TEXT DEFAULT '',
                status           TEXT DEFAULT 'open',
                deadline         TEXT DEFAULT '',
                org              TEXT DEFAULT '',
                project_id       TEXT DEFAULT '',
                area_id          TEXT DEFAULT '',
                confidence       REAL DEFAULT 1.0,
                source           TEXT DEFAULT 'email',
                context_tag      TEXT DEFAULT '',
                cluster_id       TEXT DEFAULT '',
                source_doc_id    TEXT DEFAULT '',
                thread_id        TEXT DEFAULT '',
                resolved_by      TEXT DEFAULT '',
                resolved_at      TEXT DEFAULT '',
                text_fingerprint TEXT DEFAULT '',
                snoozed_until    TEXT DEFAULT '',
                created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at       TEXT DEFAULT CURRENT_TIMESTAMP)""")
            # Back-fill Phase 1 columns on pre-existing actions tables that were
            # created before the full column set was defined. Must run before the
            # index CREATE statements below, because idx_actions_status references
            # deadline and idx_actions_thread references thread_id.
            _act_p1_cols = {row["name"] for row in db.execute("PRAGMA table_info(actions)").fetchall()}
            for _col, _def in (
                ("owner_entity_id",  "TEXT DEFAULT ''"),
                ("deadline",         "TEXT DEFAULT ''"),
                ("org",              "TEXT DEFAULT ''"),
                ("project_id",       "TEXT DEFAULT ''"),
                ("area_id",          "TEXT DEFAULT ''"),
                ("confidence",       "REAL DEFAULT 1.0"),
                ("source",           "TEXT DEFAULT 'email'"),
                ("context_tag",      "TEXT DEFAULT ''"),
                ("cluster_id",       "TEXT DEFAULT ''"),
                ("source_doc_id",    "TEXT DEFAULT ''"),
                ("thread_id",        "TEXT DEFAULT ''"),
                ("resolved_by",      "TEXT DEFAULT ''"),
                ("resolved_at",      "TEXT DEFAULT ''"),
                ("text_fingerprint", "TEXT DEFAULT ''"),
                ("snoozed_until",    "TEXT DEFAULT ''"),
                ("updated_at",       "TEXT DEFAULT CURRENT_TIMESTAMP"),
            ):
                if _col not in _act_p1_cols:
                    db.execute(f"ALTER TABLE actions ADD COLUMN {_col} {_def}")
            db.execute("CREATE INDEX IF NOT EXISTS idx_actions_owner  ON actions(owner)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status, deadline)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_actions_thread ON actions(thread_id)")

            # One-time migration: copy graph_actions/graph_decisions into actions,
            # then rename the originals to *_legacy. Guarded by a meta flag so
            # re-running init() is a no-op (no duplicate rows, no re-rename).
            migrated = db.execute(
                "SELECT v FROM meta WHERE k='actions_migrated'").fetchone()
            if migrated is None:
                ga_exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_actions'"
                ).fetchone() is not None
                gd_exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_decisions'"
                ).fetchone() is not None
                if ga_exists:
                    db.execute(
                        "INSERT INTO actions(text, owner, deadline, status, "
                        "source_doc_id, thread_id, created_at, source) "
                        "SELECT text, owner, deadline, status, source_doc_id, "
                        "thread_id, created_at, 'email' FROM graph_actions")
                    db.execute("ALTER TABLE graph_actions RENAME TO graph_actions_legacy")
                if gd_exists:
                    # decided_on is deliberately NOT mapped to deadline: a
                    # recorded decision is a past event, and putting its date in
                    # deadline would make it surface as a stalled action (the
                    # Nexus decided_at/deadline split, migrations.py:967-972).
                    db.execute(
                        "INSERT INTO actions(text, source_doc_id, created_at, "
                        "source, status, owner) "
                        "SELECT text, source_doc_id, created_at, "
                        "'decision', 'recorded', '' FROM graph_decisions")
                    db.execute("ALTER TABLE graph_decisions RENAME TO graph_decisions_legacy")
                db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('actions_migrated','1')")

            # --- email_context + doc_context (Phase 1, Task 1.2) ----------
            # email_context ports doc_db.py:124-143 and folds the two triage
            # signals (reply_needed, reply_reason) on, since mcpbrain has no
            # separate triage surface. doc_context mirrors the same context
            # columns for Drive chunks, keyed by doc_id.
            db.execute("""CREATE TABLE IF NOT EXISTS email_context(
                message_id   TEXT PRIMARY KEY,
                subject      TEXT DEFAULT '',
                sender       TEXT DEFAULT '',
                sender_email TEXT DEFAULT '',
                sender_id    TEXT DEFAULT '',
                date_str     TEXT DEFAULT '',
                date_iso     TEXT DEFAULT '',
                thread_id    TEXT DEFAULT '',
                org          TEXT DEFAULT '',
                content_type TEXT DEFAULT '',
                summary      TEXT DEFAULT '',
                topics       TEXT DEFAULT '',
                enriched_at  TEXT DEFAULT '',
                labels       TEXT DEFAULT '',
                contextual_summary TEXT,
                reply_needed INTEGER DEFAULT 0,
                reply_reason TEXT DEFAULT '')""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ec_org    ON email_context(org)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ec_date   ON email_context(date_iso)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ec_thread ON email_context(thread_id)")

            # email->entity link table (Phase 1, Task 2.5). Ported from
            # doc_db.py:146-153. Keyed (message_id, entity_id); role records how
            # the entity appears (sender / mentioned / about). graph_write.apply
            # writes these via link_email_entity.
            db.execute("""CREATE TABLE IF NOT EXISTS email_entities(
                message_id TEXT NOT NULL,
                entity_id  TEXT NOT NULL,
                role       TEXT DEFAULT '',
                PRIMARY KEY (message_id, entity_id))""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ee_entity  ON email_entities(entity_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_ee_message ON email_entities(message_id)")

            # --- entity_observations (Phase 1, Task 1.3) ------------------
            # Bi-temporal role-provenance table ported verbatim from
            # memory_db.py:430-451 (Spec 7 shape: valid_to + REAL confidence +
            # confidence_source + invalidation chain).
            db.execute("""CREATE TABLE IF NOT EXISTS entity_observations(
                id                            INTEGER PRIMARY KEY,
                entity_id                     TEXT NOT NULL REFERENCES entities(id),
                attribute                     TEXT NOT NULL,
                value                         TEXT,
                source                        TEXT,
                valid_from                    TEXT,
                valid_to                      TEXT,
                confidence                    REAL DEFAULT 1.0,
                confidence_source             TEXT DEFAULT 'pipeline_snapshot',
                invalidated_at                TEXT,
                invalidated_by_observation_id INTEGER REFERENCES entity_observations(id),
                created_at                    DATETIME DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_eo_entity   ON entity_observations(entity_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_eo_valid_to ON entity_observations(valid_to)")
            # Non-unique partial index: multiple sources can share the same
            # (entity_id, attribute) at once; rank is resolved at read time.
            db.execute("CREATE INDEX IF NOT EXISTS idx_eo_entity_attr "
                       "ON entity_observations(entity_id, attribute) WHERE valid_to IS NULL")

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
            if "enriched_version" not in cols:
                # The enrichment-logic version a chunk was last enriched under.
                # Chunks below ENRICH_LOGIC_VERSION are re-flowed for re-extraction
                # under current rules/guards. Pre-existing chunks default to 0.
                db.execute("ALTER TABLE chunks ADD COLUMN enriched_version INTEGER DEFAULT 0")

            # --- Phase 3, Task 0.1: entity_communities + community_summaries --
            # Community membership table: one row per (entity, level). PK is
            # (entity_id, level) so an entity can appear in different communities
            # at different hierarchy levels. idx_ec_community supports fast
            # lookup of all members in a community.
            db.execute("""CREATE TABLE IF NOT EXISTS entity_communities (
                entity_id    TEXT NOT NULL,
                community_id INTEGER NOT NULL,
                level        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (entity_id, level)
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_ec_community "
                "ON entity_communities(community_id, level)"
            )
            # Community summary table: one row per (community_id, level) with
            # aggregated metadata written by the community-detection pipeline.
            db.execute("""CREATE TABLE IF NOT EXISTS community_summaries (
                community_id INTEGER NOT NULL,
                level        INTEGER NOT NULL DEFAULT 0,
                title        TEXT DEFAULT '',
                summary      TEXT DEFAULT '',
                member_count INTEGER DEFAULT 0,
                key_entities TEXT DEFAULT '',
                updated      TEXT DEFAULT '',
                PRIMARY KEY (community_id, level)
            )""")

            # --- Phase 3, Task 0.2: thread_context ----------------------------
            # Per-thread context row written by the thread-summariser pipeline.
            # thread_id mirrors Gmail's thread identifier so joins to email_context
            # work without an intermediate key.
            db.execute("""CREATE TABLE IF NOT EXISTS thread_context (
                thread_id           TEXT PRIMARY KEY,
                subject             TEXT DEFAULT '',
                org                 TEXT DEFAULT '',
                email_count         INTEGER DEFAULT 0,
                participant_ids     TEXT DEFAULT '',
                summary             TEXT DEFAULT '',
                last_updated        TEXT DEFAULT '',
                contextual_summary  TEXT DEFAULT '',
                contextual_summary_at TEXT DEFAULT ''
            )""")
            # Existing DBs: add contextual_summary_at (stamp of when the
            # contextual_summary was written) so a thread re-summarises when it
            # gains messages after being summarised, not just when it has none.
            _tc_cols = {row["name"] for row in
                        db.execute("PRAGMA table_info(thread_context)").fetchall()}
            if "contextual_summary_at" not in _tc_cols:
                db.execute("ALTER TABLE thread_context ADD COLUMN contextual_summary_at TEXT DEFAULT ''")

            # --- Phase 3, Task 0.3: proactive_findings ------------------------
            # Surface for pipeline-generated signals (overdue actions, missing
            # replies, stalled threads, etc.). UNIQUE(finding_type, ref_id) so
            # re-detecting the same signal upserts in place rather than
            # accumulating duplicates.
            db.execute("""CREATE TABLE IF NOT EXISTS proactive_findings (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                finding_type TEXT NOT NULL,
                ref_id       TEXT NOT NULL DEFAULT '',
                org          TEXT DEFAULT '',
                summary      TEXT DEFAULT '',
                detail       TEXT DEFAULT '',
                severity     TEXT DEFAULT 'info',
                detected_at  TEXT DEFAULT '',
                resolved_at  TEXT,
                UNIQUE(finding_type, ref_id)
            )""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_pf_type "
                "ON proactive_findings(finding_type)"
            )
            # Partial index: only open (unresolved) findings. Used by the MCP
            # tool to scan live signals efficiently.
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_pf_open "
                "ON proactive_findings(finding_type) WHERE resolved_at IS NULL"
            )

            # --- Session-4, Task 2.1: reversible entity suppression ------------
            # Presence of a row here is the entire suppression mechanism: the
            # entities row itself is never mutated or deleted, so suppression is
            # trivially reversible via unsuppress_entity (just delete this row).
            db.execute("""CREATE TABLE IF NOT EXISTS entity_suppressions (
                entity_id     TEXT PRIMARY KEY,
                reason        TEXT DEFAULT '',
                suppressed_at TEXT DEFAULT ''
            )""")

            # --- Session-4, Task 3.2: org-suggestion inbox ----------------------
            # Purely additive/inspectable: an org string the extractor keeps
            # seeing that isn't in the configured taxonomy. Never auto-applied
            # to config.json — a human (or a future dashboard/CLI) reviews these.
            db.execute("""CREATE TABLE IF NOT EXISTS org_suggestions (
                raw_org       TEXT PRIMARY KEY,
                reason        TEXT DEFAULT '',
                suggested_at  TEXT DEFAULT ''
            )""")

            # --- Phase 1 capture: change_log -----------------------------------
            db.execute("""CREATE TABLE IF NOT EXISTS change_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                change_type TEXT NOT NULL,
                source      TEXT DEFAULT '',
                ref_id      TEXT DEFAULT '',
                summary     TEXT DEFAULT '',
                detail      TEXT DEFAULT '',
                revert_ref  TEXT DEFAULT '',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP)""")
            # ORDER BY id DESC uses the PK — no secondary index needed.
            cl_cols = {row["name"] for row in db.execute(
                "PRAGMA table_info(change_log)").fetchall()}
            if "source" not in cl_cols:
                db.execute("ALTER TABLE change_log ADD COLUMN source TEXT DEFAULT ''")

            # --- Phase 3, Task 0.4: actions.waiting_on* columns ---------------
            # Back-fill waiting-on tracking onto the unified actions table.
            # Mirrors Nexus waiting_on columns (knowledge_db waiting_on_cleared_by_message_id)
            # with one rename: mcpbrain uses waiting_on_cleared_by_doc_id because
            # the reconcile trigger here is an arriving chunk (doc_id), not a
            # Gmail message row.
            act_cols = {row["name"] for row in db.execute("PRAGMA table_info(actions)").fetchall()}
            for col_name, col_def in (
                ("waiting_on",                    "TEXT"),
                ("waiting_on_entity_id",          "TEXT"),
                ("waiting_on_set_at",             "TEXT"),
                ("waiting_on_cleared_at",         "TEXT"),
                ("waiting_on_cleared_by_doc_id",  "TEXT"),
                ("reply_received",                "INTEGER DEFAULT 0"),
                # ClickUp two-way sync (2026-06-08): link anchor + priority.
                ("clickup_task_id",               "TEXT DEFAULT ''"),
                ("priority",                      "TEXT DEFAULT ''"),
                # Last-synced ClickUp closed-state for reopen detection
                # (2026-06-09). Nullable: NULL = never observed.
                ("clickup_closed",                "INTEGER"),
            ):
                if col_name not in act_cols:
                    db.execute(f"ALTER TABLE actions ADD COLUMN {col_name} {col_def}")
            # Partial index: open actions actively waiting on someone.
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_waiting "
                "ON actions(status, waiting_on_set_at) "
                "WHERE status='open' AND waiting_on IS NOT NULL"
            )

            # --- meeting_packs (Mac capability uplift) -----------------------
            db.execute("""CREATE TABLE IF NOT EXISTS meeting_packs(
                event_id       TEXT PRIMARY KEY,
                event_title    TEXT DEFAULT '',
                event_date     TEXT DEFAULT '',
                pack_text      TEXT DEFAULT '',
                attendees      TEXT DEFAULT '[]',
                built_at       TEXT DEFAULT '',
                cowork_session TEXT DEFAULT '',
                context_hash   TEXT DEFAULT '')""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_mp_date ON meeting_packs(event_date)")
            # context_hash (2026-06-16): a fingerprint of the inputs a pack was
            # built from, so the hourly meeting-packs task rebuilds a pack only
            # when its context actually changed. Back-filled on pre-existing stores.
            mp_cols = {row["name"] for row in db.execute("PRAGMA table_info(meeting_packs)").fetchall()}
            if "context_hash" not in mp_cols:
                db.execute("ALTER TABLE meeting_packs ADD COLUMN context_hash TEXT DEFAULT ''")

            # --- stale_reextract (Gap A re-extraction trigger, 2026-06-09) ----
            # Records that a thread was reset to enriched=0 for a fresh LLM
            # at-bat, keyed by a content signature so the same unchanged thread
            # is never re-triggered (would re-pay the re-extraction token cost).
            db.execute("""CREATE TABLE IF NOT EXISTS stale_reextract(
                thread_id    TEXT PRIMARY KEY,
                signature    TEXT NOT NULL,
                triggered_at TEXT NOT NULL
            )""")

            # --- draft_records (Mac capability uplift) ------------------------
            db.execute("""CREATE TABLE IF NOT EXISTS draft_records(
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id        TEXT DEFAULT '',
                thread_id       TEXT DEFAULT '',
                intent          TEXT DEFAULT '',
                audience_tier   TEXT DEFAULT '',
                draft_text      TEXT DEFAULT '',
                critique        TEXT DEFAULT '',
                voice_issues    TEXT DEFAULT '[]',
                samples_used    INTEGER DEFAULT 0,
                model           TEXT DEFAULT '',
                parent_draft_id INTEGER,
                refinement      TEXT DEFAULT '',
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_dr_email  ON draft_records(email_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_dr_thread ON draft_records(thread_id)")

            # --- S2 recall-acceptance feedback (Phase 0, 2026-06-22) ----------
            # recall_feedback: raw event log (one row per recall event).
            # chunk_quality:   per-chunk quality float updated by nightly aggregate.
            db.execute("""CREATE TABLE IF NOT EXISTS recall_feedback(
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id     TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                event_type TEXT NOT NULL,
                ts         TEXT DEFAULT '')""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rf_doc_id "
                "ON recall_feedback(doc_id)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_rf_ts "
                "ON recall_feedback(ts)")

            db.execute("""CREATE TABLE IF NOT EXISTS chunk_quality(
                doc_id     TEXT PRIMARY KEY,
                quality    REAL DEFAULT 1.0,
                exposures  INTEGER DEFAULT 0,
                uses       INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT '')""")

            # --- Q1 salience gate: enrich_state on chunks (Phase 0, 2026-06-22)
            # 'cold' = embedded/searchable but skip graph-extraction.
            # unenriched_chunks() excludes cold chunks so they don't re-queue.
            ch_cols = {row["name"] for row in
                       db.execute("PRAGMA table_info(chunks)").fetchall()}
            if "enrich_state" not in ch_cols:
                db.execute("ALTER TABLE chunks ADD COLUMN enrich_state TEXT DEFAULT ''")
            # --- Q8: per-chunk extraction attempt counter (2026-06-23) ---------
            # Bumped each time an extraction covering this chunk yields nothing.
            # After a cap the chunk is consumed (mark_enriched) so a genuinely
            # content-empty doc stops re-queuing+re-extracting forever.
            if "enrich_attempts" not in ch_cols:
                db.execute("ALTER TABLE chunks ADD COLUMN enrich_attempts INTEGER DEFAULT 0")
            # --- B3 salience (Phase 2, 2026-06-23) ----------------------------
            if "salience" not in ch_cols:
                db.execute("ALTER TABLE chunks ADD COLUMN salience REAL DEFAULT 0.0")
            # --- B2 memory tier + type (Phase 2, 2026-06-23) ------------------
            # tier: core/hot/warm/cold/''.  type: episodic/semantic/procedural
            if "memory_tier" not in ch_cols:
                db.execute("ALTER TABLE chunks ADD COLUMN memory_tier TEXT DEFAULT ''")
            if "memory_type" not in ch_cols:
                db.execute("ALTER TABLE chunks ADD COLUMN memory_type TEXT DEFAULT 'episodic'")

            # --- B5 decay: strength + last_accessed on chunk_quality (Phase 2) -
            cq_cols = {row["name"] for row in
                       db.execute("PRAGMA table_info(chunk_quality)").fetchall()}
            if "memory_strength" not in cq_cols:
                db.execute("ALTER TABLE chunk_quality ADD COLUMN memory_strength REAL DEFAULT 5.0")
            if "last_accessed" not in cq_cols:
                db.execute("ALTER TABLE chunk_quality ADD COLUMN last_accessed TEXT DEFAULT ''")

            # --- B6 voice suggestions table (Phase 2, 2026-06-23) -------------
            db.execute("""CREATE TABLE IF NOT EXISTS voice_suggestions(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                kind         TEXT NOT NULL,
                rule         TEXT NOT NULL,
                confidence   REAL DEFAULT 0.0,
                explanation  TEXT DEFAULT '',
                evidence_ids TEXT DEFAULT '[]',
                status       TEXT DEFAULT 'pending',
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                applied_at   TEXT DEFAULT '')""")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_vs_status ON voice_suggestions(status)")

            # --- B6 voice state table (Phase 2, 2026-06-23) ------------------
            db.execute("""CREATE TABLE IF NOT EXISTS voice_analyser_state(
                key        TEXT PRIMARY KEY,
                value      TEXT DEFAULT '',
                updated_at TEXT DEFAULT '')""")

    # --- S2 recall-acceptance feedback methods --------------------------------

    def record_recall_feedback(self, doc_id: str, session_id: str,
                               event_type: str, ts: str) -> None:
        """Append one recall feedback event row."""
        with self._connect() as db:
            db.execute(
                "INSERT INTO recall_feedback(doc_id,session_id,event_type,ts) VALUES(?,?,?,?)",
                (doc_id, session_id, event_type, ts))

    def record_recall_feedback_batch(self, rows: list[tuple]) -> None:
        """Append many recall feedback events in one transaction.

        rows: list of (doc_id, session_id, event_type, ts). One executemany so the
        recall hot path pays a single write, not one connection per chunk.
        """
        if not rows:
            return
        with self._connect() as db:
            db.executemany(
                "INSERT INTO recall_feedback(doc_id,session_id,event_type,ts) VALUES(?,?,?,?)",
                rows)

    def all_feedback_rows(self) -> list[dict]:
        """Return every recall_feedback row as {doc_id, event_type, ts}."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id,event_type,ts FROM recall_feedback ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_chunk_quality(self, doc_id: str) -> float:
        """Return the chunk's quality score (1.0 = neutral if not yet computed)."""
        with self._connect() as db:
            row = db.execute(
                "SELECT quality FROM chunk_quality WHERE doc_id=?", (doc_id,)).fetchone()
            return float(row["quality"]) if row else 1.0

    def update_chunk_quality(self, doc_id: str, quality: float,
                             exposures: int, uses: int) -> None:
        """Upsert the quality row for one chunk."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            db.execute(
                """INSERT INTO chunk_quality(doc_id,quality,exposures,uses,updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                       quality=excluded.quality,
                       exposures=excluded.exposures,
                       uses=excluded.uses,
                       updated_at=excluded.updated_at""",
                (doc_id, quality, exposures, uses, ts))

    # --- Q1 salience gate methods ----------------------------------------------

    def set_enrich_state(self, doc_ids: list[str], state: str) -> None:
        """Set enrich_state on the given chunks.

        state='cold'  → chunk stays embedded/searchable but skips graph-extraction.
        state=''      → reset to normal; chunk re-enters the extraction backlog.
        """
        if not doc_ids:
            return
        with self._connect() as db:
            db.executemany(
                "UPDATE chunks SET enrich_state=? WHERE doc_id=?",
                [(state, d) for d in doc_ids])

    def cold_chunk_count(self) -> int:
        """Number of chunks currently in the cold (gated) state."""
        with self._connect() as db:
            return db.execute(
                "SELECT COUNT(*) FROM chunks WHERE enrich_state='cold'").fetchone()[0]

    def bump_enrich_attempts(self, doc_ids: list[str]) -> int:
        """Increment the extraction-attempt counter for the given chunks (Q8).

        Returns the MAX attempt count across them after the bump, so the caller
        can decide whether the cap is reached. No-op (returns 0) for an empty list.
        """
        if not doc_ids:
            return 0
        with self._connect() as db:
            qmarks = ",".join("?" for _ in doc_ids)
            db.execute(
                f"UPDATE chunks SET enrich_attempts = COALESCE(enrich_attempts,0) + 1 "
                f"WHERE doc_id IN ({qmarks})", list(doc_ids))
            row = db.execute(
                f"SELECT MAX(COALESCE(enrich_attempts,0)) FROM chunks "
                f"WHERE doc_id IN ({qmarks})", list(doc_ids)).fetchone()
            return int(row[0] or 0)

    def email_mentions(self, *needles: str) -> bool:
        """True if any email chunk's text references one of the given strings.

        Used by the salience gate's optional Drive mention-check (a Drive doc is
        "worth enriching" when its file_id or file_name appears in an email). Scans
        gmail-source chunks with a LIKE for each non-empty needle; cheap at our
        email volume. Returns False on any error (fail-open is the caller's choice).
        """
        terms = [n.strip() for n in needles if n and n.strip() and len(n.strip()) >= 4]
        if not terms:
            return False
        clause = " OR ".join("text LIKE ?" for _ in terms)
        params = [f"%{t}%" for t in terms]
        sql = ("SELECT 1 FROM chunks "
               "WHERE json_extract(metadata,'$.source_type')='gmail' "
               f"AND ({clause}) LIMIT 1")
        try:
            with self._connect() as db:
                return db.execute(sql, params).fetchone() is not None
        except Exception:  # noqa: BLE001
            return False

    # --- Q4 org-backfill helper -----------------------------------------------

    def entities_without_org(self, limit: int | None = None) -> list[dict]:
        """Entities where org is empty (candidates for org_from_email backfill).

        Returns {id, name, type, email_addr} dicts, ordered by id.
        Only returns entities with a non-empty email_addr (needed for domain lookup).
        limit caps how many rows are returned; None = all.
        """
        sql = ("SELECT id,name,type,email_addr FROM entities "
               "WHERE (org='' OR org IS NULL) AND email_addr!='' "
               "ORDER BY id")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._connect() as db:
            rows = db.execute(sql).fetchall()
            return [dict(r) for r in rows]

    def update_entity_org(self, entity_id: str, org: str, org_valid_from: str = "") -> bool:
        """Set the org (and optionally org_valid_from) on one entity. Returns True if a row was actually updated."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE entities SET org=?, org_valid_from=? WHERE id=?",
                (org, org_valid_from, entity_id))
            return cur.rowcount > 0

    def update_entity_org_if_empty(self, entity_id: str, org: str) -> bool:
        """Set org on an entity only if it currently has none. Returns True if set.

        Used by write-time dedup: when a new mention with a real org redirects to
        an existing entity that lacks one, fill it without clobbering a known org.
        """
        if not org:
            return False
        with self._connect() as db:
            cur = db.execute(
                "UPDATE entities SET org=? WHERE id=? AND (org='' OR org IS NULL)",
                (org, entity_id))
            return cur.rowcount > 0

    def update_entity_email_if_empty(self, entity_id: str, email_addr: str) -> bool:
        """Set email_addr on an entity only if it currently has none. Returns True if set.

        Used by write-time dedup: when a new mention carries a header-sourced
        email and redirects to an existing entity that lacks one, fill it
        without clobbering a better-sourced address.
        """
        if not email_addr:
            return False
        with self._connect() as db:
            cur = db.execute(
                "UPDATE entities SET email_addr=? WHERE id=? AND (email_addr='' OR email_addr IS NULL)",
                (email_addr, entity_id))
            return cur.rowcount > 0

    def upsert_meeting_pack(self, event_id: str, event_title: str,
                            event_date: str, pack_text: str,
                            attendees: list | None = None,
                            cowork_session: str = "",
                            context_hash: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO meeting_packs(event_id, event_title, event_date, pack_text,
                       attendees, built_at, cowork_session, context_hash)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(event_id) DO UPDATE SET
                       event_title=excluded.event_title,
                       event_date=excluded.event_date,
                       pack_text=excluded.pack_text,
                       attendees=excluded.attendees,
                       built_at=excluded.built_at,
                       cowork_session=excluded.cowork_session,
                       context_hash=excluded.context_hash""",
                (event_id, event_title, event_date, pack_text,
                 json.dumps(attendees or []), now, cowork_session, context_hash))

    def get_meeting_pack(self, event_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM meeting_packs WHERE event_id=?", (event_id,)).fetchone()
            return dict(row) if row else None

    def pack_event_ids_for_date(self, date_iso: str) -> set:
        with self._connect() as db:
            rows = db.execute(
                "SELECT event_id FROM meeting_packs WHERE event_date=?",
                (date_iso,)).fetchall()
            return {r["event_id"] for r in rows}

    def save_draft(self, email_id: str, thread_id: str, intent: str,
                   audience_tier: str, draft_text: str, critique: str,
                   voice_issues: list, samples_used: int, model: str,
                   parent_draft_id: int | None = None,
                   refinement: str = "") -> int:
        with self._connect() as db:
            cur = db.execute(
                """INSERT INTO draft_records(email_id, thread_id, intent, audience_tier,
                       draft_text, critique, voice_issues, samples_used, model,
                       parent_draft_id, refinement)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (email_id, thread_id, intent, audience_tier, draft_text, critique,
                 json.dumps(voice_issues), samples_used, model,
                 parent_draft_id, refinement))
            return cur.lastrowid

    def get_draft(self, draft_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM draft_records WHERE id=?", (draft_id,)).fetchone()
            return dict(row) if row else None

    def upsert_chunk(self, doc_id, text, content_hash, metadata) -> bool:
        """Insert or update a chunk. Returns True when a row was inserted or its
        content changed, False when the call was a no-op (same content_hash).

        Callers use the return value as a single signal for "did this write
        anything", which closes the crash-retry gap where a read-then-write pair
        could double-count or silently drop a re-processed envelope.
        """
        with self._connect() as db:
            cur = db.execute("SELECT rowid, content_hash FROM chunks WHERE doc_id=?", (doc_id,))
            row = cur.fetchone()
            if row and row["content_hash"] == content_hash:
                return False  # idempotent: unchanged
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
            return True

    def mark_all_unembedded(self) -> None:
        with self._connect() as db:
            db.execute("UPDATE chunks SET embedded=0")

    def delete_calendar_chunks_after(self, iso_cutoff: str) -> int:
        """Delete calendar chunks whose start time is after iso_cutoff.

        Called when the calendar sync's forward horizon shrinks: events that
        were previously synced past the new horizon stay in the store unless
        we evict them, and they'd keep occupying embedding/enrichment slots.

        ISO-8601 dates sort lexicographically, so a string comparison on the
        stored metadata.start field is correct. Removes the row from chunks
        plus its mirrors in vec_chunks and fts_chunks (keyed on rowid). Graph
        rows that reference entities/actions from those chunks are NOT
        touched — the graph is canonical knowledge, the chunk is just one of
        the evidence sources. Returns the number of chunk rows deleted.
        """
        with self._connect() as db:
            cur = db.execute(
                "SELECT rowid FROM chunks "
                "WHERE json_extract(metadata,'$.source_type')='calendar' "
                "  AND json_extract(metadata,'$.start') > ?",
                (iso_cutoff,),
            )
            rowids = [r["rowid"] for r in cur.fetchall()]
            if not rowids:
                return 0
            placeholders = ",".join("?" * len(rowids))
            db.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({placeholders})", rowids)
            db.execute(f"DELETE FROM fts_chunks WHERE rowid IN ({placeholders})", rowids)
            db.execute(f"DELETE FROM chunks WHERE rowid IN ({placeholders})", rowids)
            return len(rowids)

    def chunk_count(self) -> int:
        """Total number of rows in the chunks table."""
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def enriched_count(self) -> int:
        """Number of chunks that have been enriched into the graph."""
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM chunks WHERE enriched=1").fetchone()[0]

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
        # Exclude 'cold' chunks: they are deliberately gated by the salience gate
        # and must not re-queue for extraction while in cold state.
        sql = ("SELECT rowid,doc_id,text,metadata FROM chunks "
               "WHERE enriched=0 AND COALESCE(enrich_state,'') != 'cold' "
               "ORDER BY rowid DESC")
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

    def mark_enriched(self, doc_ids: list[str], version: int = ENRICH_LOGIC_VERSION) -> None:
        """Set enriched=1 and stamp the enrichment-logic version for the given
        doc_ids, so reflow_outdated_chunks can later re-extract anything enriched
        under older logic."""
        if not doc_ids:
            return
        with self._connect() as db:
            db.executemany(
                "UPDATE chunks SET enriched=1, enriched_version=? WHERE doc_id=?",
                [(version, d) for d in doc_ids],
            )

    def reflow_outdated_chunks(self, version: int, cap: int) -> int:
        """Reset enriched=0 on up to `cap` enriched chunks whose enriched_version
        is below `version` (oldest first), so they re-flow through enrichment under
        current logic. The embedding is kept (only enriched is cleared). Returns the
        number reset. This is the change-driven re-extraction lever: bump
        ENRICH_LOGIC_VERSION when enrichment improves and the corpus re-extracts
        itself gradually as the daemon calls this each cycle."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id FROM chunks "
                "WHERE enriched=1 AND COALESCE(enriched_version,0) < ? "
                "ORDER BY rowid LIMIT ?",
                (version, cap),
            ).fetchall()
            ids = [r["doc_id"] for r in rows]
            if ids:
                qs = ",".join("?" for _ in ids)
                db.execute(f"UPDATE chunks SET enriched=0 WHERE doc_id IN ({qs})", ids)
        return len(ids)

    def embed_doc(self, doc_id: str, embedder) -> bool:
        """Embed a single chunk by doc_id, in place.

        Fetches the chunk's rowid + text, runs embedder.embed_passages on the
        contextual-prefixed passage (matching the index_pending batch path), and
        writes the vector via write_embedding (which also flips embedded=1 and
        refreshes the FTS row). Returns True on success, False if no such chunk.

        Used by graph_write.apply when an embedder is injected so the enriched
        semantic doc is searchable immediately; when no embedder is passed the
        chunk is left at embedded=0 for the daemon's index_pending pass.
        """
        from mcpbrain.embed import contextual_prefix
        with self._connect() as db:
            r = db.execute(
                "SELECT rowid, text, metadata FROM chunks WHERE doc_id=?",
                (doc_id,)).fetchone()
        if not r:
            return False
        metadata = json.loads(r["metadata"])
        passage = contextual_prefix(metadata) + r["text"]
        vector = embedder.embed_passages([passage])[0]
        self.write_embedding(r["rowid"], vector)
        return True

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
        match = _fts_match_query(query)
        if not match:
            return []
        with self._connect() as db:
            cur = db.execute(
                "SELECT c.doc_id, bm25(fts_chunks) AS rank FROM fts_chunks "
                "JOIN chunks c ON c.rowid=fts_chunks.rowid "
                "WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                (match, k))
            return [(r["doc_id"], r["rank"]) for r in cur.fetchall()]

    def get_chunk(self, doc_id: str) -> dict | None:
        with self._connect() as db:
            try:
                r = db.execute(
                    "SELECT doc_id,text,metadata,memory_tier FROM chunks WHERE doc_id=?",
                    (doc_id,)).fetchone()
                tier = (r["memory_tier"] or "") if r else ""
            except sqlite3.OperationalError:
                # Pre-Phase-2 schema (memory_tier not yet ALTERed in): a read-only
                # handle can hit this in the window between a wheel upgrade and the
                # daemon's init() migration. Degrade gracefully — recall must not crash.
                r = db.execute(
                    "SELECT doc_id,text,metadata FROM chunks WHERE doc_id=?",
                    (doc_id,)).fetchone()
                tier = ""
            if not r:
                return None
            return {
                "doc_id": r["doc_id"],
                "text": r["text"],
                "metadata": json.loads(r["metadata"]),
                "memory_tier": tier,
            }

    def patch_chunk_metadata(self, doc_id: str, **patch) -> bool:
        """Merge kwargs into a chunk's metadata JSON without touching content_hash or embedded.

        Returns True if the chunk exists and was updated, False if not found.
        Leaves content_hash and embedded untouched so an expiry flag (or any other
        metadata patch) does not re-queue embedding.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT metadata FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
            if row is None:
                return False
            meta = json.loads(row["metadata"])
            meta.update(patch)
            db.execute(
                "UPDATE chunks SET metadata=? WHERE doc_id=?",
                (json.dumps(meta), doc_id))
            return True

    def note_chunks(self, *, observation_type: str | None = None,
                    include_expired: bool = False, limit: int = 500) -> list[dict]:
        """Return capture-note chunks (doc_id starting with 'note-'), with parsed metadata.

        Excludes expired chunks (meta["expired"] is truthy) unless include_expired=True.
        Filters by observation_type if provided. Returns the newest `limit` live
        results (ORDER BY rowid DESC). The limit is applied AFTER the Python-side
        expired/observation_type filter, so a store full of expired notes never
        truncates live ones — we iterate the cursor and stop once `limit` live
        rows are collected rather than pre-truncating in SQL.
        """
        sql = ("SELECT doc_id, text, metadata FROM chunks "
               "WHERE doc_id LIKE 'note-%' ORDER BY rowid DESC")
        results = []
        with self._connect() as db:
            for r in db.execute(sql):
                try:
                    meta = json.loads(r["metadata"])
                except Exception:
                    continue
                if not include_expired and meta.get("expired"):
                    continue
                if observation_type is not None and meta.get("observation_type") != observation_type:
                    continue
                results.append({
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": meta,
                })
                if len(results) == limit:
                    break
        return results

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

    def upsert_topic_entity(self, tag: str) -> str | None:
        """Insert or bump a topic entity (id 'topic-<slug>', type 'topic').

        Ported from memory_db.py:1720-1745. Tags under 2 chars (after collapse)
        return None and write nothing. First insert sets email_count=1; each
        subsequent call bumps email_count and refreshes last_seen. Returns the
        entity id both times. Depends on the entities.email_count column (1.5).
        """
        tag = re.sub(r"\s+", " ", (tag or "").strip().lower())
        if len(tag) < 2:
            return None
        eid = slugify(f"topic-{tag}")
        if not eid:
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as db:
            existing = db.execute("SELECT 1 FROM entities WHERE id=?", (eid,)).fetchone()
            if existing:
                db.execute(
                    "UPDATE entities SET email_count = email_count + 1, last_seen = ? WHERE id = ?",
                    (today, eid))
            else:
                db.execute(
                    "INSERT INTO entities(id, name, type, org, first_seen, last_seen, email_count) "
                    "VALUES(?,?,'topic','',?,?,1)",
                    (eid, tag, today, today))
        return eid

    def add_relation(self, entity_a, relation, entity_b, source_doc_id="") -> bool:
        """Insert a relation triple. Returns True if a new row was inserted,
        False if the (entity_a, relation, entity_b) triple already existed."""
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO entity_relations(entity_a, relation, entity_b, source_doc_id) "
                "VALUES(?,?,?,?)",
                (entity_a, relation, entity_b, source_doc_id))
            return cursor.rowcount == 1

    def upsert_email_context(self, message_id, *, subject="", sender="",
                             sender_email="", sender_id="", date_str="",
                             date_iso="", thread_id="", org="", content_type="",
                             summary="", topics="", enriched_at="", labels="",
                             contextual_summary="", reply_needed=0,
                             reply_reason="") -> None:
        """Insert or update the email_context row for a message.

        Ported from doc_db.upsert_email_context (doc_db.py:406-444), with the two
        triage signals (reply_needed, reply_reason) folded on per the mcpbrain
        schema. The org is written as-passed: callers canonicalise via
        graph_write.canonical_org before calling (store stays decoupled from the
        org map). On conflict, the situational fields refresh; identity columns
        (sender, date) are left as first written.
        """
        with self._connect() as db:
            db.execute(
                """INSERT INTO email_context
                   (message_id, subject, sender, sender_email, sender_id, date_str,
                    date_iso, thread_id, org, content_type, summary, topics,
                    enriched_at, labels, contextual_summary, reply_needed, reply_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(message_id) DO UPDATE SET
                     org                = excluded.org,
                     content_type       = excluded.content_type,
                     summary            = excluded.summary,
                     topics             = excluded.topics,
                     enriched_at        = excluded.enriched_at,
                     labels             = excluded.labels,
                     contextual_summary = excluded.contextual_summary,
                     reply_needed       = excluded.reply_needed,
                     reply_reason       = excluded.reply_reason""",
                (message_id, subject, sender, sender_email, sender_id, date_str,
                 date_iso, thread_id, org, content_type, summary, topics,
                 enriched_at or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                 labels, contextual_summary, 1 if reply_needed else 0, reply_reason))

    def link_email_entity(self, message_id, entity_id, role="") -> bool:
        """Link an entity to a message. Ported from doc_db.py:447-453.

        Keyed (message_id, entity_id); INSERT OR IGNORE so re-linking the same
        pair is a no-op (role is set on first link only). Returns True when a new
        link row was created, False when the pair already existed — callers use
        this to drive email_count, so the link table is the single source of
        truth for how many distinct messages an entity appears in.
        """
        with self._connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO email_entities (message_id, entity_id, role) "
                "VALUES (?, ?, ?)",
                (message_id, entity_id, role))
            return cur.rowcount > 0

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

    # --- unified actions table (Task 1.7) ---------------------------------

    def add_unified_action(self, *, text, owner="", owner_entity_id="",
                           status="open", deadline="", org="", project_id="",
                           area_id="", confidence=1.0, source="email",
                           context_tag="", cluster_id="", source_doc_id="",
                           thread_id="", text_fingerprint="", waiting_on="",
                           waiting_on_entity_id="", waiting_on_set_at="",
                           created_at="", clickup_task_id="",
                           priority="") -> int:
        """Insert a row into the unified actions table. Returns the new id.

        waiting_on* mark an action as awaiting a reply from a person (cleared by
        the waiting-on reconciler when that person's chunk arrives). created_at,
        when given, overrides the CURRENT_TIMESTAMP default so a caller with an
        injected clock (apply's `now`) keeps the near-duplicate window aligned
        with the clock the gates use. clickup_task_id and priority, when given,
        are set atomically on insert so callers like import_baseline avoid a
        separate update_action_fields call.
        """
        cols = ["text", "owner", "owner_entity_id", "status", "deadline", "org",
                "project_id", "area_id", "confidence", "source", "context_tag",
                "cluster_id", "source_doc_id", "thread_id", "text_fingerprint",
                "waiting_on", "waiting_on_entity_id", "waiting_on_set_at"]
        vals = [text, owner, owner_entity_id, status, deadline, org, project_id,
                area_id, confidence, source, context_tag, cluster_id,
                source_doc_id, thread_id, text_fingerprint,
                waiting_on or None, waiting_on_entity_id or None,
                waiting_on_set_at or None]
        if created_at:
            cols.append("created_at")
            vals.append(created_at)
        if clickup_task_id:
            cols.append("clickup_task_id")
            vals.append(clickup_task_id)
        if priority:
            cols.append("priority")
            vals.append(priority)
        placeholders = ",".join("?" * len(cols))
        with self._connect() as db:
            cur = db.execute(
                f"INSERT INTO actions({','.join(cols)}) VALUES({placeholders})",
                vals)
            return cur.lastrowid

    def set_action_status(self, action_id: int, status: str,
                          resolved_by: str = "", *, thread_id: str | None = None,
                          only_if_open: bool = False) -> int:
        """Close or reopen a unified action. Returns the number of rows changed.

        Ported from knowledge_db/memory_db.update_action_status (memory_db.py:1346-1375),
        stripped of the ClickUp sync path (out of scope for Phase 1). Sets
        resolved_at when closing, clears it when reopening, and refreshes
        updated_at.

        thread_id / only_if_open scope the update: an LLM-supplied
        resolved_action_id should only close an OPEN action belonging to the
        thread that raised it, never an arbitrary or already-closed row. The
        rowcount lets the caller log a miss instead of silently re-stamping.
        """
        resolved_at = "" if status == "open" else datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        where = ["id = ?"]
        params: list = [status, resolved_by, resolved_at,
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        action_id]
        if only_if_open:
            where.append("status = 'open'")
        if thread_id is not None:
            where.append("thread_id = ?")
            params.append(thread_id)
        with self._connect() as db:
            cur = db.execute(
                f"UPDATE actions SET status = ?, resolved_by = ?, resolved_at = ?, "
                f"updated_at = ? WHERE {' AND '.join(where)}", params)
            return cur.rowcount

    def assign_action_owner(self, action_id: int, owner: str, owner_entity_id: str = "") -> bool:
        """Set owner/owner_entity_id on one action. Returns True if a row was actually updated.

        Used by the ownerless-action review applier (Session-4, Task 3.1): a
        rowcount check so a stale/nonexistent action_id (e.g. resolved or
        deleted between detection and verdict) is reported as a no-op miss
        rather than a phantom success.
        """
        with self._connect() as db:
            cur = db.execute(
                "UPDATE actions SET owner=?, owner_entity_id=? WHERE id=?",
                (owner, owner_entity_id, action_id))
            return cur.rowcount > 0

    def snooze_action(self, action_id: int, until_iso: str) -> bool:
        """Snooze an OPEN action until until_iso (a YYYY-MM-DD date).

        The dashboard listing hides a snoozed action until today reaches
        until_iso, then it reappears automatically. Only open actions can be
        snoozed: a missing or closed row is a no-op.

        until_iso must parse as an ISO date — date.fromisoformat raises
        ValueError on garbage, which the control API maps to a 400 (distinct
        from the 404 a missing/closed row returns False for). Returns True only
        when a row was updated, and records the prior snoozed_until in the
        change_log revert_ref so the snooze can be undone.
        """
        date.fromisoformat(until_iso)  # raises ValueError on a non-date string
        # The UPDATE's status='open' predicate, not a prior SELECT, decides
        # whether the row is snoozable — so a concurrent close on another
        # ThreadingHTTPServer thread can't be clobbered and a closed/missing
        # row never gains a snooze or a change_log entry. The prior-value read
        # for revert_ref shares the same connection (single SQLite write-lock
        # window), and record_change runs only when rowcount confirms a hit.
        with self._connect() as db:
            row = db.execute(
                "SELECT snoozed_until FROM actions WHERE id = ?",
                (action_id,)).fetchone()
            prev = (row["snoozed_until"] if row else "") or ""
            cur = db.execute(
                "UPDATE actions SET snoozed_until = ?, updated_at = ? "
                "WHERE id = ? AND status = 'open'",
                (until_iso,
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 action_id))
            if cur.rowcount == 0:
                return False
        self.record_change(
            "action_snoozed", ref_id=str(action_id),
            summary=f"Snoozed action {action_id} until {until_iso}",
            revert_ref=f"snoozed_until:{prev}", source="dashboard")
        return True

    def set_action_text(self, action_id: int, new_text: str, *,
                        thread_id: str | None = None,
                        only_if_open: bool = False) -> int:
        """Rewrite a unified action's text. Returns the number of rows changed.

        Ported from update_action_text (memory_db.py:1411-1421). Also refreshes
        the text_fingerprint so later near-duplicate checks see the new text, and
        bumps updated_at. thread_id / only_if_open scope the update the same way
        set_action_status does, for the same reason.
        """
        where = ["id = ?"]
        params: list = [new_text, _action_fingerprint(new_text),
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        action_id]
        if only_if_open:
            where.append("status = 'open'")
        if thread_id is not None:
            where.append("thread_id = ?")
            params.append(thread_id)
        with self._connect() as db:
            cur = db.execute(
                f"UPDATE actions SET text = ?, text_fingerprint = ?, updated_at = ? "
                f"WHERE {' AND '.join(where)}", params)
            return cur.rowcount

    # --- ClickUp sync (2026-06-08) ----------------------------------------

    def set_action_clickup_id(self, action_id: int, clickup_task_id: str) -> int:
        """Cache the linked ClickUp task id on an action. Returns rows changed."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE actions SET clickup_task_id = ?, updated_at = ? WHERE id = ?",
                (clickup_task_id,
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 action_id))
            return cur.rowcount

    def set_action_clickup_closed(self, action_id: int, closed: bool) -> int:
        """Record the last-observed ClickUp closed-state (bookkeeping for reopen
        detection). Stores 1/0; does not touch updated_at."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE actions SET clickup_closed=? WHERE id=?",
                (1 if closed else 0, action_id))
            return cur.rowcount

    def update_action_fields(self, action_id: int, **fields) -> int:
        """Update a whitelisted set of action columns in one statement.

        Used by the ClickUp inbound pass to mirror ClickUp edits (org, deadline,
        priority). Status/text have dedicated methods (they touch resolved_at /
        fingerprint); this handles the plain columns. Unknown keys are ignored.
        Always bumps updated_at. Returns rows changed.
        """
        allowed = {"org", "deadline", "priority", "clickup_task_id",
                   "owner", "owner_entity_id"}
        sets, params = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f"{k} = ?")
                params.append(v)
        if not sets:
            return 0
        sets.append("updated_at = ?")
        params.append(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        params.append(action_id)
        with self._connect() as db:
            cur = db.execute(
                f"UPDATE actions SET {', '.join(sets)} WHERE id = ?", params)
            return cur.rowcount

    def action_by_clickup_id(self, clickup_task_id: str) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT * FROM actions WHERE clickup_task_id = ?",
                           (clickup_task_id,)).fetchone()
            return dict(r) if r else None

    def get_stale_reextract(self, thread_id: str) -> dict | None:
        """Return the stale-reextract marker row for a thread, or None."""
        with self._connect() as db:
            r = db.execute(
                "SELECT thread_id, signature, triggered_at "
                "FROM stale_reextract WHERE thread_id=?",
                (thread_id,)).fetchone()
            return dict(r) if r else None

    def set_stale_reextract(self, thread_id: str, signature: str,
                            triggered_at: str) -> None:
        """Upsert the marker recording that `thread_id` was re-triggered at the
        given content `signature` and time."""
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO stale_reextract"
                "(thread_id, signature, triggered_at) VALUES(?,?,?)",
                (thread_id, signature, triggered_at))

    def get_unified_action(self, action_id: int) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT * FROM actions WHERE id = ?",
                           (action_id,)).fetchone()
            return dict(r) if r else None

    def list_unified_actions(self) -> list[dict]:
        with self._connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM actions ORDER BY id").fetchall()]

    def actions_for_owner_unified(self, owner: str) -> list[dict]:
        """Unified-table actions owned by `owner` (case-insensitive)."""
        with self._connect() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM actions WHERE lower(owner)=lower(?) ORDER BY id",
                (owner,)).fetchall()]

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

    def relations_for(self, ent_id: str, *, at_time: str | None = None,
                      include_invalidated: bool = False) -> list[dict]:
        """All relations touching ent_id (as entity_a or entity_b).

        DEFAULT excludes invalidated rows (invalidated_at IS NOT NULL). Relations
        written by add_relation leave invalidated_at NULL, so they still surface.
        include_invalidated=True returns every row regardless.

        at_time (ISO date string) returns the graph as it stood at that point:
        valid_from <= at_time AND (valid_to IS NULL OR valid_to > at_time). Rows
        with a NULL valid_from are treated as always-valid (they pre-date the
        bitemporal columns), so the at_time gate only narrows rows that carry a
        valid_from. Returned dict keeps the backward-compatible shape
        (entity_a, relation, entity_b, source_doc_id) plus valid_from/valid_to.
        """
        sql = ("SELECT entity_a, relation, entity_b, source_doc_id, "
               "valid_from, valid_to FROM entity_relations "
               "WHERE (entity_a=:eid OR entity_b=:eid)")
        params: dict = {"eid": ent_id}
        if not include_invalidated:
            sql += " AND invalidated_at IS NULL"
        if at_time is not None:
            sql += (" AND (valid_from IS NULL OR valid_from <= :at) "
                    "AND (valid_to IS NULL OR valid_to > :at)")
            params["at"] = at_time
        sql += " ORDER BY id"
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def unified_actions(self, owner: str | None = None, status: str | None = None,
                        thread_id: str | None = None) -> list[dict]:
        """Rows from the unified actions table, filtered by any combination of
        owner (case-insensitive), status, and thread_id. A None argument applies
        no filter on that column. ORDER BY id for stable output."""
        clauses, params = [], []
        if owner is not None:
            clauses.append("lower(owner)=lower(?)")
            params.append(owner)
        if status is not None:
            clauses.append("status=?")
            params.append(status)
        if thread_id is not None:
            clauses.append("thread_id=?")
            params.append(thread_id)
        sql = "SELECT * FROM actions"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

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

    def thread_has_unenriched(self, thread_id: str) -> bool:
        """True if any chunk in the thread is enriched=0 (so the normal
        enrichment path already owns it; the stale sweep must not double-trigger)."""
        with self._connect() as db:
            r = db.execute(
                "SELECT 1 FROM chunks "
                "WHERE json_extract(metadata,'$.thread_id')=? AND enriched=0 "
                "LIMIT 1",
                (thread_id,)).fetchone()
            return r is not None

    def mark_thread_unenriched(self, thread_id: str) -> int:
        """Set enriched=0 on every enriched chunk in the thread so the next
        enrichment cycle re-extracts it. Returns the number of rows flipped.
        Touches only this thread; leaves embedded untouched."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE chunks SET enriched=0 "
                "WHERE json_extract(metadata,'$.thread_id')=? AND enriched=1",
                (thread_id,))
            return cur.rowcount

    def thread_signature(self, thread_id: str) -> str:
        """sha256 over the thread's (doc_id, content_hash) pairs in doc_id order.
        Stable across enriched-flag changes; changes iff thread content changes.
        Empty thread -> a fixed empty-set digest."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id, content_hash FROM chunks "
                "WHERE json_extract(metadata,'$.thread_id')=? ORDER BY doc_id",
                (thread_id,)).fetchall()
        h = hashlib.sha256()
        for r in rows:
            h.update(r["doc_id"].encode())
            h.update(b"\x1f")
            h.update((r["content_hash"] or "").encode())
            h.update(b"\x1e")
        return h.hexdigest()

    def doc_ids_for_messages(self, message_ids) -> list[str]:
        """doc_ids of the chunks whose messages these are.

        A chunk's "message id" is its metadata.message_id, or its doc_id when the
        chunk carries no message_id (mirroring thread_enrich.reassemble_thread,
        which keys each message on `message_id or doc_id`). drain recovers the
        chunks an extraction covers this way, so it marks exactly the messages
        that were extracted — a late-arriving or dropped message is not in the
        extraction, so its chunk stays enriched=0 and re-queues next cycle.

        Returns doc_ids ordered by the chunk rowid for stable output.
        """
        ids = [m for m in (message_ids or []) if m]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT doc_id FROM chunks "
                f"WHERE json_extract(metadata, '$.message_id') IN ({ph}) "
                f"   OR (json_extract(metadata, '$.message_id') IS NULL "
                f"       AND doc_id IN ({ph})) "
                f"ORDER BY rowid",
                ids + ids,
            ).fetchall()
        return [r["doc_id"] for r in rows]

    # --- Phase 3, Task 0.5A: community reader/writer methods ----------------

    def replace_communities(self, partition: dict, summaries: dict) -> None:
        """Atomically replace all community membership and summary data.

        partition: {entity_id: community_id} — every entity's community at level 0.
        summaries: {community_id: {"member_count": int, "key_entities": str,
                                   "title": str, "summary": str, "updated": str}}

        Deletes both tables then reinserts in a single transaction so callers
        always see either the full old set or the full new set.
        """
        with self._connect() as db:
            db.execute("DELETE FROM entity_communities")  # admin-delete-ok
            db.execute("DELETE FROM community_summaries")  # admin-delete-ok
            db.executemany(
                "INSERT INTO entity_communities(entity_id, community_id, level) "
                "VALUES(?, ?, 0)",
                [(eid, cid) for eid, cid in partition.items()],
            )
            db.executemany(
                "INSERT INTO community_summaries"
                "(community_id, level, title, summary, member_count, key_entities, updated) "
                "VALUES(?, 0, ?, ?, ?, ?, ?)",
                [
                    (
                        cid,
                        meta.get("title", ""),
                        meta.get("summary", ""),
                        meta.get("member_count", 0),
                        meta.get("key_entities", ""),
                        meta.get("updated", ""),
                    )
                    for cid, meta in summaries.items()
                ],
            )

    def communities_for(self, entity_ids: list) -> list[dict]:
        """Return entity_communities rows for the given entity_ids."""
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        with self._connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    f"SELECT * FROM entity_communities WHERE entity_id IN ({placeholders})",
                    entity_ids,
                ).fetchall()
            ]

    def community_members(self, community_id: int) -> list[dict]:
        """Return entity rows for all members of a community (level=0).

        Joins entity_communities → entities so each result is a full entity dict.
        """
        with self._connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT e.* FROM entities e "
                    "JOIN entity_communities ec ON ec.entity_id = e.id "
                    "WHERE ec.community_id = ? AND ec.level = 0 "
                    "ORDER BY e.id",
                    (community_id,),
                ).fetchall()
            ]

    def list_communities(self) -> list[dict]:
        """Return all community_summaries rows at level=0."""
        with self._connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM community_summaries WHERE level = 0 "
                    "ORDER BY community_id"
                ).fetchall()
            ]

    # --- Phase 3, Task 0.5B: thread_context reader/writer methods -----------

    def upsert_thread_context(
        self,
        thread_id: str,
        *,
        subject: str = "",
        org: str = "",
        email_count: int = 0,
        summary: str = "",
        contextual_summary: str = "",
        participant_ids: str = "",
    ) -> None:
        """Insert or update the thread_context row for thread_id.

        On conflict, only non-empty/non-zero values overwrite the existing row.
        This lets callers pass a partial update (e.g. summary= only) without
        clobbering already-populated subject/org/email_count.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            db.execute(
                "INSERT INTO thread_context"
                "(thread_id, subject, org, email_count, participant_ids, "
                "summary, last_updated, contextual_summary, contextual_summary_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id) DO UPDATE SET "
                "    subject            = CASE WHEN excluded.subject != '' THEN excluded.subject ELSE subject END, "
                "    org                = CASE WHEN excluded.org != '' THEN excluded.org ELSE org END, "
                "    email_count        = CASE WHEN excluded.email_count != 0 THEN excluded.email_count ELSE email_count END, "
                "    participant_ids    = CASE WHEN excluded.participant_ids != '' THEN excluded.participant_ids ELSE participant_ids END, "
                "    summary            = CASE WHEN excluded.summary != '' THEN excluded.summary ELSE summary END, "
                "    last_updated       = CASE WHEN excluded.last_updated != '' THEN excluded.last_updated ELSE last_updated END, "
                "    contextual_summary = CASE WHEN excluded.contextual_summary != '' THEN excluded.contextual_summary ELSE contextual_summary END, "
                # Stamp the summary's write-time only when a contextual_summary is
                # actually being written, so threads_needing_summary can tell that a
                # later message (which bumps last_updated) post-dates the summary.
                "    contextual_summary_at = CASE WHEN excluded.contextual_summary != '' THEN excluded.last_updated ELSE contextual_summary_at END",
                (
                    thread_id,
                    subject,
                    org,
                    email_count,
                    participant_ids,
                    summary,
                    now,
                    contextual_summary,
                    now if contextual_summary else "",
                ),
            )

    def threads_needing_summary(self, min_emails: int = 5) -> list[dict]:
        """Threads with email_count >= min_emails that need a contextual_summary —
        either they have none yet, OR they have gained messages SINCE the summary
        was written (last_updated > contextual_summary_at), so a long thread's
        narrative refreshes as it grows instead of freezing at first summary.
        """
        with self._connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM thread_context "
                    "WHERE email_count >= ? "
                    "  AND (contextual_summary IS NULL OR contextual_summary = '' "
                    "       OR (COALESCE(contextual_summary_at,'') != '' "
                    "           AND last_updated > contextual_summary_at)) "
                    "ORDER BY thread_id",
                    (min_emails,),
                ).fetchall()
            ]

    def thread_messages(self, thread_id: str) -> list[dict]:
        """Return email_context rows for a thread, ordered by date ascending.

        Reads email_context WHERE thread_id=? ORDER BY date_iso. Each result is
        a full email_context dict (message_id, subject, sender, date_iso,
        content_type, summary, thread_id, ...).
        """
        with self._connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM email_context WHERE thread_id = ? ORDER BY date_iso",
                    (thread_id,),
                ).fetchall()
            ]

    # --- Phase 3, Task 0.5C: proactive_findings reader/writer methods -------

    def record_finding(
        self,
        finding_type: str,
        ref_id: str,
        org: str = "",
        summary: str = "",
        detail: str = "",
        severity: str = "info",
        detected_at: str = "",
    ) -> None:
        """Insert or update a proactive finding.

        Upserts on the UNIQUE(finding_type, ref_id) constraint so re-detecting
        the same signal updates the existing row rather than accumulating
        duplicates. resolved_at is cleared on upsert so a previously resolved
        finding resurfaces if it is re-detected.
        """
        if not detected_at:
            detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            db.execute(
                "INSERT INTO proactive_findings"
                "(finding_type, ref_id, org, summary, detail, severity, detected_at, resolved_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(finding_type, ref_id) DO UPDATE SET "
                "  org         = excluded.org, "
                "  summary     = excluded.summary, "
                "  detail      = excluded.detail, "
                "  severity    = excluded.severity, "
                "  detected_at = excluded.detected_at, "
                "  resolved_at = NULL",
                (finding_type, ref_id, org, summary, detail, severity, detected_at),
            )

    def open_findings(self, finding_type: str | None = None) -> list[dict]:
        """Return unresolved proactive_findings rows.

        finding_type=None returns all types. Pass a type string to filter.
        """
        sql = "SELECT * FROM proactive_findings WHERE resolved_at IS NULL"
        params: list = []
        if finding_type is not None:
            sql += " AND finding_type = ?"
            params.append(finding_type)
        sql += " ORDER BY id"
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def resolve_findings_not_in(
        self, finding_type: str, live_ref_ids: list, now: str
    ) -> int:
        """Close open findings of finding_type whose ref_id is not in live_ref_ids.

        Used at the end of a pipeline pass to retire findings whose underlying
        condition has cleared. Returns the count of rows resolved.
        """
        with self._connect() as db:
            if not live_ref_ids:
                # Everything of this type is stale.
                cur = db.execute(
                    "UPDATE proactive_findings SET resolved_at = ? "
                    "WHERE finding_type = ? AND resolved_at IS NULL",
                    (now, finding_type),
                )
            else:
                placeholders = ",".join("?" * len(live_ref_ids))
                cur = db.execute(
                    f"UPDATE proactive_findings SET resolved_at = ? "
                    f"WHERE finding_type = ? AND resolved_at IS NULL "
                    f"AND ref_id NOT IN ({placeholders})",
                    [now, finding_type] + list(live_ref_ids),
                )
            return cur.rowcount

    # --- Phase 1 capture: change_log + finding helpers -----------------------

    def record_change(self, change_type: str, *, ref_id: str = "", summary: str = "",
                      detail: str = "", revert_ref: str = "", source: str = "") -> int:
        """Append one row to the change digest's audit trail."""
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO change_log(change_type, ref_id, summary, detail, revert_ref, source) "
                "VALUES(?,?,?,?,?,?)",
                (change_type, ref_id, summary, detail, revert_ref, source))
            return cur.lastrowid

    def recent_changes(self, limit: int = 20) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM change_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def prune_change_log(self, keep: int = 500) -> int:
        """Delete old change_log rows, keeping the most recent `keep`. Returns count deleted."""
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM change_log ORDER BY id DESC LIMIT 1 OFFSET ?",
                (keep - 1,)).fetchone()
            if row is None:
                return 0
            cur = db.execute("DELETE FROM change_log WHERE id < ?", (row["id"],))
            return cur.rowcount

    def open_findings_count(self) -> int:
        with self._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) FROM proactive_findings WHERE resolved_at IS NULL"
            ).fetchone()
            return row[0] if row else 0

    def resolve_finding(self, finding_id: int) -> bool:
        """Dismiss one finding (sets resolved_at). True if a row changed."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            cur = db.execute(
                "UPDATE proactive_findings SET resolved_at=? "
                "WHERE id=? AND resolved_at IS NULL", (now, finding_id))
            return cur.rowcount > 0

    # --- Session-4, Task 2.1: reversible entity suppression ------------------

    def suppress_entity(self, entity_id: str, reason: str = "") -> bool:
        """Suppress an entity by inserting a row into entity_suppressions.

        Purely additive/reversible: the entities row is validated to exist but
        never touched (no column added, no delete). Returns False (no-op) if
        entity_id isn't a real entity. Reverse with unsuppress_entity.
        """
        with self._connect() as db:
            row = db.execute("SELECT id FROM entities WHERE id=?", (entity_id,)).fetchone()
            if not row:
                return False
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            db.execute(
                "INSERT OR REPLACE INTO entity_suppressions(entity_id, reason, suppressed_at) "
                "VALUES(?, ?, ?)",
                (entity_id, reason, now),
            )
            return True

    def unsuppress_entity(self, entity_id: str) -> bool:
        """Remove a suppression. True if a row was deleted."""
        with self._connect() as db:
            cur = db.execute("DELETE FROM entity_suppressions WHERE entity_id=?", (entity_id,))
            return cur.rowcount > 0

    # --- Session-4, Task 3.2: org-suggestion inbox ---------------------------

    def suggest_org_mapping(self, raw_org: str, reason: str = "") -> bool:
        """Record a suggestion that `raw_org` (an unrecognised org string the
        extractor keeps seeing) be added to the configured taxonomy.

        Purely additive: never writes config.json itself, just an inspectable
        row for a human (or a future dashboard/CLI) to review. INSERT OR
        REPLACE keyed on raw_org, so re-suggesting the same string refreshes
        reason/suggested_at instead of accumulating duplicates. Always
        succeeds — unlike suppress_entity, there's no existence precondition
        to fail (raw_org is a free-text string, not a foreign key).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO org_suggestions(raw_org, reason, suggested_at) "
                "VALUES(?, ?, ?)",
                (raw_org, reason, now),
            )
            return True

    def find_open_action_by_fingerprint(self, fp: str) -> int | None:
        if not fp:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM actions WHERE text_fingerprint=? AND status='open' "
                "LIMIT 1", (fp,)).fetchone()
            return row["id"] if row else None

    # --- Phase 3, Task 0.5D: waiting-on reader/writer methods ---------------

    def open_waiting_actions(
        self, window_days: int = 30, now: str | None = None
    ) -> list[dict]:
        """Open actions with a non-null waiting_on set within window_days of now.

        Filters: status='open', waiting_on IS NOT NULL, waiting_on_set_at > cutoff.
        If now is None, uses the current UTC time. Returns dicts ordered by id.
        """
        if now is None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Compute cutoff as an ISO string. SQLite text comparison on ISO-8601
        # strings is lexicographic and correct for this date range.
        cutoff_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        cutoff = (cutoff_dt - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM actions "
                    "WHERE status = 'open' "
                    "  AND waiting_on IS NOT NULL "
                    "  AND waiting_on_set_at > ? "
                    "ORDER BY id",
                    (cutoff,),
                ).fetchall()
            ]

    def clear_waiting(
        self, action_id: int, cleared_by_doc_id: str, now: str
    ) -> None:
        """Clear the waiting_on state for an action when a reply arrives.

        Sets waiting_on=NULL, stamps waiting_on_cleared_at and
        waiting_on_cleared_by_doc_id, sets reply_received=1, refreshes updated_at.
        No-op if the id does not exist.
        """
        with self._connect() as db:
            db.execute(
                "UPDATE actions SET "
                "  waiting_on = NULL, "
                "  waiting_on_cleared_at = ?, "
                "  waiting_on_cleared_by_doc_id = ?, "
                "  reply_received = 1, "
                "  updated_at = ? "
                "WHERE id = ?",
                (now, cleared_by_doc_id, now, action_id),
            )

    def inbound_chunks_since(self, cursor: str | None) -> list[dict]:
        """Return inbound chunks with metadata date > cursor.

        The date filter is pushed into SQL (json_extract over metadata.date, then
        metadata.date_iso) so a sweep no longer loads and JSON-parses the entire
        chunks table every cycle — only chunks newer than the cursor come back.
        cursor is an ISO date string or None (returns all inbound chunks). Chunks
        with no date are excluded when a cursor is set (NULL > cursor is NULL),
        included when cursor=None.

        SENT chunks are excluded in Python on the reduced set (labels may be a
        JSON array or a comma string, awkward to test in SQL).

        Note: the cursor uses strict `>`. With day-granular dates a same-day
        arrival after the cursor advances would be skipped; clearing is idempotent
        so a future move to `>=` (re-scanning the boundary day) would be safe. At
        larger corpus sizes, add a generated-column index on the extracted date.
        """
        date_expr = ("COALESCE(json_extract(metadata, '$.date'), "
                     "json_extract(metadata, '$.date_iso'))")
        with self._connect() as db:
            if cursor:
                rows = db.execute(
                    f"SELECT rowid, doc_id, text, metadata FROM chunks "
                    f"WHERE metadata IS NOT NULL AND {date_expr} > ?",
                    (cursor,),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT rowid, doc_id, text, metadata FROM chunks "
                    "WHERE metadata IS NOT NULL",
                ).fetchall()
        results = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                continue
            # Skip SENT chunks.
            labels = meta.get("labels", [])
            if isinstance(labels, str):
                labels = json.loads(labels) if labels.startswith("[") else labels.split(",")
            if "SENT" in labels:
                continue
            results.append({
                "rowid": r["rowid"],
                "doc_id": r["doc_id"],
                "text": r["text"],
                "metadata": meta,
            })
        return results

    # --- B3 salience methods (Phase 2, 2026-06-23) ----------------------------

    def chunks_needing_salience(self, limit: int = 500) -> list[dict]:
        """Return chunks whose salience is 0.0 (never scored) and are embedded.

        Only embedded chunks are scored so the embedding exists as context.
        Returns {rowid, doc_id, text, metadata} dicts.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT rowid, doc_id, text, metadata FROM chunks "
                "WHERE embedded=1 AND COALESCE(salience, 0.0) = 0.0 "
                "ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                meta = {}
            result.append({"rowid": r["rowid"], "doc_id": r["doc_id"],
                           "text": r["text"], "metadata": meta})
        return result

    def set_chunk_salience(self, doc_id: str, salience: float) -> None:
        """Write the salience score for one chunk."""
        with self._connect() as db:
            db.execute(
                "UPDATE chunks SET salience=? WHERE doc_id=?",
                (round(float(salience), 3), doc_id))

    def set_chunk_salience_batch(self, pairs: list[tuple]) -> None:
        """Write salience for many chunks. pairs = [(doc_id, salience), ...]."""
        if not pairs:
            return
        with self._connect() as db:
            db.executemany(
                "UPDATE chunks SET salience=? WHERE doc_id=?",
                [(round(float(s), 3), d) for d, s in pairs])

    def get_chunk_salience(self, doc_id: str) -> float:
        """Return the stored salience for a chunk (0.0 if unscored).

        Defensive against a pre-Phase-2 schema (no `salience` column) that a
        read-only handle can see in the upgrade→migration window — recall must
        not crash; an unscored chunk is simply 0.0.
        """
        with self._connect() as db:
            try:
                row = db.execute(
                    "SELECT salience FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
            except sqlite3.OperationalError:
                return 0.0
            return float(row["salience"] or 0.0) if row else 0.0

    # --- B2 memory tier/type methods (Phase 2, 2026-06-23) -------------------

    def set_chunk_tier(self, doc_id: str, tier: str) -> None:
        """Set memory_tier for one chunk. tier: core/hot/warm/cold/''."""
        with self._connect() as db:
            db.execute(
                "UPDATE chunks SET memory_tier=? WHERE doc_id=?", (tier, doc_id))

    def set_chunk_type(self, doc_id: str, memory_type: str) -> None:
        """Set memory_type for one chunk. type: episodic/semantic/procedural."""
        with self._connect() as db:
            db.execute(
                "UPDATE chunks SET memory_type=? WHERE doc_id=?", (memory_type, doc_id))

    def core_chunks(self, max_chars: int = 800) -> list[dict]:
        """Return chunks in the 'core' tier, newest first, up to max_chars total.

        The budget is calculated on snippet[:200] (same cap get_core_block uses),
        so a 1200-char consolidated note contributes at most 200 chars to the budget
        rather than exceeding it and being silently dropped.
        """
        _SNIPPET_CAP = 200
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id, text, metadata FROM chunks "
                "WHERE memory_tier='core' AND COALESCE(enrich_state,'')!='cold' "
                "ORDER BY COALESCE(salience,0.0) DESC, rowid DESC",
            ).fetchall()
        result = []
        total = 0
        for r in rows:
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                meta = {}
            snippet = (r["text"] or "").strip()
            if not snippet:
                continue
            contribution = min(len(snippet), _SNIPPET_CAP)
            if total + contribution > max_chars:
                break
            result.append({"doc_id": r["doc_id"], "text": snippet, "metadata": meta})
            total += contribution
        return result

    def promote_chunk_tier(self, doc_id: str, from_tier: str, to_tier: str) -> bool:
        """Promote a chunk from from_tier to to_tier. Returns True if updated."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE chunks SET memory_tier=? "
                "WHERE doc_id=? AND memory_tier=?",
                (to_tier, doc_id, from_tier))
            return cur.rowcount > 0

    def chunks_by_tier(self, tier: str, limit: int = 200) -> list[dict]:
        """Return {doc_id, text, metadata} for chunks in the given tier."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id, text, metadata FROM chunks "
                "WHERE memory_tier=? ORDER BY rowid DESC LIMIT ?",
                (tier, limit),
            ).fetchall()
        result = []
        for r in rows:
            try:
                meta = json.loads(r["metadata"])
            except Exception:
                meta = {}
            result.append({"doc_id": r["doc_id"], "text": r["text"], "metadata": meta})
        return result

    def top_core_candidates(self, limit: int = 12) -> list[dict]:
        """Top durable notes for the 'core' tier: highest-salience semantic/
        procedural chunks (consolidated knowledge + voice model), excluding cold.

        Returns [{doc_id}]. Episodic email is intentionally excluded — core is for
        durable distilled facts, not raw correspondence.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT doc_id FROM chunks "
                "WHERE memory_type IN ('semantic','procedural') "
                "  AND COALESCE(enrich_state,'') != 'cold' "
                "ORDER BY COALESCE(salience,0.0) DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"doc_id": r["doc_id"]} for r in rows]

    def warm_chunks_above_strength(self, min_strength: float, limit: int = 2000) -> list[dict]:
        """Warm/untiered chunks whose memory_strength (built by recall) is at or
        above min_strength — candidates for promotion to 'hot'. Returns [{doc_id}]."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.doc_id FROM chunks c "
                "JOIN chunk_quality cq ON cq.doc_id = c.doc_id "
                "WHERE COALESCE(c.memory_tier,'') IN ('', 'warm') "
                "  AND COALESCE(cq.memory_strength, 5.0) >= ? "
                "ORDER BY cq.memory_strength DESC LIMIT ?",
                (float(min_strength), limit),
            ).fetchall()
        return [{"doc_id": r["doc_id"]} for r in rows]

    # --- B4 thread context reader (Phase 2, 2026-06-23) ----------------------

    def thread_context(self, thread_id: str) -> str:
        """Return the contextual_summary for a thread (empty string if none).

        The summary is written by the thread synthesis cadence and piped into
        extraction context by prepare._thread_block. Returns '' so the caller
        can use `or ''` safely.
        """
        if not thread_id:
            return ""
        with self._connect() as db:
            row = db.execute(
                "SELECT contextual_summary FROM thread_context WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        if not row:
            return ""
        return (row["contextual_summary"] or "").strip()

    # --- B5 decay / memory strength methods (Phase 2, 2026-06-23) -----------

    def get_memory_strength(self, doc_id: str) -> tuple:
        """Return (strength, last_accessed_iso) for a chunk.

        Strength defaults to 5.0 (mid-range) and last_accessed to '' if the
        chunk_quality row doesn't exist yet.
        """
        with self._connect() as db:
            row = db.execute(
                "SELECT memory_strength, last_accessed FROM chunk_quality WHERE doc_id=?",
                (doc_id,),
            ).fetchone()
        if not row:
            return (5.0, "")
        return (float(row["memory_strength"] or 5.0), row["last_accessed"] or "")

    def update_memory_strength(self, doc_id: str, strength: float, last_accessed: str) -> None:
        """Upsert memory_strength + last_accessed in chunk_quality."""
        with self._connect() as db:
            db.execute(
                "INSERT INTO chunk_quality(doc_id, quality, memory_strength, last_accessed) "
                "VALUES(?, 1.0, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "  memory_strength=excluded.memory_strength, "
                "  last_accessed=excluded.last_accessed",
                (doc_id, round(float(strength), 3), last_accessed),
            )

    def update_memory_strength_batch(self, rows: list[tuple]) -> None:
        """Update many rows. rows = [(doc_id, strength, last_accessed), ...]."""
        if not rows:
            return
        with self._connect() as db:
            db.executemany(
                "INSERT INTO chunk_quality(doc_id, quality, memory_strength, last_accessed) "
                "VALUES(?, 1.0, ?, ?) "
                "ON CONFLICT(doc_id) DO UPDATE SET "
                "  memory_strength=excluded.memory_strength, "
                "  last_accessed=excluded.last_accessed",
                [(d, round(float(s), 3), la) for d, s, la in rows],
            )

    def chunks_for_decay_pass(self, limit: int = 2000) -> list[dict]:
        """Chunks eligible for decay evaluation: embedded, not core tier.

        Returns {doc_id, salience, memory_tier, memory_strength, last_accessed,
        metadata}. metadata lets decay anchor never-accessed chunks on their
        source date (email/file/event time) instead of exempting them forever.
        Joins chunks → chunk_quality (LEFT JOIN so unscored chunks still appear).
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.doc_id, COALESCE(c.salience,0.0) AS salience, "
                "       COALESCE(c.memory_tier,'') AS memory_tier, "
                "       COALESCE(c.metadata,'') AS metadata, "
                "       COALESCE(cq.memory_strength, 5.0) AS memory_strength, "
                "       COALESCE(cq.last_accessed, '') AS last_accessed "
                "FROM chunks c "
                "LEFT JOIN chunk_quality cq ON cq.doc_id = c.doc_id "
                "WHERE c.embedded=1 AND COALESCE(c.memory_tier,'') != 'core' "
                "ORDER BY c.rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def demote_chunks_to_cold(self, doc_ids: list[str]) -> int:
        """Set memory_tier='cold' for the given doc_ids. Returns count changed."""
        if not doc_ids:
            return 0
        with self._connect() as db:
            qs = ",".join("?" for _ in doc_ids)
            cur = db.execute(
                f"UPDATE chunks SET memory_tier='cold' "
                f"WHERE doc_id IN ({qs}) AND memory_tier != 'core'",
                doc_ids,
            )
            return cur.rowcount

    # --- B6 voice suggestion methods (Phase 2, 2026-06-23) ------------------

    def insert_voice_suggestion(self, kind: str, rule: str, confidence: float,
                                evidence_ids: list, explanation: str) -> int:
        """Insert one voice suggestion. Returns the new row id."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            cur = db.execute(
                "INSERT INTO voice_suggestions(kind, rule, confidence, explanation, "
                "evidence_ids, status, created_at) VALUES(?,?,?,?,?,?,?)",
                (kind, rule, round(float(confidence), 3), explanation,
                 json.dumps(evidence_ids), "pending", ts),
            )
            return cur.lastrowid

    def pending_voice_suggestions(self) -> list[dict]:
        """Return all pending voice suggestions."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM voice_suggestions WHERE status='pending' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_voice_suggestion_applied(self, suggestion_id: int) -> None:
        """Mark one suggestion as applied."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            db.execute(
                "UPDATE voice_suggestions SET status='applied', applied_at=? WHERE id=?",
                (ts, suggestion_id),
            )

    def get_voice_analyser_state(self, key: str, default: str = "") -> str:
        """Read a key from voice_analyser_state."""
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM voice_analyser_state WHERE key=?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_voice_analyser_state(self, key: str, value: str) -> None:
        """Upsert a key in voice_analyser_state."""
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._connect() as db:
            db.execute(
                "INSERT INTO voice_analyser_state(key, value, updated_at) "
                "VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, ts),
            )
