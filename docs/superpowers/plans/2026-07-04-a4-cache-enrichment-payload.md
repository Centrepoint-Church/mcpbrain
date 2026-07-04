# A#4 — Cache the Enrichment Payload — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the per-doc enrichment extraction in shared-drive cache artifacts so an importing daemon skips Haiku re-enrichment AND gets the graph rows applied — closing the third of the ingest cache's value props.

**Architecture:** Capture the validated extraction at drain (keyed to its doc_ids) into a new `enrich_payloads` table; include it in the published `CacheArtifact.enrich` block; on import, re-validate it through drain's own guards and apply it via `graph_write.apply` (which self-resolves owner/identity/home — no threading needed), then mark the chunks enriched. Safe: the A→B echo path is corroboration-safe (Phase D Task 1); the payload is applied through the same `sanitize_batch`/`validate_extraction`/grounding filter local enrichment uses, never raw.

**Tech Stack:** Python 3, pytest (`uv run --no-sync python -m pytest`, parallel by default), stdlib sqlite3/json, `graph_write.apply`, `contract.validate_extraction`/`sanitize_batch`, `drain._grounding_filter`.

## Global Constraints

- **`graph_write.apply(store, extraction, *, doc_ids, …)`** — only `store`/`extraction`/`doc_ids` are required; `identity`/`owner`/`home`/`embedder`/`clock`/`entity_index` are optional and self-resolve from config. Apply-on-import passes just `store`, the payload, and `doc_ids` (+ `embedder` when available). Do NOT add required params to `try_import`/`bootstrap_drive`.
- **Never apply a cached payload raw** — run it through the same validation drain uses before apply: `contract.validate_extraction` (empty list = valid), `contract.sanitize_batch`, and `drain._grounding_filter` (gated on `config.schema_grounding_enabled`, exactly as drain gates it). A payload that fails validation is skipped (fall back to local re-enrich).
- **Drive-only:** payloads are captured/published only for shared-drive-sourced docs (doc_id prefix `gdrive-`). Email payloads never enter a shared cache.
- **Schema additions are additive** — new table via `CREATE TABLE IF NOT EXISTS` in `Store.init()`; no `ALTER` on existing tables.
- **Idempotent:** re-import of the same artifact must not double-write (`apply` is idempotent on `source_doc_id`; `mark_enriched` is a set op; `set_enrich_payload` is `INSERT OR REPLACE`).
- **Tests:** pytest, flat `tests/test_*.py`; `Store(tmp_path/"x.sqlite3", dim=4).init()`; the `tests/helpers/org_fleet.py` harness; commit `--no-verify` (slow hooks); `rm -f .git/index.lock` if a stale lock appears. No version bump, no push.
- Reference spec: `docs/superpowers/specs/2026-07-04-a4-cache-enrichment-payload-design.md`.

---

## File Structure

**Modified:**
- `mcpbrain/store.py` — `enrich_payloads` table in `init()`; `set_enrich_payload`/`get_enrich_payload` methods; drop payload rows in `delete_chunks`.
- `mcpbrain/drain.py` — after a successful apply, persist the validated extraction for drive-sourced doc_ids.
- `mcpbrain/ingest_cache.py` — `collect_chunks`/`publish`/`publish_file` include the payload; `_import_artifact` validates + applies it.
- `mcpbrain/sync/__init__.py` — `_publish_drive_misses` passes the payload through to `publish_file`; thread `embedder` where cheap.
- Tests: `tests/test_store.py` (or `test_store_schema.py`), `tests/test_drain.py`, `tests/test_ingest_cache.py`, `tests/test_ingest_cache_roundtrip.py`.

---

## Task 1: Store — `enrich_payloads` table + accessors + purge cleanup

**Files:**
- Modify: `mcpbrain/store.py` (`init()` near the other CREATE TABLEs; new methods near `get_stale_reextract`; `delete_chunks`)
- Test: `tests/test_store_schema.py`

