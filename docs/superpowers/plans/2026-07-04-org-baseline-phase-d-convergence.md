# Org Baseline — Phase D (Convergence) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the merged A+B+C org-baseline system upholds its load-bearing invariants end-to-end, surface it in the UI/status, and document the fleet-wide enable order — the convergence work that no single subsystem could verify in isolation.

**Architecture:** Phase D is mostly *verification*, not new core logic — A/B/C are merged and the hardening pass already fixed the correctness bugs (corroboration echo-safety, C→A/B real-symbol wiring, privacy name-anchoring, import validation, revocation guard). This plan adds: three integration tests over the merged surface (A↔B echo, C real end-to-end, egress gate), a thin observability slice (`origin` in `/graph`; cache-hit-rate + curator-queue in `/api/status`), and a rollout runbook doc. Tasks 1–3 are convergence tests whose expected result is PASS (the invariant holds) — a RED is a genuine regression to fix, not a TDD stub.

**Tech Stack:** Python 3, pytest (parallel via pytest-xdist `-n auto`), stdlib sqlite3, the `tests/helpers/org_fleet.py` fleet simulator.

## Global Constraints

- **No new core behaviour in Tasks 1–3** — they assert existing merged behaviour. If a test is RED, stop and fix the *source* bug it found; do not weaken the test.
- **Tests:** pytest, flat `tests/test_*.py`, functions `test_*`. Construct stores as `Store(tmp_path / "x.sqlite3", dim=4)` then `.init()`. Use `tests/helpers/org_fleet.py` — `LocalDirFleetStorage(root)`, `make_install(root, name, *, dim=4, role="member")`, `make_fleet(root, n_members, *, dim=4) -> (members, curator, fleet_storage)`.
- **Suite runs parallel by default** (`addopts = -n auto`). Do NOT use `monkeypatch.setitem(sys.modules, "mcpbrain.fleet_storage", …)` — it is bypassed once the real module is imported (a bug the hardening pass already had to fix). Patch attributes on the real module instead: `monkeypatch.setattr(fleet_storage, "fleet_folder_storage", …)`.
- **No version bump, no push, no release** — Phase D is source work; shipping is a separate explicit instruction (per `CLAUDE.md`).
- **A#4 (publishing the `enrich` payload in cache artifacts) is OUT OF SCOPE** — it is entangled with activating the A→B echo path and is tracked separately. Task 1 verifies the echo path is currently *inert* (cache import writes no graph rows), which is what makes the system safe today.
- Reference spec: `docs/superpowers/specs/2026-07-03-org-baseline-personal-overlay-design.md` (Phase D section).

---

## File Structure

**Created:**
- `tests/test_org_phase_d_echo.py` — A↔B echo-dedup + cache-import-writes-no-graph invariant.
- `tests/test_org_phase_d_e2e.py` — full fleet: curator publishes → new member bootstraps with real A/B, zero extraction.
- `tests/test_org_phase_d_egress.py` — adversarial "nothing content-shaped escapes" over the real serialized contribution/cache bytes.
- `docs/ORG-BASELINE-ROLLOUT.md` — fleet-wide enablement runbook.

**Modified:**
- `mcpbrain/graph_view.py` — add `origin` to the node SELECT + node dict (`graph_canvas`).
- `mcpbrain/wizard/graph.html` — colour nodes by `origin` (org vs local), legend entry.
- `mcpbrain/sync/__init__.py` — return per-drive cache hit/miss counts from `run_sync_cycle`.
- `mcpbrain/daemon.py` — surface cache-hit-rate + curator-queue counts in `status()`.
- `tests/test_graph_view.py`, `tests/test_sync_cycle.py`, `tests/test_daemon.py` (or the daemon-status test file) — assertions for the observability additions.

---

## Task 1: A↔B echo-dedup convergence test

**Files:**
- Create: `tests/test_org_phase_d_echo.py`

**Interfaces:**
- Consumes: `mcpbrain.org_contracts.source_ref(fleet_secret, doc_id) -> str` (HMAC-SHA256, 64-hex, stable across contributors for the same doc); `mcpbrain.org_curate._corroborated(relation, agg) -> bool` (counts `len(agg["srefs"])`; `mentioned_with` also needs `len(agg["contribs"]) >= 2`); `mcpbrain.ingest_cache._import_artifact` (writes only chunk rows via `store.import_cached_chunks`, never entities/relations).
- Produces: nothing (test-only).

