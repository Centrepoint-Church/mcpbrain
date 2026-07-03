# Org Baseline — Phase 0 (Foundations) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land every piece of shared surface — schema, config, interface contracts, the curator role, and a test harness — that subsystems A, B, and C will all depend on, so the three can then be built in parallel worktrees without colliding.

**Architecture:** Phase 0 adds *no user-visible behaviour*. It migrates the SQLite store in place (new `origin` column + three org tables), adds config accessors and the fleet-pinning plumbing, freezes the on-the-wire data shapes as stdlib dataclasses in a new `mcpbrain/org_contracts.py`, registers three **no-op** cadence slots that subsystem B will later fill in, and ships a filesystem-backed multi-user + curator test harness. Everything defaults to safe/off-by-effect: flags exist but the code paths that read them aren't wired until A/B/C.

**Tech Stack:** Python 3, stdlib `sqlite3` + `sqlite-vec`, stdlib `dataclasses`/`hashlib`/`hmac`, pytest.

## Global Constraints

- **No migration framework.** Schema lives inline in `Store.init()`. Add columns with the idempotent idiom: `cols = {r["name"] for r in db.execute("PRAGMA table_info(T)").fetchall()}; if "c" not in cols: db.execute("ALTER TABLE T ADD COLUMN c ...")`. SQLite `ADD COLUMN` cannot take a non-constant default — every added column is nullable or has a constant default.
- **`drive_id` lives inside the chunk `metadata` JSON**, not as a new column (mirrors the existing `file_id`/`source_type` convention in `sync/drive.py`). Phase 0 only defines the key constant.
- **Typed contracts use stdlib `@dataclass` only** — no `pydantic`, no `TypedDict` (matches house style: `graph_write.OwnerIdentity`, `sync/normalise.Chunk`). Frozen dataclasses for immutable value/wire contracts.
- **Feature-flag accessor idiom:** `def x_enabled(home) -> bool: return bool(read_config(home).get("x", DEFAULT))`, with a docstring stating the default.
- **Tests:** pytest, flat `tests/test_*.py`, functions `test_*`. Construct stores as `Store(tmp_path / "x.sqlite3", dim=4)` then `.init()`. Read cols via `PRAGMA table_info`.
- **No version bump, no release, no push in Phase 0** (per `CLAUDE.md`: pushing/releasing is a separate explicit instruction). Commit locally only.
- Reference spec: `docs/superpowers/specs/2026-07-03-org-baseline-personal-overlay-design.md`.

---

## File Structure

**Created:**
- `mcpbrain/org_contracts.py` — all frozen wire dataclasses (`CacheArtifact`, `CacheChunk`, `ContributionRecord`, `Tombstone`, `SnapshotManifest`), schema-version + key constants, the pure helpers (`pipeline_fingerprint`, `source_ref`, `artifact_filename`), the `FleetPin` config-view dataclass, and the `FleetStorage` transport `Protocol`. One responsibility: *the frozen shared vocabulary*.
- `tests/helpers/org_fleet.py` — test-only `LocalDirFleetStorage` (a tmp-dir `FleetStorage`) + the multi-user/curator simulation builders (`make_install`, `make_fleet`).
- `tests/test_org_contracts.py`, `tests/test_org_fleet.py`, `tests/test_org_config_flags.py`, `tests/test_fleet_pin.py`, `tests/test_org_cadence_stubs.py` — new test modules.

**Modified:**
- `mcpbrain/store.py` — `origin` columns + indexes and the three org tables, added inside `Store.init()`.
- `mcpbrain/config.py` — role + flag accessors and `fleet_pin()`.
- `mcpbrain/fleet.py` — extend `_ALLOWLIST` with `org_pin`.
- `mcpbrain/daemon.py` — three no-op cadence slots (registration + defaults + keys + interval wiring + `_run_*` stubs).
- `tests/test_store_schema.py` — migration tests for the new columns/tables.

---

## Task 1: Schema — `origin` column on entities + entity_relations