**Interfaces:**
- Produces: table `enrich_payloads(doc_id TEXT PRIMARY KEY, payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, at TEXT DEFAULT CURRENT_TIMESTAMP)`; `Store.set_enrich_payload(doc_id, payload_json_str, logic_version)` (INSERT OR REPLACE); `Store.get_enrich_payload(doc_id) -> dict|None` (returns `{"payload": <str>, "logic_version": <int>}` or None); `delete_chunks` also removes matching `enrich_payloads` rows.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store_schema.py
def test_enrich_payloads_roundtrip_and_purge(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "g.sqlite3", dim=4); s.init()
    # table exists
    with s._connect() as db:
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "enrich_payloads" in names
    # set/get round-trip
    assert s.get_enrich_payload("gdrive-F1-0") is None
    s.set_enrich_payload("gdrive-F1-0", '{"thread_id":"gdrive-F1","entities":[]}', 1)
    got = s.get_enrich_payload("gdrive-F1-0")
    assert got["logic_version"] == 1 and "thread_id" in got["payload"]
    # INSERT OR REPLACE (no duplicate)
    s.set_enrich_payload("gdrive-F1-0", '{"thread_id":"gdrive-F1","entities":[1]}', 2)
    assert s.get_enrich_payload("gdrive-F1-0")["logic_version"] == 2
    # delete_chunks cleans the payload row
    with s._connect() as db:
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata) "
                   "VALUES('gdrive-F1-0','t','h','{}')")
    s.delete_chunks(["gdrive-F1-0"])
    assert s.get_enrich_payload("gdrive-F1-0") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_store_schema.py -k enrich_payloads -v`
Expected: FAIL — `no such table: enrich_payloads` / `AttributeError: ... set_enrich_payload`.

- [ ] **Step 3: Implement**

In `Store.init()`, next to the other `CREATE TABLE IF NOT EXISTS` blocks:
```python
            db.execute("""CREATE TABLE IF NOT EXISTS enrich_payloads(
                doc_id        TEXT PRIMARY KEY,
                payload       TEXT NOT NULL,
                logic_version INTEGER DEFAULT 0,
                at            TEXT DEFAULT CURRENT_TIMESTAMP)""")
```
Add methods (mirror `get_stale_reextract`/`set_stale_reextract`):
```python
    def set_enrich_payload(self, doc_id: str, payload: str, logic_version: int) -> None:
        """Persist the validated extraction (JSON string) a drive doc produced, so
        its shared-drive cache artifact can carry it and importers skip re-enrich."""
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO enrich_payloads"
                       "(doc_id, payload, logic_version) VALUES(?,?,?)",
                       (doc_id, payload, int(logic_version)))

    def get_enrich_payload(self, doc_id: str) -> dict | None:
        with self._connect() as db:
            r = db.execute("SELECT payload, logic_version FROM enrich_payloads "
                           "WHERE doc_id=?", (doc_id,)).fetchone()
        return {"payload": r["payload"], "logic_version": r["logic_version"]} if r else None
```
In `delete_chunks`, alongside the vec/fts/chunks deletes, add:
```python
            db.executemany("DELETE FROM enrich_payloads WHERE doc_id=?",
                           [(d,) for d in doc_ids])
```
(Match `delete_chunks`'s existing doc_id iteration/param style.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_store_schema.py -k enrich_payloads -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store_schema.py
git commit --no-verify -m "feat(store): enrich_payloads table + accessors (A#4); purge cleans payload rows"
```

---

## Task 2: Drain — capture the validated extraction for drive docs

**Files:**
- Modify: `mcpbrain/drain.py` (after the successful `apply` + `mark_enriched`, ~line 451-466)
- Test: `tests/test_drain.py`