- [ ] **Step 1: Write the echo-safety test**

```python
# tests/test_org_phase_d_echo.py
import base64
import struct

from mcpbrain import org_contracts as oc
from mcpbrain import org_curate, ingest_cache
from mcpbrain.org_contracts import FleetPin, CacheArtifact, CacheChunk, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage


def _store(tmp_path, name="s.sqlite3"):
    s = Store(tmp_path / name, dim=4); s.init(); return s


def test_shared_doc_echo_yields_one_source_ref_across_contributors():
    # The SAME doc, hashed by N contributors sharing the fleet_secret, collapses
    # to ONE source_ref — so corroboration (counted by distinct srefs) can never
    # be inflated by many members importing the same cached enrichment.
    secret = "s3cret"
    refs = {oc.source_ref(secret, "gdrive-DOC-1") for _ in range(5)}
    assert len(refs) == 1


def test_corroboration_not_inflated_by_echo():
    # 5 contributors, one shared doc (one sref) -> NOT corroborated for the
    # strict type; a genuinely independent 2-source/2-contributor set IS.
    echo = {"srefs": {"H"}, "contribs": {f"u{i}@x.org" for i in range(5)}}
    assert org_curate._corroborated("mentioned_with", echo) is False
    indep = {"srefs": {"H1", "H2"}, "contribs": {"a@x.org", "b@x.org"}}
    assert org_curate._corroborated("mentioned_with", indep) is True


def test_cache_import_writes_no_graph_rows(tmp_path):
    # The A->B echo path is INERT: importing a cache artifact populates chunks
    # only, never entities/relations, so there is nothing for the contribution
    # edge to re-emit. (A#4 would change this and is out of scope for Phase D.)
    s = _store(tmp_path)
    fs = LocalDirFleetStorage(tmp_path / "drv")
    pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                   enrich_logic_floor=1, fleet_secret="s3cret")
    vec_b64 = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    art = CacheArtifact(
        file_id="FID", content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="Acme quarterly numbers", embedding_b64=vec_b64,
                           metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),),
        enrich={"logic_version": 1}, published_by="p@x.org", published_at="2026-07-04")
    import gzip, json
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}",
                 gzip.compress(json.dumps(art.to_dict()).encode()))
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", pin) is True
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM entity_relations").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"] == 1
```

- [ ] **Step 2: Run — expected PASS (invariant holds)**

