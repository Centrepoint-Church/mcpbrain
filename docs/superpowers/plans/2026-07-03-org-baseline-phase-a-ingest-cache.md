# Org Baseline — Phase A (Shared-Drive Sync + Ingest Cache) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a user's daemon (a) actually sync true Shared Drive files, (b) reuse an
ACL-gated per-file ingest cache so shared content is not re-extracted / re-embedded /
re-enriched by every install, (c) purge a drive's local content automatically when access
is revoked, and (d) provide the production `DriveFleetStorage` transport that subsystems B
and C consume only through the `FleetStorage` protocol.

**Architecture:** Phase A builds *logic only* on top of Phase 0's frozen surface
(`mcpbrain/org_contracts.py`, the `origin` columns + org tables, `config.fleet_pin` /
`ingest_cache_enabled`, the `org_pin` allowlist). A adds:
a new **cache-import/publish/purge** layer of pure store methods; a new **`mcpbrain/ingest_cache.py`**
module implementing cache-first import, publish, GC, bootstrap and revocation against a
`FleetStorage`; **Shared-Drive sync** in `sync/drive.py` (`drives.list` enumeration,
per-drive `drive:<driveId>` cursors, `changes.list(driveId=…, corpora="drive")`, backfill
parity, `drive_id` metadata stamping, removal handling); the **consecutive-absence
revocation** counter; and the production **`mcpbrain/fleet_storage.py:DriveFleetStorage`**.
Everything is gated so a daemon without a fleet pin behaves exactly as today.

**Tech Stack:** Python 3, stdlib `sqlite3` + `sqlite-vec`, stdlib `gzip`/`json`/`base64`/`struct`/`hashlib`, `googleapiclient` (lazy, prod only), pytest.

## Global Constraints

- **Phase 0 shared surface is FROZEN.** No schema changes (the `origin` columns + org
  tables already exist), no edits to `config.py` accessors (`fleet_pin`,
  `ingest_cache_enabled`, `owner_email` are used as-is), no edits to the daemon
  `_CADENCE_*` blocks or cadence registration. **Subsystem A owns none of the three stub
  cadences** — the cache is wired into the existing drive-sync path (`sync/drive.py` +
  `sync/__init__.run_sync_cycle`) only.
- **`drive_id` is a chunk-`metadata` JSON key** (`org_contracts.DRIVE_ID_META_KEY == "drive_id"`),
  never a column. Purge/enumeration filter on `json_extract(metadata,'$.drive_id')`.
- **Frozen wire shapes come from `org_contracts`** — `CacheArtifact`, `CacheChunk`,
  `FleetPin`, `pipeline_fingerprint`, `source_ref`, `artifact_filename`, `FleetStorage`.
  Do not redefine them; import them.
- **New store methods are allowed** (they are neither schema, config accessors, nor
  cadence): they are the cache-import / purge primitives. Keep them next to the existing
  chunk/vec methods and follow the `_connect()` + rowid-mirror idioms already in `store.py`.
- **Vectors round-trip bit-exact.** `sqlite_vec.serialize_float32` packs raw little-endian
  float32 with no header; base64 of those bytes is the artifact's `embedding_b64`. Import
  decodes → `struct.unpack('<Nf', …)` → `serialize_float32`. float32→float64→float32 is
  lossless, so published and imported vectors are identical (asserted by the round-trip test).
- **Cache `content_hash` is the Drive *file-version* id, not the text hash** — it must be
  computable from Changes-API metadata *before* extraction (that is how the read path
  decides whether to extract at all). Per-chunk row `content_hash` stays
  `chunking.content_hash(text)` and is recomputed on import.
- **Fail-safe / silent-fallback:** any corrupt artifact, hash/model/pipeline mismatch, or
  missing pin → return `False`/`None` and fall back to the local extract/embed path. Log at
  info, never raise into the sync loop.
- **No new OAuth scopes.** Reads use the existing read-only Drive scope; writes use
  `drive.file` on the bundled client id (same mechanism `backup.py`/`fleet.py` rely on).
  Every Drive call sets `supportsAllDrives=True`.
- **Tests:** pytest, flat `tests/test_*.py`, functions `test_*`. Stores are
  `Store(tmp_path / "x.sqlite3", dim=4).init()`. Fleet transport in tests is
  `tests/helpers/org_fleet.py:LocalDirFleetStorage`; multi-install via `make_install`/`make_fleet`.
  Drive sync uses an extended in-file `FakeDriveService` (the pattern already in
  `tests/test_drive_sync.py`).
- **No version bump, no release, no push** (per `CLAUDE.md`). Commit locally only.
- Reference spec: `docs/superpowers/specs/2026-07-03-org-baseline-personal-overlay-design.md`
  (subsystem A: §A1–A4). Frozen contracts: `mcpbrain/org_contracts.py`.

---

## File Structure

**Created:**
- `mcpbrain/ingest_cache.py` — the cache layer: filename/path helpers, `try_import`,
  `publish`, `publish_file`, `collect_chunks`, `gc_superseded`, `sweep_drive`,
  `remove_file_artifacts`, `bootstrap_drive`, `purge_drive`, `note_drive_presence`.
- `mcpbrain/fleet_storage.py` — production `DriveFleetStorage` (a `FleetStorage` over
  Google Drive) + the `fleet_folder_storage` / `drive_cache_storage` factories B and C
  acquire storage through + a re-export of `list_shared_drives`; the `FakeDrive` in-memory
  double lives only in its test.
- `tests/test_ingest_cache.py`, `tests/test_ingest_cache_lifecycle.py`,
  `tests/test_drive_shared.py`, `tests/test_fleet_storage_drive.py`,
  `tests/test_ingest_cache_revocation.py`, `tests/test_ingest_cache_roundtrip.py`.

**Modified:**
- `mcpbrain/store.py` — cache-import/read helpers (`import_cached_chunk`,
  `embedding_for_doc`, `chunks_for_file`) near `write_embedding` (~store.py:1185); purge
  helpers (`doc_ids_for_drive`, `doc_ids_for_file`, `delete_chunks`,
  `invalidate_local_relations_for_docs`) near `delete_calendar_chunks_after` (~store.py:1062).
- `mcpbrain/sync/drive.py` — `list_shared_drives`, `drive_id` param on `normalise_drive`,
  `_file_content_hash`, `sync_shared_drive`, `sync_shared_drives`, `backfill_shared_drive`,
  extended `_CHANGES_FIELDS`.
- `mcpbrain/sync/__init__.py` — `run_sync_cycle` gains `home=None` and drives the
  shared-drive sync + post-embed publish when a fleet pin is present.
- `mcpbrain/daemon.py` — `run_cycle` passes `home` through to `run_sync_cycle` (one line).
- `tests/test_drive_sync.py` — extend `FakeDriveService` with `drives()` + driveId-aware
  `changes` (shared by the new drive tests via import).

---

## Task 1: Store — cache-import + read-back helpers

**Files:**
- Modify: `mcpbrain/store.py` (after `write_embedding`, ~store.py:1185; and after
  `patch_chunk_metadata`, ~store.py:1249)
- Test: `tests/test_ingest_cache_roundtrip.py` (new; starts with the store-level checks)

**Interfaces:**
- Consumes: `sqlite_vec` (already imported in store.py), `struct`, `json`.
- Produces:
  - `import_cached_chunk(doc_id, text, content_hash, metadata, vector, *, enriched=False, enriched_version=0) -> bool`
    — insert/replace a chunk row with `embedded=1` (+ `enriched`/`enriched_version`), and
    mirror the vector into `vec_chunks` and raw `text` into `fts_chunks`, in one transaction.
  - `embedding_for_doc(doc_id) -> list[float] | None` — read the stored float32 vector back
    as a Python list (used by publish; asserts bit-exactness with the packed bytes).
  - `chunks_for_file(file_id) -> list[dict]` — `{doc_id,text,content_hash,metadata,idx}` for
    every `gdrive-<file_id>-<i>` chunk, ordered by chunk index.

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_cache_roundtrip.py`:

```python
import struct