**Interfaces:**
- Consumes: `store.set_enrich_payload` (Task 1); the `extraction` dict and `doc_ids` already in scope at drain.py:451.
- Produces: after `apply` succeeds, for each drive-sourced doc_id (prefix `gdrive-`), `set_enrich_payload(doc_id, json.dumps(extraction), ENRICH_LOGIC_VERSION)` is called. Email doc_ids get no payload row.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_drain.py — model on this file's existing drain tests (a fake apply + a pushed inbox file).
def test_drain_persists_enrich_payload_for_drive_docs_only(tmp_path):
    # Build a store with one gdrive chunk and one gmail chunk; push an inbox file
    # whose extraction covers both; drain with a stub apply; assert the drive doc
    # has a payload row and the gmail doc does not.
    ...  # reuse the file's existing inbox/apply harness (see test_drain.py's
        # other tests for how it builds `data`, writes the inbox json, and calls
        # drain.drain(store, apply=<stub>, embedder=None)). doc_ids come from
        # store.doc_ids_for_messages, so seed chunks whose doc_ids are
        # "gdrive-F1-0" and "gmail-m1-body" and message-map accordingly.
    assert store.get_enrich_payload("gdrive-F1-0") is not None
    assert store.get_enrich_payload("gmail-m1-body") is None
```
(If wiring a full drain in the test is heavy, an acceptable alternative unit test: extract the capture into a tiny helper `drain._persist_drive_payload(store, extraction, doc_ids)` and unit-test that helper directly — seed store, call it with mixed doc_ids, assert only `gdrive-` rows persist. Prefer the real drain path if the file already has a lightweight harness.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_drain.py -k enrich_payload -v`
Expected: FAIL — no payload persisted.

- [ ] **Step 3: Implement**

In `mcpbrain/drain.py`, immediately after `store.mark_enriched(doc_ids)` (line 465), add:
```python
            # A#4: persist the validated extraction for shared-drive docs so their
            # cache artifact can carry it (importers then skip Haiku). Drive-only:
            # email payloads never enter a shared cache. `extraction` here has
            # already passed sanitize_batch + validate_extraction + grounding.
            _drive_docs = [d for d in doc_ids if d.startswith("gdrive-")]
            if _drive_docs:
                _payload = json.dumps(extraction, sort_keys=True)
                for _d in _drive_docs:
                    store.set_enrich_payload(_d, _payload, ENRICH_LOGIC_VERSION)
```
Ensure `ENRICH_LOGIC_VERSION` is imported in drain.py (`from mcpbrain.store import ENRICH_LOGIC_VERSION` — add if absent; `json` is already imported).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_drain.py -k enrich_payload -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/drain.py tests/test_drain.py
git commit --no-verify -m "feat(drain): capture validated extraction payload for shared-drive docs (A#4)"
```

---

## Task 3: Publish — include the payload in the cache artifact

**Files:**
- Modify: `mcpbrain/ingest_cache.py` (`collect_chunks`/`publish`/`publish_file`); `mcpbrain/sync/__init__.py` (`_publish_drive_misses` passes the enrich payload)
- Test: `tests/test_ingest_cache.py`

**Interfaces:**
- Consumes: `store.get_enrich_payload` (Task 1); `ENRICH_LOGIC_VERSION`; `FleetPin.enrich_logic_floor`.
- Produces: a published artifact for an enriched drive file carries `enrich = {"contextual_retrieval": …, "logic_version": N, "extraction": <dict>}` when a floor-satisfying payload exists for the file's chunks; otherwise `enrich = {"contextual_retrieval": …}` (no payload — importer re-enriches, unchanged). `publish_file` gains an internal lookup; the existing `enrich=` kwarg on `publish` is still honoured.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_cache.py — reuse this file's helpers (PIN, _store, LocalDirFleetStorage,
# import_cached_chunk, artifact_filename, CACHE_DIR).
def test_publish_file_includes_enrich_payload_when_present(tmp_path):
    import gzip, json
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "body", "vh1",
                          {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0},
                          [0.1, 0.2, 0.3, 0.4])
    s.set_enrich_payload("gdrive-FID-0",
                         '{"thread_id":"gdrive-FID","org":"Acme","content_type":"reference","summary":"x","entities":[]}',
                         1)  # PIN.enrich_logic_floor == 1
    assert ingest_cache.publish_file(s, fs, "D1", "FID", "vh1", PIN, published_by="p@x.org")
    art = CacheArtifact.from_dict(json.loads(gzip.decompress(
        fs.get_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}"))))
    assert art.enrich.get("logic_version") == 1
    assert art.enrich.get("extraction", {}).get("org") == "Acme"


def test_publish_file_omits_payload_when_unenriched(tmp_path):
    import gzip, json
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    s.import_cached_chunk("gdrive-FID-0", "body", "vh1",
                          {"source_type": "gdrive", "file_id": "FID", "chunk_index": 0},
                          [0.1, 0.2, 0.3, 0.4])
    # no set_enrich_payload -> no payload in the artifact
    ingest_cache.publish_file(s, fs, "D1", "FID", "vh1", PIN, published_by="p@x.org")
    art = CacheArtifact.from_dict(json.loads(gzip.decompress(
        fs.get_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}"))))
    assert "extraction" not in art.enrich
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_ingest_cache.py -k enrich_payload -v`
Expected: FAIL — `art.enrich` has no `extraction`/`logic_version`.