**Files:**
- Modify: `mcpbrain/store.py` (inside `Store.init()`, after the entity_relations index block, ~store.py:212)
- Test: `tests/test_store_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `entities.origin` and `entity_relations.origin` columns (`TEXT DEFAULT 'local'`, values `'local'`|`'org'`); indexes `idx_ent_origin`, `idx_er_origin`. Every later subsystem relies on these existing.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store_schema.py` (reuse the module's existing `_store`/`_cols` helpers at the top of that file):

```python
def test_origin_column_present_on_fresh_store(tmp_path):
    s = _store(tmp_path)
    assert "origin" in _cols(s, "entities")
    assert "origin" in _cols(s, "entity_relations")


def test_origin_defaults_to_local_and_backfills_old_store(tmp_path):
    import sqlite3
    path = tmp_path / "old_origin.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE entities(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL)""")
    db.execute("""CREATE TABLE entity_relations(
        id INTEGER PRIMARY KEY AUTOINCREMENT, entity_a TEXT NOT NULL,
        relation TEXT NOT NULL, entity_b TEXT NOT NULL,
        UNIQUE(entity_a, relation, entity_b))""")
    db.execute("INSERT INTO entities(id,name,type) VALUES('joel','Joel','person')")
    db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) "
               "VALUES('joel','works_at','acme')")
    db.commit(); db.close()

    s = Store(path, dim=4); s.init()
    with s._connect() as conn:
        assert "origin" in _cols(s, "entities")
        ent = conn.execute("SELECT origin FROM entities WHERE id='joel'").fetchone()
        rel = conn.execute("SELECT origin FROM entity_relations "
                           "WHERE entity_a='joel'").fetchone()
    assert ent["origin"] == "local"
    assert rel["origin"] == "local"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store_schema.py::test_origin_column_present_on_fresh_store tests/test_store_schema.py::test_origin_defaults_to_local_and_backfills_old_store -v`
Expected: FAIL — `assert 'origin' in {...}` (column not yet added).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/store.py`, inside `Store.init()`, immediately after the existing `entity_relations` index creation (the block ending ~store.py:212, before the `entity_observations` table), insert:

```python
        # Org-baseline layer tag: 'local' (default) or 'org'. Populated stores
        # migrate in place via the PRAGMA-check ALTER idiom (const default only).
        ent_cols = {row["name"] for row in
                    db.execute("PRAGMA table_info(entities)").fetchall()}
        if "origin" not in ent_cols:
            db.execute("ALTER TABLE entities ADD COLUMN origin TEXT DEFAULT 'local'")
        er_origin_cols = {row["name"] for row in
                          db.execute("PRAGMA table_info(entity_relations)").fetchall()}
        if "origin" not in er_origin_cols:
            db.execute("ALTER TABLE entity_relations ADD COLUMN origin TEXT DEFAULT 'local'")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ent_origin ON entities(origin)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_er_origin ON entity_relations(origin)")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store_schema.py -k origin -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store_schema.py
git commit -m "feat(store): add origin column (local|org) to entities + entity_relations"
```

---

## Task 2: Schema — org contribution/staging/re-point tables

**Files:**
- Modify: `mcpbrain/store.py` (inside `Store.init()`, after the `entity_merge_log` CREATE, ~store.py:395)
- Test: `tests/test_store_schema.py`

**Interfaces:**
- Consumes: nothing.
- Produces: three tables —
  - `org_contrib_outbox(id, record TEXT, created_at, uploaded_at)` — edge-side queue of JSON `ContributionRecord`s awaiting upload (`uploaded_at=''` means pending).
  - `org_contrib_staging(id, contributor_email, source_ref, claim TEXT, confidence, valid_from, valid_to, source_kind, batch_file, ingested_at, UNIQUE(contributor_email, source_ref, claim))` — curator-side ingested contributions; the UNIQUE makes re-ingest idempotent.
  - `org_repoint_log(id, from_entity_id, to_entity_id, snapshot_version, reason, at)` — records local→org merge re-points for split recovery (spec B4a rule 4).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_store_schema.py`:

```python
def test_org_tables_created(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"org_contrib_outbox", "org_contrib_staging", "org_repoint_log"} <= names


def test_org_contrib_staging_dedups_identical_rows(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        for _ in range(2):
            db.execute(
                "INSERT OR IGNORE INTO org_contrib_staging"
                "(contributor_email, source_ref, claim) VALUES(?,?,?)",
                ("a@x.org", "deadbeef", '{"kind":"entity","id":"joel"}'))
        n = db.execute("SELECT COUNT(*) c FROM org_contrib_staging").fetchone()["c"]
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store_schema.py -k org_tables -v`
Expected: FAIL — tables absent (`no such table` on the staging insert, or set assertion fails).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/store.py`, inside `Store.init()`, immediately after the `entity_merge_log` CREATE TABLE (~store.py:395), insert:

```python
        # -- Org-baseline (Phase 0) --------------------------------------------
        # Edge outbox: allowlisted, redacted ContributionRecords queued locally
        # per drain; a daily cadence uploads pending rows (uploaded_at == '').
        db.execute("""CREATE TABLE IF NOT EXISTS org_contrib_outbox(
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            record      TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            uploaded_at TEXT DEFAULT '')""")
        # Curator staging: contributions ingested from the fleet folder awaiting
        # deterministic merge + adjudication. UNIQUE makes re-ingest idempotent.
        db.execute("""CREATE TABLE IF NOT EXISTS org_contrib_staging(
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            contributor_email TEXT NOT NULL,
            source_ref        TEXT NOT NULL,
            claim             TEXT NOT NULL,
            confidence        REAL DEFAULT 1.0,
            valid_from        TEXT DEFAULT '',
            valid_to          TEXT DEFAULT '',
            source_kind       TEXT DEFAULT '',
            batch_file        TEXT DEFAULT '',
            ingested_at       TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(contributor_email, source_ref, claim))""")
        # Consumer re-point log: local entities merged into an org node at import,
        # so a later curator split can restore local flesh (spec B4a rule 4).
        db.execute("""CREATE TABLE IF NOT EXISTS org_repoint_log(
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity_id   TEXT NOT NULL,
            to_entity_id     TEXT NOT NULL,
            snapshot_version INTEGER DEFAULT 0,
            reason           TEXT DEFAULT '',
            at               TEXT DEFAULT CURRENT_TIMESTAMP)""")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store_schema.py -k org -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store_schema.py
git commit -m "feat(store): add org_contrib_outbox/staging + org_repoint_log tables"
```

---

## Task 3: Interface contracts module (`org_contracts.py`)

**Files:**
- Create: `mcpbrain/org_contracts.py`
- Test: `tests/test_org_contracts.py`

**Interfaces:**
- Consumes: nothing (stdlib only).
- Produces (imported by A, B, C):
  - Constants: `CACHE_ARTIFACT_SCHEMA=1`, `CONTRIBUTION_SCHEMA=1`, `SNAPSHOT_SCHEMA=1`, `DRIVE_ID_META_KEY="drive_id"`, `DEFAULT_RELATION_ALLOWLIST=("works_at","member_of","mentioned_with")`.
  - Frozen dataclasses `CacheChunk`, `CacheArtifact`, `ContributionRecord`, `Tombstone`, `SnapshotManifest`, each with `to_dict() -> dict` and a `from_dict(d) -> Self` classmethod (JSON round-trippable).
  - `FleetPin` frozen dataclass (fields: `embed_model:str`, `dim:int`, `chunker_version:str`, `enrich_logic_floor:int`, `relation_allowlist:tuple[str,...]`, `fleet_secret:str`; property `is_pinned -> bool` == `bool(fleet_secret)`).
  - Pure helpers: `pipeline_fingerprint(embed_model, dim, chunker_version) -> str` (64-hex sha256), `source_ref(fleet_secret, doc_id) -> str` (64-hex HMAC-SHA256), `artifact_filename(file_id, content_hash, embed_model, dim, chunker_version) -> str` (`<file_id>.<hash[:12]>.<fp[:8]>.mbc.gz`).
  - `FleetStorage` `typing.Protocol` with `put_bytes(path, data)`, `get_bytes(path) -> bytes|None`, `list_paths(prefix) -> list[str]`, `delete(path)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_contracts.py`:

```python
from mcpbrain import org_contracts as oc


def test_pipeline_fingerprint_deterministic_and_sensitive():
    a = oc.pipeline_fingerprint("bge-small", 384, "v1")
    b = oc.pipeline_fingerprint("bge-small", 384, "v1")
    c = oc.pipeline_fingerprint("bge-small", 768, "v1")
    assert a == b and len(a) == 64
    assert a != c


def test_source_ref_stable_across_callers_but_secret_dependent():
    r1 = oc.source_ref("s3cret", "gdrive-abc")
    r2 = oc.source_ref("s3cret", "gdrive-abc")
    r3 = oc.source_ref("other", "gdrive-abc")
    assert r1 == r2 and len(r1) == 64
    assert r1 != r3


def test_artifact_filename_shape():
    fn = oc.artifact_filename("FID", "0123456789abcdef", "bge-small", 384, "v1")
    parts = fn.split(".")
    assert fn.endswith(".mbc.gz")
    assert parts[0] == "FID"
    assert parts[1] == "0123456789ab"          # 12 hex chars
    assert len(parts[2]) == 8                    # pipeline fp prefix


def test_cache_artifact_round_trips():
    art = oc.CacheArtifact(
        file_id="FID", content_hash="abc", extraction_method="text",
        chunker_version="v1", embed_model="bge-small", dim=384,
        chunks=(oc.CacheChunk(idx=0, text="hi", embedding_b64="AAAA", metadata={"k": 1}),),
        enrich={"logic_version": 1}, published_by="a@x.org", published_at="2026-07-03")
    d = art.to_dict()
    import json
    again = oc.CacheArtifact.from_dict(json.loads(json.dumps(d)))
    assert again == art
    assert again.chunks[0].text == "hi"


def test_contribution_record_round_trips():
    rec = oc.ContributionRecord(
        claim={"kind": "relation", "a": "joel", "rel": "works_at", "b": "acme"},
        confidence=0.9, valid_from="2026-01-01", contributor_email="a@x.org",
        source_kind="email", source_ref="deadbeef")
    import json
    again = oc.ContributionRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert again == rec


def test_manifest_and_tombstone_round_trip():
    m = oc.SnapshotManifest(version=3, created_at="t", entity_count=10,
                            relation_count=5, tombstone_count=1, snapshot_sha256="ff")
    assert oc.SnapshotManifest.from_dict(m.to_dict()) == m
    t = oc.Tombstone(entity_id="dup", merged_into="joel-chelliah")
    assert oc.Tombstone.from_dict(t.to_dict()) == t


def test_fleet_pin_defaults_and_is_pinned():
    empty = oc.FleetPin()
    assert empty.relation_allowlist == oc.DEFAULT_RELATION_ALLOWLIST
    assert empty.is_pinned is False
    assert oc.FleetPin(fleet_secret="x").is_pinned is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_contracts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.org_contracts'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/org_contracts.py`:

```python
"""Frozen shared vocabulary for the org-baseline feature (Phase 0).

Everything subsystems A (ingest cache), B (org graph), and C (onboarding) must
agree on lives here: the on-the-wire dataclasses, the pure fingerprint/HMAC
helpers, the config-view of the fleet pin, and the transport Protocol. Frozen
by design — changing a shape is a fleet-wide contract change, not a local edit.

House style: stdlib @dataclass only (no pydantic/TypedDict).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass, field
from typing import Protocol

CACHE_ARTIFACT_SCHEMA = 1
CONTRIBUTION_SCHEMA = 1
SNAPSHOT_SCHEMA = 1

# Chunk-metadata JSON key naming the Shared Drive a chunk came from (nullable;
# absent/None => My Drive or shared-with-me). NOT a store column — see plan.
DRIVE_ID_META_KEY = "drive_id"

# Built-in relation allowlist floor; org-config may narrow/extend it via the pin.
DEFAULT_RELATION_ALLOWLIST = ("works_at", "member_of", "mentioned_with")


# -- wire dataclasses -------------------------------------------------------

@dataclass(frozen=True)
class CacheChunk:
    idx: int
    text: str
    embedding_b64: str
    metadata: dict


@dataclass(frozen=True)
class CacheArtifact:
    file_id: str
    content_hash: str
    extraction_method: str
    chunker_version: str
    embed_model: str
    dim: int
    chunks: tuple[CacheChunk, ...]
    enrich: dict
    published_by: str
    published_at: str
    schema: int = CACHE_ARTIFACT_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CacheArtifact":
        chunks = tuple(CacheChunk(**c) for c in d.get("chunks", []))
        return cls(
            file_id=d["file_id"], content_hash=d["content_hash"],
            extraction_method=d.get("extraction_method", ""),
            chunker_version=d.get("chunker_version", ""),
            embed_model=d["embed_model"], dim=int(d["dim"]),
            chunks=chunks, enrich=d.get("enrich") or {},
            published_by=d.get("published_by", ""),
            published_at=d.get("published_at", ""),
            schema=int(d.get("schema", CACHE_ARTIFACT_SCHEMA)))


@dataclass(frozen=True)
class ContributionRecord:
    claim: dict
    confidence: float
    valid_from: str
    contributor_email: str
    source_kind: str
    source_ref: str
    valid_to: str = ""
    schema: int = CONTRIBUTION_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ContributionRecord":
        return cls(
            claim=d["claim"], confidence=float(d.get("confidence", 1.0)),
            valid_from=d.get("valid_from", ""),
            contributor_email=d["contributor_email"],
            source_kind=d.get("source_kind", ""), source_ref=d["source_ref"],
            valid_to=d.get("valid_to", ""),
            schema=int(d.get("schema", CONTRIBUTION_SCHEMA)))


@dataclass(frozen=True)
class Tombstone:
    entity_id: str
    merged_into: str = ""   # "" => hard delete; else re-point target

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Tombstone":
        return cls(entity_id=d["entity_id"], merged_into=d.get("merged_into", ""))


@dataclass(frozen=True)
class SnapshotManifest:
    version: int
    created_at: str
    entity_count: int
    relation_count: int
    tombstone_count: int
    snapshot_sha256: str
    schema: int = SNAPSHOT_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotManifest":
        return cls(
            version=int(d["version"]), created_at=d.get("created_at", ""),
            entity_count=int(d.get("entity_count", 0)),
            relation_count=int(d.get("relation_count", 0)),
            tombstone_count=int(d.get("tombstone_count", 0)),
            snapshot_sha256=d.get("snapshot_sha256", ""),
            schema=int(d.get("schema", SNAPSHOT_SCHEMA)))


@dataclass(frozen=True)
class FleetPin:
    """Config-view of the fleet-wide pin (org-config.json 'org_pin' block)."""
    embed_model: str = ""
    dim: int = 0
    chunker_version: str = ""
    enrich_logic_floor: int = 0
    relation_allowlist: tuple[str, ...] = DEFAULT_RELATION_ALLOWLIST
    fleet_secret: str = ""

    @property
    def is_pinned(self) -> bool:
        # A secret is required before anything is contributed; its presence is
        # the "org has configured the fleet" signal (spec Phase D ordering).
        return bool(self.fleet_secret)


# -- pure helpers -----------------------------------------------------------

def pipeline_fingerprint(embed_model: str, dim: int, chunker_version: str) -> str:
    """64-hex sha256 of the embedding pipeline identity. Cache artifacts from
    different pipelines never collide (spec A2)."""
    raw = f"{embed_model}|{int(dim)}|{chunker_version}".encode()
    return hashlib.sha256(raw).hexdigest()


def source_ref(fleet_secret: str, doc_id: str) -> str:
    """HMAC-SHA256(fleet_secret, doc_id) as 64-hex. Stable across users (so the
    curator dedupes echoes) yet reveals nothing about the source doc (spec B2)."""
    return hmac.new(fleet_secret.encode(), doc_id.encode(), hashlib.sha256).hexdigest()


def artifact_filename(file_id: str, content_hash: str, embed_model: str,
                      dim: int, chunker_version: str) -> str:
    """`<file_id>.<hash[:12]>.<pipeline_fp[:8]>.mbc.gz` (spec A2)."""
    pf8 = pipeline_fingerprint(embed_model, dim, chunker_version)[:8]
    return f"{file_id}.{content_hash[:12]}.{pf8}.mbc.gz"


# -- transport contract -----------------------------------------------------

class FleetStorage(Protocol):
    """Blob transport over the fleet folder / in-drive cache folders. Prod
    implements this over Google Drive (built in Phase A); tests implement it
    over a temp dir (LocalDirFleetStorage). Paths are '/'-separated relatives."""

    def put_bytes(self, path: str, data: bytes) -> None: ...
    def get_bytes(self, path: str) -> bytes | None: ...   # None when absent
    def list_paths(self, prefix: str) -> list[str]: ...
    def delete(self, path: str) -> None: ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_contracts.py -v`
Expected: PASS (all 7 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_contracts.py tests/test_org_contracts.py
git commit -m "feat(org): freeze wire contracts, fingerprint/HMAC helpers, FleetStorage protocol"
```

---

## Task 4: `LocalDirFleetStorage` test transport

**Files:**
- Create: `tests/helpers/org_fleet.py`
- Test: `tests/test_org_fleet.py`

**Interfaces:**
- Consumes: `mcpbrain.org_contracts.FleetStorage` (structural — the class must satisfy the Protocol).
- Produces: `LocalDirFleetStorage(root: pathlib.Path)` — a filesystem-backed `FleetStorage`. `put_bytes` creates parent dirs; `get_bytes` returns `None` for a missing path; `list_paths(prefix)` returns sorted `/`-relative paths under the prefix; `delete` is a no-op on a missing path.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_fleet.py`:

```python
from tests.helpers.org_fleet import LocalDirFleetStorage


def test_put_get_roundtrip(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    fs.put_bytes("org-graph/manifest.json", b'{"version":1}')
    assert fs.get_bytes("org-graph/manifest.json") == b'{"version":1}'


def test_get_missing_returns_none(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    assert fs.get_bytes("nope/missing.bin") is None


def test_list_paths_by_prefix_sorted(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    fs.put_bytes("contrib/b@x.org/2.jsonl", b"b")
    fs.put_bytes("contrib/a@x.org/1.jsonl", b"a")
    fs.put_bytes("org-graph/manifest.json", b"m")
    assert fs.list_paths("contrib/") == [
        "contrib/a@x.org/1.jsonl", "contrib/b@x.org/2.jsonl"]


def test_delete_is_idempotent(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    fs.put_bytes("x/y.bin", b"1")
    fs.delete("x/y.bin")
    fs.delete("x/y.bin")   # missing — must not raise
    assert fs.get_bytes("x/y.bin") is None


def test_satisfies_protocol(tmp_path):
    from mcpbrain.org_contracts import FleetStorage
    fs = LocalDirFleetStorage(tmp_path)
    assert isinstance(fs, FleetStorage)   # runtime_checkable structural check
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_fleet.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tests.helpers.org_fleet'`.

- [ ] **Step 3: Write minimal implementation**

First make the Protocol runtime-checkable so `test_satisfies_protocol` works. In `mcpbrain/org_contracts.py`, change the import and decorate the Protocol:

```python
from typing import Protocol, runtime_checkable
```
```python
@runtime_checkable
class FleetStorage(Protocol):
```

Then create `tests/helpers/org_fleet.py`:

```python
"""Test-only fleet substrate: a filesystem-backed FleetStorage plus multi-user
+ curator simulation builders. Shared by A/B/C and Phase D tests."""
from __future__ import annotations

from pathlib import Path


class LocalDirFleetStorage:
    """A FleetStorage backed by a local directory tree (see org_contracts)."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _abs(self, path: str) -> Path:
        return self.root / path

    def put_bytes(self, path: str, data: bytes) -> None:
        p = self._abs(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get_bytes(self, path: str) -> bytes | None:
        p = self._abs(path)
        return p.read_bytes() if p.is_file() else None

    def list_paths(self, prefix: str) -> list[str]:
        base = self.root
        out = []
        for p in base.rglob("*"):
            if p.is_file():
                rel = p.relative_to(base).as_posix()
                if rel.startswith(prefix):
                    out.append(rel)
        return sorted(out)

    def delete(self, path: str) -> None:
        p = self._abs(path)
        if p.is_file():
            p.unlink()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_fleet.py tests/test_org_contracts.py -v`
Expected: PASS (org_contracts still green after the `runtime_checkable` change).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_contracts.py tests/helpers/org_fleet.py tests/test_org_fleet.py
git commit -m "feat(test): LocalDirFleetStorage + runtime_checkable FleetStorage"
```

---

## Task 5: Config accessors — role, flags, `fleet_pin`

**Files:**
- Modify: `mcpbrain/config.py` (add accessors near the other flag accessors)
- Test: `tests/test_org_config_flags.py`

**Interfaces:**
- Consumes: `mcpbrain.org_contracts.FleetPin`, `DEFAULT_RELATION_ALLOWLIST`.
- Produces:
  - `install_role(home) -> str` → `read_config(home).get("role", "member")` (`"member"`|`"org_curator"`).
  - `is_org_curator(home) -> bool` → `install_role(home) == "org_curator"`.
  - `org_contrib_enabled(home) -> bool` (default True).
  - `org_import_enabled(home) -> bool` (default True).
  - `ingest_cache_enabled(home) -> bool` (default True; config key `"ingest_cache"`).
  - `fleet_pin(home) -> FleetPin` — reads `org_config.org_pin` (staged by the fleet overlay); returns a `FleetPin` with defaults for absent fields.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_config_flags.py`:

```python
from mcpbrain import config
from mcpbrain.org_contracts import FleetPin, DEFAULT_RELATION_ALLOWLIST


def test_role_defaults_to_member(tmp_path):
    assert config.install_role(str(tmp_path)) == "member"
    assert config.is_org_curator(str(tmp_path)) is False


def test_role_curator(tmp_path):
    config.write_config(str(tmp_path), {"role": "org_curator"})
    assert config.install_role(str(tmp_path)) == "org_curator"
    assert config.is_org_curator(str(tmp_path)) is True


def test_org_flags_default_true(tmp_path):
    h = str(tmp_path)
    assert config.org_contrib_enabled(h) is True
    assert config.org_import_enabled(h) is True
    assert config.ingest_cache_enabled(h) is True


def test_org_contrib_can_be_disabled(tmp_path):
    config.write_config(str(tmp_path), {"org_contrib_enabled": False})
    assert config.org_contrib_enabled(str(tmp_path)) is False


def test_fleet_pin_empty_when_no_org_config(tmp_path):
    pin = config.fleet_pin(str(tmp_path))
    assert isinstance(pin, FleetPin)
    assert pin.is_pinned is False
    assert pin.relation_allowlist == DEFAULT_RELATION_ALLOWLIST


def test_fleet_pin_reads_org_config_block(tmp_path):
    config.write_config(str(tmp_path), {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 384, "chunker_version": "v1",
        "enrich_logic_floor": 2, "fleet_secret": "s3cret",
        "relation_allowlist": ["works_at", "member_of"]}}})
    pin = config.fleet_pin(str(tmp_path))
    assert pin.embed_model == "bge-small" and pin.dim == 384
    assert pin.enrich_logic_floor == 2 and pin.is_pinned is True
    assert pin.relation_allowlist == ("works_at", "member_of")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_config_flags.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.config' has no attribute 'install_role'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/config.py`, add near the other flag accessors (e.g. after `tiered_memory_enabled`):

```python
def install_role(home) -> str:
    """This install's role: 'member' (default) or 'org_curator'. The curator
    runs the org-graph adjudication cadence; members contribute + consume."""
    return read_config(home).get("role", "member")


def is_org_curator(home) -> bool:
    """True when this install curates the org graph (config['role']=='org_curator')."""
    return install_role(home) == "org_curator"


def org_contrib_enabled(home) -> bool:
    """Contribute allowlisted/redacted claims to the org graph. Default True —
    safe because contribution additionally requires a fleet_secret (fleet_pin)."""
    return bool(read_config(home).get("org_contrib_enabled", True))


def org_import_enabled(home) -> bool:
    """Import the published org-graph snapshot. Default True — no-ops until a
    snapshot exists in the fleet folder."""
    return bool(read_config(home).get("org_import_enabled", True))


def ingest_cache_enabled(home) -> bool:
    """Use/publish the shared-drive ingest cache. Default True — no-ops until a
    fleet pin is present."""
    return bool(read_config(home).get("ingest_cache", True))


def fleet_pin(home):
    """Typed view of the fleet-wide pin staged under config['org_config']['org_pin']
    by fleet.merge_org_config. Absent fields fall back to FleetPin defaults."""
    from mcpbrain.org_contracts import FleetPin
    raw = (read_config(home).get("org_config") or {}).get("org_pin") or {}
    kwargs = dict(
        embed_model=raw.get("embed_model", ""),
        dim=int(raw.get("dim", 0) or 0),
        chunker_version=raw.get("chunker_version", ""),
        enrich_logic_floor=int(raw.get("enrich_logic_floor", 0) or 0),
        fleet_secret=raw.get("fleet_secret", ""),
    )
    allow = raw.get("relation_allowlist")
    if allow is not None:
        kwargs["relation_allowlist"] = tuple(allow)
    return FleetPin(**kwargs)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_config_flags.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py tests/test_org_config_flags.py
git commit -m "feat(config): role + org flags + fleet_pin accessor"
```

---

## Task 6: Fleet allowlist — permit `org_pin`

**Files:**
- Modify: `mcpbrain/fleet.py:225` (`_ALLOWLIST`)
- Test: `tests/test_fleet_pin.py`

**Interfaces:**
- Consumes: `fleet.merge_org_config` (existing), `config.fleet_pin` (Task 5).
- Produces: `_ALLOWLIST == frozenset({"cadences", "org_pin"})`, so an `org_pin` block in the fleet `org-config.json` is staged into local config (and therefore visible to `config.fleet_pin`); all other keys stay dropped.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fleet_pin.py`:

```python
from mcpbrain import fleet, config


def test_org_pin_is_allowlisted():
    assert "org_pin" in fleet._ALLOWLIST
    assert "cadences" in fleet._ALLOWLIST


def test_non_allowlisted_keys_still_dropped():
    # A fabricated key must NOT be allowed through the overlay.
    assert not fleet._is_allowed("secrets")
    assert not fleet._is_allowed("owner_email")


def test_merge_stages_org_pin_into_config(tmp_path, monkeypatch):
    home = str(tmp_path)
    config.write_config(home, {"fleet": {"folder_id": "FID"}})
    monkeypatch.setattr(fleet, "read_org_config", lambda folder_id, svc: {
        "org_pin": {"fleet_secret": "s3cret", "dim": 384},
        "cadences": {"review_interval_s": 3600},
        "evil_key": {"x": 1}})
    allowed = fleet.merge_org_config(home, drive_service=object())
    assert "org_pin" in allowed and "cadences" in allowed
    assert "evil_key" not in allowed
    assert config.fleet_pin(home).fleet_secret == "s3cret"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_fleet_pin.py -v`
Expected: FAIL — `test_org_pin_is_allowlisted` (org_pin not in the frozenset) and the merge test.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/fleet.py:225`, extend the allowlist:

```python
_ALLOWLIST = frozenset({"cadences", "org_pin"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_fleet_pin.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/fleet.py tests/test_fleet_pin.py
git commit -m "feat(fleet): allowlist org_pin so the fleet-wide pin reaches config"
```

---

## Task 7: No-op cadence slots (`org_contrib_upload`, `org_import`, `org_curate`)

**Files:**
- Modify: `mcpbrain/daemon.py` — `_CADENCE_PASSES` (~154), `__init__` placeholders (~548), `_CADENCE_DEFAULTS` (~2092), `_CADENCE_KEYS` (~2115), post-construct wiring (~2220), under-lock rewire (~901), and three new `_run_*` methods.
- Test: `tests/test_org_cadence_stubs.py`

**Interfaces:**
- Consumes: existing cadence machinery (`CadencePass`, `_is_due`, `_cadences_from_config`).
- Produces: three daemon cadences registered and interval-wired with **no-op bodies** that subsystem B fills in later. Each `_run_*` returns `None` today. Daily defaults (86400s). Subsystem B implements bodies only — it does not touch the shared registration blocks. `_run_org_curate` will gate on `is_org_curator` inside its own body (added by B).

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_cadence_stubs.py`:

```python
from mcpbrain import daemon as d


def test_org_cadence_passes_registered():
    names = {cp.name for cp in d._CADENCE_PASSES}
    assert {"org_contrib_upload", "org_import", "org_curate"} <= names


def test_org_cadence_defaults_and_keys_present():
    for key in ("org_contrib_upload_interval_s", "org_import_interval_s",
                "org_curate_interval_s"):
        assert key in d._CADENCE_DEFAULTS
        assert key in d._CADENCE_KEYS


def test_run_methods_exist_and_are_noops():
    # The Daemon class must define the three stub methods.
    for name in ("_run_org_contrib_upload", "_run_org_import", "_run_org_curate"):
        assert hasattr(d.Daemon, name)


def test_cadences_from_config_includes_org_keys(tmp_path):
    cad = d._cadences_from_config(str(tmp_path))
    assert cad["org_contrib_upload_interval_s"] == 86400.0
    assert cad["org_import_interval_s"] == 86400.0
    assert cad["org_curate_interval_s"] == 86400.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_cadence_stubs.py -v`
Expected: FAIL — passes not registered / keys absent.

- [ ] **Step 3: Write minimal implementation**

**(3a)** In `_CADENCE_PASSES` (daemon.py:154, before the closing `)`), add three entries:

```python
    CadencePass("org_contrib_upload", "_org_contrib_upload_interval_s",
                "_last_org_contrib_upload", "_run_org_contrib_upload"),
    CadencePass("org_import", "_org_import_interval_s",
                "_last_org_import", "_run_org_import"),
    CadencePass("org_curate", "_org_curate_interval_s",
                "_last_org_curate", "_run_org_curate"),
```

**(3b)** In `Daemon.__init__` (after the `_auto_enable` placeholder, ~daemon.py:551), add placeholder attributes:

```python
        # Org-baseline (Phase 0) cadences: registered as no-op stubs; subsystem B
        # fills the _run_* bodies. Intervals set from cadences config on start.
        self._org_contrib_upload_interval_s: float | None = None
        self._last_org_contrib_upload = None
        self._org_import_interval_s: float | None = None
        self._last_org_import = None
        self._org_curate_interval_s: float | None = None
        self._last_org_curate = None
```

**(3c)** In `_CADENCE_DEFAULTS` (daemon.py:2092), add:

```python
    "org_contrib_upload_interval_s":  86400.0,   # Phase 0 stub: daily contribution upload
    "org_import_interval_s":          86400.0,   # Phase 0 stub: daily snapshot import
    "org_curate_interval_s":          86400.0,   # Phase 0 stub: daily curator adjudication
```

**(3d)** In `_CADENCE_KEYS` (daemon.py:2115), add:

```python
    "org_contrib_upload_interval_s",
    "org_import_interval_s",
    "org_curate_interval_s",
```

**(3e)** In `main()` post-construct wiring (after daemon.py:2220), add:

```python
    daemon._org_contrib_upload_interval_s = cadences["org_contrib_upload_interval_s"]
    daemon._org_import_interval_s = cadences["org_import_interval_s"]
    daemon._org_curate_interval_s = cadences["org_curate_interval_s"]
```

**(3f)** In the under-lock rewire block (after daemon.py:901, still inside `with self._config_lock:`), add:

```python
            self._org_contrib_upload_interval_s = cadences["org_contrib_upload_interval_s"]
            self._org_import_interval_s = cadences["org_import_interval_s"]
            self._org_curate_interval_s = cadences["org_curate_interval_s"]
```

**(3g)** Add the three stub methods to the `Daemon` class (next to `_run_review`, ~daemon.py:1595):

```python
    # -- Org-baseline cadences (Phase 0 stubs; bodies land in subsystem B) ----

    def _run_org_contrib_upload(self) -> dict | None:
        """Upload pending org_contrib_outbox rows to the fleet folder.
        Phase 0: no-op stub — subsystem B implements the body."""
        if not self._is_due("_org_contrib_upload_interval_s", "_last_org_contrib_upload"):
            return None
        return None

    def _run_org_import(self) -> dict | None:
        """Import a newer org-graph snapshot into origin='org' rows.
        Phase 0: no-op stub — subsystem B implements the body."""
        if not self._is_due("_org_import_interval_s", "_last_org_import"):
            return None
        return None

    def _run_org_curate(self) -> dict | None:
        """Curator-only: ingest contributions, adjudicate, publish a snapshot.
        Phase 0: no-op stub — subsystem B implements the body (and gates on
        config.is_org_curator inside it)."""
        if not self._is_due("_org_curate_interval_s", "_last_org_curate"):
            return None
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_cadence_stubs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/daemon.py tests/test_org_cadence_stubs.py
git commit -m "feat(daemon): register no-op org cadence slots (contrib_upload/import/curate)"
```

---

## Task 8: Multi-user + curator test harness

**Files:**
- Modify: `tests/helpers/org_fleet.py` (add `FakeInstall`, `make_install`, `make_fleet`)
- Test: `tests/test_org_fleet.py` (extend)

**Interfaces:**
- Consumes: `mcpbrain.store.Store`, `mcpbrain.config`, `LocalDirFleetStorage` (Task 4).
- Produces:
  - `FakeInstall` dataclass: `name:str`, `home:Path`, `store:Store`, `role:str`.
  - `make_install(root, name, *, dim=4, role="member") -> FakeInstall` — creates an isolated `MCPBRAIN_HOME` dir (`root/name`), writes `config.json` with `role` + `owner_email=f"{name}@x.org"`, opens+inits a `Store`.
  - `make_fleet(root, n_members, *, dim=4) -> tuple[list[FakeInstall], FakeInstall, LocalDirFleetStorage]` — `n_members` member installs + one `org_curator` install + a shared `LocalDirFleetStorage` rooted at `root/"fleet"`.

- [ ] **Step 1: Write the failing test**

Extend `tests/test_org_fleet.py`:

```python
def test_make_install_isolated_and_configured(tmp_path):
    from tests.helpers.org_fleet import make_install
    from mcpbrain import config
    inst = make_install(tmp_path, "alice", role="member")
    assert inst.home.is_dir()
    assert config.install_role(str(inst.home)) == "member"
    assert config.owner_email(str(inst.home)) == "alice@x.org"
    # store is initialised: entities table + origin column exist
    with inst.store._connect() as db:
        cols = {r["name"] for r in
                db.execute("PRAGMA table_info(entities)").fetchall()}
    assert "origin" in cols


def test_make_fleet_shape(tmp_path):
    from tests.helpers.org_fleet import make_fleet
    from mcpbrain import config
    members, curator, fs = make_fleet(tmp_path, n_members=3)
    assert len(members) == 3
    assert config.is_org_curator(str(curator.home)) is True
    assert all(config.install_role(str(m.home)) == "member" for m in members)
    # the shared fleet storage round-trips for every install
    fs.put_bytes("org-graph/manifest.json", b"{}")
    assert fs.get_bytes("org-graph/manifest.json") == b"{}"
    # installs are isolated: distinct home dirs
    homes = {str(m.home) for m in members} | {str(curator.home)}
    assert len(homes) == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_fleet.py -k "make_" -v`
Expected: FAIL — `ImportError: cannot import name 'make_install'`.

- [ ] **Step 3: Write minimal implementation**

Append to `tests/helpers/org_fleet.py`:

```python
from dataclasses import dataclass

from mcpbrain import config
from mcpbrain.store import Store


@dataclass
class FakeInstall:
    name: str
    home: Path
    store: Store
    role: str = "member"


def make_install(root: Path, name: str, *, dim: int = 4,
                 role: str = "member") -> FakeInstall:
    home = Path(root) / name
    home.mkdir(parents=True, exist_ok=True)
    config.write_config(str(home), {"role": role,
                                    "owner_email": f"{name}@x.org"})
    store = Store(home / "brain.sqlite3", dim=dim)
    store.init()
    return FakeInstall(name=name, home=home, store=store, role=role)


def make_fleet(root: Path, n_members: int, *, dim: int = 4
               ) -> tuple[list[FakeInstall], FakeInstall, LocalDirFleetStorage]:
    members = [make_install(root, f"member{i}", dim=dim)
               for i in range(n_members)]
    curator = make_install(root, "curator", dim=dim, role="org_curator")
    fs = LocalDirFleetStorage(Path(root) / "fleet")
    return members, curator, fs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_fleet.py -v`
Expected: PASS (all tests in the module).

- [ ] **Step 5: Commit**

```bash
git add tests/helpers/org_fleet.py tests/test_org_fleet.py
git commit -m "feat(test): multi-user + curator fleet simulation harness"
```

---

## Task 9: Phase 0 exit-gate verification

**Files:**
- Test: `tests/test_org_phase0_gate.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a single migration-idempotency guard proving `init()` is safe to run twice on a populated store (the "migration runs clean on a real-corpus copy" gate, expressed as a deterministic test), plus the full-suite green check.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_phase0_gate.py`:

```python
from mcpbrain.store import Store
from mcpbrain import org_contracts  # contracts import cleanly


def test_init_is_idempotent_on_populated_store(tmp_path):
    path = tmp_path / "brain.sqlite3"
    s = Store(path, dim=4); s.init()
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) "
                   "VALUES('joel','Joel','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) "
                   "VALUES('ceo','CEO','person','org')")
    # Re-init (simulates a daemon restart / re-open): must not error, must not
    # drop or mutate existing rows or their origin tags.
    s2 = Store(path, dim=4); s2.init()
    with s2._connect() as db:
        rows = dict(db.execute("SELECT id, origin FROM entities").fetchall())
    assert rows == {"joel": "local", "ceo": "org"}