Run: `uv run --no-sync python -m pytest tests/test_org_phase_d_echo.py -v`
Expected: 3 passed. A RED on `test_cache_import_writes_no_graph_rows` means the echo path was activated (A#4 landed without corroboration protection) — stop and investigate.

- [ ] **Step 3: Commit**

```bash
git add tests/test_org_phase_d_echo.py
git commit --no-verify -m "test(org-baseline): Phase D — A<->B echo-dedup invariant (shared doc -> one source_ref; cache import writes no graph rows)"
```

---

## Task 2: C real end-to-end (zero-extraction bootstrap)

**Files:**
- Create: `tests/test_org_phase_d_e2e.py`

**Interfaces:**
- Consumes: `mcpbrain.onboarding.bootstrap_baseline(store, fleet_storage, drives, pin, *, import_snapshot=…, bootstrap_drive=…, make_drive_storage=None, done_drive_ids=(), snapshot_done=False)`; the REAL `mcpbrain.org_import.import_snapshot(store, fleet_storage) -> {"status": …}` and `mcpbrain.ingest_cache.bootstrap_drive(store, fleet_storage, drive_id, pin) -> {"imported","chunks","skipped","cache_hits"}`; `org_curate.run(store, fleet_storage, home)` to produce a published snapshot; `tests/helpers/org_fleet.make_fleet`.
- Produces: nothing (test-only). This replaces C's fakes with the real A/B functions — the convergence the spec deferred.

- [ ] **Step 1: Write the end-to-end test**

```python
# tests/test_org_phase_d_e2e.py
import base64
import gzip
import json
import struct

from mcpbrain import onboarding, org_curate, ingest_cache
from mcpbrain.org_contracts import (FleetPin, CacheArtifact, CacheChunk,
                                    artifact_filename)
from mcpbrain.store import Store
from tests.helpers.org_fleet import make_fleet, LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _publish_cache_artifact(drive_fs, file_id="F1"):
    vec = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    art = CacheArtifact(
        file_id=file_id, content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="shared drive doc body", embedding_b64=vec,
                           metadata={"source_type": "gdrive", "file_id": file_id, "chunk_index": 0}),),
        enrich={}, published_by="p@x.org", published_at="2026-07-04")
    drive_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename(file_id,'vh1','bge-small',4,'v1')}",
                       gzip.compress(json.dumps(art.to_dict()).encode()))


def test_new_member_bootstraps_from_real_snapshot_and_cache(tmp_path):
    members, curator, fleet_fs = make_fleet(tmp_path, n_members=1)
    (bob,) = members

    # (a) curator has an org entity + relation and publishes a real snapshot.
    with curator.store._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,email_addr) "
                   "VALUES('joel','Joel Chelliah','person','org','joel@acme.org')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('acme','Acme','org','org')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin,valid_from) "
                   "VALUES('joel','works_at','acme','org','2026-01-01')")
    from mcpbrain import config
    # curator.home is configured with role=org_curator by make_fleet; give it the pin.
    config.write_config(str(curator.home), {"org_config": {"org_pin": {
        "fleet_secret": "s3cret", "embed_model": "bge-small", "dim": 4,
        "chunker_version": "v1"}}})
    org_curate._publish(curator.store, fleet_fs, str(curator.home))  # publish snapshot v1

    # (b) a shared drive carries a cached artifact (bob's per-drive storage).
    drive_fs = LocalDirFleetStorage(tmp_path / "drive-D1")
    _publish_cache_artifact(drive_fs)

    # (c) bob bootstraps with the REAL A/B functions (no fakes), one drive D1.
    res = onboarding.bootstrap_baseline(
        bob.store, fleet_fs, ["D1"], PIN,
        make_drive_storage=lambda drive_id: drive_fs)

    # snapshot imported -> layer-1 graph present as origin='org'
    assert res["snapshot"]["status"] == "imported"
    joel = bob.store.get_entity("joel")
    assert joel is not None and joel["origin"] == "org"
    with bob.store._connect() as db:
        rel = db.execute("SELECT origin FROM entity_relations "
                         "WHERE entity_a='joel' AND relation='works_at'").fetchone()
    assert rel is not None and rel["origin"] == "org"

    # cache imported -> chunk present with ZERO local extraction (cache_hits>0)
    assert res["drives"]["D1"]["status"] == "ok"
    assert res["cache_hits"] >= 1
    assert bob.store.get_chunk("gdrive-F1-0") is not None


def test_bootstrap_ordering_snapshot_before_drives(tmp_path):
    # The contract: snapshot import is attempted before any drive cache import.
    members, curator, fleet_fs = make_fleet(tmp_path, n_members=1)
    (bob,) = members
    order = []

    def imp(store, fs):
        order.append("snapshot")
        return {"status": "no_snapshot"}

    def boot(store, fs, drive_id, pin):
        order.append(f"drive:{drive_id}")
        return {"status": "ok", "cache_hits": 0}

    onboarding.bootstrap_baseline(bob.store, fleet_fs, ["D1", "D2"], PIN,
                                  import_snapshot=imp, bootstrap_drive=boot,
                                  make_drive_storage=lambda d: fleet_fs)
    assert order[0] == "snapshot"
    assert set(order[1:]) == {"drive:D1", "drive:D2"}
```

- [ ] **Step 2: Run — expected PASS**

Run: `uv run --no-sync python -m pytest tests/test_org_phase_d_e2e.py -v`
Expected: 2 passed. If `test_new_member_bootstraps_…` is RED, the A/B convergence has a real gap (e.g. `bootstrap_drive` return shape or `import_snapshot` status drifted) — fix the source, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_org_phase_d_e2e.py
git commit --no-verify -m "test(org-baseline): Phase D — C real end-to-end bootstrap (real snapshot import + cache, zero extraction)"
```

---

## Task 3: Security / egress adversarial gate

**Files:**
- Create: `tests/test_org_phase_d_egress.py`

**Interfaces:**
- Consumes: `mcpbrain.org_contrib.collect_from_drain(store, drain_delta, pin, contributor_email) -> int` and `upload_pending(store, fleet_storage, contributor_email) -> {"uploaded": n, "batch": path}` (writes newline-joined `ContributionRecord.to_dict()` JSON to `contrib/<email>/<ts>.jsonl`); `ingest_cache.publish_file(...)` (writes only under `.mcpbrain-cache/`). `drain_delta = {"relations": [rows], "entities": {id: row}}`.
- Produces: nothing (test-only). Asserts the merged egress surface leaks nothing content-shaped, for adversarial inputs.

- [ ] **Step 1: Write the egress gate test**

```python
# tests/test_org_phase_d_egress.py
import json

from mcpbrain import org_contrib, ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(fleet_secret="s3cret",
               relation_allowlist=("works_at", "member_of", "mentioned_with"))

# Secret strings that must NEVER appear in anything uploaded to the fleet.
SECRETS = ["divorce lawyer", "my private note", "SSN 123-45-6789",
           "gdrive-secret-doc-42", "raw chunk body text"]


def _store(tmp_path):
    s = Store(tmp_path / "s.sqlite3", dim=4); s.init(); return s


def _adversarial_delta():
    # A drain delta packed with things that MUST be filtered/redacted:
    # - a person with a private annotation in name + aliases
    # - a person with NO email (must be dropped, unanchored)
    # - a non-allowlisted (sensitive) relation
    # - a role-address entity
    return {
        "relations": [
            {"entity_a": "joel", "relation": "works_at", "entity_b": "acme",
             "valid_from": "2026-01-01", "valid_to": "", "confidence": 0.9,
             "origin": "local", "source_doc_id": "gdrive-secret-doc-42"},
            {"entity_a": "joel", "relation": "has_diagnosis", "entity_b": "condition",
             "valid_from": "2026-01-01", "valid_to": "", "confidence": 0.9,
             "origin": "local", "source_doc_id": "gdrive-secret-doc-42"},
        ],
        "entities": {
            "joel": {"id": "joel", "name": "Joel (divorce lawyer)", "type": "person",
                     "org": "Acme", "email_addr": "joel@acme.org",
                     "aliases": "JC, my private note", "origin": "local",
                     "profile": "SSN 123-45-6789", "notes": "raw chunk body text"},
            "acme": {"id": "acme", "name": "Acme", "type": "org", "org": "",
                     "email_addr": "", "aliases": "", "origin": "local"},
            "condition": {"id": "condition", "name": "A Condition", "type": "person",
                          "org": "", "email_addr": "office@acme.org", "aliases": "",
                          "origin": "local"},
        },
    }


def _seed_chunk(store, doc_id, text):
    with store._connect() as db:
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,enrich_state) "
                   "VALUES(?,?,?,?, '')", (doc_id, text, "h",
                   json.dumps({"source_type": "gdrive"})))