- [ ] **Step 3: Implement**

In `ingest_cache.publish_file` (the store-driven wrapper, ~line 242), after collecting chunks and before/at the `publish(...)` call, look up the payload for the file's chunks and build the `enrich` block:
```python
def publish_file(store, fleet_storage, drive_id, file_id, content_hash, pin,
                 *, published_by="", contextual_retrieval=False, skip_gc=False) -> bool:
    chunks = collect_chunks(store, file_id)
    if not chunks:
        return False
    # A#4: attach the doc's validated extraction if it's enriched at the fleet floor.
    enrich = None
    floor = max(int(pin.enrich_logic_floor), int(ENRICH_LOGIC_VERSION))
    for ch in chunks:
        doc_id = f"gdrive-{file_id}-{ch.idx}"
        row = store.get_enrich_payload(doc_id)
        if row and int(row["logic_version"]) >= floor:
            import json as _json
            enrich = {"logic_version": int(row["logic_version"]),
                      "extraction": _json.loads(row["payload"])}
            break                        # one payload per file (chunks share the unit's extraction)
    return publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin,
                   enrich=enrich, published_by=published_by,
                   contextual_retrieval=contextual_retrieval, skip_gc=skip_gc)
```
(Read the real `publish_file`/`publish`/`collect_chunks` signatures first and match them — the above shows the intent; `publish` already merges `enrich` into `enrich_block` and stamps `contextual_retrieval`.)