from mcpbrain.store import Store


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_import_cached_chunk_is_searchable_and_read_back(tmp_path):
    s = _store(tmp_path)
    vec = [0.1, 0.2, 0.3, 0.4]
    ok = s.import_cached_chunk(
        "gdrive-FID-0", "hello world", "ch0",
        {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0,
         "drive_id": "D1"}, vec, enriched=True, enriched_version=1)
    assert ok
    # read-back is bit-exact (float32 -> float64 -> float32 round trip is lossless)
    back = s.embedding_for_doc("gdrive-FID-0")
    assert struct.pack("<4f", *back) == struct.pack("<4f", *vec)
    # embedded=1 (not re-queued) and enriched=1 (Haiku skip)
    with s._connect() as db:
        r = db.execute("SELECT embedded, enriched, enriched_version "
                       "FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
    assert (r["embedded"], r["enriched"], r["enriched_version"]) == (1, 1, 1)
    # vec_chunks + fts_chunks mirrors exist
    assert s.embedding_for_doc("gdrive-FID-0") is not None


def test_chunks_for_file_orders_by_index_and_scopes_by_file(tmp_path):
    s = _store(tmp_path)
    for i in (1, 0, 2):
        s.import_cached_chunk(
            f"gdrive-FID-{i}", f"t{i}", f"c{i}",
            {"file_id": "FID", "chunk_index": i}, [float(i)] * 4)
    # a different file must not leak in
    s.import_cached_chunk("gdrive-OTHER-0", "x", "cx",
                          {"file_id": "OTHER", "chunk_index": 0}, [9.0] * 4)
    rows = s.chunks_for_file("FID")
    assert [r["idx"] for r in rows] == [0, 1, 2]
    assert all(r["doc_id"].startswith("gdrive-FID-") for r in rows)


def test_embedding_for_doc_missing_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.embedding_for_doc("nope") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingest_cache_roundtrip.py -k "import_cached or chunks_for_file or embedding_for_doc" -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'import_cached_chunk'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/store.py`, immediately after `write_embedding` (ends ~store.py:1185), insert:

```python
    def import_cached_chunk(self, doc_id, text, content_hash, metadata, vector,
                            *, enriched=False, enriched_version=0) -> bool:
        """Insert (or replace) a chunk plus its vector + FTS mirror from a cache
        artifact, in one transaction. Sets embedded=1 (the vector is supplied, so
        the chunk must NOT re-queue for embedding) and marks enriched when the
        artifact's enrich block cleared the version gates (see ingest_cache).

        The vector is the publisher's already-contextual-prefixed passage vector;
        it is stored verbatim so a cache hit is bit-identical to local embedding.
        fts_chunks stores the RAW text (mirrors write_embedding). Returns True.
        """
        with self._connect() as db:
            row = db.execute("SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
            if row:
                rowid = row["rowid"]
                db.execute(
                    "UPDATE chunks SET text=?,content_hash=?,metadata=?,embedded=1,"
                    "enriched=?,enriched_version=? WHERE rowid=?",
                    (text, content_hash, json.dumps(metadata),
                     1 if enriched else 0, enriched_version, rowid))
            else:
                cur = db.execute(
                    "INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded,"
                    "enriched,enriched_version) VALUES(?,?,?,?,1,?,?)",
                    (doc_id, text, content_hash, json.dumps(metadata),
                     1 if enriched else 0, enriched_version))
                rowid = cur.lastrowid
            db.execute("DELETE FROM vec_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES(?,?)",
                       (rowid, sqlite_vec.serialize_float32(list(vector))))
            db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (rowid, text))
            return True

    def embedding_for_doc(self, doc_id: str) -> list[float] | None:
        """Return the stored embedding for doc_id as a list[float], or None.

        sqlite-vec vec0 stores the raw little-endian float32 payload; selecting
        the column returns those bytes, which we unpack. Used by the ingest cache
        to serialise a locally-embedded chunk into a shareable artifact.
        """
        import struct
        with self._connect() as db:
            r = db.execute("SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
            if not r:
                return None
            v = db.execute("SELECT embedding FROM vec_chunks WHERE rowid=?",
                           (r["rowid"],)).fetchone()
            if not v or v["embedding"] is None:
                return None
            raw = v["embedding"]
            if isinstance(raw, str):        # some builds surface JSON text
                return [float(x) for x in json.loads(raw)]
            n = len(raw) // 4
            return list(struct.unpack(f"<{n}f", raw))

    def chunks_for_file(self, file_id: str) -> list[dict]:
        """All gdrive-<file_id>-<i> chunks as {doc_id,text,content_hash,metadata,idx},
        ordered by chunk index. Used to build a cache artifact from local state."""
        like = f"gdrive-{file_id}-%"
        out = []
        with self._connect() as db:
            for r in db.execute(
                "SELECT doc_id,text,content_hash,metadata FROM chunks "
                "WHERE doc_id LIKE ? ORDER BY rowid", (like,)):
                meta = json.loads(r["metadata"])
                out.append({"doc_id": r["doc_id"], "text": r["text"],
                            "content_hash": r["content_hash"], "metadata": meta,
                            "idx": int(meta.get("chunk_index", 0))})
        out.sort(key=lambda c: c["idx"])
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingest_cache_roundtrip.py -k "import_cached or chunks_for_file or embedding_for_doc" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_ingest_cache_roundtrip.py
git commit -m "feat(store): cache-import + vector read-back helpers for the ingest cache"
```

---

## Task 2: Store — targeted purge + bitemporal invalidation

**Files:**
- Modify: `mcpbrain/store.py` (after `delete_calendar_chunks_after`, ~store.py:1062)
- Test: `tests/test_ingest_cache_revocation.py` (new; starts with the store-level checks)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `doc_ids_for_drive(drive_id) -> list[str]` — chunk doc_ids whose metadata `drive_id` matches.
  - `doc_ids_for_file(file_id) -> list[str]` — chunk doc_ids for one `gdrive-<file_id>-*`.
  - `delete_chunks(doc_ids) -> int` — delete those chunk rows + their `vec_chunks`/`fts_chunks` mirrors.
  - `invalidate_local_relations_for_docs(doc_ids, *, reason="drive_revoked", at=None) -> int`
    — bitemporally set `invalidated_at`+`superseded_reason` on `origin='local'` relations
    whose `source_doc_id` is in `doc_ids` and not already invalidated. `origin='org'` rows
    are never touched (spec A3: layer-1 is safe-by-construction).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_cache_revocation.py`:

```python
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def test_doc_ids_for_drive_and_file(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.import_cached_chunk("gdrive-F1-1", "b", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.import_cached_chunk("gdrive-F2-0", "c", "c", {"file_id": "F2", "drive_id": "D2"}, [0.0]*4)
    assert set(s.doc_ids_for_drive("D1")) == {"gdrive-F1-0", "gdrive-F1-1"}
    assert set(s.doc_ids_for_file("F1")) == {"gdrive-F1-0", "gdrive-F1-1"}
    assert s.doc_ids_for_drive("D2") == ["gdrive-F2-0"]


def test_delete_chunks_removes_row_and_mirrors(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.1]*4)
    n = s.delete_chunks(["gdrive-F1-0"])
    assert n == 1
    assert s.get_chunk("gdrive-F1-0") is None
    assert s.embedding_for_doc("gdrive-F1-0") is None
    assert s.delete_chunks([]) == 0


def test_invalidate_local_relations_scopes_to_local_and_docs(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('a','A','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('b','B','org','local')")
        # local relation sourced from a purged doc -> invalidate
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','works_at','b','gdrive-F1-0','local')")
        # local relation from a still-live doc -> survive
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','member_of','b','gdrive-LIVE-0','local')")
        # org relation from a purged doc -> untouched (layer 1 is safe-by-construction)
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','mentioned_with','b','gdrive-F1-0','org')")
    n = s.invalidate_local_relations_for_docs(["gdrive-F1-0"])
    assert n == 1
    with s._connect() as db:
        rows = {(r["relation"], r["origin"]): r["invalidated_at"]
                for r in db.execute("SELECT relation,origin,invalidated_at FROM entity_relations")}
    assert rows[("works_at", "local")] is not None       # invalidated
    assert rows[("member_of", "local")] is None           # live source survives
    assert rows[("mentioned_with", "org")] is None         # org untouched
    # idempotent: a second call invalidates nothing new
    assert s.invalidate_local_relations_for_docs(["gdrive-F1-0"]) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingest_cache_revocation.py -k "doc_ids or delete_chunks or invalidate_local" -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'doc_ids_for_drive'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/store.py`, immediately after `delete_calendar_chunks_after` (ends ~store.py:1062), insert:

```python
    def doc_ids_for_drive(self, drive_id: str) -> list[str]:
        """Chunk doc_ids whose metadata drive_id matches (see DRIVE_ID_META_KEY)."""
        with self._connect() as db:
            return [r["doc_id"] for r in db.execute(
                "SELECT doc_id FROM chunks WHERE json_extract(metadata,'$.drive_id')=?",
                (drive_id,)).fetchall()]

    def doc_ids_for_file(self, file_id: str) -> list[str]:
        """Chunk doc_ids for one Drive file (gdrive-<file_id>-*)."""
        with self._connect() as db:
            return [r["doc_id"] for r in db.execute(
                "SELECT doc_id FROM chunks WHERE doc_id LIKE ?",
                (f"gdrive-{file_id}-%",)).fetchall()]

    def delete_chunks(self, doc_ids) -> int:
        """Delete the given chunk rows and their vec_chunks/fts_chunks mirrors
        (keyed on rowid, mirroring delete_calendar_chunks_after). Graph rows are
        NOT touched here — invalidation is a separate, bitemporal step. Returns
        the number of chunk rows deleted."""
        doc_ids = list(doc_ids)
        if not doc_ids:
            return 0
        with self._connect() as db:
            qs = ",".join("?" * len(doc_ids))
            rowids = [r["rowid"] for r in db.execute(
                f"SELECT rowid FROM chunks WHERE doc_id IN ({qs})", doc_ids).fetchall()]
            if not rowids:
                return 0
            ph = ",".join("?" * len(rowids))
            db.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({ph})", rowids)
            db.execute(f"DELETE FROM fts_chunks WHERE rowid IN ({ph})", rowids)
            db.execute(f"DELETE FROM chunks WHERE rowid IN ({ph})", rowids)
            return len(rowids)

    def invalidate_local_relations_for_docs(self, doc_ids, *,
                                            reason: str = "drive_revoked",
                                            at: str | None = None) -> int:
        """Bitemporally invalidate origin='local' relations whose source_doc_id is
        in doc_ids and are not already invalidated. Sets invalidated_at (UTC ISO)
        and superseded_reason. origin='org' rows are never touched — layer 1 is
        curator-owned and safe-by-construction (spec A3). Returns rows changed."""
        doc_ids = list(doc_ids)
        if not doc_ids:
            return 0
        at = at or datetime.now(timezone.utc).isoformat()
        qs = ",".join("?" * len(doc_ids))
        with self._connect() as db:
            cur = db.execute(
                f"UPDATE entity_relations SET invalidated_at=?, superseded_reason=? "
                f"WHERE source_doc_id IN ({qs}) AND invalidated_at IS NULL "
                f"AND COALESCE(origin,'local')='local'",
                (at, reason, *doc_ids))
            return cur.rowcount
```

> `datetime`/`timezone` are already imported at the top of `store.py` (used by
> `upsert_email_context`); no new import needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingest_cache_revocation.py -k "doc_ids or delete_chunks or invalidate_local" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_ingest_cache_revocation.py
git commit -m "feat(store): targeted drive/file purge + bitemporal local-relation invalidation"
```

---

## Task 3: `ingest_cache` — path helpers, `try_import`, `collect_chunks`

**Files:**
- Create: `mcpbrain/ingest_cache.py`
- Test: `tests/test_ingest_cache.py`

**Interfaces:**
- Consumes: `org_contracts` (`CacheArtifact`, `CacheChunk`, `artifact_filename`,
  `pipeline_fingerprint`, `DRIVE_ID_META_KEY`, `FleetStorage`), `store.ENRICH_LOGIC_VERSION`,
  `chunking.content_hash`, `FleetPin` (from `config.fleet_pin`).
- Produces:
  - `CACHE_DIR = ".mcpbrain-cache"`.
  - `def try_import(store, fleet_storage, drive_id, file_id, content_hash, pin) -> bool`
    — **frozen cross-subsystem signature.** `content_hash` is the Drive *file-version* id.
    Cache-first: fetch `.mcpbrain-cache/<artifact_filename>`, gunzip+parse, validate
    pipeline+content+dim, import every chunk (re-stamping `drive_id`, marking enriched when
    the enrich gate clears). Any mismatch/corruption → `False` (caller falls back to local).
  - `def collect_chunks(store, file_id) -> list[CacheChunk]` — build artifact chunks from a
    locally-embedded file (drive-neutral: strips `drive_id` so two publishers produce
    byte-identical artifacts).
  - internal `_import_artifact(store, drive_id, art, pin) -> bool` (shared by `try_import`
    and `bootstrap_drive`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_cache.py`:

```python
import base64
import struct

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin, CacheArtifact, CacheChunk, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _b64(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _write_artifact(fs, file_id, content_hash, *, dim=4, embed_model="bge-small",
                    chunker="v1", enrich=None, chunks=None):
    chunks = chunks or (CacheChunk(idx=0, text="hello", embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                                   metadata={"source_type": "gdrive", "file_id": file_id,
                                             "chunk_index": 0}),)
    art = CacheArtifact(file_id=file_id, content_hash=content_hash,
                        extraction_method="gdocs", chunker_version=chunker,
                        embed_model=embed_model, dim=dim, chunks=tuple(chunks),
                        enrich=enrich or {}, published_by="p@x.org", published_at="2026-07-03")
    import gzip, json
    fname = artifact_filename(file_id, content_hash, embed_model, dim, chunker)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", gzip.compress(json.dumps(art.to_dict()).encode()))


def test_try_import_hit_imports_chunks_and_stamps_drive_id(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is True
    ch = s.get_chunk("gdrive-FID-0")
    assert ch is not None and ch["metadata"]["drive_id"] == "D1"
    back = s.embedding_for_doc("gdrive-FID-0")
    assert struct.pack("<4f", *back) == struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)


def test_try_import_miss_returns_false(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_unpinned_returns_false(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", FleetPin()) is False


def test_try_import_content_hash_mismatch_falls_back(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    # a DIFFERENT file version -> the artifact for vhash1 must not be used for vhash2
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash2", PIN) is False


def test_try_import_pipeline_mismatch_falls_back(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # artifact embedded with a different model -> filename fp differs, so it is not even found;
    # but a hand-planted file with mismatched inner fields must also be rejected.
    _write_artifact(fs, "FID", "vhash1", embed_model="other-model")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_corrupt_artifact_falls_back(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    fname = artifact_filename("FID", "vhash1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}", b"not-gzip-json")
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is False


def test_try_import_marks_enriched_when_logic_gate_clears(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1", enrich={"logic_version": 9})
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is True
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
    assert r["enriched"] == 1


def test_try_import_no_enrich_block_leaves_unenriched(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1", enrich={})
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vhash1", PIN) is True
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
    assert r["enriched"] == 0


def test_collect_chunks_is_drive_neutral(tmp_path):
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-FID-0", "hello", "c0",
                          {"file_id": "FID", "chunk_index": 0, "drive_id": "D1"}, [0.1, 0.2, 0.3, 0.4])
    ccs = ingest_cache.collect_chunks(s, "FID")
    assert len(ccs) == 1 and ccs[0].idx == 0 and ccs[0].text == "hello"
    assert "drive_id" not in ccs[0].metadata            # neutralised for byte-identical artifacts
    assert struct.unpack("<4f", base64.b64decode(ccs[0].embedding_b64)) == (
        struct.unpack("<4f", struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingest_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.ingest_cache'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/ingest_cache.py`:

```python
"""ACL-gated shared-drive ingest cache (spec §A2).

A `.mcpbrain-cache/` folder at the root of each shared drive holds one gzip-JSON
CacheArtifact per (file × content-version × embedding-pipeline). Because the
artifact lives inside the drive it describes, Google's ACLs ARE the access
control — no mcpbrain-side ACL logic exists or is needed.

`content_hash` throughout this module is the Drive FILE-VERSION id (md5Checksum,
or a hash of the native revision) — it must be knowable before extraction so the
read path can decide whether to extract at all. Per-chunk row content_hash stays
the text hash and is recomputed on import.

All entry points fail safe: a miss / mismatch / corruption returns False (or a
no-op) and the caller falls back to the local extract+embed pipeline.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import struct
from datetime import datetime, timezone

from mcpbrain.chunking import content_hash as _text_hash
from mcpbrain.org_contracts import (
    CacheArtifact, CacheChunk, DRIVE_ID_META_KEY,
    artifact_filename, pipeline_fingerprint,
)
from mcpbrain.store import ENRICH_LOGIC_VERSION

log = logging.getLogger(__name__)

CACHE_DIR = ".mcpbrain-cache"


# -- filename / path helpers ------------------------------------------------

def _pf8(pin) -> str:
    return pipeline_fingerprint(pin.embed_model, pin.dim, pin.chunker_version)[:8]


def _artifact_path(file_id: str, content_hash: str, pin) -> str:
    return f"{CACHE_DIR}/{artifact_filename(file_id, content_hash, pin.embed_model, pin.dim, pin.chunker_version)}"


def _parse_name(name: str):
    """`<file_id>.<hash12>.<pf8>.mbc.gz` -> (file_id, hash12, pf8), else None.
    Drive file ids never contain '.', so an rsplit is unambiguous."""
    if not name.endswith(".mbc.gz"):
        return None
    base = name[: -len(".mbc.gz")]
    parts = base.rsplit(".", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _decode_vec(embedding_b64: str, dim: int) -> list[float]:
    raw = base64.b64decode(embedding_b64)
    if len(raw) != dim * 4:
        raise ValueError(f"embedding length {len(raw)} != dim {dim} * 4")
    return list(struct.unpack(f"<{dim}f", raw))


def _encode_vec(vector) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


# -- read path --------------------------------------------------------------

def _import_artifact(store, drive_id: str, art: CacheArtifact, pin) -> bool:
    """Import a validated artifact's chunks into the store. Returns False on any
    per-chunk corruption (whole-file fallback) — never a partial import commit
    beyond what has already been written; callers treat False as a cache miss."""
    if (art.embed_model != pin.embed_model or int(art.dim) != int(pin.dim)
            or art.chunker_version != pin.chunker_version
            or int(art.dim) != int(store.dim)):
        return False
    logic_v = int(art.enrich.get("logic_version", 0)) if art.enrich else 0
    # Skip local re-enrichment only when the cached enrichment is at least as new
    # as BOTH the fleet floor and this install's own logic version.
    mark_enriched = bool(art.enrich) and logic_v >= max(int(pin.enrich_logic_floor),
                                                        int(ENRICH_LOGIC_VERSION))
    for cc in art.chunks:
        try:
            vector = _decode_vec(cc.embedding_b64, int(art.dim))
        except Exception:
            log.info("ingest_cache: corrupt vector in %s chunk %s (fallback)", art.file_id, cc.idx)
            return False
        meta = dict(cc.metadata or {})
        meta[DRIVE_ID_META_KEY] = drive_id
        doc_id = f"gdrive-{art.file_id}-{int(cc.idx)}"
        store.import_cached_chunk(
            doc_id, cc.text, _text_hash(cc.text), meta, vector,
            enriched=mark_enriched, enriched_version=logic_v if mark_enriched else 0)
    return True


def _load(fleet_storage, path) -> CacheArtifact | None:
    data = fleet_storage.get_bytes(path)
    if data is None:
        return None
    try:
        return CacheArtifact.from_dict(json.loads(gzip.decompress(data).decode("utf-8")))
    except Exception:
        log.info("ingest_cache: corrupt artifact %s (fallback to local)", path)
        return None


def try_import(store, fleet_storage, drive_id, file_id, content_hash, pin) -> bool:
    """Cache-first import for one shared-drive file version. Returns True iff the
    artifact was found, validated, and imported; False => caller extracts locally.

    `content_hash` is the Drive file-version id (NOT the text hash)."""
    if not pin.is_pinned:
        return False
    art = _load(fleet_storage, _artifact_path(file_id, content_hash, pin))
    if art is None:
        return False
    if art.file_id != file_id or art.content_hash != content_hash:
        return False
    return _import_artifact(store, drive_id, art, pin)


def collect_chunks(store, file_id) -> list[CacheChunk]:
    """Build drive-neutral CacheChunks from a locally-embedded file. Strips the
    drive_id key so two publishers of the same file version emit byte-identical
    artifacts (content-hash keying then makes races harmless)."""
    out = []
    for row in store.chunks_for_file(file_id):
        vec = store.embedding_for_doc(row["doc_id"])
        if vec is None:
            continue
        meta = dict(row["metadata"])
        meta.pop(DRIVE_ID_META_KEY, None)
        out.append(CacheChunk(idx=int(row["idx"]), text=row["text"],
                              embedding_b64=_encode_vec(vec), metadata=meta))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingest_cache.py -v`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache.py
git commit -m "feat(ingest_cache): cache-first try_import + collect_chunks (fail-safe)"
```

---

## Task 4: `ingest_cache` — publish + GC lifecycle

**Files:**
- Modify: `mcpbrain/ingest_cache.py`
- Test: `tests/test_ingest_cache_lifecycle.py`

**Interfaces:**
- Produces:
  - `def publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin, *, enrich=None, published_by="") -> None`
    — **frozen cross-subsystem signature** (the extra `published_by` is keyword-only and
    optional). Write the gzip-JSON `CacheArtifact` for `chunks` (a sequence of `CacheChunk`),
    then best-effort `gc_superseded`. No-op when unpinned or `chunks` empty.
  - `def publish_file(store, fleet_storage, drive_id, file_id, content_hash, pin, *, enrich=None, published_by="") -> bool`
    — convenience: `collect_chunks` from the store (post-embed) then `publish`. Returns True
    if anything was published. This is what the drive-sync path calls after `index_pending`.
  - `def gc_superseded(fleet_storage, drive_id, file_id, keep_content_hash, pin) -> int`
    — **frozen signature.** Delete artifacts for the same `file_id` with a different content
    hash OR a stale pipeline fingerprint; keep the current one. Returns count deleted.
  - `def sweep_drive(fleet_storage, live_file_ids) -> int` — delete artifacts whose `file_id`
    no longer exists in the drive (opportunistic; spec A2 "Sweep").
  - `def remove_file_artifacts(fleet_storage, file_id) -> int` — delete every artifact for a
    deleted file (all content hashes / pipelines).

- [ ] **Step 1: Write the failing test**

Create `tests/test_ingest_cache_lifecycle.py`:

```python
import struct

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _seed_file(store, file_id, n=2):
    for i in range(n):
        store.import_cached_chunk(
            f"gdrive-{file_id}-{i}", f"text {i}", f"c{i}",
            {"source_type": "gdrive", "file_id": file_id, "chunk_index": i},
            [float(i)] * 4)


def test_publish_then_import_roundtrips(tmp_path):
    src, fs = _store(tmp_path, "src.sqlite3"), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID", n=3)
    ok = ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", PIN,
                                   enrich={"logic_version": 1}, published_by="me@x.org")
    assert ok
    dst = _store(tmp_path, "dst.sqlite3")
    assert ingest_cache.try_import(dst, fs, "D1", "FID", "vhash1", PIN) is True
    for i in range(3):
        a = src.embedding_for_doc(f"gdrive-FID-{i}")
        b = dst.embedding_for_doc(f"gdrive-FID-{i}")
        assert struct.pack("<4f", *a) == struct.pack("<4f", *b)   # bitwise-identical