def test_no_content_shaped_data_escapes_in_contributions(tmp_path):
    s = _store(tmp_path)
    _seed_chunk(s, "gdrive-secret-doc-42", "raw chunk body text")
    org_contrib.collect_from_drain(s, _adversarial_delta(), PIN, "alice@x.org")
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    org_contrib.upload_pending(s, fs, "alice@x.org")

    # Read back exactly the bytes that left the machine.
    blobs = [fs.get_bytes(p).decode() for p in fs.list_paths("contrib/")]
    uploaded = "\n".join(blobs)

    for secret in SECRETS:
        assert secret not in uploaded, f"leaked: {secret!r}"
    # No forbidden keys anywhere in the records.
    for line in uploaded.splitlines():
        rec = json.loads(line)
        assert set(rec) <= {"claim", "confidence", "valid_from", "valid_to",
                            "contributor_email", "source_kind", "source_ref", "schema"}
        assert set(rec["claim"]) <= {"kind", "id", "name", "type", "org",
                                     "email_addr", "aliases",
                                     "entity_a", "relation", "entity_b"}
        # the raw doc id is HMAC'd, never present
        assert "gdrive-secret-doc-42" != rec["source_ref"]
    # the sensitive relation and the unanchored/role-address people are gone.
    assert "has_diagnosis" not in uploaded
    assert "condition" not in uploaded          # role-address person dropped