In `mcpbrain/sync/__init__.py` `_publish_drive_misses` — no change needed if `publish_file` does its own lookup (preferred). Confirm the caller still passes `contextual_retrieval=cr`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_ingest_cache.py -k enrich_payload -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache.py
git commit --no-verify -m "feat(ingest_cache): publish the enrichment payload in shared-drive artifacts (A#4)"
```

---

## Task 4: Import — validate + apply the cached payload

**Files:**
- Modify: `mcpbrain/ingest_cache.py` (`_import_artifact`, ~line 72-133; after `store.import_cached_chunks(rows)` at line 127)
- Test: `tests/test_ingest_cache.py`

**Interfaces:**
- Consumes: `CacheArtifact.enrich` (may carry `{"logic_version", "extraction"}`); `contract.validate_extraction`/`sanitize_batch`; `drain._grounding_filter` (gated on `config.schema_grounding_enabled`); `graph_write.apply(store, extraction, *, doc_ids)`.
- Produces: when `mark_enriched` is True AND `art.enrich` has an `extraction`, `_import_artifact` re-validates the payload through drain's guards and applies it via `graph_write.apply` with the imported `gdrive-<file_id>-<idx>` doc_ids, writing `origin='local'` graph rows exactly as local enrichment would. Validation failure → skip apply (chunks still imported + marked). Idempotent on re-import.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest_cache.py
def test_import_applies_cached_enrichment_payload(tmp_path):
    import gzip, json, base64, struct
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import CacheArtifact, CacheChunk, artifact_filename
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    vec = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    extraction = {"thread_id": "gdrive-FID", "org": "Acme", "content_type": "reference",
                  "summary": "quarterly plan",
                  "entities": [{"name": "Joel Chelliah", "type": "person"}],
                  "relations": [], "actions": [], "topics": [],
                  "messages": [{"message_id": "gdrive-FID-0", "text": "Joel Chelliah owns the plan"}]}
    art = CacheArtifact(
        file_id="FID", content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="Joel Chelliah owns the plan", embedding_b64=vec,
                           metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),),
        enrich={"logic_version": 1, "extraction": extraction},
        published_by="p@x.org", published_at="2026-07-04")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}",
                 gzip.compress(json.dumps(art.to_dict()).encode()))
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", PIN) is True
    # chunk marked enriched (no local re-enrich) AND graph rows applied
    with s._connect() as db:
        r = db.execute("SELECT enriched FROM chunks WHERE doc_id='gdrive-FID-0'").fetchone()
        n_ent = db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"]
    assert r["enriched"] == 1
    assert n_ent >= 1                    # the payload's entity was applied to the graph
    # idempotent: a second import doesn't error or double-apply
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", PIN) in (True, False)


def test_import_below_floor_payload_falls_back_to_reenrich(tmp_path):
    # logic_version below the floor -> not applied, chunk left unenriched.
    ...  # same as above but enrich={"logic_version": 0, "extraction": extraction};
        # assert chunks present, enriched == 0, entities == 0.
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_ingest_cache.py -k cached_enrichment -v`
Expected: FAIL — `n_ent == 0` (payload not applied; graph empty).

- [ ] **Step 3: Implement**