def test_contracts_module_surface():
    # The frozen surface A/B/C import must all be present.
    for name in ("CacheArtifact", "ContributionRecord", "SnapshotManifest",
                 "Tombstone", "FleetPin", "FleetStorage",
                 "pipeline_fingerprint", "source_ref", "artifact_filename",
                 "DRIVE_ID_META_KEY", "DEFAULT_RELATION_ALLOWLIST"):
        assert hasattr(org_contracts, name), name
```

- [ ] **Step 2: Run test to verify it fails (then passes — no new code needed)**

Run: `python -m pytest tests/test_org_phase0_gate.py -v`
Expected: PASS immediately (this task asserts the accumulated surface; if either test fails, an earlier task regressed — fix there, don't patch here).

- [ ] **Step 3: Run the full suite as the exit gate**

Run: `python -m pytest tests/ -q`
Expected: the whole suite passes (no regressions from the schema/config/daemon edits). If red, fix the offending task before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_org_phase0_gate.py
git commit -m "test(org): Phase 0 exit gate — init idempotency + contract surface"
```

- [ ] **Step 5: Phase 0 complete — hand off to parallel tracks**

Phase 0 foundations are merged on `main`. A, B, and C may now fork into separate worktrees (see spec §"Phases A ∥ B ∥ C"). No push/release — that remains a separate explicit instruction.