def test_cache_artifacts_only_written_under_cache_dir(tmp_path):
    s = _store(tmp_path)
    # a fresh chunk to publish
    with s._connect() as db:
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,embedded) "
                   "VALUES('gdrive-F1-0','body','vh1','{\"source_type\":\"gdrive\",\"file_id\":\"F1\"}',1)")
    fs = LocalDirFleetStorage(tmp_path / "drv")
    ingest_cache.publish_file(s, fs, "D1", "F1", "vh1", PIN, published_by="p@x.org")
    paths = fs.list_paths("")
    assert paths, "expected an artifact to be published"
    assert all(p.startswith(ingest_cache.CACHE_DIR + "/") for p in paths), paths
```

- [ ] **Step 2: Run — expected PASS**

Run: `uv run --no-sync python -m pytest tests/test_org_phase_d_egress.py -v`
Expected: 2 passed. A RED here is a **privacy leak** — the single most serious finding; stop and fix the edge filter, never the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_org_phase_d_egress.py
git commit --no-verify -m "test(org-baseline): Phase D — egress gate (no content-shaped data escapes; cache stays in .mcpbrain-cache)"
```

---

## Task 4: Observability — `origin` in the `/graph` explorer

**Files:**
- Modify: `mcpbrain/graph_view.py` (SELECT ~lines 65-72; node dict ~lines 103-109)
- Modify: `mcpbrain/wizard/graph.html` (`nodeColour` ~lines 144-145; legend ~line 286)
- Test: `tests/test_graph_view.py`

**Interfaces:**
- Consumes: the `entities.origin` column (`'local'|'org'`, added in Phase 0).
- Produces: `graph_canvas` node dicts now include an `"origin"` key; the frontend colours org-origin nodes distinctly.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_graph_view.py
def test_graph_canvas_nodes_include_origin(tmp_path):
    from mcpbrain import graph_view
    from mcpbrain.store import Store
    s = Store(tmp_path / "g.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('a','Alice','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('b','Acme','org','org')")
    canvas = graph_view.graph_canvas(s)
    by_id = {n["id"]: n for n in canvas["nodes"]}
    assert by_id["a"]["origin"] == "local"
    assert by_id["b"]["origin"] == "org"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_graph_view.py -k origin -v`
Expected: FAIL — `KeyError: 'origin'` (node dict has no origin yet).

- [ ] **Step 3: Add `origin` to the SELECT and node dict**

In `mcpbrain/graph_view.py`, the entity SELECT (~65-72) — add `origin`:
```python
        rows = db.execute(
            "SELECT id, name, type, org, email_count, email_addr, degree, "
            "first_seen, last_seen, COALESCE(origin,'local') AS origin "
            "FROM entities " + where_clause + " ORDER BY degree DESC LIMIT ?",
            params).fetchall()
```
(Keep the existing column list; add the `COALESCE(origin,'local') AS origin` term — match the real SELECT's exact column set and WHERE/params.)

In the node dict (~103-109), add the field:
```python
            nodes.append({
                "id": r["id"], "name": r["name"], "type": r["type"],
                "org": r["org"], "email_count": r["email_count"],
                "email_addr": r["email_addr"], "connections": deg,
                "community": comm, "first_seen": r["first_seen"],
                "last_seen": r["last_seen"], "origin": r["origin"],
            })
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_graph_view.py -k origin -v`
Expected: PASS.

- [ ] **Step 5: Colour org nodes in the frontend**

In `mcpbrain/wizard/graph.html`, `nodeColour` (~144-145) — branch on origin so org-baseline nodes read as a distinct, consistent colour while local nodes keep type colouring:
```javascript
    function nodeColour(n){
      if (n.origin === "org") return palette.orgOrigin;
      return palette.byType[n.type] || palette.nodeFallback;
    }
```
Add `orgOrigin` to the palette object (near where `nodeFallback` is set, ~135-142), reading a CSS var e.g. `--graph-node-org` with a sensible fallback (a muted blue that works in both themes), and add one legend row "org baseline" near the existing entity-type legend (~286).

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/graph_view.py mcpbrain/wizard/graph.html tests/test_graph_view.py
git commit --no-verify -m "feat(graph): surface entity origin (org vs local) in the /graph explorer"
```