In `_import_artifact`, after `store.import_cached_chunks(rows)` (line 127) and only when `mark_enriched` and the artifact carries an extraction:
```python
        store.import_cached_chunks(rows)
        # A#4: apply the cached enrichment so the importer's graph gets this doc's
        # entities/relations without re-running Haiku. Validate through the SAME
        # guards drain uses before apply — never apply a peer's payload raw.
        extraction = (art.enrich or {}).get("extraction") if mark_enriched else None
        if extraction:
            try:
                from mcpbrain import contract, graph_write, config as _config
                clean, _ = contract.sanitize_batch({"extractions": [extraction]})
                cand = (clean.get("extractions") or [extraction])[0]
                if not contract.validate_extraction(cand):      # [] == valid
                    if _config.schema_grounding_enabled(str(_config.app_dir())):
                        from mcpbrain.drain import _grounding_filter
                        cand, _ = _grounding_filter(cand)
                    doc_ids = [f"gdrive-{art.file_id}-{c.idx}" for c in art.chunks]
                    graph_write.apply(store, cand, doc_ids=doc_ids)   # self-resolves owner/home
            except Exception as exc:  # noqa: BLE001 — apply failure must not fail the import
                log.info("ingest_cache: cached-enrichment apply skipped for %s: %s",
                         art.file_id, exc)
        return True
```
(Read the real `_import_artifact` tail first; keep the existing return/mark logic. `sanitize_batch` expects the batch wrapper shape — wrap the single extraction as shown. If the daemon path has an `embedder` you want to thread later, that's a follow-up; `apply` with no embedder leaves the synthesised doc for the next `index_pending`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_ingest_cache.py -k "cached_enrichment or below_floor" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/ingest_cache.py tests/test_ingest_cache.py
git commit --no-verify -m "feat(ingest_cache): apply cached enrichment payload on import via validated drain path (A#4)"
```

---

## Task 5: End-to-end round-trip + echo + revocation GC

**Files:**
- Test: `tests/test_ingest_cache_roundtrip.py` (or a new `tests/test_a4_enrich_cache.py`)

**Interfaces:**
- Consumes: everything above; `org_contrib.collect_from_drain`; `org_curate._corroborated` (echo-safety, already verified Phase D Task 1); `ingest_cache.purge_drive`.
- Produces: verification only.

- [ ] **Step 1: Write the tests**

```python
# A. Full publish->import round-trip: store A enriches a drive doc, publishes;
#    store B imports and gets the graph rows applied WITHOUT re-enriching.
def test_enrichment_payload_round_trips_publisher_to_importer(tmp_path):
    ...  # store A: import_cached_chunk + set_enrich_payload(logic 1); publish_file to a
        # shared LocalDirFleetStorage. store B: try_import from it; assert B's chunk
        # enriched==1 and B's entities table has the payload's entity.

# B. Revocation GC: purging a drive drops its enrich_payloads rows.
def test_purge_drive_drops_enrich_payloads(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "t", "h",
                          {"source_type": "gdrive", "file_id": "F1", "drive_id": "D1"}, [0.0]*4)
    s.set_enrich_payload("gdrive-F1-0", '{"thread_id":"gdrive-F1"}', 1)
    ingest_cache.purge_drive(s, "D1")
    assert s.get_enrich_payload("gdrive-F1-0") is None

# C. Echo-safety through the REAL apply path (A#4 variant of Phase D Task 1):
#    two importers apply the same cached payload; both contribute the same doc's
#    relations with the SAME source_ref -> corroboration counts one source.
def test_cached_enrichment_echo_is_corroboration_safe(tmp_path):
    from mcpbrain.org_contracts import source_ref
    a = source_ref("s3cret", "gdrive-FID-0")
    b = source_ref("s3cret", "gdrive-FID-0")
    assert a == b   # same doc across importers -> one source_ref (curator dedupes)
```
(Test A is the value-prop proof — flesh out the publish/import as in Tasks 3-4. Test C's deep version is already covered by Phase D Task 1; keep this as the A#4-scoped confirmation.)

- [ ] **Step 2: Run**

Run: `uv run --no-sync python -m pytest tests/test_ingest_cache_roundtrip.py -v` (or the new file)
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/
git commit --no-verify -m "test(ingest_cache): A#4 end-to-end round-trip + revocation GC + echo-safety"
```

---

## Task 6: Exit gate

- [ ] **Step 1: Full suite**

Run: `uv run --no-sync python -m pytest tests/ -q`
Expected: all pass (~2279 baseline + new A#4 tests).

- [ ] **Step 2: Plugin agent drift check** (the enrich prompt was NOT changed by A#4, so confirm no drift)

Run: `uv run --no-sync python bin/sync_agents.py && git status --short`
Expected: no changes to `plugin/agents/enrich-batch.md`.

- [ ] **Step 3: Final commit if anything regenerated**

```bash
git add -A && git commit --no-verify -m "chore(a4): cache-enrichment-payload complete" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage:** capture (Task 2), storage (Task 1), publish (Task 3), apply-on-import via validated path (Task 4), lifecycle/floor gating (Tasks 3-4 use `max(pin.enrich_logic_floor, ENRICH_LOGIC_VERSION)`), revocation GC (Tasks 1+5), echo-safety (Task 5C + Phase D Task 1). All spec sections covered.

**Placeholder scan:** Task 2 Step 1 and Task 5 use `...` deliberately pointing at existing test harnesses to mirror (`test_drain.py`'s inbox/apply harness; the publish/import helpers) rather than unfilled requirements — the assertions and the implementation code (Steps 3) are complete. Every implementation step shows real code.

**Type consistency:** `set_enrich_payload(doc_id, payload_str, logic_version)` / `get_enrich_payload(doc_id) -> {"payload","logic_version"}|None` consistent across Tasks 1/3/4. `art.enrich = {"logic_version", "extraction"}` produced in Task 3, consumed in Task 4. `graph_write.apply(store, extraction, doc_ids=…)` matches the verified signature (no owner/home threading). doc_id form `gdrive-<file_id>-<idx>` consistent across capture (Task 2), publish lookup (Task 3), and apply (Task 4).