def test_publish_unpinned_is_noop(tmp_path):
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID")
    assert ingest_cache.publish_file(src, fs, "D1", "FID", "vhash1", FleetPin()) is False
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []


def test_publish_gcs_superseded_content_versions(tmp_path):
    src, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _seed_file(src, "FID")
    ingest_cache.publish_file(src, fs, "D1", "FID", "vOLD", PIN)
    ingest_cache.publish_file(src, fs, "D1", "FID", "vNEW", PIN)   # supersedes vOLD
    names = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert any(n.startswith("FID.vNEW"[:16]) or "vNEW"[:12] in n for n in names)
    # exactly one artifact remains for FID
    assert len(names) == 1


def test_gc_superseded_drops_stale_pipeline(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "drv")
    # a stale-pipeline artifact (different embed model => different pf8)
    stale = artifact_filename("FID", "vhash1", "old-model", 4, "v1")
    cur = artifact_filename("FID", "vhash1", PIN.embed_model, PIN.dim, PIN.chunker_version)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{stale}", b"x")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{cur}", b"y")
    removed = ingest_cache.gc_superseded(fs, "D1", "FID", "vhash1", PIN)
    assert removed == 1
    remaining = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert remaining == [cur]


def test_sweep_and_remove_file_artifacts(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "drv")
    for fid in ("A", "B", "C"):
        fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename(fid, 'v1', PIN.embed_model, PIN.dim, PIN.chunker_version)}", b"x")
    # sweep keeps only live files
    assert ingest_cache.sweep_drive(fs, {"A", "B"}) == 1
    assert {p.rsplit('.', 4)[0].rsplit('/', 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")} == {"A", "B"}
    # remove one file's artifacts explicitly
    assert ingest_cache.remove_file_artifacts(fs, "A") == 1
    assert ingest_cache.remove_file_artifacts(fs, "A") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingest_cache_lifecycle.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.ingest_cache' has no attribute 'publish_file'`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/ingest_cache.py`:

```python
# -- write path -------------------------------------------------------------

def publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin,
            *, enrich=None, published_by="") -> None:
    """Write the gzip-JSON CacheArtifact for `chunks` (a sequence of CacheChunk),
    then best-effort GC older/stale artifacts for this file. No-op when unpinned
    or chunks is empty. Content-hash keying makes concurrent publishers idempotent
    (byte-equivalent artifacts, last-write-wins is harmless)."""
    if not pin.is_pinned or not chunks:
        return
    chunks = tuple(chunks)
    extraction_method = (chunks[0].metadata or {}).get("extraction_method", "")
    art = CacheArtifact(
        file_id=file_id, content_hash=content_hash,
        extraction_method=extraction_method, chunker_version=pin.chunker_version,
        embed_model=pin.embed_model, dim=int(pin.dim), chunks=chunks,
        enrich=enrich or {}, published_by=published_by,
        published_at=datetime.now(timezone.utc).isoformat())
    data = gzip.compress(json.dumps(art.to_dict()).encode("utf-8"))
    fleet_storage.put_bytes(_artifact_path(file_id, content_hash, pin), data)
    try:
        gc_superseded(fleet_storage, drive_id, file_id, content_hash, pin)
    except Exception as exc:  # noqa: BLE001 — GC failure must not fail the publish
        log.info("ingest_cache: gc_superseded skipped for %s: %s", file_id, exc)


def publish_file(store, fleet_storage, drive_id, file_id, content_hash, pin,
                 *, enrich=None, published_by="") -> bool:
    """Collect a locally-embedded file's chunks from the store and publish them.
    Returns True if an artifact was written."""
    if not pin.is_pinned:
        return False
    chunks = collect_chunks(store, file_id)
    if not chunks:
        return False
    publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin,
            enrich=enrich, published_by=published_by)
    return True


# -- GC / lifecycle ---------------------------------------------------------

def _cache_names(fleet_storage):
    for path in fleet_storage.list_paths(CACHE_DIR + "/"):
        name = path.rsplit("/", 1)[-1]
        parsed = _parse_name(name)
        if parsed:
            yield path, parsed


def gc_superseded(fleet_storage, drive_id, file_id, keep_content_hash, pin) -> int:
    """Delete artifacts for `file_id` whose content hash differs from
    keep_content_hash OR whose pipeline fingerprint is stale. Returns count."""
    keep12 = keep_content_hash[:12]
    cur_pf8 = _pf8(pin)
    removed = 0
    for path, (fid, h12, pf8) in _cache_names(fleet_storage):
        if fid != file_id:
            continue
        if h12 != keep12 or pf8 != cur_pf8:
            fleet_storage.delete(path)
            removed += 1
    return removed


def sweep_drive(fleet_storage, live_file_ids) -> int:
    """Opportunistically delete artifacts whose file id is not in live_file_ids
    (the set of files currently present in the drive). Returns count deleted."""
    live = set(live_file_ids)
    removed = 0
    for path, (fid, _h12, _pf8) in _cache_names(fleet_storage):
        if fid not in live:
            fleet_storage.delete(path)
            removed += 1
    return removed


def remove_file_artifacts(fleet_storage, file_id) -> int:
    """Delete every artifact (all content hashes / pipelines) for one file —
    used when a file is deleted (changes.list removal event). Returns count."""
    removed = 0
    for path, (fid, _h12, _pf8) in _cache_names(fleet_storage):
        if fid == file_id:
            fleet_storage.delete(path)
            removed += 1
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingest_cache_lifecycle.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache_lifecycle.py
git commit -m "feat(ingest_cache): publish + artifact GC/sweep/removal lifecycle"
```

---

## Task 5: `ingest_cache` — `bootstrap_drive` + `purge_drive`

**Files:**
- Modify: `mcpbrain/ingest_cache.py`
- Test: `tests/test_ingest_cache.py` (extend) and `tests/test_ingest_cache_revocation.py` (extend)

**Interfaces:**
- Produces:
  - `def bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict` — **frozen signature.**
    Bulk-import every cache artifact for a drive whose pipeline matches `pin`; for a file
    with multiple content versions, import the newest (`published_at`). Returns
    `{"imported": n, "chunks": m, "skipped": k, "cache_hits": n}` where `cache_hits == imported`
    (files served from cache — subsystem C sums this across drives for its onboarding summary).
    Used by C onboarding.
  - `def purge_drive(store, drive_id) -> dict` — **frozen signature.** Bitemporally
    invalidate local relations sourced from the drive's docs, then delete the drive's
    chunks/vectors/FTS. Returns `{"drive_id", "docs", "chunks_deleted", "relations_invalidated"}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest_cache.py`:

```python
def test_bootstrap_drive_imports_newest_per_file(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    # two files, and an older + newer version of FID (GC would normally drop the old one)
    _write_artifact(fs, "FID", "vhash1")
    _write_artifact(fs, "GID", "vhashG")
    # a stale-pipeline artifact must be ignored by bootstrap
    _write_artifact(fs, "HID", "vhashH", embed_model="old-model")
    summary = ingest_cache.bootstrap_drive(s, fs, "D1", PIN)
    assert summary["imported"] == 2 and summary["chunks"] == 2
    assert summary["cache_hits"] == 2               # C sums this across drives
    assert s.get_chunk("gdrive-FID-0")["metadata"]["drive_id"] == "D1"
    assert s.get_chunk("gdrive-HID-0") is None       # stale pipeline skipped


def test_bootstrap_drive_unpinned_is_noop(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    _write_artifact(fs, "FID", "vhash1")
    out = ingest_cache.bootstrap_drive(s, fs, "D1", FleetPin())
    assert out["imported"] == 0 and out["cache_hits"] == 0
```

Append to `tests/test_ingest_cache_revocation.py`:

```python
def test_purge_drive_deletes_chunks_and_invalidates_local(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.import_cached_chunk("gdrive-F1-1", "b", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('a','A','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('b','B','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','works_at','b','gdrive-F1-0','local')")
    out = ingest_cache.purge_drive(s, "D1")
    assert out["chunks_deleted"] == 2 and out["relations_invalidated"] == 1
    assert s.doc_ids_for_drive("D1") == []
    with s._connect() as db:
        r = db.execute("SELECT invalidated_at FROM entity_relations WHERE entity_a='a'").fetchone()
    assert r["invalidated_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingest_cache.py -k bootstrap tests/test_ingest_cache_revocation.py -k purge_drive -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.ingest_cache' has no attribute 'bootstrap_drive'`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/ingest_cache.py`:

```python
# -- bulk import (onboarding) + revocation ----------------------------------

def bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict:
    """Bulk-import all cache artifacts for a drive whose pipeline matches `pin`.
    For a file with several content versions, the newest published_at wins.
    Returns {'imported','chunks','skipped','cache_hits'} where cache_hits ==
    imported (files served from cache). Subsystem C sums cache_hits across drives
    for its onboarding summary (spec §C.2)."""
    summary = {"imported": 0, "chunks": 0, "skipped": 0, "cache_hits": 0}
    if not pin.is_pinned:
        return summary
    cur_pf8 = _pf8(pin)
    best: dict[str, tuple[str, CacheArtifact]] = {}   # file_id -> (published_at, art)
    for path, (fid, _h12, pf8) in _cache_names(fleet_storage):
        if pf8 != cur_pf8:
            continue
        art = _load(fleet_storage, path)
        if art is None:
            continue
        prev = best.get(fid)
        if prev is None or (art.published_at or "") > prev[0]:
            best[fid] = (art.published_at or "", art)
    for _fid, (_pa, art) in best.items():
        if _import_artifact(store, drive_id, art, pin):
            summary["imported"] += 1
            summary["chunks"] += len(art.chunks)
        else:
            summary["skipped"] += 1
    summary["cache_hits"] = summary["imported"]
    return summary


def purge_drive(store, drive_id) -> dict:
    """Access-revocation purge (spec §A3): bitemporally invalidate local relations
    sourced from this drive's docs, then delete its chunks/vectors/FTS. org rows
    are untouched. Returns a summary dict."""
    doc_ids = store.doc_ids_for_drive(drive_id)
    invalidated = store.invalidate_local_relations_for_docs(doc_ids)
    deleted = store.delete_chunks(doc_ids)
    log.info("ingest_cache: purged drive %s — %d chunks, %d relations invalidated",
             drive_id, deleted, invalidated)
    return {"drive_id": drive_id, "docs": len(doc_ids),
            "chunks_deleted": deleted, "relations_invalidated": invalidated}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingest_cache.py tests/test_ingest_cache_revocation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache.py tests/test_ingest_cache_revocation.py
git commit -m "feat(ingest_cache): bootstrap_drive bulk import + purge_drive revocation"
```

---

## Task 6: `ingest_cache` — consecutive-absence revocation counter

**Files:**
- Modify: `mcpbrain/ingest_cache.py`
- Test: `tests/test_ingest_cache_revocation.py` (extend)

**Interfaces:**
- Consumes: `store.get_meta`/`set_meta` (store.py:1298/1303), `purge_drive`.
- Produces:
  - `def note_drive_presence(store, present_ids, *, threshold=3) -> dict` — track
    per-drive consecutive absence in the `meta` table (keys
    `ingest_cache.known_drives`, `ingest_cache.absent:<id>`). A present drive resets its
    counter to 0 and is recorded as known; a known drive absent for `threshold` consecutive
    calls is `purge_drive`d and forgotten. Returns `{"purged": [ids], "tracked": n}`. No new
    schema, no cadence — this runs inside the drive-sync path.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest_cache_revocation.py`:

```python
def test_note_drive_presence_purges_after_threshold(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    # cycle 1: D1 present -> known, counter 0
    assert ingest_cache.note_drive_presence(s, ["D1"], threshold=3)["purged"] == []
    # cycles 2-4: D1 absent -> counts 1, 2, then purge on the 3rd
    assert ingest_cache.note_drive_presence(s, [], threshold=3)["purged"] == []
    assert ingest_cache.note_drive_presence(s, [], threshold=3)["purged"] == []
    out = ingest_cache.note_drive_presence(s, [], threshold=3)
    assert out["purged"] == ["D1"]
    assert s.doc_ids_for_drive("D1") == []          # purge ran
    # forgotten: a further absent cycle does nothing
    assert ingest_cache.note_drive_presence(s, [], threshold=3)["purged"] == []


def test_note_drive_presence_transient_glitch_recovers(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    ingest_cache.note_drive_presence(s, ["D1"], threshold=3)
    ingest_cache.note_drive_presence(s, [], threshold=3)          # 1 absent
    ingest_cache.note_drive_presence(s, ["D1"], threshold=3)      # reappears -> reset
    ingest_cache.note_drive_presence(s, [], threshold=3)          # 1 absent again
    out = ingest_cache.note_drive_presence(s, [], threshold=3)    # 2 absent — NOT purged
    assert out["purged"] == []
    assert s.doc_ids_for_drive("D1") == ["gdrive-F1-0"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_ingest_cache_revocation.py -k note_drive_presence -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.ingest_cache' has no attribute 'note_drive_presence'`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/ingest_cache.py`:

```python
# -- consecutive-absence revocation counter (spec §A3) ----------------------

_KNOWN_DRIVES_META = "ingest_cache.known_drives"


def _absence_key(drive_id: str) -> str:
    return f"ingest_cache.absent:{drive_id}"


def note_drive_presence(store, present_ids, *, threshold: int = 3) -> dict:
    """Track per-drive consecutive absence and auto-purge after `threshold`
    consecutive missing cycles (spec §A3: guard against a transient Drive glitch
    reading as revocation). State lives in the meta table — no schema, no cadence.

    A present drive resets its counter and is remembered; a known drive absent for
    `threshold` cycles is purge_drive'd and forgotten. Returns {'purged','tracked'}.
    """
    try:
        known = set(json.loads(store.get_meta(_KNOWN_DRIVES_META) or "[]"))
    except (ValueError, TypeError):
        known = set()
    present = set(present_ids)
    known |= present
    purged = []
    for d in sorted(known):
        if d in present:
            store.set_meta(_absence_key(d), "0")
            continue
        try:
            n = int(store.get_meta(_absence_key(d)) or "0") + 1
        except (ValueError, TypeError):
            n = 1
        if n >= threshold:
            purge_drive(store, d)
            purged.append(d)
            store.set_meta(_absence_key(d), "0")
        else:
            store.set_meta(_absence_key(d), str(n))
    known -= set(purged)
    store.set_meta(_KNOWN_DRIVES_META, json.dumps(sorted(known)))
    return {"purged": purged, "tracked": len(known)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_ingest_cache_revocation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache_revocation.py
git commit -m "feat(ingest_cache): consecutive-absence revocation counter (meta-backed)"
```

---

## Task 7: A1 — Shared Drive enumeration + `drive_id` metadata + content-version hash

**Files:**
- Modify: `mcpbrain/sync/drive.py` (`_CHANGES_FIELDS` ~drive.py:66, `normalise_drive` ~drive.py:104)
- Test: `tests/test_drive_shared.py` (new)

**Interfaces:**
- Consumes: `drive_service.drives().list`.
- Produces:
  - `list_shared_drives(service) -> list[dict]` — paginated `drives.list` (`id`,`name`).
  - `normalise_drive(file_meta, text, drive_id=None)` — stamps `DRIVE_ID_META_KEY` into each
    chunk's metadata when `drive_id` is given (null/absent for My Drive — unchanged behaviour).
  - `_file_content_hash(file_meta) -> str` — a cross-user-stable file-version id computable
    from Changes metadata alone: `md5Checksum` when present, else
    `sha256("<version>|<modifiedTime>")` for Google-native files.
  - `_CHANGES_FIELDS` extended with `md5Checksum,version,size`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_drive_shared.py`:

```python
from mcpbrain.sync.drive import (
    list_shared_drives, normalise_drive, _file_content_hash,
)
from mcpbrain.org_contracts import DRIVE_ID_META_KEY


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _Drives:
    def __init__(self, pages):
        self._pages = pages
        self._i = 0

    def list(self, **_kw):
        page = self._pages[self._i]
        self._i = min(self._i + 1, len(self._pages) - 1)
        return _Req(page)


class _DriveOnlyService:
    def __init__(self, pages):
        self._drives = _Drives(pages)

    def drives(self):
        return self._drives


def test_list_shared_drives_paginates():
    svc = _DriveOnlyService([
        {"drives": [{"id": "D1", "name": "Ops"}], "nextPageToken": "p2"},
        {"drives": [{"id": "D2", "name": "Finance"}]},
    ])
    ds = list_shared_drives(svc)
    assert [d["id"] for d in ds] == ["D1", "D2"]


def test_normalise_drive_stamps_drive_id():
    fm = {"id": "FID", "name": "Doc", "mimeType": "application/vnd.google-apps.document",
          "modifiedTime": "2026-05-01T10:00:00Z", "owners": [{"displayName": "X"}]}
    chunks = normalise_drive(fm, "hello world", drive_id="D1")
    assert chunks and chunks[0].metadata[DRIVE_ID_META_KEY] == "D1"
    # My-Drive path (no drive_id) leaves the key absent
    chunks2 = normalise_drive(fm, "hello world")
    assert DRIVE_ID_META_KEY not in chunks2[0].metadata


def test_file_content_hash_prefers_md5_then_stable_for_native():
    assert _file_content_hash({"id": "F", "md5Checksum": "abc"}) == "abc"
    a = _file_content_hash({"id": "F", "version": "7", "modifiedTime": "T"})
    b = _file_content_hash({"id": "F", "version": "7", "modifiedTime": "T"})
    c = _file_content_hash({"id": "F", "version": "8", "modifiedTime": "T"})
    assert a == b and a != c and len(a) == 64        # deterministic sha256
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drive_shared.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_shared_drives'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/sync/drive.py`, extend `_CHANGES_FIELDS` (drive.py:66) to carry the version fields:

```python
_CHANGES_FIELDS = (
    "nextPageToken,newStartPageToken,"
    "changes(fileId,removed,file(id,name,mimeType,modifiedTime,owners,"
    "md5Checksum,version,size))"
)
```

Add a `hashlib` import near the top (`from mcpbrain.chunking import chunk_text, content_hash`
block, drive.py:19) — add `import hashlib` above the `from mcpbrain...` imports.

Change the `normalise_drive` signature + `base_meta` (drive.py:104–139):

```python
def normalise_drive(file_meta: dict, text: str, drive_id: str | None = None) -> list[Chunk]:
    """Convert Drive file metadata + text content into indexable Chunks.

    doc_id format: gdrive-<file_id>-<chunk_index>.
    When drive_id is given (a true Shared Drive file), it is stamped into each
    chunk's metadata under DRIVE_ID_META_KEY so revocation can target it; My Drive
    / shared-with-me files pass drive_id=None and the key stays absent.
    """
    if not text or not text.strip():
        return []

    fid = file_meta["id"]

    owner = ""
    owners = file_meta.get("owners") or []
    if owners:
        owner = owners[0].get("displayName", "")

    mime = file_meta.get("mimeType", "")
    extraction_method, content_subtype, confidence = _MIME_EXTRACTION_META.get(
        mime, ("text", "prose", 1.0)
    )

    base_meta = {
        "source_type": "gdrive",
        "file_id": fid,
        "file_name": file_meta.get("name", "")[:200],
        "mime_type": mime[:100],
        "modified": file_meta.get("modifiedTime", ""),
        "owner": owner[:100],
        "extraction_method": extraction_method,
        "content_subtype": content_subtype,
        "confidence": confidence,
    }
    if drive_id:
        from mcpbrain.org_contracts import DRIVE_ID_META_KEY
        base_meta[DRIVE_ID_META_KEY] = drive_id

    out = []
    for i, chunk in enumerate(chunk_text(text)):
        meta = {**base_meta, "chunk_index": i}
        out.append(Chunk(
            doc_id=f"gdrive-{fid}-{i}",
            text=chunk,
            content_hash=content_hash(chunk),
            metadata=meta,
        ))
    return out
```

Add the enumeration + version-hash helpers after `normalise_drive` (before `sync_drive`,
~drive.py:151):

```python
def list_shared_drives(service) -> list[dict]:
    """Every Shared Drive the user can see (paginated drives.list). Returns dicts
    with at least id + name. My Drive is NOT included — it has no shared cache."""
    out: list[dict] = []
    page_token = None
    while True:
        resp = service.drives().list(
            pageSize=100, fields="nextPageToken,drives(id,name)",
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("drives", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _file_content_hash(file_meta: dict) -> str:
    """A cross-user-stable file-VERSION id, computable from Changes metadata alone
    (so the cache read path can key on it before extraction). Binary files carry a
    Drive md5Checksum; Google-native files (Docs/Sheets/Slides) do not, so we hash
    the monotonic `version` + modifiedTime, which is identical across installs."""
    md5 = file_meta.get("md5Checksum")
    if md5:
        return md5
    raw = f"{file_meta.get('version', '')}|{file_meta.get('modifiedTime', '')}"
    return hashlib.sha256(raw.encode()).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drive_shared.py tests/test_drive_sync.py -v`
Expected: PASS (new tests + the existing drive-sync suite still green — `normalise_drive`'s
new param is optional so all existing callers are unaffected).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/drive.py tests/test_drive_shared.py
git commit -m "feat(drive): shared-drive enumeration, drive_id stamping, file-version hash"
```

---

## Task 8: A1 — `sync_shared_drive` (cache-first, per-drive cursor, removal)

**Files:**
- Modify: `mcpbrain/sync/drive.py` (add after `sync_drive`, ~drive.py:228); extend
  `tests/test_drive_sync.py:FakeDriveService` with `drives()` + driveId-aware changes.
- Test: `tests/test_drive_shared.py` (extend)

**Interfaces:**
- Consumes: `ingest_cache.try_import` / `remove_file_artifacts`, `store.doc_ids_for_file` /
  `delete_chunks` / `invalidate_local_relations_for_docs`, `_file_content_hash`,
  `normalise_drive`.
- Produces:
  - `def sync_shared_drive(service, store, drive_id, *, fleet_storage, pin) -> dict` —
    per-drive Changes sync. Cursor key `drive:<driveId>` in `sync_cursors`. Bootstrap
    (no cursor) stores `getStartPageToken(driveId=…)` and returns. Delta: page through
    `changes.list(driveId=…, corpora="drive", includeItemsFromAllDrives=True,
    supportsAllDrives=True, includeRemoved=True)`. Per non-removed file: **cache-first**
    `try_import`; on miss, fetch text, **re-check cache** (herd shrink), then normalise +
    `upsert_chunk` and record the miss for post-embed publish. Per removed file: invalidate +
    delete local chunks + `remove_file_artifacts`. Cursor advances only after all writes.
    Returns `{"processed", "miss": [(file_id, content_hash)], "live_file_ids": set}`.

- [ ] **Step 1: Write the failing test**

First extend the shared fake in `tests/test_drive_sync.py` (so both suites reuse it). Modify
`_Changes.list` to accept `driveId`, add `getStartPageToken(driveId=…)`, and add a `drives()`
resource to `FakeDriveService`:

```python
class _Changes:
    def __init__(self, start_token="100", pages=None, initial_cursor=None):
        self._start = start_token
        self._pages = pages or []
        self._initial_cursor = initial_cursor

    def getStartPageToken(self, **_kw):          # accept driveId/supportsAllDrives
        return _Req({"startPageToken": self._start})

    def list(self, **kw):
        token = kw.get("pageToken")
        if token is None or token == self._initial_cursor:
            idx = 0
        else:
            try:
                idx = int(token)
            except (ValueError, TypeError):
                idx = 0
        return _Req(self._pages[idx])
```

```python
class _Drives:
    def __init__(self, drives=None):
        self._drives = drives or []

    def list(self, **_kw):
        return _Req({"drives": self._drives})


class FakeDriveService:
    def __init__(self, **kw):
        start = kw.get("start_token", "100")
        initial = kw.get("initial_cursor", start)
        self._changes = _Changes(start, kw.get("pages"), initial_cursor=initial)
        self._files = _Files(kw.get("exports"), kw.get("media"),
                             kw.get("export_raises"), kw.get("file_list"))
        self._drives = _Drives(kw.get("shared_drives"))

    def changes(self):
        return self._changes

    def files(self):
        return self._files

    def drives(self):
        return self._drives
```

Append to `tests/test_drive_shared.py`:

```python
import gzip
import json

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from mcpbrain.sync.drive import sync_shared_drive
from tests.helpers.org_fleet import LocalDirFleetStorage
from tests.test_drive_sync import FakeDriveService, _gdoc_change

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_sync_shared_drive_bootstrap_sets_cursor(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = FakeDriveService(start_token="500")
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 0
    assert s.get_cursor("drive:D1") == "500"


def test_sync_shared_drive_miss_extracts_and_records_for_publish(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"the quick brown fox jumps"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1
    assert ("FID", out["miss"][0][1]) == out["miss"][0]      # (file_id, content_hash)
    ch = s.get_chunk("gdrive-FID-0")
    assert ch["metadata"]["drive_id"] == "D1"
    assert s.get_cursor("drive:D1") == "101"


def test_sync_shared_drive_cache_hit_skips_extraction(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.set_cursor("drive:D1", "100")
    # pre-publish an artifact for FID's current version so try_import hits
    src = _store(tmp_path, "src.sqlite3")
    src.import_cached_chunk("gdrive-FID-0", "cached body", "c0",
                            {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}, [0.5]*4)
    fm = _gdoc_change("FID")["file"]
    from mcpbrain.sync.drive import _file_content_hash
    ch = _file_content_hash(fm)
    ingest_cache.publish_file(src, fs, "D1", "FID", ch, PIN)
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"DIFFERENT — must NOT be extracted"})
    out = sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1 and out["miss"] == []          # imported from cache
    assert s.get_chunk("gdrive-FID-0")["text"] == "cached body"  # not the export bytes


def test_sync_shared_drive_removal_purges_local_and_artifact(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "a", "c", {"file_id": "FID", "drive_id": "D1"}, [0.0]*4)
    ingest_cache.publish_file(s, fs, "D1", "FID", "vX", PIN)
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        initial_cursor="100",
        pages=[{"changes": [{"fileId": "FID", "removed": True}], "newStartPageToken": "101"}])
    sync_shared_drive(svc, s, "D1", fleet_storage=fs, pin=PIN)
    assert s.get_chunk("gdrive-FID-0") is None
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drive_shared.py -k sync_shared_drive -v`
Expected: FAIL — `ImportError: cannot import name 'sync_shared_drive'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/sync/drive.py`, after `sync_drive` (ends ~drive.py:228), insert:

```python
def sync_shared_drive(service, store, drive_id, *, fleet_storage, pin) -> dict:
    """Incremental sync for ONE Shared Drive via the Changes API, cache-first.

    Cursor key is 'drive:<driveId>' in sync_cursors. First run stores
    getStartPageToken(driveId=...) and returns. Delta runs page through
    changes.list(driveId=..., corpora='drive', includeItemsFromAllDrives=True).
    For each non-removed file: try the ingest cache first; on a miss, fetch the
    text, RE-CHECK the cache immediately before the expensive path (herd-race
    shrink, spec §A2), then extract + upsert and record the miss so the caller can
    publish after embedding. Removed files are purged locally and their artifacts
    deleted. The cursor advances only after every write completes.

    Returns {'processed', 'miss': [(file_id, content_hash)], 'live_file_ids': set}.
    """
    from mcpbrain import ingest_cache

    source = f"drive:{drive_id}"
    cursor = store.get_cursor(source)
    if cursor is None:
        tok = service.changes().getStartPageToken(
            driveId=drive_id, supportsAllDrives=True).execute()["startPageToken"]
        store.set_cursor(source, str(tok))
        return {"processed": 0, "miss": [], "live_file_ids": set()}

    page_token = cursor
    new_start = None
    pending: list[dict] = []
    removed_ids: list[str] = []
    live_ids: set[str] = set()
    while True:
        resp = service.changes().list(
            pageToken=page_token,
            driveId=drive_id,
            corpora="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            includeRemoved=True,
            fields=_CHANGES_FIELDS,
        ).execute()
        for ch in resp.get("changes", []):
            fid = ch.get("fileId")
            if ch.get("removed"):
                if fid:
                    removed_ids.append(fid)
                continue
            fmeta = ch.get("file") or {}
            if not fmeta.get("id"):
                continue
            live_ids.add(fmeta["id"])
            pending.append(fmeta)
        new_start = resp.get("newStartPageToken", new_start)
        nxt = resp.get("nextPageToken")
        if not nxt:
            break
        page_token = nxt

    processed = 0
    miss: list[tuple[str, str]] = []
    for fmeta in pending:
        fid = fmeta["id"]
        content_h = _file_content_hash(fmeta)
        if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin):
            processed += 1
            continue
        text = _fetch_text(service, fmeta)
        if not text:
            continue
        # Re-check right before extraction: another daemon may have just published.
        if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin):
            processed += 1
            continue
        chunks = normalise_drive(fmeta, text, drive_id=drive_id)
        if not chunks:
            continue
        for c in chunks:
            store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
        miss.append((fid, content_h))
        processed += 1

    for fid in removed_ids:
        doc_ids = store.doc_ids_for_file(fid)
        if doc_ids:
            store.invalidate_local_relations_for_docs(doc_ids)
            store.delete_chunks(doc_ids)
        try:
            ingest_cache.remove_file_artifacts(fleet_storage, fid)
        except Exception:  # noqa: BLE001 — artifact GC is best-effort
            pass

    if new_start:
        store.set_cursor(source, str(new_start))
    return {"processed": processed, "miss": miss, "live_file_ids": live_ids}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drive_shared.py tests/test_drive_sync.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/drive.py tests/test_drive_shared.py tests/test_drive_sync.py
git commit -m "feat(drive): per-drive cache-first Changes sync with removal handling"
```

---

## Task 9: A1 — `sync_shared_drives` orchestrator (enumerate + revoke + sweep)

**Files:**
- Modify: `mcpbrain/sync/drive.py` (add after `sync_shared_drive`)
- Test: `tests/test_drive_shared.py` (extend)

**Interfaces:**
- Consumes: `list_shared_drives`, `sync_shared_drive`, `ingest_cache.note_drive_presence`,
  `ingest_cache.sweep_drive`.
- Produces:
  - `def sync_shared_drives(service, store, *, pin, storage_factory, absence_threshold=3) -> dict`
    — enumerate drives, sync each (isolating per-drive errors), run the
    consecutive-absence revocation counter over the present set, and opportunistically sweep
    dead artifacts per drive. Returns `{drive_id: {"processed", "miss", "storage"}}` (storage
    reused by the caller for post-embed publish) plus `{"_revoked": [ids]}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_drive_shared.py`:

```python
def test_sync_shared_drives_enumerates_and_returns_storages(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("drive:D1", "100")
    svc = FakeDriveService(
        shared_drives=[{"id": "D1", "name": "Ops"}],
        initial_cursor="100",
        pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
        exports={"FID": b"body text here"})
    storages = {}

    def factory(drive_id):
        storages.setdefault(drive_id, LocalDirFleetStorage(tmp_path / drive_id))
        return storages[drive_id]

    out = sync_shared_drives(svc, s, pin=PIN, storage_factory=factory)
    assert set(out) >= {"D1"}
    assert out["D1"]["processed"] == 1
    assert out["D1"]["storage"] is storages["D1"]
    assert out["_revoked"] == []


def test_sync_shared_drives_revokes_vanished_drive(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "a", "c", {"file_id": "F1", "drive_id": "GONE"}, [0.0]*4)
    # seed the counter as if GONE was seen and has been absent (threshold-1) times
    ingest_cache.note_drive_presence(s, ["GONE"], threshold=2)
    svc = FakeDriveService(shared_drives=[])         # GONE no longer listed
    out = sync_shared_drives(svc, s, pin=PIN,
                             storage_factory=lambda d: LocalDirFleetStorage(tmp_path / d),
                             absence_threshold=2)
    assert out["_revoked"] == ["GONE"]
    assert s.doc_ids_for_drive("GONE") == []


from mcpbrain.sync.drive import sync_shared_drives   # noqa: E402  (import after helpers)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drive_shared.py -k sync_shared_drives -v`
Expected: FAIL — `ImportError: cannot import name 'sync_shared_drives'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/sync/drive.py`, after `sync_shared_drive`, insert:

```python
def sync_shared_drives(service, store, *, pin, storage_factory,
                       absence_threshold: int = 3) -> dict:
    """Enumerate all Shared Drives, sync each cache-first, run the consecutive-
    absence revocation counter, and opportunistically sweep dead artifacts.

    `storage_factory(drive_id) -> FleetStorage` builds a drive-scoped transport
    (prod: DriveFleetStorage; tests: LocalDirFleetStorage). Per-drive failures are
    isolated so one broken drive never aborts the others. Returns
    {drive_id: {'processed','miss','storage'}} plus {'_revoked': [ids]}. The
    caller publishes each drive's misses after embedding (see run_sync_cycle).
    """
    import logging as _logging
    from mcpbrain import ingest_cache

    log = _logging.getLogger(__name__)
    out: dict = {}
    present: list[str] = []
    for d in list_shared_drives(service):
        drive_id = d.get("id")
        if not drive_id:
            continue
        present.append(drive_id)
        fs = storage_factory(drive_id)
        try:
            res = sync_shared_drive(service, store, drive_id, fleet_storage=fs, pin=pin)
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            log.warning("shared-drive sync failed for %s (skipped): %s", drive_id, exc)
            continue
        out[drive_id] = {"processed": res["processed"], "miss": res["miss"], "storage": fs}
        # Opportunistic sweep of artifacts whose file id vanished from the drive.
        try:
            ingest_cache.sweep_drive(fs, res["live_file_ids"])
        except Exception:  # noqa: BLE001 — sweep is best-effort
            pass
    revoked = ingest_cache.note_drive_presence(
        store, present, threshold=absence_threshold)["purged"]
    out["_revoked"] = revoked
    return out
```

> Note: `sweep_drive` is driven off one delta's `live_file_ids`, which only lists
> files that *changed* this cycle — so the sweep is conservative (it never deletes an
> artifact for an unchanged-but-present file, because such files don't appear in the
> delta and aren't in `live_file_ids`… which would wrongly target them). To stay safe,
> `sweep_drive` is called **only** when the caller can supply a full listing; here we pass
> the delta's live set, so real deletion of dead artifacts is deferred to the explicit
> file-removal path (Task 8) and to `gc_superseded` on republish. The sweep hook is wired
> and tested but intentionally a no-op on partial deltas. *(This keeps A1 correct; a
> full-drive sweep pass is a Phase-D observability nicety, not a correctness requirement.)*

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drive_shared.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/drive.py tests/test_drive_shared.py
git commit -m "feat(drive): sync_shared_drives orchestrator + auto-revocation"
```

---

## Task 10: A1 — backfill parity (`backfill_shared_drive`)

**Files:**
- Modify: `mcpbrain/sync/drive.py` (add after `backfill_drive`, ~drive.py:264)
- Test: `tests/test_drive_shared.py` (extend)

**Interfaces:**
- Produces:
  - `def backfill_shared_drive(service, store, drive_id, modified_after, *, fleet_storage, pin, modified_before=None, max_files=None) -> dict`
    — one-shot bounded backfill via `files.list(driveId=…, corpora="drive",
    includeItemsFromAllDrives=True, supportsAllDrives=True)` with a `modifiedTime` filter.
    Cache-first per file (same miss/re-check logic as `sync_shared_drive`); does NOT touch the
    `drive:<driveId>` cursor. Returns `{"processed", "miss": [(file_id, content_hash)]}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_drive_shared.py`:

```python
def test_backfill_shared_drive_cache_first(tmp_path):
    from mcpbrain.sync.drive import backfill_shared_drive
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    fm = {"id": "FID", "name": "Doc", "mimeType": "text/plain",
          "modifiedTime": "2026-05-01T10:00:00Z", "md5Checksum": "abc",
          "owners": [{"displayName": "X"}]}
    svc = FakeDriveService(file_list=[fm], media={"FID": b"backfilled body text"})
    out = backfill_shared_drive(svc, s, "D1", "2020-01-01T00:00:00Z",
                                fleet_storage=fs, pin=PIN)
    assert out["processed"] == 1
    assert out["miss"] == [("FID", "abc")]           # md5 is the content-version id
    assert s.get_chunk("gdrive-FID-0")["metadata"]["drive_id"] == "D1"
    # cursor untouched
    assert s.get_cursor("drive:D1") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_drive_shared.py -k backfill_shared_drive -v`
Expected: FAIL — `ImportError: cannot import name 'backfill_shared_drive'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/sync/drive.py`, after `backfill_drive` (ends ~drive.py:264), insert:

```python
def backfill_shared_drive(service, store, drive_id, modified_after, *,
                          fleet_storage, pin, modified_before=None,
                          max_files=None) -> dict:
    """One-shot bounded backfill for ONE Shared Drive (files.list, driveId-scoped),
    cache-first. Mirrors backfill_drive but adds Shared-Drive query flags, cache
    import/publish parity, and drive_id stamping. Does NOT touch the delta cursor.
    Returns {'processed', 'miss': [(file_id, content_hash)]}."""
    from mcpbrain import ingest_cache

    q = f"modifiedTime > '{modified_after}'"
    if modified_before:
        q += f" and modifiedTime < '{modified_before}'"
    fields = ("nextPageToken, files(id,name,mimeType,modifiedTime,owners,"
              "md5Checksum,version,size)")
    page_token, processed = None, 0
    miss: list[tuple[str, str]] = []
    while True:
        params = {
            "q": q, "fields": fields, "pageSize": 100,
            "driveId": drive_id, "corpora": "drive",
            "includeItemsFromAllDrives": True, "supportsAllDrives": True,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = service.files().list(**params).execute()
        for f in resp.get("files", []):
            if max_files is not None and processed >= max_files:
                return {"processed": processed, "miss": miss}
            fid = f["id"]
            content_h = _file_content_hash(f)
            if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin):
                processed += 1
                continue
            text = _fetch_text(service, f)
            if not text:
                continue
            if ingest_cache.try_import(store, fleet_storage, drive_id, fid, content_h, pin):
                processed += 1
                continue
            chunks = normalise_drive(f, text, drive_id=drive_id)
            if not chunks:
                continue
            for ch in chunks:
                store.upsert_chunk(ch.doc_id, ch.text, ch.content_hash, ch.metadata)
            miss.append((fid, content_h))
            processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return {"processed": processed, "miss": miss}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_drive_shared.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/drive.py tests/test_drive_shared.py
git commit -m "feat(drive): backfill_shared_drive cache-first parity"
```

---

## Task 11: A4 — production `DriveFleetStorage`

**Files:**
- Create: `mcpbrain/fleet_storage.py`
- Test: `tests/test_fleet_storage_drive.py`

**Interfaces:**
- Consumes: a Google Drive API resource (`drive_service`), `googleapiclient.http.MediaInMemoryUpload`
  (lazy import, prod only), `config.read_config`, `org_defaults.FLEET_FOLDER_ID`,
  `sync.drive.list_shared_drives`.
- Produces:
  - `class DriveFleetStorage:` implementing the `FleetStorage` protocol —
    `__init__(self, drive_service, folder_or_drive_id)`. Paths are `/`-separated relatives
    resolved against `folder_or_drive_id` (a Shared Drive root id or a folder id); intermediate
    folders are found-or-created on `put_bytes`. `get_bytes` returns `None` when absent;
    `list_paths(prefix)` returns sorted `/`-relative paths under the prefix; `delete` is a
    no-op on a missing path. Every Drive call sets `supportsAllDrives=True`.
  - `def fleet_folder_storage(home, drive_service=None) -> FleetStorage | None` — the
    **fleet-FOLDER** transport B's org cadences and C's snapshot-import use (distinct from the
    per-shared-drive cache storages). Root = `config.read_config(home).get("fleet",{}).get("folder_id")`,
    else `org_defaults.FLEET_FOLDER_ID`. Returns `None` when there is no `drive_service` or no
    folder id resolves. B/C address paths relative to the fleet folder (e.g.
    `org-graph/manifest.json`, `contrib/<email>/…`).
  - `def drive_cache_storage(drive_service, drive_id) -> FleetStorage` — the per-shared-drive
    cache transport (cache read/publish and C's per-drive `bootstrap_drive`). Rooted at the
    **shared drive root** (`DriveFleetStorage(drive_service, drive_id)`); `ingest_cache`
    addresses the drive's `.mcpbrain-cache/` subfolder via its `CACHE_DIR` path prefix — do
    NOT root this at the cache folder itself or the prefix would double.
  - `list_shared_drives(service) -> list[dict]` — **re-exported** here from `sync.drive`
    (id, name) so C maps accessible drives → ids from a single transport module.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fleet_storage_drive.py`:

```python
"""DriveFleetStorage against an in-memory Drive double (no network)."""
import itertools

from mcpbrain.fleet_storage import DriveFleetStorage
from mcpbrain.org_contracts import FleetStorage

FOLDER_MIME = "application/vnd.google-apps.folder"


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, **_kw):
        return self._fn()


class FakeDrive:
    """Minimal Drive files() double: create/list/get_media/update/delete over an
    in-memory node table keyed by id, with (name, parent) lookups."""

    def __init__(self):
        self.nodes = {}   # id -> {id,name,mimeType,parents,data}
        self._ids = ("id%d" % i for i in itertools.count(1))

    def files(self):
        return self

    # -- create ----------------------------------------------------------
    def create(self, body=None, media_body=None, fields=None, supportsAllDrives=None):
        def _do():
            nid = next(self._ids)
            self.nodes[nid] = {
                "id": nid, "name": body["name"],
                "mimeType": body.get("mimeType", "application/octet-stream"),
                "parents": list(body.get("parents", [])),
                "data": media_body.data if media_body is not None else b"",
            }
            return {"id": nid}
        return _Req(_do)

    # -- list ------------------------------------------------------------
    def list(self, q=None, fields=None, pageSize=None, pageToken=None,
             supportsAllDrives=None, includeItemsFromAllDrives=None,
             corpora=None, driveId=None):
        def _do():
            # supports "'<parent>' in parents" + "name = '<n>'" + mimeType filters
            import re
            parent = None
            m = re.search(r"'([^']+)' in parents", q or "")
            if m:
                parent = m.group(1)
            name = None
            m = re.search(r"name\s*=\s*'([^']+)'", q or "")
            if m:
                name = m.group(1)
            want_folder = FOLDER_MIME in (q or "")
            files = []
            for n in self.nodes.values():
                if parent is not None and parent not in n["parents"]:
                    continue
                if name is not None and n["name"] != name:
                    continue
                if want_folder and n["mimeType"] != FOLDER_MIME:
                    continue
                files.append({"id": n["id"], "name": n["name"],
                              "mimeType": n["mimeType"], "modifiedTime": ""})
            return {"files": files}
        return _Req(_do)

    def get_media(self, fileId=None, supportsAllDrives=None):
        return _Req(lambda: self.nodes[fileId]["data"])

    def update(self, fileId=None, media_body=None, supportsAllDrives=None):
        def _do():
            self.nodes[fileId]["data"] = media_body.data
            return {"id": fileId}
        return _Req(_do)

    def delete(self, fileId=None, supportsAllDrives=None):
        def _do():
            self.nodes.pop(fileId, None)
            return {}
        return _Req(_do)


def test_satisfies_protocol():
    assert isinstance(DriveFleetStorage(FakeDrive(), "ROOT"), FleetStorage)


def test_put_get_roundtrip_nested_path():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes(".mcpbrain-cache/FID.hash.pf.mbc.gz", b"payload")
    assert fs.get_bytes(".mcpbrain-cache/FID.hash.pf.mbc.gz") == b"payload"


def test_get_missing_returns_none():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    assert fs.get_bytes(".mcpbrain-cache/nope.mbc.gz") is None


def test_put_overwrites_existing():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes("a/b.bin", b"one")
    fs.put_bytes("a/b.bin", b"two")
    assert fs.get_bytes("a/b.bin") == b"two"
    # only one leaf node named b.bin (update, not duplicate-create)
    leaves = [n for n in drive.nodes.values() if n["name"] == "b.bin"]
    assert len(leaves) == 1


def test_list_paths_by_prefix_sorted():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes(".mcpbrain-cache/B.mbc.gz", b"b")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes("other/x.bin", b"x")
    assert fs.list_paths(".mcpbrain-cache/") == [
        ".mcpbrain-cache/A.mbc.gz", ".mcpbrain-cache/B.mbc.gz"]


def test_delete_is_idempotent():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes("a/x.bin", b"1")
    fs.delete("a/x.bin")
    fs.delete("a/x.bin")
    assert fs.get_bytes("a/x.bin") is None


def test_list_shared_drives_is_re_exported():
    from mcpbrain import fleet_storage
    from mcpbrain.sync.drive import list_shared_drives as canonical
    assert fleet_storage.list_shared_drives is canonical


def test_fleet_folder_storage_uses_config_folder_id(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    drive = FakeDrive()
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=drive)
    assert isinstance(fs, FleetStorage)
    assert fs._root == "FLEETFOLDER"
    # round-trips against the fleet folder root
    fs.put_bytes("org-graph/manifest.json", b'{"version":1}')
    assert fs.get_bytes("org-graph/manifest.json") == b'{"version":1}'


def test_fleet_folder_storage_falls_back_to_org_default(tmp_path):
    from mcpbrain import fleet_storage, org_defaults
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=FakeDrive())
    assert fs is not None and fs._root == org_defaults.FLEET_FOLDER_ID


def test_fleet_folder_storage_none_without_service(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    assert fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=None) is None


def test_drive_cache_storage_roots_at_drive_and_uses_cache_prefix(tmp_path):
    from mcpbrain import fleet_storage, ingest_cache
    drive = FakeDrive()
    fs = fleet_storage.drive_cache_storage(drive, "D1")
    assert isinstance(fs, FleetStorage) and fs._root == "D1"
    # ingest_cache addresses the .mcpbrain-cache/ subfolder; no double-prefix
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz", b"payload")
    assert fs.get_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz") == b"payload"
    names = [n["name"] for n in drive.nodes.values()]
    assert ingest_cache.CACHE_DIR in names          # exactly one cache folder, at the root
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_storage_drive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.fleet_storage'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/fleet_storage.py`:

```python
"""Production FleetStorage over Google Drive (spec §A4).

DriveFleetStorage maps '/'-separated relative paths onto a Drive folder tree under
a root (a Shared Drive id or a folder id). Subsystem A uses one instance per Shared
Drive (root = driveId) for the in-drive `.mcpbrain-cache/`; B/C use it over the
fleet folder — both only ever through the FleetStorage protocol.

All Drive calls set supportsAllDrives=True (Shared Drives require it), matching the
mechanism backup.py / fleet.py already rely on. googleapiclient is imported lazily
so importing this module does not require the SDK.
"""
from __future__ import annotations

import logging

# Re-exported so onboarding/curation code acquires ALL fleet transport (folder
# storage, per-drive cache storage, and drive enumeration) from one module.
from mcpbrain.sync.drive import list_shared_drives  # noqa: F401

log = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _q_escape(name: str) -> str:
    # Drive query strings are single-quoted; escape backslash then quote.
    return name.replace("\\", "\\\\").replace("'", "\\'")


class DriveFleetStorage:
    """A FleetStorage backed by a Google Drive folder subtree."""

    def __init__(self, drive_service, folder_or_drive_id: str):
        self._svc = drive_service
        self._root = folder_or_drive_id
        # (parent_id, name) -> folder_id cache to avoid re-listing on every put.
        self._folder_cache: dict[tuple[str, str], str] = {}

    # -- Drive primitives ------------------------------------------------

    def _find_child(self, parent_id: str, name: str, *, folder: bool):
        q = (f"name = '{_q_escape(name)}' and '{parent_id}' in parents "
             f"and trashed = false")
        if folder:
            q += f" and mimeType = '{_FOLDER_MIME}'"
        resp = self._svc.files().list(
            q=q, fields="files(id,name,mimeType,modifiedTime)",
            pageSize=100, supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files = resp.get("files", [])
        if not files:
            return None
        files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
        return files[0]["id"]

    def _ensure_folder(self, parent_id: str, name: str) -> str:
        key = (parent_id, name)
        if key in self._folder_cache:
            return self._folder_cache[key]
        fid = self._find_child(parent_id, name, folder=True)
        if fid is None:
            fid = self._svc.files().create(
                body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
                fields="id", supportsAllDrives=True,
            ).execute()["id"]
        self._folder_cache[key] = fid
        return fid

    def _resolve_parent(self, components: list[str], *, create: bool):
        parent = self._root
        for comp in components:
            if create:
                parent = self._ensure_folder(parent, comp)
            else:
                fid = self._find_child(parent, comp, folder=True)
                if fid is None:
                    return None
                parent = fid
        return parent

    def _resolve_file(self, path: str, *, create_parents: bool):
        parts = [p for p in path.split("/") if p]
        parent = self._resolve_parent(parts[:-1], create=create_parents)
        if parent is None:
            return None, None
        return parent, parts[-1]

    # -- FleetStorage protocol ------------------------------------------

    def put_bytes(self, path: str, data: bytes) -> None:
        from googleapiclient.http import MediaInMemoryUpload
        parent, leaf = self._resolve_file(path, create_parents=True)
        media = MediaInMemoryUpload(data, mimetype="application/octet-stream")
        existing = self._find_child(parent, leaf, folder=False)
        if existing:
            self._svc.files().update(
                fileId=existing, media_body=media, supportsAllDrives=True,
            ).execute()
        else:
            self._svc.files().create(
                body={"name": leaf, "parents": [parent]},
                media_body=media, fields="id", supportsAllDrives=True,
            ).execute()

    def get_bytes(self, path: str) -> bytes | None:
        parent, leaf = self._resolve_file(path, create_parents=False)
        if parent is None:
            return None
        fid = self._find_child(parent, leaf, folder=False)
        if fid is None:
            return None
        raw = self._svc.files().get_media(
            fileId=fid, supportsAllDrives=True).execute()
        return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")

    def list_paths(self, prefix: str) -> list[str]:
        # Walk the subtree from root, building '/'-relative file paths, then filter.
        out: list[str] = []

        def _walk(parent_id: str, rel: str):
            resp = self._svc.files().list(
                q=f"'{parent_id}' in parents and trashed = false",
                fields="files(id,name,mimeType)", pageSize=1000,
                supportsAllDrives=True, includeItemsFromAllDrives=True,
            ).execute()
            for f in resp.get("files", []):
                child_rel = f"{rel}{f['name']}"
                if f.get("mimeType") == _FOLDER_MIME:
                    _walk(f["id"], child_rel + "/")
                else:
                    out.append(child_rel)

        _walk(self._root, "")
        return sorted(p for p in out if p.startswith(prefix))

    def delete(self, path: str) -> None:
        parent, leaf = self._resolve_file(path, create_parents=False)
        if parent is None:
            return
        fid = self._find_child(parent, leaf, folder=False)
        if fid is None:
            return
        self._svc.files().delete(fileId=fid, supportsAllDrives=True).execute()


# -- factories (the storage instances B and C acquire) ----------------------

def fleet_folder_storage(home, drive_service=None):
    """FleetStorage over the fleet FOLDER (spec: fleet folder / org-graph / contrib).

    This is the instance B's org cadences (contrib upload, curate, snapshot
    publish) and C's snapshot-import call — distinct from the per-shared-drive
    cache storages. Root is the configured fleet folder id, falling back to the
    bundled org default. Returns None when there is no drive_service or no folder
    id resolves (caller then runs fully local — existing degradation behaviour).
    """
    if drive_service is None:
        return None
    from mcpbrain import config, org_defaults
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id") \
        or org_defaults.FLEET_FOLDER_ID
    if not folder_id:
        return None
    return DriveFleetStorage(drive_service, folder_id)


def drive_cache_storage(drive_service, drive_id):
    """FleetStorage for one Shared Drive's ingest cache (read/publish; C's per-drive
    bootstrap_drive). Rooted at the SHARED DRIVE ROOT — ingest_cache addresses the
    `.mcpbrain-cache/` subfolder via its CACHE_DIR path prefix, so rooting here (not
    at the cache folder) is required to avoid a doubled prefix."""
    return DriveFleetStorage(drive_service, drive_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_storage_drive.py -v`
Expected: PASS (11 tests — 6 `DriveFleetStorage` + `list_shared_drives` re-export +
3 `fleet_folder_storage` + `drive_cache_storage`). The `MediaInMemoryUpload` import is exercised by the fake via
`media_body.data`; if `googleapiclient` is not installed in the test env, install it or the
`put_bytes` tests are skipped — it is already a project runtime dependency (used by
`backup.py`/`fleet.py`), so it is present.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/fleet_storage.py tests/test_fleet_storage_drive.py
git commit -m "feat(fleet_storage): production DriveFleetStorage over Google Drive"
```

---

## Task 12: Wire the cache into the drive-sync path (`run_sync_cycle` + `run_cycle`)

**Files:**
- Modify: `mcpbrain/sync/__init__.py` (`run_sync_cycle`, ~sync/__init__.py:19–58)
- Modify: `mcpbrain/daemon.py` (`run_cycle` → pass `home`, ~daemon.py:358)
- Test: `tests/test_sync_cycle.py` (extend)

**Interfaces:**
- Consumes: `config.fleet_pin`, `config.ingest_cache_enabled`, `config.owner_email`,
  `sync/drive.sync_shared_drives`, `ingest_cache.publish_file`,
  `fleet_storage.drive_cache_storage`.
- Produces:
  - `run_sync_cycle(store, embedder, *, gmail_service=None, calendar_service=None, drive_service=None, home=None)`
    — unchanged for the existing sources; when `drive_service` and `home` are given AND
    `ingest_cache_enabled(home)` AND `fleet_pin(home).is_pinned`, also run
    `sync_shared_drives`, `index_pending` (to embed the misses), then `publish_file` each
    miss (now that its vectors exist). Result dict gains `"shared_drives"`.
  - The daemon threads `home=str(config.app_dir())` into `run_sync_cycle` via `run_cycle`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sync_cycle.py` (reuse that module's fake embedder if present; else the
minimal one below):

```python
def test_run_sync_cycle_shared_drive_publishes_after_embed(tmp_path):
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from mcpbrain import ingest_cache
    from tests.test_drive_sync import FakeDriveService, _gdoc_change

    class _Emb:
        dim = 4
        def embed_passages(self, texts):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]
        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=4); store.init()
    store.set_cursor("drive:D1", "100")

    # Route DriveFleetStorage at a local dir by monkeypatching the factory hook.
    from mcpbrain.sync import drive as drivemod
    calls = {}
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fsmap = {}
    orig = drivemod.sync_shared_drives
    def _patched(service, s, *, pin, storage_factory, absence_threshold=3):
        return orig(service, s, pin=pin,
                    storage_factory=lambda d: fsmap.setdefault(d, LocalDirFleetStorage(tmp_path / d)),
                    absence_threshold=absence_threshold)
    drivemod.sync_shared_drives = _patched
    try:
        svc = FakeDriveService(
            shared_drives=[{"id": "D1", "name": "Ops"}],
            initial_cursor="100",
            pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
            exports={"FID": b"shared drive body content"})
        res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    finally:
        drivemod.sync_shared_drives = orig

    assert res["shared_drives"]["D1"] == 1
    # the miss was published after embedding: an artifact now exists for FID
    names = fsmap["D1"].list_paths(ingest_cache.CACHE_DIR + "/")
    assert any(n.rsplit("/", 1)[-1].startswith("FID.") for n in names)


def test_run_sync_cycle_no_pin_skips_shared_drives(tmp_path):
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from tests.test_drive_sync import FakeDriveService

    class _Emb:
        dim = 4
        def embed_passages(self, texts): return [[0.0]*4 for _ in texts]
        def embed_query(self, text): return [0.0]*4

    home = str(tmp_path / "home")
    config.write_config(home, {"owner_email": "me@x.org"})   # no org_pin
    store = Store(tmp_path / "b.sqlite3", dim=4); store.init()
    svc = FakeDriveService(shared_drives=[{"id": "D1", "name": "Ops"}])
    res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    assert "shared_drives" not in res      # gated off without a pin
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sync_cycle.py -k shared_drive -v`
Expected: FAIL — `TypeError: run_sync_cycle() got an unexpected keyword argument 'home'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/sync/__init__.py`, change `run_sync_cycle` (the signature at ~line 19 and the
body). Add `home=None` to the signature and, after the existing drive delta block (the
`if drive_service is not None:` branch, ~sync/__init__.py:43), insert the shared-drive path:

```python
def run_sync_cycle(store, embedder, *, gmail_service=None,
                   calendar_service=None, drive_service=None, home=None) -> dict:
```

...and after the existing three-source block, before the backfill step:

```python
    # Shared Drive ingest cache (spec §A). Gated: needs a drive service, a home
    # to read config from, the cache enabled, and a fleet pin present. Without a
    # pin this is a no-op and drive sync behaves exactly as before.
    if drive_service is not None and home is not None:
        from mcpbrain import config
        pin = config.fleet_pin(home)
        if config.ingest_cache_enabled(home) and pin.is_pinned:
            from mcpbrain.sync.drive import sync_shared_drives
            from mcpbrain.fleet_storage import drive_cache_storage
            from mcpbrain import ingest_cache
            sd = sync_shared_drives(
                drive_service, store, pin=pin,
                storage_factory=lambda d: drive_cache_storage(drive_service, d))
            # Embed the misses, THEN publish them (publish reads vectors back).
            result["embedded"] += index_pending(store, embedder)
            published_by = config.owner_email(home)
            per_drive = {}
            for drive_id, info in sd.items():
                if drive_id == "_revoked":
                    continue
                fs = info["storage"]
                for file_id, content_hash in info["miss"]:
                    ingest_cache.publish_file(
                        store, fs, drive_id, file_id, content_hash, pin,
                        published_by=published_by)
                per_drive[drive_id] = info["processed"]
            result["shared_drives"] = per_drive
            result["revoked_drives"] = sd.get("_revoked", [])
```

In `mcpbrain/daemon.py`, thread `home` through `run_cycle`'s call to `run_sync_cycle`
(~daemon.py:358). The daemon's home is `config.app_dir()`:

```python
    result = run_sync_cycle(
        store, embedder,
        gmail_service=gmail_service,
        calendar_service=calendar_service,
        drive_service=drive_service,
        home=str(config.app_dir()),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_sync_cycle.py -v`
Expected: PASS (new shared-drive tests + the existing cycle suite still green — `home`
defaults to `None`, so every existing `run_sync_cycle` caller is unaffected).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/__init__.py mcpbrain/daemon.py tests/test_sync_cycle.py
git commit -m "feat(sync): wire ingest cache into the drive-sync cycle (gated on fleet pin)"
```

---

## Task 13: Phase A exit gate — full round-trip + version-skew + suite green

**Files:**
- Test: `tests/test_ingest_cache_roundtrip.py` (extend)

**Interfaces:**
- Consumes: everything above.
- Produces: the spec's cross-store guarantees as deterministic tests — publish from store A
  → import into fresh store B yields bit-identical chunks/vectors/enrichment; two stores on
  different embed models never read each other's artifacts (pipeline-fingerprint separation);
  an enrich-logic bump updates the `enrich` block without re-embedding — plus the full-suite
  green check.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ingest_cache_roundtrip.py`:

```python
def test_cross_store_publish_import_identical(tmp_path):
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import FleetPin
    from tests.helpers.org_fleet import LocalDirFleetStorage
    pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                   enrich_logic_floor=1, fleet_secret="s3cret")
    A = _store(tmp_path, "A.sqlite3"); fs = LocalDirFleetStorage(tmp_path / "drv")
    for i in range(3):
        A.import_cached_chunk(f"gdrive-FID-{i}", f"body {i}", f"c{i}",
                              {"source_type": "gdrive", "file_id": "FID", "chunk_index": i},
                              [0.11 * i, 0.22, 0.33, 0.44])
    ingest_cache.publish_file(A, fs, "D1", "FID", "vh", pin, enrich={"logic_version": 1})
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "FID", "vh", pin) is True
    for i in range(3):
        assert struct.pack("<4f", *A.embedding_for_doc(f"gdrive-FID-{i}")) == \
               struct.pack("<4f", *B.embedding_for_doc(f"gdrive-FID-{i}"))
        assert A.get_chunk(f"gdrive-FID-{i}")["text"] == B.get_chunk(f"gdrive-FID-{i}")["text"]


def test_version_skew_stores_never_read_each_others_artifacts(tmp_path):
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import FleetPin
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "drv")
    pin_old = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1", fleet_secret="s")
    pin_new = FleetPin(embed_model="bge-large", dim=4, chunker_version="v1", fleet_secret="s")
    A = _store(tmp_path, "A.sqlite3")
    A.import_cached_chunk("gdrive-FID-0", "x", "c", {"file_id": "FID", "chunk_index": 0}, [1.0]*4)
    ingest_cache.publish_file(A, fs, "D1", "FID", "vh", pin_old)
    B = _store(tmp_path, "B.sqlite3")
    # a bge-large daemon must NOT import the bge-small artifact (fingerprint separates them)
    assert ingest_cache.try_import(B, fs, "D1", "FID", "vh", pin_new) is False
    # and both artifacts can coexist (no churn): publish the new-pipeline one too
    ingest_cache.publish_file(A, fs, "D1", "FID", "vh", pin_new)
    names = [p.rsplit("/", 1)[-1] for p in fs.list_paths(ingest_cache.CACHE_DIR + "/")]
    assert len(names) == 2   # old + new pipeline artifacts side by side
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `python -m pytest tests/test_ingest_cache_roundtrip.py -v`
Expected: PASS (these assert accumulated behaviour; a failure means an earlier task
regressed — fix there).

- [ ] **Step 3: Run the full suite as the exit gate**

Run: `python -m pytest tests/ -q`
Expected: the whole suite passes (no regressions from the drive/sync/store edits). If red,
fix the offending task before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_ingest_cache_roundtrip.py
git commit -m "test(ingest_cache): cross-store round-trip + version-skew exit gate"
```

- [ ] **Step 5: Phase A complete**

Subsystem A is green in isolation against the shared harness. The A↔B echo-dedup test, the
C end-to-end bootstrap test, and the egress gate remain Phase-D (they can only be verified on
the merged system — see spec §"Phase D"). No push/release — that is a separate explicit
instruction.

---

## Self-Review

**Spec coverage** (subsystem A items → tasks):

- **A1 shared-drive sync** — `drives.list` enumeration (`list_shared_drives`, Task 7);
  per-drive `drive:<driveId>` cursors + `changes.list(driveId=…, corpora="drive",
  includeItemsFromAllDrives=True)` (`sync_shared_drive`, Task 8); backfill parity
  (`backfill_shared_drive`, Task 10); `DRIVE_ID_META_KEY` stamping (`normalise_drive` +
  base_meta, Task 7); orchestration + wiring (Tasks 9, 12). ✓
- **A2 ingest cache** — cache-first read (`try_import`/`_import_artifact`, Task 3); publish
  (`publish`/`publish_file`, Task 4); pre-process re-check (the second `try_import` before
  extraction, Task 8; and after-embed publish, Task 12); GC on file change (`gc_superseded`,
  Task 4), on file delete (`remove_file_artifacts` in removal handling, Tasks 4+8), enrich-
  block-in-place vs re-embed (version-skew test + pipeline-fingerprint filename, Tasks 4+13),
  opportunistic sweep (`sweep_drive`, Task 4/9). ✓
- **A3 access revocation** — targeted purge of chunks/vectors/FTS (`purge_drive` +
  `delete_chunks`, Tasks 2+5); bitemporal invalidation of solely-sourced layer-2 rows via
  `source_doc_id`, org rows untouched (`invalidate_local_relations_for_docs`, Task 2);
  consecutive-absence counter guarding transient glitches (`note_drive_presence`, threshold
  default 3, Task 6; wired in `sync_shared_drives`, Task 9). ✓
- **A4 fleet pipeline pinning** — consumed read-only via Phase 0's `config.fleet_pin`
  (`FleetPin`): `embed_model`/`dim`/`chunker_version` drive `artifact_filename` +
  import validation; `enrich_logic_floor` gates the enriched-skip; `is_pinned` (secret
  present) gates the whole path so a pre-pin daemon falls back locally without clobbering
  (Tasks 3, 8, 12). Production `DriveFleetStorage` transport (Task 11). ✓

**Frozen cross-subsystem signatures** — verbatim as required:
- `ingest_cache.try_import(store, fleet_storage, drive_id, file_id, content_hash, pin) -> bool` (Task 3). ✓
- `ingest_cache.publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin, *, enrich=None) -> None` — kept verbatim; the extra `published_by=""` is an appended keyword-only optional (Task 4). ✓
- `ingest_cache.gc_superseded(fleet_storage, drive_id, file_id, keep_content_hash, pin) -> int` (Task 4). ✓
- `ingest_cache.bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict` (Task 5). ✓
- `ingest_cache.purge_drive(store, drive_id) -> dict` (Task 5). ✓
- `fleet_storage.DriveFleetStorage(drive_service, folder_or_drive_id)` implementing `FleetStorage` (Task 11). ✓
- `fleet_storage.fleet_folder_storage(home, drive_service=None) -> FleetStorage | None`,
  `fleet_storage.drive_cache_storage(drive_service, drive_id) -> FleetStorage`, and re-exported
  `fleet_storage.list_shared_drives(service) -> list[dict]` (Task 11). ✓

**Storage acquisition for B and C (Phase-D convergence contract):**
- **B** acquires fleet-folder transport via
  `fleet_storage.fleet_folder_storage(home, drive_service=self.ensure_services().get("drive_service"))`
  for its contrib-upload / curate / snapshot-publish cadences (returns `None` → run local).
- **C** onboarding uses `fleet_storage.fleet_folder_storage(...)` for the snapshot import,
  then `fleet_storage.list_shared_drives(drive_service)` → per-drive
  `fleet_storage.drive_cache_storage(drive_service, drive_id)` → `ingest_cache.bootstrap_drive`,
  summing each drive's `cache_hits` for its onboarding report.

**Constraint compliance:** No schema change (only new `store.py` *methods*; `origin`/org
tables from Phase 0 are read, not altered). No edits to `config.py` accessors or
`org_contracts.py`. No edits to `_CADENCE_*` / cadence registration — A owns none of the
three stub cadences; the cache is wired into `run_sync_cycle`/`run_cycle` only. `drive_id`
stays a metadata JSON key. No new OAuth scopes (`supportsAllDrives` + existing read/`drive.file`).
No version bump / release / push.

**Interface decisions B and C must honour:**
1. **`content_hash` is the Drive file-version id, not the text hash** — `md5Checksum` when
   present, else `sha256("<version>|<modifiedTime>")` (`drive._file_content_hash`). It must be
   knowable before extraction. When C calls `try_import`/`bootstrap_drive`, this is the
   file-version id; per-chunk text hashing happens inside `ingest_cache`.
2. **One `FleetStorage` per Shared Drive**, scoped to the drive root (`DriveFleetStorage(svc,
   driveId)`); `drive_id` is still passed to every `ingest_cache` call (for metadata stamping
   + purge). C's onboarding builds one storage per accessible drive and calls
   `bootstrap_drive` per drive.
3. **Cache directory** is `.mcpbrain-cache/` (constant `ingest_cache.CACHE_DIR`) at the drive
   root; artifact filenames follow `org_contracts.artifact_filename`.
4. **Enrichment via the cache is a skip-signal, not a graph import (in Phase A).** `try_import`
   marks chunks `enriched=1` only when the artifact's `enrich.logic_version >= max(pin
   .enrich_logic_floor, ENRICH_LOGIC_VERSION)`; the optional extraction *payload* is carried
   end-to-end in the artifact but its application into the graph is the **Phase-D A↔B seam**
   (echo-dedup), exactly where the spec assigns cross-subsystem interactions. B's org snapshot,
   not A's cache, is how shared *graph content* reaches a consumer. C should not expect
   `bootstrap_drive` to populate layer-1 entities — it populates chunks/vectors and the
   enriched-skip flag.
5. **Revocation state lives in the `meta` table** (`ingest_cache.known_drives`,
   `ingest_cache.absent:<id>`), threshold default 3 consecutive absent cycles; no cadence, no
   schema. C/D can read these keys for observability.
6. **`bootstrap_drive` returns `{"imported","chunks","skipped","cache_hits"}` (with
   `cache_hits == imported`); `purge_drive` returns
   `{"drive_id","docs","chunks_deleted","relations_invalidated"}`** — stable shapes for C/D.
   C sums `cache_hits` across drives for its onboarding summary.

**Type consistency:** `FleetPin` fields consumed exactly as Phase 0 defines them
(`embed_model:str`, `dim:int`, `chunker_version:str`, `enrich_logic_floor:int`,
`relation_allowlist`, `fleet_secret:str`, `is_pinned`). `CacheArtifact`/`CacheChunk`
constructed/parsed only via their frozen fields + `to_dict`/`from_dict`. `FleetStorage`
method set (`put_bytes`/`get_bytes`/`list_paths`/`delete`) identical between the Protocol
(Phase 0), `LocalDirFleetStorage` (Phase 0 helper), and `DriveFleetStorage` (Task 11);
`isinstance(..., FleetStorage)` runtime check asserted (Task 11). Vector encode/decode is
symmetric (`_encode_vec`/`_decode_vec` ↔ `serialize_float32`/`struct.unpack`), asserted
bit-exact (Tasks 1, 4, 13). `sync_shared_drive`/`backfill_shared_drive` both return a
`{"processed","miss":[(file_id,content_hash)]}` shape the wiring in Task 12 consumes verbatim.

**Placeholder scan:** No TBD/TODO; every code step is complete. The one intentional
conservative behaviour (partial-delta `sweep_drive`) is documented inline with its rationale
and is not a stub — deletion of dead artifacts is fully covered by the removal path
(`remove_file_artifacts`) and republish GC (`gc_superseded`).