---

## Task 5: Observability — cache hit-rate + curator queue in `/api/status`

**Files:**
- Modify: `mcpbrain/sync/__init__.py` (`run_sync_cycle` result dict — the shared-drive block ~lines 106-160)
- Modify: `mcpbrain/daemon.py` (`status()` ~lines 648-726)
- Test: `tests/test_sync_cycle.py`, and the daemon-status test (`tests/test_daemon.py` or `tests/test_control_api*.py`)

**Interfaces:**
- Consumes: per-drive `info["processed"]` and `info["miss"]` from `sync_shared_drives`; the curator queue counts — `org_contrib_staging` row count (pending contributions), `_suppressed_pairs(store)` size (`org_curate._SUPPRESS_META`), and `meta['org_curator_version']`.
- Produces: `run_sync_cycle` result gains `shared_drive_cache = {"hits": int, "misses": int}`; `daemon.status()` gains `org = {"cache_hits", "cache_misses", "curator_version", "contrib_staged", "merge_suppressed"}`.

- [ ] **Step 1: Write the failing test for cache hit/miss counts**

```python
# add to tests/test_sync_cycle.py — reuse this file's existing _patched harness.
def test_run_sync_cycle_reports_cache_hit_miss_counts(tmp_path, monkeypatch):
    # Drive with 2 files: 1 cache hit, 1 miss. The cycle result must report both.
    ... # build a FakeDriveService + pinned config as the other sync_cycle tests do,
        # pre-publish an artifact for file A (hit), leave file B unpublished (miss),
        # run run_sync_cycle, then:
    assert result["shared_drive_cache"] == {"hits": 1, "misses": 1}
```
(Model the fixture on the existing `test_run_sync_cycle_backfills_pinned_shared_drive_pre_existing_files` in this file — same `_patched` `sync_shared_drives` wrapper, same pinned config. The miss count is `len(info["miss"])` summed across drives; hits = `processed - len(miss)`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_sync_cycle.py -k cache_hit_miss -v`
Expected: FAIL — `KeyError: 'shared_drive_cache'`.

- [ ] **Step 3: Aggregate hit/miss in `run_sync_cycle`**

In `mcpbrain/sync/__init__.py`, in the shared-drive block where `per_drive`/`total_files` are built (the loop over `sd.items()` ~110-127), accumulate misses and derive hits, then add to `result` alongside `result["shared_drives"]`:
```python
                total_miss = 0
                for drive_id, info in sd.items():
                    if drive_id == "_revoked":
                        continue
                    ...
                    total_miss += len(info["miss"])
                    ...
                result["shared_drive_cache"] = {
                    "hits": max(0, total_files - total_miss), "misses": total_miss}
```
(Place the `result["shared_drive_cache"] = …` next to `result["shared_drives"] = per_drive` at ~128. `total_files` already sums `info["processed"]`.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_sync_cycle.py -k cache_hit_miss -v`
Expected: PASS.

- [ ] **Step 5: Write the failing test for the status `org` block**

```python
# add to the daemon-status test file (same style as existing daemon.status() tests)
def test_status_includes_org_block(tmp_path, monkeypatch):
    ... # build a Daemon over a store (as existing status tests do); seed:
        #   meta org_curator_version=3; one org_contrib_staging row;
        #   org_curate._suppress_pair(store, "a|b")
    st = daemon.status()
    assert st["org"]["curator_version"] == 3
    assert st["org"]["contrib_staged"] == 1
    assert st["org"]["merge_suppressed"] == 1
    assert "cache_hits" in st["org"] and "cache_misses" in st["org"]
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run --no-sync python -m pytest -k status_includes_org -v`
Expected: FAIL — `KeyError: 'org'`.

- [ ] **Step 7: Add the `org` block to `daemon.status()`**

In `mcpbrain/daemon.py` `status()` (~648-726), before the return, assemble the org block from the store (best-effort; never raise out of status):
```python
        org = {"cache_hits": self._last_cache_hits, "cache_misses": self._last_cache_misses,
               "curator_version": 0, "contrib_staged": 0, "merge_suppressed": 0}
        try:
            from mcpbrain import org_curate
            with self._store._connect() as db:
                org["curator_version"] = int(self._store.get_meta("org_curator_version") or 0)
                org["contrib_staged"] = db.execute(
                    "SELECT COUNT(*) c FROM org_contrib_staging").fetchone()["c"]
            org["merge_suppressed"] = len(org_curate._suppressed_pairs(self._store))
        except Exception as exc:  # noqa: BLE001 — status must never raise
            log.debug("status org block degraded: %s", exc)
```
Add `self._last_cache_hits = 0` / `self._last_cache_misses = 0` in `Daemon.__init__` (near the other `_last_*` fields ~510-552), and set them from `run_sync_cycle`'s `result["shared_drive_cache"]` in `run_cycle` where the sync result is handled (mirror how other per-cycle counts are stashed). Add `"org": org` to the status dict returned at the end.

- [ ] **Step 8: Run to verify it passes**

Run: `uv run --no-sync python -m pytest -k "status_includes_org or cache_hit_miss" -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/sync/__init__.py mcpbrain/daemon.py tests/test_sync_cycle.py tests/test_daemon.py
git commit --no-verify -m "feat(org-baseline): surface cache hit-rate + curator queue counts in /api/status"
```

---

## Task 6: Rollout enablement runbook

**Files:**
- Create: `docs/ORG-BASELINE-ROLLOUT.md`
- Test: `tests/test_org_baseline_rollout_doc.py` (a lightweight doc-contract check)

**Interfaces:**
- Consumes: the config gates verified in the spec — `is_configured`, `install_role`/`is_org_curator`, `fleet_pin().is_pinned` (master gate = `fleet_secret` distributed via `org-config.json`), `org_import_enabled`/`org_contrib_enabled`/`ingest_cache_enabled` (all default True), `fleet._ALLOWLIST = {"cadences","org_pin"}`.
- Produces: an operator runbook. The test guards that the documented enable-order invariants match the code (so the doc can't silently drift).

- [ ] **Step 1: Write the doc-contract test**

```python
# tests/test_org_baseline_rollout_doc.py
from pathlib import Path

DOC = Path("docs/ORG-BASELINE-ROLLOUT.md")


def test_runbook_exists_and_states_the_master_gate():
    text = DOC.read_text()
    # The load-bearing facts the runbook MUST state (guards against drift).
    assert "fleet_secret" in text            # the master enable gate
    assert "org-config.json" in text         # how the pin is distributed
    assert "org_pin" in text
    assert "role" in text and "org_curator" in text
    # enable order: curator publishes a snapshot BEFORE members import
    assert "before" in text.lower()


def test_runbook_matches_code_gates():
    # If any of these flip default in code, the runbook's "default ON" claim is
    # stale — this test forces the doc and code to be reconciled together.
    from mcpbrain import config
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        assert config.org_import_enabled(home) is True
        assert config.ingest_cache_enabled(home) is True
        assert config.org_contrib_enabled(home) is True
        assert config.fleet_pin(home).is_pinned is False  # nothing moves without the secret
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run --no-sync python -m pytest tests/test_org_baseline_rollout_doc.py -v`
Expected: FAIL — `FileNotFoundError` (doc not written yet).

- [ ] **Step 3: Write the runbook**

Create `docs/ORG-BASELINE-ROLLOUT.md` with the enable order verified from code (each step names the exact gate). Content:

```markdown
# Org baseline — fleet rollout runbook

The three org flags (`org_import_enabled`, `ingest_cache_enabled`,
`org_contrib_enabled`) all default **ON**, so they are NOT the fleet-wide switch.
**The master gate is `FleetPin.is_pinned`, which is true only once a `fleet_secret`
is distributed** — until then nothing content-shaped, no cache, and no
contributions move. Enable in this order:

1. **Each install is configured** (`is_configured`: owner name + email + ≥1 org).
   Nothing enriches or bootstraps before this.
2. **Designate the curator.** On one always-on install set `role = "org_curator"`
   in `config.json` (`is_org_curator`). Only it runs the curator cadence.
3. **Curator publishes the first snapshot** (`org_curate.run` → `org-graph/manifest.json`
   in the fleet folder) BEFORE any member import is expected to succeed — a member
   importing before a snapshot exists gets `status: no_snapshot` and stays retryable.
4. **Distribute the pin.** Put an `org_pin` block (with `fleet_secret`, `embed_model`,
   `dim`, `chunker_version`) into `org-config.json` in the fleet folder. `fleet.py`'s
   allowlist (`{"cadences","org_pin"}`) permits exactly this; each install picks it up
   on next daemon start via `merge_org_config` (wholesale-replaced, so removing it
   reverts). After this, `is_pinned` is True fleet-wide.
5. **Flow begins automatically** (flags already default ON): cache publish/import,
   contributions (also gated on `is_pinned`), and snapshot import all activate. New
   users bootstrap instantly via `mcpbrain setup` → the baseline-bootstrap step.

**Disable / rollback:** remove `org_pin` from `org-config.json` → `is_pinned` reverts
on next start (cache + contributions stop; already-imported org rows remain until the
curator republishes/tombstones). Per-install opt-out: set any of the three flags false
in that install's `config.json` (an opt-out still consumes the snapshot).

**Data-safety notes:** revocation purge only fires after a drive is absent for
`ingest_cache_revocation_threshold` (default 5) consecutive cycles AND never on a
blanket-empty enumeration; contributions are typed/redacted/HMAC-referenced; the
curator re-enforces the relation allowlist as a backstop.
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run --no-sync python -m pytest tests/test_org_baseline_rollout_doc.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/ORG-BASELINE-ROLLOUT.md tests/test_org_baseline_rollout_doc.py
git commit --no-verify -m "docs(org-baseline): fleet rollout enablement runbook (+ doc-contract test)"
```

---

## Task 7: Phase D exit gate

**Files:**
- Test: full suite.

- [ ] **Step 1: Run the whole suite (parallel)**

Run: `uv run --no-sync python -m pytest tests/ -q`
Expected: all pass (the ~2263 baseline + the new Phase D tests), ~25-40s. Any RED in a Task 1–3 test is a real invariant break — fix the source.

- [ ] **Step 2: Confirm the plugin agent is in sync (no drift)**

Run: `uv run --no-sync python bin/sync_agents.py` then `git status --short`
Expected: no changes to `plugin/agents/enrich-batch.md` (Phase D doesn't touch extraction rules; this just confirms no drift).

- [ ] **Step 3: Final commit if anything was regenerated**

```bash
git add -A && git commit --no-verify -m "chore(org-baseline): Phase D convergence complete — invariants verified, observability + rollout runbook" || echo "nothing to commit"
```

---

## Self-Review

**Spec coverage** (Phase D items from the spec):
- A↔B echo-dedup integration test — Task 1 (echo → one source_ref; corroboration not inflated; cache import writes no graph rows). ✓
- C real end-to-end (fakes → real bootstrap_drive + import_snapshot, zero extraction) — Task 2. ✓
- Security / egress adversarial gate — Task 3 (nothing content-shaped escapes; cache stays in `.mcpbrain-cache/`). ✓
- Rollout enablement runbook — Task 6 (+ doc-contract test guarding drift). ✓
- Observability slice (origin in /graph; cache hit-rate; curator queue) — Task 4 (/graph origin) + Task 5 (cache hit-rate + curator queue in status). ✓
- A#4 enrich-payload publish — explicitly OUT OF SCOPE (Global Constraints); Task 1 verifies the echo path is inert without it. ✓

**Placeholder scan:** Task 5 Step 1 uses `...` for the fixture body — this is a deliberate pointer to an existing named fixture (`test_run_sync_cycle_backfills_pinned_shared_drive_pre_existing_files`) to copy, not an unfilled requirement; the assertion and the aggregation code (Step 3) are complete. All other steps have complete code.

**Type consistency:** `shared_drive_cache = {"hits","misses"}` is produced in Task 5 Step 3 and asserted in Step 1 with the same keys. `status()["org"]` keys (`cache_hits`, `cache_misses`, `curator_version`, `contrib_staged`, `merge_suppressed`) match between Step 5 (test) and Step 7 (impl). `graph_canvas` node `origin` key matches between Task 4 Step 1 (test) and Step 3 (impl). `bootstrap_baseline` / `import_snapshot` / `bootstrap_drive` signatures match the verified current code. `_suppressed_pairs` / `_suppress_pair` / `CACHE_DIR` / `artifact_filename` / `source_ref` are all real current symbols.