---

## Self-Review

**Spec coverage** (Phase 0 items from the spec's Implementation phasing → Phase 0):
- Full schema migration — `origin` on entities+relations (Task 1), three org tables (Task 2), `drive_id` convention as `DRIVE_ID_META_KEY` constant (Task 3). ✓
- All config flags + accessors + fleet pinning block — role/flags/`fleet_pin` (Task 5), `org_pin` allowlist (Task 6), `FleetPin` (Task 3). ✓
- Frozen interface contracts as code — `org_contracts.py` dataclasses + helpers + `FleetStorage` (Tasks 3–4). ✓
- `role=org_curator` plumbing — `install_role`/`is_org_curator` (Task 5), curator install in harness (Task 8), `_run_org_curate` gate note (Task 7). ✓
- Multi-user + curator test harness — `make_install`/`make_fleet` + `LocalDirFleetStorage` (Tasks 4, 8). ✓
- Cadence hook-points (no-op slots A/B fill later) — three cadences (Task 7). ✓
- Exit gate (migration clean on populated store; contracts compile; harness spins up a fleet) — Task 9 + Task 8. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; no "add error handling" hand-waves.

**Type consistency:** `FleetPin` fields/defaults identical across Task 3 (definition), Task 5 (`fleet_pin` constructor kwargs), Task 6 (test). `FleetStorage` method set identical in Task 3 (Protocol) and Task 4 (impl). Cadence attribute names (`_org_*_interval_s`, `_last_org_*`) consistent across `__init__`, `_CADENCE_PASSES`, wiring, and `_run_*` in Task 7. `origin` default `'local'` consistent in Task 1 and Task 9. Table/column names match between Task 2 CREATE and Task 2/9 tests.
