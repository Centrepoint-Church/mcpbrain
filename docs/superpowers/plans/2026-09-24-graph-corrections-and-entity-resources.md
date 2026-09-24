# Graph Corrections and Entity Resources Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the model correct the knowledge graph from conversation (sticky against re-enrichment, fully undoable, with a real confirmation gate for inferred corrections), and expose people/orgs/projects as @-mentionable MCP resources.

**Architecture:** Part A adds one daemon-executed tool, `brain_graph_correct`, backed by a new `mcpbrain/graph_corrections.py` that performs each correction plus its ledger row in ONE write transaction, and by stickiness hooks in the existing writers (`graph_write.upsert_relation`, the entity field setters, and every merge path). The MCP server intercepts inferred corrections to run an elicitation (Claude Code) and passes the user's answer to the daemon out-of-band, never through model-controlled arguments; clients without elicitation (Desktop) get a pending row approved on the dashboard. Part B adds `mcpbrain/entity_resource.py` (daemon side: top-N list, markdown render) behind three control-API GET routes, and teaches the MCP server a second resource scheme `mcpbrain://entity/<id>`, a resource template, and completion.

**Tech Stack:** Python 3.12, SQLite (STRICT tables, WAL, single-writer daemon), `mcp>=2.0,<3` low-level `Server` (constructor-kwarg handlers), `jsonschema`, pytest (no pytest-asyncio; tests drive coroutines with `asyncio.run`).

**Spec:** `docs/superpowers/specs/2026-09-24-graph-corrections-and-entity-resources-design.md`

## Global Constraints

- Australian English in code comments, docs and user-facing strings.
- **No real people's names in tests or docs.** Use the neutral fictional cast: Dana Okafor, Marcus Reyes, Priya Anand, Rina T; orgs Northgate Trust, Southbank Community Trust, The Lantern Co. `tests/test_no_tenant_literals.py` scans `tests/`.
- **Scoped test runs only**: run the files a task touches plus directly impacted ones. Josh runs the full suite himself.
- `mcpbrain/tools.py` and `mcpbrain/graph_corrections.py` and `mcpbrain/entity_resource.py` must NOT import `mcp`, `Store`, or anything native at module scope (`tests/test_tool_registry.py` AST-guards `tools.py`; keep the new modules to the same rule so the MCP server can import `graph_corrections.describe` cheaply).
- Every tool failure returns a result (`isError` or a `status: "refused"|"error"` dict). Never let an exception escape a handler.
- Every schema change is an additive, idempotent migration inside `Store.init()` (PRAGMA-checked `ALTER TABLE ... ADD COLUMN`, `CREATE TABLE IF NOT EXISTS ...{_S}`).
- Deletes in store code carry the `# admin-delete-ok` trailing comment (an existing lint convention).
- `mcp` pin stays `>=2.0,<3`. The local venv is on 2.0.0 while the fleet resolves 2.2.0: for Tasks 7 and 11 also run the task's tests under `uv run --with "mcp==2.2.0" pytest <files>`.
- Commit on `main` after each task (Josh works on main). **Do not push, bump versions or release.**
- Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_016mbe1v4oWKmAhVLeRA9ppz
  ```

## File Structure

| File | Responsibility |
|---|---|
| `mcpbrain/store.py` (modify) | DDL for the ledger/pairs/locks + `user_verdict`; read helpers; field-lock guards in setters; merge refactored into `_merge_entities_tx` (returns an undo snapshot) + `_unmerge_tx` |
| `mcpbrain/graph_write.py` (modify) | `upsert_relation` split into `upsert_relation_in(conn, ...)`; revive branch honours `user_verdict='rejected'` |
| `mcpbrain/resolve.py`, `mcpbrain/review_apply.py`, `mcpbrain/graph_view.py`, `mcpbrain/org_backfill.py` (modify) | honour distinct pairs / locks |
| `mcpbrain/graph_corrections.py` (create) | the correction engine: payload building, dedup, apply-per-op, undo-per-op, pending/approve/decline, `describe()` |
| `mcpbrain/tools.py` (modify) | `brain_graph_correct` declaration + factory |
| `mcpbrain/daemon.py`, `mcpbrain/control_api.py`, `mcpbrain/control_client.py` (modify) | routed handler; out-of-band `confirmation`; corrections routes; entity-resource routes |
| `mcpbrain/mcp_server.py` (modify) | dispatch + elicitation gate; `mcpbrain://` resources, template, completion |
| `mcpbrain/entity_resource.py` (create) | top-N (daily cache), id resolution through the merge log, markdown render |
| `mcpbrain/wizard/dashboard.html` (modify) | "Pending corrections" card |
| `mcpbrain/config.py`, `mcpbrain/session_hooks.py` (modify) | instructions mention the new tool |

---

## Part A — Graph corrections

### Task 1: Schema and read helpers

**Files:**
- Modify: `mcpbrain/store.py` (inside `Store.init()`, beside the `entity_suppressions` DDL ~line 1097; the `entity_relations` column loop ~line 693; new methods near `suppress_entity` ~line 4542)
- Test: `tests/test_graph_corrections_store.py` (create)

**Interfaces:**
- Produces:
  - tables `graph_corrections`, `entity_distinct_pairs(a, b)`, `entity_field_locks(entity_id, field)`; column `entity_relations.user_verdict TEXT`
  - `Store.get_correction(correction_id: int) -> dict | None` (payload/snapshot decoded to dicts)
  - `Store.pending_corrections(limit: int = 50) -> list[dict]`
  - `Store.distinct_pair_set() -> set[tuple[str, str]]` (each tuple sorted)
  - `Store.is_distinct_pair(a: str, b: str) -> bool`
  - `Store.locked_fields(entity_id: str) -> set[str]`

- [ ] **Step 1: Write the failing test**

```python
"""Schema + read helpers for graph corrections (Task 1)."""
import json

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    return s


def _cols(s, table):
    with s._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}


def test_init_creates_correction_tables_and_verdict_column(tmp_path):
    s = _store(tmp_path)
    assert {"id", "op", "basis", "status", "payload", "snapshot", "reason",
            "confirmed_via", "dedup_key", "error", "created_at", "applied_at",
            "reverted_at", "change_log_id"} <= _cols(s, "graph_corrections")
    assert {"a", "b"} <= _cols(s, "entity_distinct_pairs")
    assert {"entity_id", "field"} <= _cols(s, "entity_field_locks")
    assert "user_verdict" in _cols(s, "entity_relations")


def test_init_is_idempotent(tmp_path):
    s = _store(tmp_path)
    s.init()  # second run must not raise
    assert "user_verdict" in _cols(s, "entity_relations")


def _seed(s):
    with s._connect(write=True) as db:
        for eid, name in (("dana-okafor", "Dana Okafor"), ("marcus-reyes", "Marcus Reyes")):
            db.execute("INSERT INTO entities(id,name,type) VALUES(?,?,'person')", (eid, name))


def test_get_and_pending_corrections_decode_json(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute(
            "INSERT INTO graph_corrections(op,basis,status,payload,snapshot,dedup_key) "
            "VALUES('hide','inferred','pending',?,'{}','hide:[\"x\"]')",
            (json.dumps({"entity_id": "x"}),))
    rows = s.pending_corrections()
    assert len(rows) == 1 and rows[0]["payload"] == {"entity_id": "x"}
    assert s.get_correction(rows[0]["id"])["snapshot"] == {}
    assert s.get_correction(999) is None


def test_distinct_pairs_and_locks(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES('dana-okafor','marcus-reyes')")
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES('dana-okafor','org')")
    assert s.is_distinct_pair("marcus-reyes", "dana-okafor")  # order-insensitive
    assert s.distinct_pair_set() == {("dana-okafor", "marcus-reyes")}
    assert s.locked_fields("dana-okafor") == {"org"}
    assert s.locked_fields("marcus-reyes") == set()


def test_pairs_and_locks_cascade_with_entity(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES('dana-okafor','marcus-reyes')")
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES('dana-okafor','org')")
        db.execute("DELETE FROM entities WHERE id='dana-okafor'")  # admin-delete-ok
    assert s.distinct_pair_set() == set()
    assert s.locked_fields("dana-okafor") == set()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_corrections_store.py -v`
Expected: FAIL (`graph_corrections` table missing / `AttributeError: pending_corrections`).

- [ ] **Step 3: Implement the DDL**

In `Store.init()`, add `("user_verdict", "TEXT")` to the `entity_relations` bitemporal column loop (the `for col_name, col_def in (...)` tuple that already carries `valid_from`, `valid_to`, `invalidated_at`, `superseded_reason`, `confidence`), so it is ALTERed on existing stores exactly like its siblings.

Directly after the `entity_suppressions` `CREATE TABLE`, add:

```python
            # --- Graph corrections (2026-09-24 spec) ---------------------------
            # The ledger for brain_graph_correct: every correction the model makes
            # (applied, pending approval, declined, failed, reverted), with the
            # snapshot undo needs. Pending rows are the approval queue for
            # inferred corrections on clients without elicitation.
            db.execute(f"""CREATE TABLE IF NOT EXISTS graph_corrections(
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                op            TEXT NOT NULL,
                basis         TEXT NOT NULL,
                status        TEXT NOT NULL,
                payload       TEXT NOT NULL DEFAULT '{{}}',
                snapshot      TEXT NOT NULL DEFAULT '{{}}',
                reason        TEXT DEFAULT '',
                confirmed_via TEXT DEFAULT '',
                dedup_key     TEXT DEFAULT '',
                error         TEXT DEFAULT '',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                applied_at    TEXT DEFAULT '',
                reverted_at   TEXT DEFAULT '',
                change_log_id INTEGER){_S}""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_gc_status ON graph_corrections(status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_gc_dedup ON graph_corrections(dedup_key)")
            # "These two are different entities": consulted by every merge path.
            # a < b always (the writer sorts), so one row per unordered pair.
            db.execute(f"""CREATE TABLE IF NOT EXISTS entity_distinct_pairs(
                a TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                b TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                PRIMARY KEY(a, b)){_S}""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_edp_b ON entity_distinct_pairs(b)")
            # A field the user corrected: automated writers must not overwrite it.
            db.execute(f"""CREATE TABLE IF NOT EXISTS entity_field_locks(
                entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                field     TEXT NOT NULL,
                PRIMARY KEY(entity_id, field)){_S}""")
```

(The spec listed `correction_id` columns on the pairs/locks tables; they are dropped here because the id is not known until the ledger row is inserted in the same transaction, and the ledger payload already records which pair or lock each correction created.)

- [ ] **Step 4: Implement the read helpers** (beside `suppress_entity`)

```python
    # --- Graph corrections (read side) ----------------------------------------

    @staticmethod
    def _decode_correction(row) -> dict:
        d = dict(row)
        d["payload"] = json.loads(d.get("payload") or "{}")
        d["snapshot"] = json.loads(d.get("snapshot") or "{}")
        return d

    def get_correction(self, correction_id: int) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM graph_corrections WHERE id=?",
                             (int(correction_id),)).fetchone()
        return self._decode_correction(row) if row else None

    def pending_corrections(self, limit: int = 50) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM graph_corrections WHERE status='pending' "
                "ORDER BY id LIMIT ?", (int(limit),)).fetchall()
        return [self._decode_correction(r) for r in rows]

    def distinct_pair_set(self) -> set[tuple[str, str]]:
        with self._connect() as db:
            return {(r["a"], r["b"]) for r in db.execute(
                "SELECT a, b FROM entity_distinct_pairs")}

    def is_distinct_pair(self, a: str, b: str) -> bool:
        lo, hi = sorted((a, b))
        with self._connect() as db:
            return db.execute("SELECT 1 FROM entity_distinct_pairs WHERE a=? AND b=?",
                              (lo, hi)).fetchone() is not None

    def locked_fields(self, entity_id: str) -> set[str]:
        with self._connect() as db:
            return {r["field"] for r in db.execute(
                "SELECT field FROM entity_field_locks WHERE entity_id=?", (entity_id,))}
```

(`json` is already imported at the top of `store.py`; confirm with `grep -n '^import json' mcpbrain/store.py`.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_graph_corrections_store.py tests/test_store.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/store.py tests/test_graph_corrections_store.py
git commit -m "feat(store): graph-corrections ledger, distinct pairs, field locks, user_verdict"
```

---

### Task 2: Stickiness in the existing writers

**Files:**
- Modify: `mcpbrain/graph_write.py:530-620` (`upsert_relation`)
- Modify: `mcpbrain/store.py:1582-1650` (`update_entity_org`, `rename_entity`, `set_entity_email`, `rewrite_org_field`, `update_entity_org_if_empty`)
- Modify: `mcpbrain/graph_view.py:352-371` (`update_entity`), `mcpbrain/org_backfill.py:61`
- Test: `tests/test_graph_corrections_sticky.py` (create)

**Interfaces:**
- Consumes: Task 1 tables.
- Produces:
  - `graph_write.upsert_relation_in(conn, entity_a, relation, entity_b, *, valid_from, evidence="", confidence=1.0, strength=1, source_doc_id=None) -> int` (the body of `upsert_relation`, on a caller-held write connection; `upsert_relation` becomes a wrapper)
  - Setters gain `user: bool = False`: `update_entity_org(entity_id, org, org_valid_from="", *, user=False)`, `rename_entity(entity_id, new_name, *, user=False)`, `set_entity_email(entity_id, email_addr, *, user=False)`; each returns `False` without writing when the field is locked and `user` is False.
  - `store._field_is_locked(db, entity_id, field) -> bool` (module-level helper in store.py)

- [ ] **Step 1: Write the failing tests**

```python
"""Corrections must survive the automated writers (Task 2)."""
from mcpbrain import graph_write as gw
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr) VALUES"
                   "('dana-okafor','Dana Okafor','person','Northgate Trust','dana@northgate.example')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('northgate-trust','Northgate Trust','org')")
    return s


def _reject(s, a, rel, b):
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_relations SET invalidated_at='2026-09-24T00:00:00Z', "
                   "superseded_reason='user_rejected', user_verdict='rejected' "
                   "WHERE entity_a=? AND relation=? AND entity_b=?", (a, rel, b))


def test_rejected_relation_is_not_revived_by_reextraction(tmp_path):
    s = _store(tmp_path)
    rid = gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust",
                             valid_from="2026-01-01")
    _reject(s, "dana-okafor", "works_at", "northgate-trust")
    again = gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust",
                               valid_from="2026-09-01")
    assert again == rid
    with s._connect() as db:
        row = db.execute("SELECT invalidated_at, user_verdict FROM entity_relations "
                         "WHERE id=?", (rid,)).fetchone()
    assert row["invalidated_at"] is not None and row["user_verdict"] == "rejected"


def test_unrejected_invalidated_relation_still_revives(tmp_path):
    """The existing revive behaviour is unchanged for non-user invalidations."""
    s = _store(tmp_path)
    rid = gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust",
                             valid_from="2026-01-01")
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_relations SET invalidated_at='x' WHERE id=?", (rid,))
    gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust", valid_from="2026-09-01")
    with s._connect() as db:
        assert db.execute("SELECT invalidated_at FROM entity_relations WHERE id=?",
                          (rid,)).fetchone()[0] is None


def _lock(s, field):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES('dana-okafor',?)", (field,))


def test_locked_org_refuses_automated_write_but_accepts_user(tmp_path):
    s = _store(tmp_path)
    _lock(s, "org")
    assert s.update_entity_org("dana-okafor", "Southbank Community Trust") is False
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"
    assert s.update_entity_org("dana-okafor", "Southbank Community Trust", user=True) is True
    assert s.get_entity("dana-okafor")["org"] == "Southbank Community Trust"


def test_locked_org_survives_bulk_rewrite_and_if_empty(tmp_path):
    s = _store(tmp_path)
    _lock(s, "org")
    assert s.rewrite_org_field("Northgate Trust", "The Lantern Co") == 0
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET org='' WHERE id='dana-okafor'")
    assert s.update_entity_org_if_empty("dana-okafor", "The Lantern Co") is False


def test_locked_name_and_email(tmp_path):
    s = _store(tmp_path)
    _lock(s, "name")
    _lock(s, "email")
    assert s.rename_entity("dana-okafor", "D. Okafor") is False
    assert s.set_entity_email("dana-okafor", "other@x.example") is False
    assert s.rename_entity("dana-okafor", "Dana A. Okafor", user=True) is True


def test_org_backfill_counts_only_real_updates(tmp_path, monkeypatch):
    from mcpbrain import orgs as _orgs
    from mcpbrain.org_backfill import run_backfill
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET org='' WHERE id='dana-okafor'")
    _lock(s, "org")  # the user deliberately cleared it
    monkeypatch.setattr(_orgs, "taxonomy_from_config", lambda: _orgs.OrgTaxonomy(
        names=("Northgate Trust",), domain_map={"northgate.example": "Northgate Trust"}))
    assert run_backfill(s)["updated"] == 0
    assert s.get_entity("dana-okafor")["org"] == ""


def test_graph_ui_edit_is_a_user_write(tmp_path):
    from mcpbrain import graph_view
    s = _store(tmp_path)
    _lock(s, "org")
    graph_view.update_entity(s, "dana-okafor", org="The Lantern Co")
    assert s.get_entity("dana-okafor")["org"] == "The Lantern Co"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_graph_corrections_sticky.py -v`
Expected: FAIL (rejected relation revived; `update_entity_org() got an unexpected keyword argument 'user'`).

- [ ] **Step 3: Split `upsert_relation` and honour rejections**

In `graph_write.py`, rename the body of `upsert_relation` into `upsert_relation_in(conn, ...)` (same parameters minus `store`, operating on the passed `conn` instead of opening `with store._connect(write=True) as conn:`), and make `upsert_relation` a wrapper:

```python
def upsert_relation(store, entity_a, relation, entity_b, *, valid_from,
                    evidence="", confidence=1.0, strength=1,
                    source_doc_id: str | None = None) -> int:
    """(docstring unchanged)"""
    with store._connect(write=True) as conn:
        return upsert_relation_in(conn, entity_a, relation, entity_b,
                                  valid_from=valid_from, evidence=evidence,
                                  confidence=confidence, strength=strength,
                                  source_doc_id=source_doc_id)
```

In `upsert_relation_in`, change the revive lookup to also select `user_verdict` and short-circuit:

```python
        invalidated = conn.execute(
            "SELECT id, COALESCE(user_verdict, '') AS user_verdict FROM entity_relations "
            "WHERE entity_a = ? AND relation = ? AND entity_b = ? "
            "AND invalidated_at IS NOT NULL",
            (entity_a, relation, entity_b),
        ).fetchone()
        if invalidated is not None and invalidated["user_verdict"] == "rejected":
            # The user said this fact is wrong (brain_graph_correct). Re-observing
            # it in a source must not undo that: no revive, no bump, no
            # supersession of rivals.
            return invalidated["id"]
```

Keep the rest of the function as it is. Any code between the old `with` and the end that used `store` must be switched to `conn` (read the whole body before editing).

- [ ] **Step 4: Field-lock guards in the store setters**

Add near the other module-level helpers in `store.py`:

```python
def _field_is_locked(db, entity_id: str, field: str) -> bool:
    """True when the user has corrected `field` on `entity_id` (brain_graph_correct).
    Guarded on the table existing so stores/tests predating it keep working."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='entity_field_locks'").fetchone():
        return False
    return db.execute("SELECT 1 FROM entity_field_locks WHERE entity_id=? AND field=?",
                      (entity_id, field)).fetchone() is not None
```

Then change the setters (keep their existing bodies; the lock check is the first statement inside the write connection):

```python
    def update_entity_org(self, entity_id: str, org: str, org_valid_from: str = "",
                          *, user: bool = False) -> bool:
        """Set the org (and optionally org_valid_from) on one entity. Returns True if a
        row was actually updated. A user-locked org is left alone unless user=True:
        that single check covers org_backfill and both review appliers."""
        with self._connect(write=True) as db:
            if not user and _field_is_locked(db, entity_id, "org"):
                return False
            cur = db.execute(
                "UPDATE entities SET org=?, org_valid_from=? WHERE id=?",
                (org, org_valid_from, entity_id))
            return cur.rowcount > 0
```

`rename_entity(..., *, user=False)`: after opening the connection, `if not user and _field_is_locked(db, entity_id, "name"): return False`.
`set_entity_email(..., *, user=False)`: same with `"email"`.
`update_entity_org_if_empty`: add the `"org"` lock check (no `user` parameter; it is never a user write).
`rewrite_org_field`: exclude locked rows:

```python
            cur = db.execute(
                "UPDATE entities SET org=? WHERE org=? AND id NOT IN "
                "(SELECT entity_id FROM entity_field_locks WHERE field='org')",
                (canonical_org, variant_org))
```

- [ ] **Step 5: Callers**

`graph_view.update_entity`: pass `user=True` to `rename_entity`, `update_entity_org`, `set_entity_email` (a person editing in the graph UI is a user write).
`org_backfill.run_backfill`: `if store.update_entity_org(ent["id"], org): updated += 1` (so a refused write is not counted).

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/test_graph_corrections_sticky.py tests/test_graph_write.py tests/test_graph_write_provenance.py tests/test_org_backfill.py tests/test_graph_view.py tests/test_review_apply.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/graph_write.py mcpbrain/store.py mcpbrain/graph_view.py mcpbrain/org_backfill.py tests/test_graph_corrections_sticky.py
git commit -m "feat(graph): user rejections and field locks survive re-enrichment"
```

---

### Task 3: Distinct pairs in every merge path, and an undoable merge primitive

**Files:**
- Modify: `mcpbrain/store.py:3217-3292` (`merge_entities` → `_merge_entities_tx` + wrapper; new `_unmerge_tx`)
- Modify: `mcpbrain/resolve.py:147-312`, `mcpbrain/review_apply.py:317-420`, `mcpbrain/graph_view.py:436-454`
- Test: `tests/test_graph_corrections_merge.py` (create)

**Interfaces:**
- Consumes: Task 1 tables.
- Produces:
  - `store._merge_entities_tx(db, loser_id, winner_id, *, canonical_name=None, method="deterministic") -> dict | None` (module-level; returns the undo snapshot, `None` for a no-op)
  - `store._unmerge_tx(db, snapshot: dict) -> None` (raises `ValueError` naming the conflict)
  - `Store.merge_entities(...)` unchanged signature, now returns the snapshot (callers ignore it)
  - `graph_view._orient` returns `{"ok": False, "error": "marked_distinct", "message": ...}` for a distinct pair

- [ ] **Step 1: Write the failing tests**

```python
"""Merge honours distinct pairs; merge -> unmerge round-trips (Task 3)."""
import pytest

from mcpbrain import graph_view, resolve
from mcpbrain.store import Store, _merge_entities_tx, _unmerge_tx

L, W, C = "dana-okafor", "dana-okafor-2", "priya-anand"


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org,mentions,email_addr,notes) VALUES"
                   "(?, 'Dana Okafor', 'person', 'Northgate Trust', 3, 'dana@northgate.example', 'n1')", (L,))
        db.execute("INSERT INTO entities(id,name,type,org,mentions,degree) VALUES"
                   "(?, 'Dana Okafor', 'person', '', 9, 5)", (W,))
        db.execute("INSERT INTO entities(id,name,type) VALUES(?, 'Priya Anand', 'person')", (C,))
        db.execute("INSERT INTO entities(id,name,type) VALUES('northgate-trust','Northgate Trust','org')")
        # loser-only relation, a collision with a winner triple, and a self-loop-to-be
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) VALUES(?, 'knows', ?)", (L, C))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,user_verdict,invalidated_at) "
                   "VALUES(?, 'works_at', 'northgate-trust', 'rejected', 'x')", (L,))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) VALUES(?, 'works_at', 'northgate-trust')", (W,))
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b) VALUES(?, 'mentioned_with', ?)", (L, W))
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES(?, 'role', 'Pastor', 'manual', '2026-01-01')", (L,))
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m1', ?, 'from')", (L,))
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m2', ?, 'to')", (L,))
        db.execute("INSERT INTO email_entities(message_id,entity_id,role) VALUES('m2', ?, 'to')", (W,))
        db.execute("INSERT INTO entity_suppressions(entity_id,reason) VALUES(?, 'junk')", (L,))
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES(?, 'org')", (L,))
    return s


def _state(s):
    with s._connect() as db:
        q = lambda sql: sorted(tuple(r) for r in db.execute(sql))
        return {
            "entities": q("SELECT * FROM entities ORDER BY id"),
            "relations": q("SELECT * FROM entity_relations"),
            "observations": q("SELECT * FROM entity_observations"),
            "emails": q("SELECT * FROM email_entities"),
            "suppressions": q("SELECT * FROM entity_suppressions"),
            "pairs": q("SELECT * FROM entity_distinct_pairs"),
            "locks": q("SELECT * FROM entity_field_locks"),
        }


def test_merge_then_unmerge_round_trips_exactly(tmp_path):
    s = _store(tmp_path)
    before = _state(s)
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, L, W, method="user")
    assert s.get_entity(L) is None
    with s._connect(write=True) as db:
        _unmerge_tx(db, snap)
    assert _state(s) == before
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entity_merge_log").fetchone()[0] == 0


def test_merge_carries_rejection_onto_the_surviving_triple(tmp_path):
    s = _store(tmp_path)
    s.merge_entities(L, W)
    with s._connect() as db:
        row = db.execute("SELECT user_verdict, invalidated_at FROM entity_relations "
                         "WHERE entity_a=? AND relation='works_at'", (W,)).fetchone()
    assert row["user_verdict"] == "rejected" and row["invalidated_at"] is not None


def test_unmerge_refuses_when_winner_was_merged_again(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        snap = _merge_entities_tx(db, L, W, method="user")
    s.merge_entities(W, C)  # the winner itself is folded away
    with s._connect(write=True) as db, pytest.raises(ValueError, match="no longer exists"):
        _unmerge_tx(db, snap)


def test_merge_repoints_distinct_pairs(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", tuple(sorted((L, C))))
    s.merge_entities(L, W)
    assert s.is_distinct_pair(W, C)


def _mark_distinct(s, a, b):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", tuple(sorted((a, b))))


def test_deterministic_merge_skips_distinct_pair(tmp_path):
    s = _store(tmp_path)  # L and W share a canonical key ("Dana Okafor")
    _mark_distinct(s, L, W)
    assert resolve._deterministic_merges(s) == 0
    assert s.get_entity(L) and s.get_entity(W)


def test_candidate_pairs_skip_distinct_pair(tmp_path):
    ents = [{"id": "a", "name": "Dana Okafor", "type": "person"},
            {"id": "b", "name": "Dana J Okafor", "type": "person"}]
    assert resolve._candidate_pairs(ents, distinct={("a", "b")}) == []
    assert len(resolve._candidate_pairs(ents)) == 1


def test_email_equality_merge_skips_distinct_pair(tmp_path, monkeypatch):
    from mcpbrain import config
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET email_addr='dana@northgate.example' WHERE id=?", (W,))
    _mark_distinct(s, L, W)
    monkeypatch.setattr(config, "write_time_dedup_enabled", lambda home: True)
    assert resolve._email_equality_merges(s, home=tmp_path) == 0


def test_duplicate_verdict_applier_guards_distinct_pair(tmp_path):
    from mcpbrain.review_apply import apply_duplicate_verdicts
    s = _store(tmp_path)
    _mark_distinct(s, L, W)
    out = apply_duplicate_verdicts(s, [{"pair_id": "|".join(sorted((L, W))), "same": True}], cap=5)
    assert out["merged"] == 0 and out["guarded"] == 1


def test_graph_ui_refuses_distinct_pair(tmp_path):
    s = _store(tmp_path)
    _mark_distinct(s, L, W)
    out = graph_view.merge_entities(s, L, W)
    assert out == {"ok": False, "error": "marked_distinct",
                   "message": "These were marked as different entities. Undo that correction first."}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_graph_corrections_merge.py -v`
Expected: FAIL (`ImportError: cannot import name '_merge_entities_tx'`).

- [ ] **Step 3: Refactor `merge_entities` into `_merge_entities_tx` with a snapshot**

Move the body of `Store.merge_entities` into a module-level function in `store.py` and extend it. Full function:

```python
_MERGE_TOUCHED_COLS = ("name", "type", "org", "aliases", "email_addr", "notes")


def _merge_entities_tx(db, loser_id, winner_id, *, canonical_name=None,
                       method="deterministic") -> dict | None:
    """Fold loser into winner on a caller-held write connection.

    Behaviour is exactly Store.merge_entities' (see its docstring), plus three
    things the graph-corrections work needs: distinct pairs are repointed onto
    the winner, a user rejection on a loser triple is carried onto the surviving
    winner triple, and the function RETURNS a snapshot from which _unmerge_tx
    can restore the pre-merge state exactly. Returns None for a no-op (same id,
    or either entity missing)."""
    if loser_id == winner_id:
        return None
    loser = db.execute("SELECT * FROM entities WHERE id=?", (loser_id,)).fetchone()
    win = db.execute("SELECT * FROM entities WHERE id=?", (winner_id,)).fetchone()
    if loser is None or win is None:
        return None

    def sub(x):
        return winner_id if x == loser_id else x

    def table_exists(name):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                          (name,)).fetchone() is not None

    rels = [dict(r) for r in db.execute(
        "SELECT * FROM entity_relations WHERE entity_a=? OR entity_b=?", (loser_id, loser_id))]
    collisions = []
    for (a, rel, b) in {(sub(r["entity_a"]), r["relation"], sub(r["entity_b"])) for r in rels}:
        row = db.execute("SELECT * FROM entity_relations WHERE entity_a=? AND relation=? "
                         "AND entity_b=?", (a, rel, b)).fetchone()
        if row is not None:
            collisions.append(dict(row))
    emails = [dict(r) for r in db.execute(
        "SELECT * FROM email_entities WHERE entity_id=?", (loser_id,))]
    loser_msgs = [e["message_id"] for e in emails]
    winner_overlap = [r[0] for r in db.execute(
        f"SELECT message_id FROM email_entities WHERE entity_id=? AND message_id IN "
        f"({','.join('?' * len(loser_msgs)) or 'NULL'})", (winner_id, *loser_msgs))]
    snap = {
        "loser": dict(loser),
        "winner": {c: win[c] for c in (*_MERGE_TOUCHED_COLS, "mentions")},
        "winner_id": winner_id,
        "relations": rels,
        "collisions": collisions,
        "observation_ids": [r[0] for r in db.execute(
            "SELECT id FROM entity_observations WHERE entity_id=?", (loser_id,))],
        "emails": emails,
        "winner_email_overlap": winner_overlap,
        "suppression": None,
        "distinct_pairs": [], "winner_distinct_pairs": [],
        "field_locks": [dict(r) for r in db.execute(
            "SELECT * FROM entity_field_locks WHERE entity_id=?", (loser_id,))]
            if table_exists("entity_field_locks") else [],
        "communities": [dict(r) for r in db.execute(
            "SELECT * FROM entity_communities WHERE entity_id=?", (loser_id,))]
            if table_exists("entity_communities") else [],
    }
    if table_exists("entity_suppressions"):
        row = db.execute("SELECT * FROM entity_suppressions WHERE entity_id=?",
                         (loser_id,)).fetchone()
        snap["suppression"] = dict(row) if row else None
    has_pairs = table_exists("entity_distinct_pairs")
    if has_pairs:
        snap["distinct_pairs"] = [dict(r) for r in db.execute(
            "SELECT * FROM entity_distinct_pairs WHERE a=? OR b=?", (loser_id, loser_id))]
        snap["winner_distinct_pairs"] = [dict(r) for r in db.execute(
            "SELECT * FROM entity_distinct_pairs WHERE a=? OR b=?", (winner_id, winner_id))]

    # --- the merge itself: the pre-existing statements, unchanged ------------
    db.execute("UPDATE OR IGNORE entity_relations SET entity_a=? WHERE entity_a=?",
               (winner_id, loser_id))
    db.execute("UPDATE OR IGNORE entity_relations SET entity_b=? WHERE entity_b=?",
               (winner_id, loser_id))
    db.execute("DELETE FROM entity_relations WHERE entity_a=? OR entity_b=?",  # admin-delete-ok
               (loser_id, loser_id))
    db.execute("DELETE FROM entity_relations WHERE entity_a=entity_b AND entity_a=?",  # admin-delete-ok
               (winner_id,))
    # A user rejection on a loser triple survives onto the winner's copy of it.
    for r in rels:
        if r.get("user_verdict") == "rejected":
            db.execute(
                "UPDATE entity_relations SET user_verdict='rejected', "
                "invalidated_at=COALESCE(invalidated_at, ?), superseded_reason='user_rejected' "
                "WHERE entity_a=? AND relation=? AND entity_b=? AND entity_a != entity_b",
                (r.get("invalidated_at") or datetime.now(timezone.utc).isoformat(),
                 sub(r["entity_a"]), r["relation"], sub(r["entity_b"])))
    db.execute("UPDATE entity_observations SET entity_id=? WHERE entity_id=?",
               (winner_id, loser_id))
    db.execute("UPDATE OR IGNORE email_entities SET entity_id=? WHERE entity_id=?",
               (winner_id, loser_id))
    db.execute("DELETE FROM email_entities WHERE entity_id=?", (loser_id,))  # admin-delete-ok
    if table_exists("entity_suppressions"):
        db.execute("DELETE FROM entity_suppressions WHERE entity_id=?", (loser_id,))  # admin-delete-ok
    if has_pairs:
        for p in snap["distinct_pairs"]:
            other = p["b"] if p["a"] == loser_id else p["a"]
            if other != winner_id:
                db.execute("INSERT OR IGNORE INTO entity_distinct_pairs(a,b) VALUES(?,?)",
                           tuple(sorted((winner_id, other))))
        db.execute("DELETE FROM entity_distinct_pairs WHERE a=? OR b=?",  # admin-delete-ok
                   (loser_id, loser_id))

    new_org = win["org"] if win["org"] not in ("", "unknown") else loser["org"]
    new_type = win["type"] if win["type"] != "unknown" else loser["type"]
    new_name = canonical_name or win["name"]
    new_mentions = (win["mentions"] or 0) + (loser["mentions"] or 0)
    alias_set = set()
    for src in (win["aliases"] or "", loser["aliases"] or ""):
        for a in src.split("|"):
            if a:
                alias_set.add(a)
    alias_set.add(win["name"]); alias_set.add(loser["name"])
    alias_set.discard(new_name); alias_set.discard("")
    db.execute("UPDATE entities SET name=?,type=?,org=?,mentions=?,aliases=? WHERE id=?",
               (new_name, new_type, new_org, new_mentions, "|".join(sorted(alias_set)), winner_id))
    snap["merge_log_id"] = db.execute(
        "INSERT INTO entity_merge_log(winner_id,loser_id,loser_name,method) VALUES(?,?,?,?)",
        (winner_id, loser_id, loser["name"], method)).lastrowid
    db.execute("DELETE FROM entities WHERE id=?", (loser_id,))  # admin-delete-ok
    return snap
```

Keep the explanatory comments from the old body where the statements are unchanged (copy them across). `Store.merge_entities` becomes:

```python
    def merge_entities(self, loser_id, winner_id, *, canonical_name=None,
                       method="deterministic") -> dict | None:
        """(existing docstring, plus:) Returns the undo snapshot from
        _merge_entities_tx; existing callers ignore it."""
        with self._connect(write=True) as db:
            return _merge_entities_tx(db, loser_id, winner_id,
                                      canonical_name=canonical_name, method=method)
```

The old body read `COALESCE(aliases,'')`; the new one reads the full row and treats `None` aliases as `""`. Same result.

- [ ] **Step 4: `_unmerge_tx`**

```python
def _restore_row(db, table: str, row: dict, key: str = "id") -> None:
    """Put `row` back exactly: UPDATE every column by key, or INSERT it if gone."""
    cols = [c for c in row if c != key]
    cur = db.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in cols)} WHERE {key}=?",
                     (*[row[c] for c in cols], row[key]))
    if cur.rowcount == 0:
        _insert_row(db, table, row)


def _insert_row(db, table: str, row: dict, *, or_ignore: bool = False) -> None:
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
    db.execute(f"{verb} INTO {table}({', '.join(row)}) VALUES({', '.join('?' * len(row))})",
               tuple(row.values()))


def _unmerge_tx(db, snap: dict) -> None:
    """Reverse a _merge_entities_tx exactly, or refuse. Raises ValueError naming
    the conflict when anything the merge moved has changed since: a partial
    restore would be worse than none."""
    loser_id, winner_id = snap["loser"]["id"], snap["winner_id"]

    def sub(x):
        return winner_id if x == loser_id else x

    if db.execute("SELECT 1 FROM entities WHERE id=?", (winner_id,)).fetchone() is None:
        raise ValueError(f"cannot undo: {winner_id} no longer exists (it was merged or removed since)")
    if db.execute("SELECT 1 FROM entities WHERE id=?", (loser_id,)).fetchone() is not None:
        raise ValueError(f"cannot undo: an entity with id {loser_id} exists again")
    for oid in snap["observation_ids"]:
        row = db.execute("SELECT entity_id FROM entity_observations WHERE id=?", (oid,)).fetchone()
        if row is not None and row[0] != winner_id:
            raise ValueError(f"cannot undo: observation {oid} has moved to {row[0]}")
    for r in snap["relations"]:
        row = db.execute("SELECT entity_a, entity_b FROM entity_relations WHERE id=?",
                         (r["id"],)).fetchone()
        if row is not None and (row[0], row[1]) != (sub(r["entity_a"]), sub(r["entity_b"])):
            raise ValueError(f"cannot undo: relation {r['id']} has changed since the merge")

    _insert_row(db, "entities", snap["loser"])
    w = snap["winner"]
    loser_mentions = snap["loser"].get("mentions") or 0
    db.execute(
        "UPDATE entities SET name=?, type=?, org=?, aliases=?, email_addr=?, notes=?, "
        "mentions=MAX(COALESCE(mentions,0)-?, 0) WHERE id=?",
        (w["name"], w["type"], w["org"], w["aliases"], w["email_addr"], w["notes"],
         loser_mentions, winner_id))
    for c in snap["collisions"]:
        _restore_row(db, "entity_relations", c)
    for r in snap["relations"]:
        _restore_row(db, "entity_relations", r)
    for oid in snap["observation_ids"]:
        db.execute("UPDATE entity_observations SET entity_id=? WHERE id=?", (loser_id, oid))
    overlap = set(snap["winner_email_overlap"])
    for e in snap["emails"]:
        if e["message_id"] not in overlap:
            db.execute("DELETE FROM email_entities WHERE message_id=? AND entity_id=?",  # admin-delete-ok
                       (e["message_id"], winner_id))
        _insert_row(db, "email_entities", e, or_ignore=True)
    if snap["suppression"] is not None:
        _insert_row(db, "entity_suppressions", snap["suppression"], or_ignore=True)
    prior_winner_pairs = {(p["a"], p["b"]) for p in snap["winner_distinct_pairs"]}
    for p in snap["distinct_pairs"]:
        other = p["b"] if p["a"] == loser_id else p["a"]
        repointed = tuple(sorted((winner_id, other)))
        if other != winner_id and repointed not in prior_winner_pairs:
            db.execute("DELETE FROM entity_distinct_pairs WHERE a=? AND b=?", repointed)  # admin-delete-ok
        _insert_row(db, "entity_distinct_pairs", p, or_ignore=True)
    for lk in snap["field_locks"]:
        _insert_row(db, "entity_field_locks", lk, or_ignore=True)
    for cm in snap["communities"]:
        _insert_row(db, "entity_communities", cm, or_ignore=True)
    db.execute("DELETE FROM entity_merge_log WHERE id=?", (snap["merge_log_id"],))  # admin-delete-ok
```

The tables that `ON DELETE CASCADE` from `entities` are `entity_relations`, `entity_observations`, `email_entities`, `entity_communities` and the two new tables from Task 1. All of them are in the snapshot (verified with `grep -n "REFERENCES entities(id)" mcpbrain/store.py` on 2026-09-24). If a later change adds another cascading table, add it to the snapshot the same way as `communities`, or unmerge will silently lose its rows.

Note the order: collisions are restored before loser relations so a loser triple re-inserted by id cannot trip the UNIQUE constraint against a winner row that has already been put back to its own triple. Snapshots are stored as JSON in Task 4, so every value in them must be JSON-native (they are: SQLite returns str/int/float/None).

- [ ] **Step 5: Distinct pairs in the merge paths**

`resolve._candidate_pairs(entities, distinct=frozenset())` gains a keyword; inside the inner loop, after `pair_key` is computed and before appending: `if pair_key in distinct: continue`. Its caller in `resolve_entities` (and any other caller: `grep -rn "_candidate_pairs(" mcpbrain`) passes `distinct=store.distinct_pair_set()`.

`resolve._deterministic_merges` and `resolve._email_equality_merges`: load `distinct = store.distinct_pair_set()` once at the top, then inside the `for m in members:` loop skip a member that is distinct from the survivor:

```python
        for m in members:
            if m["id"] != survivor["id"]:
                if tuple(sorted((m["id"], survivor["id"]))) in distinct:
                    continue  # the user said these are different (brain_graph_correct)
                store.merge_entities(m["id"], survivor["id"], method="deterministic")
                merged += 1
```

(`method="email"` in the email function.)

`review_apply.apply_duplicate_verdicts`: after the role-address guard and before the cap check:

```python
        if store.is_distinct_pair(a["id"], b["id"]):
            log.info("review_apply: merge pair %s is marked distinct by the user, guarding", pair_id)
            result["guarded"] += 1
            continue
```

`graph_view._orient`: after the role-inbox check:

```python
    if store.is_distinct_pair(loser_id, winner_id):
        return {"ok": False, "error": "marked_distinct",
                "message": "These were marked as different entities. Undo that correction first."}
```

and in `control_api.py`'s `/api/graph/merge` POST and `/api/graph/merge/preview` GET, a `marked_distinct` error already maps to 409 (the existing `code = 404 if ... not_found else 409`). No change needed there; confirm by reading both routes.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_graph_corrections_merge.py tests/test_resolve.py tests/test_review_apply.py tests/test_graph_view.py tests/test_store.py tests/test_sweep_merge_residue.py -q`
Expected: PASS. If an existing test asserted `merge_entities(...) is None`, update it to ignore the return value (the snapshot is the new contract).

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/store.py mcpbrain/resolve.py mcpbrain/review_apply.py mcpbrain/graph_view.py tests/test_graph_corrections_merge.py
git commit -m "feat(graph): distinct pairs block every merge path; merges return an exact undo snapshot"
```

---

### Task 4: The correction engine (all ops except merge), undo, and the pending queue

**Files:**
- Create: `mcpbrain/graph_corrections.py`
- Test: `tests/test_graph_corrections.py` (create)

**Interfaces:**
- Consumes: Task 1 tables; Task 2 `graph_write.upsert_relation_in`; `graph_write._JUNK_ROLE_VALUES`.
- Produces (used by Tasks 5-8):
  - `OPS`, `BASES`, `FIELDS`, `PENDING_CAP = 25`, `FINDING_TYPE = "graph_correction"`
  - `payload_from_args(args: dict) -> dict`
  - `describe(op: str, payload: dict) -> str` (plain English, e.g. "Mark relation dana-okafor -works_at-> northgate-trust as wrong")
  - `submit(store, args: dict, *, confirmed_via: str = "", declined: bool = False) -> dict`
  - `undo(store, correction_id: int) -> dict`
  - `approve(store, correction_id: int, *, via: str = "dashboard") -> dict`
  - `decline(store, correction_id: int) -> dict`
  - result dicts: `{"status": "applied"|"pending"|"declined"|"duplicate"|"refused"|"failed"|"reverted", "correction_id"?: int, "summary"?: str, "undo"?: str, "next"?: str, "error"?: str}`

- [ ] **Step 1: Write the failing tests**

```python
"""graph_corrections engine: ops, stickiness end to end, undo, pending (Task 4)."""
from mcpbrain import graph_corrections as gc
from mcpbrain import graph_write as gw
from mcpbrain.store import Store

D, N, S = "dana-okafor", "northgate-trust", "southbank-community-trust"


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org) VALUES(?, 'Dana Okafor', 'person', 'Northgate Trust')", (D,))
        db.execute("INSERT INTO entities(id,name,type) VALUES(?, 'Northgate Trust', 'org')", (N,))
        db.execute("INSERT INTO entities(id,name,type) VALUES(?, 'Southbank Community Trust', 'org')", (S,))
    gw.upsert_relation(s, D, "works_at", N, valid_from="2026-01-01")
    return s


def _rel(s, a, rel, b):
    with s._connect() as db:
        return db.execute("SELECT * FROM entity_relations WHERE entity_a=? AND relation=? "
                          "AND entity_b=?", (a, rel, b)).fetchone()


def _stated(**kw):
    return {"basis": "user_stated", "reason": "Dana told me", **kw}


def test_reject_relation_is_applied_logged_and_sticky(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))
    assert out["status"] == "applied" and out["undo"] == f"brain_graph_correct op=undo correction_id={out['correction_id']}"
    assert _rel(s, D, "works_at", N)["user_verdict"] == "rejected"
    gw.upsert_relation(s, D, "works_at", N, valid_from="2026-09-01")  # re-extraction
    assert _rel(s, D, "works_at", N)["invalidated_at"] is not None
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM change_log WHERE change_type='graph_corrected'").fetchone()[0] == 1


def test_undo_reject_restores_the_row(tmp_path):
    s = _store(tmp_path)
    before = dict(_rel(s, D, "works_at", N))
    cid = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))["correction_id"]
    assert gc.undo(s, cid)["status"] == "reverted"
    assert dict(_rel(s, D, "works_at", N)) == before
    assert gc.undo(s, cid)["status"] == "refused"  # already reverted


def test_reject_unknown_relation_is_refused_and_writes_nothing(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="manages", entity_b=N))
    assert out["status"] == "refused" and "no relation" in out["error"]
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0


def test_assert_relation_supersedes_singleton_and_undo_restores(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at", entity_b=S))
    assert out["status"] == "applied"
    assert _rel(s, D, "works_at", S)["user_verdict"] == "asserted"
    assert _rel(s, D, "works_at", N)["invalidated_at"] is not None  # recency rule retired it
    gc.undo(s, out["correction_id"])
    assert _rel(s, D, "works_at", S) is None
    assert _rel(s, D, "works_at", N)["invalidated_at"] is None


def test_assert_relation_overrides_an_earlier_rejection(tmp_path):
    s = _store(tmp_path)
    gc.submit(s, _stated(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))
    gc.submit(s, _stated(op="assert_relation", entity_a=D, relation="works_at", entity_b=N))
    row = _rel(s, D, "works_at", N)
    assert row["invalidated_at"] is None and row["user_verdict"] == "asserted"


def test_set_field_org_locks_and_undo_unlocks(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _stated(op="set_field", entity_id=D, field="org",
                               value="Southbank Community Trust"))["correction_id"]
    assert s.get_entity(D)["org"] == "Southbank Community Trust"
    assert s.update_entity_org(D, "The Lantern Co") is False  # automated writer blocked
    gc.undo(s, cid)
    assert s.get_entity(D)["org"] == "Northgate Trust" and s.locked_fields(D) == set()


def test_set_field_role_writes_manual_observation_and_rejects_junk(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _stated(op="set_field", entity_id=D, field="role", value="Operations Lead"))["correction_id"]
    with s._connect() as db:
        rows = db.execute("SELECT value, source FROM entity_observations WHERE entity_id=? "
                          "AND attribute='role' AND valid_to IS NULL", (D,)).fetchall()
    assert [(r["value"], r["source"]) for r in rows] == [("Operations Lead", "manual")]
    assert gc.submit(s, _stated(op="set_field", entity_id=D, field="role", value="volunteer"))["status"] == "refused"
    gc.undo(s, cid)
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entity_observations WHERE entity_id=?", (D,)).fetchone()[0] == 0


def test_not_same_and_hide_with_undo(tmp_path):
    s = _store(tmp_path)
    c1 = gc.submit(s, _stated(op="not_same", entity_id=N, other_id=S))["correction_id"]
    assert s.is_distinct_pair(S, N)
    c2 = gc.submit(s, _stated(op="hide", entity_id=S))["correction_id"]
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?", (S,)).fetchone()
    gc.undo(s, c2); gc.undo(s, c1)
    assert not s.is_distinct_pair(S, N)
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?", (S,)).fetchone() is None


def test_unknown_entity_is_refused(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _stated(op="hide", entity_id="nobody"))
    assert out["status"] == "refused" and "nobody" in out["error"]


# --- inferred: the pending queue ----------------------------------------------

def _inferred(**kw):
    return {"basis": "inferred", "reason": "newer email signature", **kw}


def test_inferred_without_confirmation_is_staged_not_applied(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="set_field", entity_id=D, field="org", value="Southbank Community Trust"))
    assert out["status"] == "pending" and "dashboard" in out["next"]
    assert s.get_entity(D)["org"] == "Northgate Trust"
    finding = [f for f in s.open_findings(gc.FINDING_TYPE)]
    assert len(finding) == 1 and finding[0]["ref_id"] == str(out["correction_id"])


def test_inferred_with_elicitation_confirmation_applies(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="hide", entity_id=S), confirmed_via="elicitation")
    assert out["status"] == "applied"
    assert s.get_correction(out["correction_id"])["confirmed_via"] == "elicitation"


def test_inferred_dedup_and_decline_memory(tmp_path):
    s = _store(tmp_path)
    args = _inferred(op="hide", entity_id=S)
    first = gc.submit(s, args)
    assert gc.submit(s, args)["status"] == "duplicate"
    gc.decline(s, first["correction_id"])
    assert gc.submit(s, args)["status"] == "duplicate"  # declined is remembered
    assert s.open_findings(gc.FINDING_TYPE) == []


def test_declined_elicitation_is_recorded(tmp_path):
    s = _store(tmp_path)
    out = gc.submit(s, _inferred(op="hide", entity_id=S), declined=True)
    assert out["status"] == "declined"
    assert gc.submit(s, _inferred(op="hide", entity_id=S))["status"] == "duplicate"


def test_pending_cap(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(gc, "PENDING_CAP", 1)
    gc.submit(s, _inferred(op="hide", entity_id=S))
    out = gc.submit(s, _inferred(op="hide", entity_id=N))
    assert out["status"] == "refused" and "await approval" in out["error"]


def test_approve_applies_and_resolves_finding(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _inferred(op="hide", entity_id=S))["correction_id"]
    assert gc.approve(s, cid)["status"] == "applied"
    assert s.get_correction(cid)["confirmed_via"] == "dashboard"
    assert s.open_findings(gc.FINDING_TYPE) == []


def test_approve_of_a_now_impossible_correction_fails_cleanly(tmp_path):
    s = _store(tmp_path)
    cid = gc.submit(s, _inferred(op="reject_relation", entity_a=D, relation="works_at", entity_b=N))["correction_id"]
    with s._connect(write=True) as db:
        db.execute("DELETE FROM entity_relations")  # admin-delete-ok
    out = gc.approve(s, cid)
    assert out["status"] == "failed"
    assert s.get_correction(cid)["status"] == "failed"
    assert s.open_findings(gc.FINDING_TYPE) == []


def test_user_stated_ignores_dedup(tmp_path):
    """A stated correction always applies (the user is the authority)."""
    s = _store(tmp_path)
    gc.submit(s, _inferred(op="hide", entity_id=S))  # pending
    assert gc.submit(s, _stated(op="hide", entity_id=S))["status"] == "applied"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_graph_corrections.py -v`
Expected: FAIL (`ModuleNotFoundError: mcpbrain.graph_corrections`).

- [ ] **Step 3: Write `mcpbrain/graph_corrections.py`**

```python
"""User-directed corrections to the knowledge graph (brain_graph_correct).

Every correction is one write transaction holding BOTH the graph change and its
ledger row (graph_corrections) plus a change_log row, so it commits whole or
not at all. The ledger row's snapshot is what undo restores from.

Two bases:
  user_stated -- the user said it in conversation. Applied immediately.
  inferred    -- the model inferred it. Applied only with a confirmation the
                 model cannot forge: `confirmed_via` is set by the MCP server
                 after an elicitation, or by the dashboard's Apply button. It
                 never arrives in the tool's own arguments (the input schema
                 forbids extra properties). Without one, the correction is
                 staged as 'pending' and surfaced as a proactive finding.

Stickiness lives in the writers, not here: graph_write.upsert_relation_in skips
user-rejected triples, the Store setters refuse user-locked fields, and every
merge path consults entity_distinct_pairs. This module only writes the markers.

No mcp / Store / native imports at module scope: the MCP server imports
describe() and payload_from_args() to build its elicitation message.
"""
import json
from datetime import datetime, timezone

OPS = ("reject_relation", "assert_relation", "merge", "not_same", "set_field", "hide", "undo")
BASES = ("user_stated", "inferred")
FIELDS = ("role", "org", "name", "email")
PENDING_CAP = 25
FINDING_TYPE = "graph_correction"

_FIELD_COLUMN = {"org": "org", "name": "name", "email": "email_addr"}
_PAYLOAD_KEYS = {
    "reject_relation": ("entity_a", "relation", "entity_b"),
    "assert_relation": ("entity_a", "relation", "entity_b", "valid_from"),
    "merge": ("entity_id", "other_id", "name"),
    "not_same": ("entity_id", "other_id"),
    "set_field": ("entity_id", "field", "value"),
    "hide": ("entity_id",),
}


class Refused(Exception):
    """A correction that must not be written. The message goes to the model."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def payload_from_args(args: dict) -> dict:
    op = args["op"]
    p = {k: (args[k].strip() if isinstance(args[k], str) else args[k])
         for k in _PAYLOAD_KEYS[op] if args.get(k) not in (None, "")}
    p["reason"] = (args.get("reason") or "").strip()
    return p


def dedup_key(op: str, p: dict) -> str:
    if op in ("reject_relation", "assert_relation"):
        ident = [p["entity_a"], p["relation"], p["entity_b"]]
    elif op in ("merge", "not_same"):
        ident = sorted([p["entity_id"], p["other_id"]])
    elif op == "set_field":
        ident = [p["entity_id"], p["field"], p["value"]]
    else:
        ident = [p["entity_id"]]
    return op + ":" + json.dumps(ident)


def describe(op: str, p: dict) -> str:
    if op == "reject_relation":
        return f"Mark relation {p['entity_a']} -{p['relation']}-> {p['entity_b']} as wrong"
    if op == "assert_relation":
        return f"Record {p['entity_a']} -{p['relation']}-> {p['entity_b']}"
    if op == "merge":
        return f"Merge {p['entity_id']} and {p['other_id']} into one entity"
    if op == "not_same":
        return f"Record that {p['entity_id']} and {p['other_id']} are different entities"
    if op == "set_field":
        return f"Set {p['field']} of {p['entity_id']} to {p['value']!r}"
    if op == "hide":
        return f"Hide {p['entity_id']} from the graph"
    return op


# --- ledger + side tables, all on the caller's connection ---------------------

def _insert(db, op, basis, status, payload, snapshot, confirmed_via, key, *, applied_at="") -> int:
    return db.execute(
        "INSERT INTO graph_corrections(op,basis,status,payload,snapshot,reason,"
        "confirmed_via,dedup_key,applied_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (op, basis, status, json.dumps(payload), json.dumps(snapshot),
         payload.get("reason", ""), confirmed_via, key, applied_at)).lastrowid


def _log_change(db, cid: int, change: str, summary: str, detail: str) -> None:
    lid = db.execute(
        "INSERT INTO change_log(change_type, ref_id, summary, detail, revert_ref, source) "
        "VALUES(?,?,?,?,?,?)",
        (change, str(cid), summary, detail, f"correction:{cid}", "brain_graph_correct")).lastrowid
    db.execute("UPDATE graph_corrections SET change_log_id=? WHERE id=?", (lid, cid))


def _stage_finding(db, cid: int, summary: str, reason: str) -> None:
    db.execute(
        "INSERT INTO proactive_findings(finding_type, ref_id, org, summary, detail, severity, "
        "detected_at, resolved_at) VALUES(?,?,?,?,?,?,?,NULL) "
        "ON CONFLICT(finding_type, ref_id) DO UPDATE SET summary=excluded.summary, "
        "detail=excluded.detail, resolved_at=NULL",
        (FINDING_TYPE, str(cid), "", f"Proposed graph correction: {summary}",
         reason, "info", _now()))


def _resolve_finding(db, cid: int, verdict: str) -> None:
    db.execute("UPDATE proactive_findings SET resolved_at=?, verdict=? "
               "WHERE finding_type=? AND ref_id=? AND resolved_at IS NULL",
               (_now(), verdict, FINDING_TYPE, str(cid)))


def _entity(db, eid: str):
    row = db.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
    if row is None:
        raise Refused(f"no entity with id {eid!r} (use the id from brain_context or brain_graph)")
    return row


def _relation(db, a, rel, b):
    return db.execute("SELECT * FROM entity_relations WHERE entity_a=? AND relation=? "
                      "AND entity_b=?", (a, rel, b)).fetchone()


# --- apply, per op. Each raises Refused BEFORE its first write. --------------

def _apply_reject_relation(store, db, p) -> dict:
    row = _relation(db, p["entity_a"], p["relation"], p["entity_b"])
    if row is None:
        raise Refused(f"no relation {p['entity_a']} -{p['relation']}-> {p['entity_b']}")
    if row["user_verdict"] == "rejected":
        raise Refused("that relation is already marked wrong")
    db.execute("UPDATE entity_relations SET invalidated_at=COALESCE(invalidated_at, ?), "
               "valid_to=COALESCE(valid_to, ?), superseded_reason='user_rejected', "
               "user_verdict='rejected' WHERE id=?", (_now(), _today(), row["id"]))
    return {"relation": dict(row)}


def _apply_assert_relation(store, db, p) -> dict:
    from mcpbrain import graph_write as gw
    a, rel, b = p["entity_a"], p["relation"], p["entity_b"]
    _entity(db, a); _entity(db, b)
    if a == b:
        raise Refused("a relation needs two different entities")
    prior = _relation(db, a, rel, b)
    if prior is not None and prior["user_verdict"] == "asserted" and prior["invalidated_at"] is None:
        raise Refused("that relation is already recorded as stated by the user")
    rivals = [dict(r) for r in db.execute(
        "SELECT * FROM entity_relations WHERE entity_a=? AND relation=? AND entity_b != ? "
        "AND invalidated_at IS NULL", (a, rel, b))]
    if prior is not None and prior["user_verdict"] == "rejected":
        # The user now says it IS true: lift the rejection so the revive path runs.
        db.execute("UPDATE entity_relations SET user_verdict=NULL WHERE id=?", (prior["id"],))
    rid = gw.upsert_relation_in(db, a, rel, b, valid_from=p.get("valid_from") or _today(),
                                evidence=f"stated by the user: {p.get('reason', '')}",
                                source_doc_id="")
    db.execute("UPDATE entity_relations SET user_verdict='asserted' WHERE id=?", (rid,))
    return {"relation_id": rid, "prior": dict(prior) if prior else None, "rivals": rivals}


def _apply_not_same(store, db, p) -> dict:
    a, b = sorted((p["entity_id"], p["other_id"]))
    if a == b:
        raise Refused("an entity is always the same as itself")
    _entity(db, a); _entity(db, b)
    if db.execute("SELECT 1 FROM entity_distinct_pairs WHERE a=? AND b=?", (a, b)).fetchone():
        raise Refused("those two are already marked as different")
    db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", (a, b))
    return {"pair": [a, b]}


def _apply_set_field(store, db, p) -> dict:
    from mcpbrain.graph_write import _JUNK_ROLE_VALUES
    field, value, eid = p["field"], p["value"], p["entity_id"]
    ent = _entity(db, eid)
    if field not in FIELDS:
        raise Refused(f"field must be one of {list(FIELDS)}")
    if not value:
        raise Refused("value must not be empty")
    if field == "role":
        if value.lower() in _JUNK_ROLE_VALUES or len(value) > 80:
            raise Refused(f"{value!r} is not a job title mcpbrain records as a role")
        retired = [r["id"] for r in db.execute(
            "SELECT id FROM entity_observations WHERE entity_id=? AND attribute='role' "
            "AND source='manual' AND valid_to IS NULL", (eid,))]
        today = _today()
        db.execute("UPDATE entity_observations SET valid_to=? WHERE entity_id=? AND "
                   "attribute='role' AND source='manual' AND valid_to IS NULL", (today, eid))
        new_id = db.execute(
            "INSERT INTO entity_observations(entity_id, attribute, value, source, valid_from, "
            "confidence_source, last_seen) VALUES(?, 'role', ?, 'manual', ?, 'high', ?)",
            (eid, value, today, today)).lastrowid
        return {"retired_ids": retired, "inserted_id": new_id}
    col = _FIELD_COLUMN[field]
    lock = db.execute("SELECT * FROM entity_field_locks WHERE entity_id=? AND field=?",
                      (eid, field)).fetchone()
    snap = {"entity": {c: ent[c] for c in (col, "aliases", "org_valid_from")},
            "lock": dict(lock) if lock else None}
    if field == "name":
        old = (ent["name"] or "").strip()
        parts = [x for x in (ent["aliases"] or "").split("|") if x]
        if old and old != value and old not in parts:
            parts.append(old)
        db.execute("UPDATE entities SET name=?, aliases=? WHERE id=?", (value, "|".join(parts), eid))
    elif field == "org":
        db.execute("UPDATE entities SET org=?, org_valid_from=? WHERE id=?", (value, _today(), eid))
    else:
        db.execute("UPDATE entities SET email_addr=? WHERE id=?", (value, eid))
    db.execute("INSERT OR IGNORE INTO entity_field_locks(entity_id, field) VALUES(?,?)", (eid, field))
    return snap


def _apply_hide(store, db, p) -> dict:
    _entity(db, p["entity_id"])
    prior = db.execute("SELECT * FROM entity_suppressions WHERE entity_id=?",
                       (p["entity_id"],)).fetchone()
    db.execute("INSERT OR REPLACE INTO entity_suppressions(entity_id, reason, suppressed_at) "
               "VALUES(?, 'user', ?)", (p["entity_id"], _now()))
    return {"prior": dict(prior) if prior else None}


def _apply_merge(store, db, p) -> dict:
    raise Refused("merge is implemented in Task 5")


_APPLY = {
    "reject_relation": _apply_reject_relation,
    "assert_relation": _apply_assert_relation,
    "merge": _apply_merge,
    "not_same": _apply_not_same,
    "set_field": _apply_set_field,
    "hide": _apply_hide,
}


# --- undo, per op --------------------------------------------------------------

def _undo_reject_relation(db, p, snap):
    r = snap["relation"]
    cur = db.execute("UPDATE entity_relations SET invalidated_at=?, valid_to=?, "
                     "superseded_reason=?, user_verdict=? WHERE id=?",
                     (r["invalidated_at"], r["valid_to"], r["superseded_reason"],
                      r["user_verdict"], r["id"]))
    if cur.rowcount == 0:
        raise Refused("the relation no longer exists")


def _undo_assert_relation(db, p, snap):
    from mcpbrain.store import _restore_row
    if snap["prior"] is None:
        row = db.execute("SELECT entity_a, entity_b FROM entity_relations WHERE id=?",
                         (snap["relation_id"],)).fetchone()
        if row is not None:
            db.execute("DELETE FROM entity_relations WHERE id=?", (snap["relation_id"],))  # admin-delete-ok
            db.execute("UPDATE entities SET degree=MAX(COALESCE(degree,0)-1, 0) WHERE id IN (?,?)",
                       (row[0], row[1]))
    else:
        _restore_row(db, "entity_relations", snap["prior"])
    for r in snap["rivals"]:
        _restore_row(db, "entity_relations", r)


def _undo_not_same(db, p, snap):
    db.execute("DELETE FROM entity_distinct_pairs WHERE a=? AND b=?", tuple(snap["pair"]))  # admin-delete-ok


def _undo_set_field(db, p, snap):
    eid, field = p["entity_id"], p["field"]
    if field == "role":
        db.execute("DELETE FROM entity_observations WHERE id=?", (snap["inserted_id"],))  # admin-delete-ok
        for oid in snap["retired_ids"]:
            db.execute("UPDATE entity_observations SET valid_to=NULL WHERE id=?", (oid,))
        return
    e = snap["entity"]
    col = _FIELD_COLUMN[field]
    cur = db.execute(f"UPDATE entities SET {col}=?, aliases=?, org_valid_from=? WHERE id=?",
                     (e[col], e["aliases"], e["org_valid_from"], eid))
    if cur.rowcount == 0:
        raise Refused(f"{eid} no longer exists")
    if snap["lock"] is None:
        db.execute("DELETE FROM entity_field_locks WHERE entity_id=? AND field=?", (eid, field))  # admin-delete-ok


def _undo_hide(db, p, snap):
    if snap["prior"] is None:
        db.execute("DELETE FROM entity_suppressions WHERE entity_id=?", (p["entity_id"],))  # admin-delete-ok
    else:
        from mcpbrain.store import _restore_row
        _restore_row(db, "entity_suppressions", snap["prior"], key="entity_id")


def _undo_merge(db, p, snap):
    from mcpbrain.store import _unmerge_tx
    try:
        _unmerge_tx(db, snap)
    except ValueError as exc:
        raise Refused(str(exc)) from exc


_UNDO = {
    "reject_relation": _undo_reject_relation,
    "assert_relation": _undo_assert_relation,
    "merge": _undo_merge,
    "not_same": _undo_not_same,
    "set_field": _undo_set_field,
    "hide": _undo_hide,
}


# --- public entry points -------------------------------------------------------

def submit(store, args: dict, *, confirmed_via: str = "", declined: bool = False) -> dict:
    """Apply, stage or record one correction. Never raises for a refusal."""
    op = args.get("op")
    if op == "undo":
        return undo(store, int(args["correction_id"]))
    if op not in _APPLY:
        return {"status": "refused", "error": f"op must be one of {list(OPS)}"}
    basis = args.get("basis")
    if basis not in BASES:
        return {"status": "refused", "error": f"basis must be one of {list(BASES)}"}
    p = payload_from_args(args)
    key = dedup_key(op, p)
    summary = describe(op, p)
    try:
        with store._connect(write=True) as db:
            if basis == "inferred":
                prior = db.execute(
                    "SELECT id, status FROM graph_corrections WHERE dedup_key=? AND "
                    "status IN ('pending','declined','applied') ORDER BY id DESC LIMIT 1",
                    (key,)).fetchone()
                if prior is not None:
                    return {"status": "duplicate", "correction_id": prior["id"],
                            "summary": f"Already {prior['status']} as correction "
                                       f"{prior['id']}; nothing written."}
                if declined:
                    cid = _insert(db, op, basis, "declined", p, {}, "elicitation", key)
                    return {"status": "declined", "correction_id": cid,
                            "summary": f"{summary}: declined by the user. It will not be "
                                       f"proposed again."}
                if not confirmed_via:
                    n = db.execute("SELECT COUNT(*) FROM graph_corrections "
                                   "WHERE status='pending'").fetchone()[0]
                    if n >= PENDING_CAP:
                        raise Refused(f"{n} corrections already await approval; ask the "
                                      f"user to review them on the dashboard first")
                    cid = _insert(db, op, basis, "pending", p, {}, "", key)
                    _stage_finding(db, cid, summary, p.get("reason", ""))
                    return {"status": "pending", "correction_id": cid,
                            "summary": f"{summary}: staged for approval.",
                            "next": "This client cannot ask the user to confirm, so the "
                                    "correction is waiting on the mcpbrain dashboard "
                                    "(Pending corrections). Ask the user to approve it "
                                    "there. Do not say it was applied."}
            snapshot = _APPLY[op](store, db, p)
            cid = _insert(db, op, basis, "applied", p, snapshot, confirmed_via, key,
                          applied_at=_now())
            _log_change(db, cid, "graph_corrected", summary, p.get("reason", ""))
    except Refused as exc:
        return {"status": "refused", "error": str(exc)}
    return {"status": "applied", "correction_id": cid, "summary": summary,
            "undo": f"brain_graph_correct op=undo correction_id={cid}"}


def undo(store, correction_id: int) -> dict:
    try:
        with store._connect(write=True) as db:
            row = db.execute("SELECT * FROM graph_corrections WHERE id=?",
                             (correction_id,)).fetchone()
            if row is None:
                raise Refused(f"no correction {correction_id}")
            if row["status"] != "applied":
                raise Refused(f"correction {correction_id} is {row['status']}, not applied")
            p, snap = json.loads(row["payload"]), json.loads(row["snapshot"])
            _UNDO[row["op"]](db, p, snap)
            db.execute("UPDATE graph_corrections SET status='reverted', reverted_at=? "
                       "WHERE id=?", (_now(), correction_id))
            _log_change(db, correction_id, "graph_correction_reverted",
                        f"Undid: {describe(row['op'], p)}", "")
    except Refused as exc:
        return {"status": "refused", "error": str(exc)}
    return {"status": "reverted", "correction_id": correction_id,
            "summary": f"Undid: {describe(row['op'], p)}"}


def approve(store, correction_id: int, *, via: str = "dashboard") -> dict:
    try:
        with store._connect(write=True) as db:
            row = db.execute("SELECT * FROM graph_corrections WHERE id=?",
                             (correction_id,)).fetchone()
            if row is None or row["status"] != "pending":
                raise Refused(f"correction {correction_id} is not pending")
            p = json.loads(row["payload"])
            snapshot = _APPLY[row["op"]](store, db, p)
            db.execute("UPDATE graph_corrections SET status='applied', snapshot=?, "
                       "confirmed_via=?, applied_at=? WHERE id=?",
                       (json.dumps(snapshot), via, _now(), correction_id))
            _resolve_finding(db, correction_id, "applied")
            _log_change(db, correction_id, "graph_corrected", describe(row["op"], p),
                        p.get("reason", ""))
    except Refused as exc:
        with store._connect(write=True) as db:
            cur = db.execute("UPDATE graph_corrections SET status='failed', error=? "
                             "WHERE id=? AND status='pending'", (str(exc), correction_id))
            if cur.rowcount:
                _resolve_finding(db, correction_id, "failed")
        return {"status": "failed", "correction_id": correction_id, "error": str(exc)}
    return {"status": "applied", "correction_id": correction_id,
            "summary": describe(row["op"], p),
            "undo": f"brain_graph_correct op=undo correction_id={correction_id}"}


def decline(store, correction_id: int) -> dict:
    with store._connect(write=True) as db:
        cur = db.execute("UPDATE graph_corrections SET status='declined' "
                         "WHERE id=? AND status='pending'", (correction_id,))
        if cur.rowcount == 0:
            return {"status": "refused", "error": f"correction {correction_id} is not pending"}
        _resolve_finding(db, correction_id, "declined")
    return {"status": "declined", "correction_id": correction_id}
```

Note `approve` of an op whose apply raises part-way: `_APPLY` functions raise `Refused` before their first write, and the surrounding `with` rolls back on the exception anyway, so the failure branch's separate transaction only ever flips the ledger status.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_graph_corrections.py tests/test_graph_corrections_sticky.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/graph_corrections.py tests/test_graph_corrections.py
git commit -m "feat(graph): correction engine with ledger, undo and an approval queue for inferred corrections"
```

---

### Task 5: The merge op

**Files:**
- Modify: `mcpbrain/graph_corrections.py` (`_apply_merge`)
- Test: `tests/test_graph_corrections.py` (append)

**Interfaces:**
- Consumes: Task 3 `_merge_entities_tx`, `graph_view._orient`, `graph_view._merge_result`, `resolve._NAME_MERGEABLE_TYPES`.
- Produces: `op="merge"` works end to end, including undo through `_unmerge_tx`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _seed_dupes(s):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,degree,email_addr) VALUES"
                   "('d-okafor','D Okafor','person',1,'dana@northgate.example')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('topic-x','x','topic')")
        db.execute("INSERT INTO entities(id,name,type) VALUES('topic-y','y','topic')")
        db.execute("INSERT INTO entities(id,name,type,email_addr) VALUES"
                   "('office','Office','person','office@northgate.example')")


def test_merge_applies_best_of_fields_and_undo_restores(tmp_path):
    s = _store(tmp_path)
    _seed_dupes(s)
    before_d = dict(s.get_entity(D))
    out = gc.submit(s, _stated(op="merge", entity_id="d-okafor", other_id=D))
    assert out["status"] == "applied"
    survivor = s.get_entity(D)
    assert s.get_entity("d-okafor") is None
    assert survivor["email_addr"] == "dana@northgate.example"  # best-of from the loser
    assert "D Okafor" in survivor["aliases"]
    assert gc.undo(s, out["correction_id"])["status"] == "reverted"
    assert s.get_entity("d-okafor")["name"] == "D Okafor"
    assert {k: s.get_entity(D)[k] for k in ("name", "email_addr", "aliases", "org")} == \
           {k: before_d[k] for k in ("name", "email_addr", "aliases", "org")}


def test_merge_guards(tmp_path):
    s = _store(tmp_path)
    _seed_dupes(s)
    assert gc.submit(s, _stated(op="merge", entity_id=D, other_id=D))["status"] == "refused"
    assert gc.submit(s, _stated(op="merge", entity_id="topic-x", other_id="topic-y"))["status"] == "refused"
    assert gc.submit(s, _stated(op="merge", entity_id="office", other_id=D))["status"] == "refused"
    gc.submit(s, _stated(op="not_same", entity_id="d-okafor", other_id=D))
    assert gc.submit(s, _stated(op="merge", entity_id="d-okafor", other_id=D))["status"] == "refused"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_graph_corrections.py -k merge -v`
Expected: FAIL ("merge is implemented in Task 5").

- [ ] **Step 3: Implement `_apply_merge`**

```python
def _apply_merge(store, db, p) -> dict:
    from mcpbrain import graph_view
    from mcpbrain.resolve import _NAME_MERGEABLE_TYPES
    from mcpbrain.store import _merge_entities_tx
    if p["entity_id"] == p["other_id"]:
        raise Refused("merge needs two different entities")
    oriented = graph_view._orient(store, p["entity_id"], p["other_id"])
    if isinstance(oriented, dict):
        raise Refused(oriented["message"])
    winner, loser = oriented
    if winner["type"] not in _NAME_MERGEABLE_TYPES or loser["type"] not in _NAME_MERGEABLE_TYPES:
        raise Refused("only people, organisations and projects can be merged "
                      f"(these are {loser['type']} and {winner['type']})")
    result = graph_view._merge_result(winner, loser, p.get("name"))
    snap = _merge_entities_tx(db, loser["id"], winner["id"],
                              canonical_name=result["name"], method="user")
    if snap is None:
        raise Refused("nothing to merge")
    db.execute("UPDATE entities SET email_addr=?, notes=? WHERE id=?",
               (result["email_addr"], result["notes"], winner["id"]))
    return snap
```

`_orient` and `_merge_result` read through `store.get_entity`/`store.is_distinct_pair` on their own read connections. That is safe inside our write transaction because they run before its first write (WAL readers see the last committed state). Keep that ordering: nothing above `_merge_entities_tx` may write.

The snapshot records the winner's `email_addr`/`notes` BEFORE the best-of update (it is captured inside `_merge_entities_tx`, which runs first), so undo restores them.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_graph_corrections.py tests/test_graph_corrections_merge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/graph_corrections.py tests/test_graph_corrections.py
git commit -m "feat(graph): merge correction with full unmerge"
```

---

### Task 6: `brain_graph_correct` tool, daemon routing and the out-of-band confirmation channel

**Files:**
- Modify: `mcpbrain/tools.py` (declaration + factory after `make_brain_finding_resolve`; update the module docstring counts "26 declarations and the 24 handler factories" to 27/25, and the annotations comment "25 of the 26" to "26 of the 27")
- Modify: `mcpbrain/daemon.py:1520-1600` (`_routed_tool_handlers`), `mcpbrain/daemon.py` `call_tool`
- Modify: `mcpbrain/control_api.py:352-370` (`/api/tool`), `mcpbrain/control_client.py:205` (`call_tool`)
- Modify: `mcpbrain/mcp_server.py` (`build_server` factory wiring, `run_tool`, `on_call_tool` branch)
- Modify: `mcpbrain/config.py:1049-1060` (`render_project_instructions`), `mcpbrain/session_hooks.py:103-117` (`_TOOL_REMINDER`)
- Test: `tests/test_graph_correct_tool.py` (create); update the tool enumerations in `tests/test_mcp_tool_annotations.py`, `tests/test_tool_seam.py`, `tests/test_tool_exec_routing.py`, `tests/test_mcp_protocol_surface.py`, `tests/test_mcp_structured_output.py` (each keeps a hand-written list of tool names or sample arguments; add `brain_graph_correct` wherever `brain_finding_resolve` appears, with sample arguments `{"op": "hide", "basis": "user_stated", "reason": "junk", "entity_id": "x"}`)

**Interfaces:**
- Consumes: Task 4/5 `graph_corrections.submit`.
- Produces:
  - `tools.make_brain_graph_correct(store)` returning `async def brain_graph_correct(arguments: dict, confirmation: dict | None = None) -> dict`
  - `ControlClient.call_tool(name, arguments, confirmation: dict | None = None)`; `/api/tool` body key `"confirmation"`; `Daemon.call_tool(name, arguments, confirmation=None)`
  - `confirmation` shape: `{"via": "elicitation"}` | `{"declined": True}`; `None` for none
  - `mcp_server.run_tool(name, arguments, local, confirmation=None)`

- [ ] **Step 1: Write the failing tests**

```python
"""brain_graph_correct: schema, daemon execution, confirmation channel (Task 6)."""
import asyncio

import jsonschema
import pytest

from mcpbrain import tools  # noqa: F401 -- populates the registry
from mcpbrain.store import Store
from mcpbrain.tool_registry import spec


def _schema():
    return spec("brain_graph_correct").input_schema


def test_schema_rejects_smuggled_confirmation():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"op": "hide", "basis": "inferred", "reason": "r",
                             "entity_id": "x", "confirmed_via": "elicitation"}, _schema())


@pytest.mark.parametrize("args", [
    {"op": "hide", "basis": "user_stated", "reason": "r"},                  # no entity_id
    {"op": "reject_relation", "basis": "user_stated", "reason": "r", "entity_a": "a"},
    {"op": "undo"},                                                          # no correction_id
    {"op": "set_field", "basis": "user_stated", "reason": "r", "entity_id": "x",
     "field": "phone", "value": "1"},                                        # bad field
])
def test_schema_requires_per_op_fields(args):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, _schema())


def test_schema_accepts_valid_calls():
    jsonschema.validate({"op": "undo", "correction_id": 3}, _schema())
    jsonschema.validate({"op": "merge", "basis": "user_stated", "reason": "r",
                         "entity_id": "a", "other_id": "b"}, _schema())


def test_annotations_are_destructive():
    a = spec("brain_graph_correct").annotations
    assert a.destructive_hint is True and a.read_only_hint is False and a.idempotent_hint is False


def _daemon(tmp_path, monkeypatch):
    from mcpbrain import daemon as daemon_mod
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('x','X','person')")
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d._store, d._tool_handlers = s, None
    monkeypatch.setattr(daemon_mod, "app_dir", lambda: tmp_path)
    return d, s


def test_daemon_applies_user_stated(tmp_path, monkeypatch):
    d, s = _daemon(tmp_path, monkeypatch)
    out = d.call_tool("brain_graph_correct", {"op": "hide", "basis": "user_stated",
                                              "reason": "junk", "entity_id": "x"})
    assert out["status"] == "applied"


def test_daemon_honours_out_of_band_confirmation_only(tmp_path, monkeypatch):
    d, s = _daemon(tmp_path, monkeypatch)
    args = {"op": "hide", "basis": "inferred", "reason": "r", "entity_id": "x"}
    assert d.call_tool("brain_graph_correct", args)["status"] == "pending"
    with s._connect(write=True) as db:
        db.execute("DELETE FROM graph_corrections")  # admin-delete-ok
    out = d.call_tool("brain_graph_correct", args, confirmation={"via": "elicitation"})
    assert out["status"] == "applied"
```

If `Daemon.__new__` plus attribute seeding does not match how `tests/test_tool_exec_routing.py` constructs a daemon for `test_daemon_executes_brain_finding_resolve` (line ~500), copy that test's construction instead; it is the established pattern.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_graph_correct_tool.py -v`
Expected: FAIL (`KeyError: 'brain_graph_correct'`).

- [ ] **Step 3: Declare the tool in `tools.py`** (after `make_brain_finding_resolve`)

```python
_CORRECT_REQUIRED = {
    "reject_relation": ["basis", "reason", "entity_a", "relation", "entity_b"],
    "assert_relation": ["basis", "reason", "entity_a", "relation", "entity_b"],
    "merge": ["basis", "reason", "entity_id", "other_id"],
    "not_same": ["basis", "reason", "entity_id", "other_id"],
    "set_field": ["basis", "reason", "entity_id", "field", "value"],
    "hide": ["basis", "reason", "entity_id"],
    "undo": ["correction_id"],
}


@tool(
    "brain_graph_correct",
    description=(
        "Correct the knowledge graph. Use entity ids from brain_context or brain_graph, "
        "never names. ops: reject_relation (a relation is wrong), assert_relation (record "
        "a relation), merge (two entries are the same person/org/project), not_same (two "
        "entries are different), set_field (role, org, name or email), hide (junk entity), "
        "undo (reverse a correction by correction_id). basis: 'user_stated' ONLY when the "
        "user said it in this conversation (applied at once); 'inferred' when you worked "
        "it out yourself (the user is asked to confirm, or it waits for approval on the "
        "dashboard). Corrections survive re-enrichment. Report the returned status "
        "faithfully: never say a pending correction was applied."
    ),
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "op": {"type": "string", "enum": list(_CORRECT_REQUIRED)},
            "basis": {"type": "string", "enum": ["user_stated", "inferred"]},
            "reason": {"type": "string",
                       "description": "the user's words, or the evidence you inferred it from"},
            "entity_a": {"type": "string"},
            "relation": {"type": "string"},
            "entity_b": {"type": "string"},
            "valid_from": {"type": "string",
                           "description": "YYYY-MM-DD, assert_relation only; default today"},
            "entity_id": {"type": "string"},
            "other_id": {"type": "string"},
            "name": {"type": "string", "description": "merge only: the final display name"},
            "field": {"type": "string", "enum": ["role", "org", "name", "email"]},
            "value": {"type": "string"},
            "correction_id": {"type": "integer"},
        },
        "required": ["op"],
        "allOf": [
            {"if": {"properties": {"op": {"const": op}}}, "then": {"required": req}}
            for op, req in _CORRECT_REQUIRED.items()
        ],
    },
    annotations=ToolAnnotations(
        title="Correct the knowledge graph", read_only_hint=False,
        destructive_hint=True, idempotent_hint=False, open_world_hint=False,
    ),
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": [
                "applied", "pending", "declined", "duplicate", "refused", "failed",
                "reverted", "not_applied", "error"]},
            "correction_id": {"type": "integer"},
            "summary": {"type": "string"},
            "undo": {"type": "string"},
            "next": {"type": "string"},
            "error": {"type": "string"},
        },
        "required": ["status"],
    },
)
def make_brain_graph_correct(store):
    async def brain_graph_correct(arguments: dict, confirmation: dict | None = None) -> dict:
        """Apply, stage or undo a graph correction. `confirmation` is set ONLY by
        the MCP server (after an elicitation) and never comes from the model's
        arguments. Returns a status dict; never raises."""
        from mcpbrain import graph_corrections
        c = confirmation or {}
        try:
            return graph_corrections.submit(
                store, arguments, confirmed_via=c.get("via", ""),
                declined=bool(c.get("declined")))
        except Exception as exc:  # noqa: BLE001 -- a tool must return, not raise
            _log.exception("brain_graph_correct failed")
            return {"status": "error", "error": str(exc)}
    return brain_graph_correct
```

- [ ] **Step 4: The confirmation channel**

`control_client.ControlClient.call_tool`:

```python
    def call_tool(self, name: str, arguments: dict, confirmation: dict | None = None):
        """(existing docstring, plus:) `confirmation` carries a user confirmation
        the MCP server obtained (brain_graph_correct only). It travels beside the
        arguments, never inside them, so the model cannot supply one."""
        body = {"name": name, "arguments": arguments}
        if confirmation:
            body["confirmation"] = confirmation
        r = self._request("/api/tool", method="POST", body=body,
                          timeout=self.TOOL_CALL_TIMEOUT_S, error_body=True)
        ...  # unchanged
```

`control_api.py` `/api/tool`: read `confirmation = body.get("confirmation")`; reject a non-dict with 400 (same message style as the `arguments` check); call `d.call_tool(name, arguments, confirmation=confirmation)`.

`Daemon.call_tool(self, name, arguments, confirmation=None)`: after validation,

```python
        handler = handlers[name]
        result = (handler(arguments, confirmation) if name in _CONFIRMABLE_TOOLS
                  else handler(arguments))
```

with a module-level `_CONFIRMABLE_TOOLS = frozenset({"brain_graph_correct"})` and, in `_routed_tool_handlers`, `from mcpbrain.tools import make_brain_graph_correct`, `graph_correct = make_brain_graph_correct(store)` and the entry `"brain_graph_correct": lambda a, c=None: graph_correct(a, c)`. Update that method's docstring list of routed tools.

- [ ] **Step 5: MCP server dispatch (user_stated and unconfirmed inferred only; elicitation is Task 7)**

In `build_server`: `graph_correct = make_brain_graph_correct(draft_store)` (the writable handle, used only on the kill-switch path, exactly like `finding_resolve`), and import the factory alongside the others.

`run_tool(name, arguments, local, confirmation=None)`: in the routed branch,

```python
                kwargs = {"confirmation": confirmation} if confirmation else {}
                return await asyncio.to_thread(client.call_tool, name, arguments, **kwargs)
```

(Passing the keyword only when set keeps every existing test double with a two-argument `call_tool` working.)

`on_call_tool`, after the `brain_finding_resolve` branch:

```python
        elif name == "brain_graph_correct":
            confirmation = None  # Task 7 fills this from an elicitation
            out = await run_tool(name, arguments,
                                 lambda: graph_correct(arguments, confirmation),
                                 confirmation=confirmation)
```

- [ ] **Step 6: Tell the model the tool exists**

`config.render_project_instructions`: in the "Keep my brain current as we work:" list add the line
`- Something in the graph is wrong (a relation, a role or org, a duplicate or a mix-up) -> brain_graph_correct: basis user_stated when I said it, inferred when you worked it out`.

`session_hooks._TOOL_REMINDER`: append to the "Keep the brain current" bullet: `a graph fact I correct (wrong relation, role, org, duplicate) -> brain_graph_correct (basis user_stated; inferred only for your own deductions, which I confirm);`. Keep the line-continuation style.

Check any test that pins these strings (`grep -rn "Keep my brain current" tests`, `grep -rn "_TOOL_REMINDER" tests`) and update it.

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_graph_correct_tool.py tests/test_tool_registry.py tests/test_tool_exec_routing.py tests/test_mcp_tool_annotations.py tests/test_tool_seam.py tests/test_mcp_protocol_surface.py tests/test_mcp_structured_output.py tests/test_mcp_input_validation.py tests/test_mcp_build_server.py tests/test_control_api_post.py tests/test_session_hooks.py -q`
Expected: PASS (after adding `brain_graph_correct` to each enumeration the failures name).

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/tools.py mcpbrain/daemon.py mcpbrain/control_api.py mcpbrain/control_client.py mcpbrain/mcp_server.py mcpbrain/config.py mcpbrain/session_hooks.py tests/
git commit -m "feat(mcp): brain_graph_correct tool, routed to the daemon with an out-of-band confirmation channel"
```

---

### Task 7: Elicitation gate for inferred corrections

**Files:**
- Modify: `mcpbrain/mcp_server.py` (new `_confirm_correction`; the `brain_graph_correct` branch)
- Test: `tests/test_graph_correct_elicitation.py` (create)

**Interfaces:**
- Consumes: Task 6 dispatch; `graph_corrections.describe`, `payload_from_args`; SDK `ServerSession.client_capabilities` (`.elicitation.form`), `ServerSession.elicit_form(message, requested_schema) -> ElicitResult` (`.action` in accept/decline/cancel, `.content`).
- Produces: `async def _confirm_correction(ctx, arguments: dict) -> dict | None` returning `{"via": "elicitation"}`, `{"declined": True}`, `{"cancelled": True}`, or `None` (client cannot elicit).

- [ ] **Step 1: Write the failing tests**

```python
"""The inferred-correction elicitation gate (Task 7)."""
import asyncio
from types import SimpleNamespace

from mcpbrain import mcp_server


class _Session:
    def __init__(self, caps, action="accept", content=None, raises=False):
        self.client_capabilities = caps
        self._action, self._content, self._raises = action, content, raises
        self.sent = []

    async def elicit_form(self, message, requested_schema, related_request_id=None):
        self.sent.append((message, requested_schema))
        if self._raises:
            raise RuntimeError("no back channel")
        return SimpleNamespace(action=self._action, content=self._content)


_FORM = SimpleNamespace(elicitation=SimpleNamespace(form=SimpleNamespace(), url=None))
_ARGS = {"op": "set_field", "basis": "inferred", "reason": "newer signature",
         "entity_id": "dana-okafor", "field": "org", "value": "Southbank Community Trust"}


def _run(session):
    return asyncio.run(mcp_server._confirm_correction(SimpleNamespace(session=session), _ARGS))


def test_accept_confirms():
    s = _Session(_FORM, "accept", {"confirm": True})
    assert _run(s) == {"via": "elicitation"}
    message, schema = s.sent[0]
    assert "Set org of dana-okafor to 'Southbank Community Trust'" in message
    assert "newer signature" in message
    assert schema["properties"]["confirm"]["type"] == "boolean"


def test_accept_with_confirm_unticked_is_a_decline():
    assert _run(_Session(_FORM, "accept", {"confirm": False})) == {"declined": True}


def test_decline_and_cancel():
    assert _run(_Session(_FORM, "decline")) == {"declined": True}
    assert _run(_Session(_FORM, "cancel")) == {"cancelled": True}


def test_no_capability_means_none():
    assert _run(_Session(SimpleNamespace(elicitation=None))) is None
    assert _run(_Session(None)) is None
    url_only = SimpleNamespace(elicitation=SimpleNamespace(form=None, url=SimpleNamespace()))
    assert _run(_Session(url_only)) is None


def test_bare_elicitation_capability_counts_as_form():
    """Pre-2025-11-25 clients advertise `elicitation: {}`, which means form mode."""
    bare = SimpleNamespace(elicitation=SimpleNamespace(form=None, url=None))
    assert _run(_Session(bare, "accept", {"confirm": True})) == {"via": "elicitation"}


def test_transport_failure_falls_back_to_pending():
    assert _run(_Session(_FORM, raises=True)) is None
```

Add one dispatch-level test in the same file that drives `on_call_tool` through `build_server(...)` with a fake `client` whose `call_tool(name, arguments, confirmation=None)` records its arguments, and a `ctx` carrying `_Session(_FORM, "cancel")`. Assert the fake was NOT called and the result JSON has `"status": "not_applied"`. Follow `tests/test_tool_exec_routing.py`'s pattern for building the server and invoking `on_call_tool` (it already constructs `build_server` with a fake client and calls the registered handler); reuse its helpers rather than writing new plumbing.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_graph_correct_elicitation.py -v`
Expected: FAIL (`AttributeError: _confirm_correction`).

- [ ] **Step 3: Implement**

Module level in `mcp_server.py`:

```python
_CONFIRM_SCHEMA = {
    "type": "object",
    "properties": {"confirm": {"type": "boolean", "title": "Apply this correction",
                               "default": True}},
    "required": ["confirm"],
}


async def _confirm_correction(ctx, arguments: dict) -> dict | None:
    """Ask the user to confirm an INFERRED graph correction via form elicitation.

    Returns the confirmation to forward to the daemon, or None when this client
    cannot elicit (Claude Desktop), in which case the daemon stages the
    correction for dashboard approval. The confirmation is produced HERE, from
    the client's answer, and never read from the model's arguments: that is the
    whole gate.
    """
    session = getattr(ctx, "session", None)
    caps = getattr(session, "client_capabilities", None) if session is not None else None
    elicitation = getattr(caps, "elicitation", None) if caps is not None else None
    if elicitation is None:
        return None
    # Form mode is present explicitly, or implied by a bare `elicitation: {}`
    # (pre-2025-11-25). URL-only clients cannot show this form.
    if getattr(elicitation, "form", None) is None and getattr(elicitation, "url", None) is not None:
        return None
    from mcpbrain.graph_corrections import describe, payload_from_args
    message = ("mcpbrain wants to correct your knowledge graph:\n\n"
               f"{describe(arguments['op'], payload_from_args(arguments))}\n\n"
               f"Why: {arguments.get('reason', '')}")
    try:
        result = await session.elicit_form(message, _CONFIRM_SCHEMA)
    except Exception:  # noqa: BLE001 -- no back channel, client gone: fall back to pending
        _log.debug("correction elicitation failed; staging instead", exc_info=True)
        return None
    if result.action == "accept":
        if (result.content or {}).get("confirm", True) is False:
            return {"declined": True}
        return {"via": "elicitation"}
    if result.action == "decline":
        return {"declined": True}
    return {"cancelled": True}
```

The `brain_graph_correct` branch in `on_call_tool` becomes:

```python
        elif name == "brain_graph_correct":
            confirmation = None
            if arguments.get("op") != "undo" and arguments.get("basis") == "inferred":
                confirmation = await _confirm_correction(ctx, arguments)
            if confirmation and confirmation.get("cancelled"):
                out = {"status": "not_applied",
                       "summary": "The confirmation was dismissed, so nothing was written."}
            else:
                out = await run_tool(name, arguments,
                                     lambda: graph_correct(arguments, confirmation),
                                     confirmation=confirmation)
```

A routed call that fails after an accepted elicitation already reports through `_RoutedCallFailed` as `isError`, so the model sees "not applied". No extra code needed, but add an assertion for it to the dispatch test from Step 1 (the fake client raising `DaemonUnavailable` gives an `isError` result).

- [ ] **Step 4: Run tests (both SDK versions)**

Run: `uv run pytest tests/test_graph_correct_elicitation.py tests/test_graph_correct_tool.py tests/test_mcp_sdk_contract.py -q`
Then: `uv run --with "mcp==2.2.0" pytest tests/test_graph_correct_elicitation.py tests/test_mcp_sdk_contract.py -q`
Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_graph_correct_elicitation.py
git commit -m "feat(mcp): confirm inferred graph corrections by elicitation, falling back to dashboard approval"
```

---

### Task 8: Dashboard approval for pending corrections

**Files:**
- Modify: `mcpbrain/control_api.py` (GET `/api/corrections/pending` beside the other `do_GET` routes ~line 140; POST `/api/corrections/<id>/(apply|decline)` beside `/api/dashboard/findings/<id>/dismiss` ~line 440)
- Modify: `mcpbrain/wizard/dashboard.html` (a card after the "Daemon" card ~line 248; script beside `refresh()` ~line 423)
- Test: `tests/test_control_api_corrections.py` (create)

**Interfaces:**
- Consumes: Task 4 `approve`, `decline`, `describe`; `Store.pending_corrections`.
- Produces: `GET /api/corrections/pending -> {"pending": [{"id", "op", "summary", "reason", "created_at"}]}`; `POST /api/corrections/<id>/apply -> 200 {status: applied, ...} | 409 {status: failed|refused, error}`; `POST /api/corrections/<id>/decline -> 200 | 404`.

- [ ] **Step 1: Write the failing tests**

Model them on `tests/test_control_api_post.py` (it already starts a real control-API server on a temp store with a bearer token; reuse its fixture/helpers). Cases:

```python
def test_pending_lists_and_apply_applies(api, store):
    from mcpbrain import graph_corrections as gc
    cid = gc.submit(store, {"op": "hide", "basis": "inferred", "reason": "r",
                            "entity_id": "x"})["correction_id"]
    status, body = api.get("/api/corrections/pending")
    assert status == 200 and body["pending"][0]["id"] == cid
    assert body["pending"][0]["summary"] == "Hide x from the graph"
    status, body = api.post(f"/api/corrections/{cid}/apply", {})
    assert status == 200 and body["status"] == "applied"
    assert store.get_correction(cid)["confirmed_via"] == "dashboard"


def test_decline_and_unknown(api, store):
    from mcpbrain import graph_corrections as gc
    cid = gc.submit(store, {"op": "hide", "basis": "inferred", "reason": "r",
                            "entity_id": "x"})["correction_id"]
    assert api.post(f"/api/corrections/{cid}/decline", {})[0] == 200
    assert api.post(f"/api/corrections/{cid}/decline", {})[0] == 404
    assert api.post("/api/corrections/999/apply", {})[0] == 409


def test_routes_require_the_token(api_unauthenticated):
    assert api_unauthenticated.get("/api/corrections/pending")[0] == 401
```

(Seed an entity `x` in the fixture's store. Adapt `api.get`/`api.post` to whatever request helper `test_control_api_post.py` actually provides.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_control_api_corrections.py -v`
Expected: FAIL (404 on the new routes).

- [ ] **Step 3: Implement the routes**

GET, in `do_GET` beside `/api/graph/search`:

```python
                if self.path.split("?")[0] == "/api/corrections/pending":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain.graph_corrections import describe
                    return h_json(self, 200, {"pending": [
                        {"id": c["id"], "op": c["op"], "summary": describe(c["op"], c["payload"]),
                         "reason": c["reason"], "created_at": c["created_at"]}
                        for c in server.store.pending_corrections()]})
```

POST, beside the findings dismiss route:

```python
            m = re.match(r"^/api/corrections/(\d+)/(apply|decline)$", h.path)
            if m:
                if self.store is None:
                    return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_corrections as gc
                cid = int(m.group(1))
                if m.group(2) == "apply":
                    out = gc.approve(self.store, cid, via="dashboard")
                    return h_json(h, 200 if out["status"] == "applied" else 409, out)
                out = gc.decline(self.store, cid)
                return h_json(h, 200 if out["status"] == "declined" else 404, out)
```

- [ ] **Step 4: Dashboard card**

After the Daemon card:

```html
    <div class="card" id="corrections-card" hidden>
      <h2>Pending corrections</h2>
      <p class="empty">Graph corrections Claude inferred and wants you to confirm.</p>
      <div id="corrections-body"></div>
    </div>
```

Script, beside `refresh()` (use the existing `H` headers, `$`, and `#toast` helpers; read how `refresh()` shows a toast and copy that):

```js
async function loadCorrections(){
  try {
    const res = await fetch("/api/corrections/pending", H);
    if (!res.ok) return;
    const {pending} = await res.json();
    const card = $("corrections-card"), body = $("corrections-body");
    card.hidden = pending.length === 0;
    body.replaceChildren(...pending.map(c => {
      const row = document.createElement("div");
      row.className = "kv";
      const label = document.createElement("span");
      label.className = "k";
      label.textContent = c.summary + (c.reason ? " (" + c.reason + ")" : "");
      const actions = document.createElement("span");
      actions.className = "v";
      for (const [verb, text] of [["apply", "Apply"], ["decline", "Decline"]]) {
        const b = document.createElement("button");
        b.textContent = text;
        b.onclick = async () => {
          const r = await fetch(`/api/corrections/${c.id}/${verb}`,
                                {method: "POST", headers: H.headers, body: "{}"});
          const out = await r.json().catch(() => ({}));
          toast(r.ok ? `${text}: ${c.summary}` : (out.error || "Failed"));
          loadCorrections();
        };
        actions.appendChild(b);
      }
      row.append(label, actions);
      return row;
    }));
  } catch (e) { /* the rest of the dashboard must keep working */ }
}
```

Call `loadCorrections()` wherever `refresh()` is first called and on the same interval. Use `textContent` only (never `innerHTML`): the summary contains model-supplied text, and the graph page's stored-XSS fix (0.7.87) is the precedent. If the page's toast helper has a different name, use that name.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_control_api_corrections.py tests/test_control_api_post.py tests/test_control_api_reads.py -q`
Expected: PASS. Then open the dashboard locally (`mcpbrain dashboard` or the daemon's dashboard URL) against a store with one pending correction and click Apply once; confirm the row disappears and `brain_proactive` no longer lists the finding.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/control_api.py mcpbrain/wizard/dashboard.html tests/test_control_api_corrections.py
git commit -m "feat(dashboard): approve or decline pending graph corrections"
```

---

## Part B — Entity resources

### Task 9: Live client check (throwaway probe)

**Files:**
- Create (throwaway, NOT committed): `$SCRATCH/probe_server.py` where `$SCRATCH` is the session scratchpad directory
- Modify: the spec's "Client support" section (commit the findings)

**Interfaces:**
- Produces: a recorded answer, per client (Claude Code CLI, Claude Desktop), to: (a) do static custom-scheme resources appear in the @-picker and read correctly? (b) is `resources/templates/list` called, and can the user reach a templated URI? (c) is `completion/complete` called, for a template variable and for a prompt argument?

- [ ] **Step 1: Write the probe**

```python
"""Throwaway MCP probe: which resource features does each client really use?"""
import asyncio
import datetime
import sys

import mcp.server.stdio
from mcp import types
from mcp.server import Server

LOG = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mcp-probe.log"


def log(msg):
    with open(LOG, "a") as f:
        f.write(f"{datetime.datetime.now().isoformat()} {msg}\n")


async def on_list_resources(ctx, params):
    log("resources/list")
    return types.ListResourcesResult(resources=[types.Resource(
        uri="probe://entity/dana-okafor", name="Dana Okafor", title="Dana Okafor, person",
        mimeType="text/markdown")])


async def on_list_resource_templates(ctx, params):
    log("resources/templates/list")
    return types.ListResourceTemplatesResult(resource_templates=[types.ResourceTemplate(
        uri_template="probe://entity/{id}", name="entity", mime_type="text/markdown")])


async def on_read_resource(ctx, params):
    log(f"resources/read {params.uri}")
    return types.ReadResourceResult(contents=[types.TextResourceContents(
        uri=params.uri, mimeType="text/markdown", text=f"# probe\nread {params.uri}")])


async def on_completion(ctx, params):
    log(f"completion/complete ref={params.ref} arg={params.argument}")
    return types.CompleteResult(completion=types.Completion(values=["dana-okafor", "marcus-reyes"]))


async def on_list_prompts(ctx, params):
    return types.ListPromptsResult(prompts=[types.Prompt(
        name="probe", arguments=[types.PromptArgument(name="who", required=True)])])


async def on_get_prompt(ctx, params):
    log(f"prompts/get {params.arguments}")
    return types.GetPromptResult(messages=[types.PromptMessage(
        role="user", content=types.TextContent(type="text", text=str(params.arguments)))])


server = Server("probe", on_list_resources=on_list_resources,
                on_list_resource_templates=on_list_resource_templates,
                on_read_resource=on_read_resource, on_completion=on_completion,
                on_list_prompts=on_list_prompts, on_get_prompt=on_get_prompt)


async def main():
    async with mcp.server.stdio.stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

asyncio.run(main())
```

- [ ] **Step 2: Claude Code check**

From a scratch directory (not this repo): `claude mcp add probe --scope local -- <repo>/.venv/bin/python $SCRATCH/probe_server.py $SCRATCH/probe.log`. Start `claude`. Type `@` and look for "Dana Okafor"; select it and ask "what did that say?". Type `@probe:probe://entity/marcus-reyes` and check whether it reads. Type `/probe` and see whether argument completion offers values. Read `$SCRATCH/probe.log`. Then `claude mcp remove probe --scope local`.

- [ ] **Step 3: Claude Desktop check (ask Josh first)**

Adding a server to `claude_desktop_config.json` changes his live Desktop setup, so **ask Josh before editing it**. If he agrees: back up the file, add `"probe": {"command": "<repo>/.venv/bin/python", "args": ["$SCRATCH/probe_server.py", "$SCRATCH/probe.log"]}` under `mcpServers`, restart Desktop, try the attach-from-MCP menu and the prompt, read the log, then restore the backup and restart Desktop. Confirm the `mcpbrain` connector entry is byte-identical to before (diff against the backup).

- [ ] **Step 4: Record and commit**

Replace the spec's "Client support (researched 2026-09-24, partly unverified)" paragraph with a small table: client × {static resource listed, static resource read, templates listed, templated URI readable, completion called (template), completion called (prompt)} with yes/no/not tested and the date. If neither client calls templates or completion, add one sentence: "Tasks 10-11 still ship the template and completion handlers (spec-correct, cheap), but they are inert in today's clients; the user-visible win is the static top-N." Delete the probe files.

```bash
git add docs/superpowers/specs/2026-09-24-graph-corrections-and-entity-resources-design.md
git commit -m "docs(spec): record measured client support for resources, templates and completion"
```

---

### Task 10: Daemon side of entity resources

**Files:**
- Create: `mcpbrain/entity_resource.py`
- Modify: `mcpbrain/control_api.py` (three GET routes in `do_GET`), `mcpbrain/control_client.py` (three methods)
- Test: `tests/test_entity_resource.py` (create)

**Interfaces:**
- Produces:
  - `entity_resource.TOP_N = 100`, `RESOURCE_TYPES = ("person", "org", "project")`
  - `top_entities(store, limit: int = TOP_N, *, today: str | None = None) -> list[dict]` → `[{"id", "name", "type", "org"}]`, cached per (store path, UTC date)
  - `resolve_id(store, entity_id: str) -> tuple[str, str | None] | None` → `(live_id, merged_from_or_None)`, following `entity_merge_log` chains (max 10 hops); `None` if unknown or suppressed
  - `render_markdown(store, entity_id: str) -> dict | None` → `{"id": live_id, "markdown": str}`
  - `reply_needed_ids(store, prefix: str, limit: int = 20) -> list[str]` (message ids for `draft-reply` completion)
  - control API: `GET /api/resources/entities`, `GET /api/resources/entity/<id>`, `GET /api/resources/reply-needed?q=`
  - `ControlClient.entity_resources() -> list[dict]`, `entity_resource(entity_id) -> dict | None`, `search_entities(q) -> list[dict]` (wraps the existing `/api/graph/search`), `reply_needed(q) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
"""Daemon-side entity resources (Task 10)."""
from mcpbrain import entity_resource as er
from mcpbrain import graph_write as gw
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "g.sqlite3", dim=4)
    s.init()
    with s._connect(write=True) as db:
        rows = [("dana-okafor", "Dana Okafor", "person", "Northgate Trust", 30),
                ("northgate-trust", "Northgate Trust", "org", "", 50),
                ("topic-budget", "budget", "topic", "", 99),
                ("hidden", "Hidden Person", "person", "", 80),
                ("marcus-reyes", "Marcus Reyes", "person", "Northgate Trust", 5)]
        for eid, name, typ, org, deg in rows:
            db.execute("INSERT INTO entities(id,name,type,org,degree) VALUES(?,?,?,?,?)",
                       (eid, name, typ, org, deg))
        db.execute("INSERT INTO entity_suppressions(entity_id,reason) VALUES('hidden','junk')")
    gw.upsert_relation(s, "dana-okafor", "works_at", "northgate-trust", valid_from="2026-01-01")
    gw.upsert_relation(s, "marcus-reyes", "reports_to", "dana-okafor", valid_from="2026-01-01")
    return s


def test_top_entities_ranks_by_degree_and_filters(tmp_path):
    s = _store(tmp_path)
    ids = [e["id"] for e in er.top_entities(s, today="2026-09-24")]
    assert ids == ["northgate-trust", "dana-okafor", "marcus-reyes"]  # no topic, no suppressed


def test_top_entities_is_cached_per_day(tmp_path):
    s = _store(tmp_path)
    first = er.top_entities(s, today="2026-09-24")
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET degree=999 WHERE id='marcus-reyes'")
    assert er.top_entities(s, today="2026-09-24") == first
    assert er.top_entities(s, today="2026-09-25")[0]["id"] == "marcus-reyes"


def test_render_markdown_has_header_role_and_grouped_relations(tmp_path):
    s = _store(tmp_path)
    gw.write_role_observation(s, "dana-okafor", "Operations Lead", "manual", "2026-01-01", "high")
    out = er.render_markdown(s, "dana-okafor")
    md = out["markdown"]
    assert md.startswith("# Dana Okafor")
    assert "person, Northgate Trust" in md and "Operations Lead" in md
    assert "## works_at" in md and "Northgate Trust (northgate-trust)" in md
    assert "## reports_to" in md and "Marcus Reyes (marcus-reyes)" in md


def test_merged_away_id_resolves_to_survivor(tmp_path):
    s = _store(tmp_path)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('d-okafor','D Okafor','person')")
    s.merge_entities("d-okafor", "dana-okafor")
    assert er.resolve_id(s, "d-okafor") == ("dana-okafor", "d-okafor")
    out = er.render_markdown(s, "d-okafor")
    assert out["id"] == "dana-okafor" and "Merged from d-okafor" in out["markdown"]


def test_unknown_and_suppressed_render_none(tmp_path):
    s = _store(tmp_path)
    assert er.render_markdown(s, "nobody") is None
    assert er.render_markdown(s, "hidden") is None


def test_relation_groups_are_bounded(tmp_path, monkeypatch):
    s = _store(tmp_path)
    monkeypatch.setattr(er, "MAX_RELATIONS", 1)
    md = er.render_markdown(s, "dana-okafor")["markdown"]
    assert "more relations not shown" in md
```

Plus a control-API test in the same style as Task 8 (reuse the `test_control_api_post.py` fixture): `GET /api/resources/entities` returns the top list, `GET /api/resources/entity/dana-okafor` returns `{"id", "markdown"}`, an unknown id returns 404, and all three need the bearer token.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_entity_resource.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Write `mcpbrain/entity_resource.py`**

```python
"""Entities as MCP resources (mcpbrain://entity/<id>): the daemon side.

The MCP server asks for three things over the control API: the top-N list it
advertises in resources/list, one entity rendered as markdown for
resources/read, and reply-needed ids for draft-reply completion. The list is
cached per UTC day so the MCP server's resource fingerprint (and so
notifications/resources/list_changed) moves about once a day, not on every
degree shift.

No mcp / Store / native imports at module scope.
"""
from datetime import datetime, timezone

TOP_N = 100
RESOURCE_TYPES = ("person", "org", "project")
MAX_RELATIONS = 150
MAX_PER_GROUP = 40
MAX_OBSERVATIONS = 15
_MERGE_HOPS = 10

_cache: dict[tuple[str, str], list[dict]] = {}


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def top_entities(store, limit: int = TOP_N, *, today: str | None = None) -> list[dict]:
    key = (str(store.path), today or _today())
    if key not in _cache:
        _cache.clear()  # one day's list at a time
        q = ",".join("?" * len(RESOURCE_TYPES))
        with store._connect() as db:
            rows = db.execute(
                f"SELECT e.id, e.name, e.type, COALESCE(e.org,'') AS org FROM entities e "
                f"LEFT JOIN entity_suppressions s ON s.entity_id = e.id "
                f"WHERE e.type IN ({q}) AND s.entity_id IS NULL "
                f"ORDER BY COALESCE(e.degree,0) DESC, e.id LIMIT ?",
                (*RESOURCE_TYPES, int(limit))).fetchall()
        _cache[key] = [dict(r) for r in rows]
    return list(_cache[key])


def resolve_id(store, entity_id: str) -> tuple[str, str | None] | None:
    with store._connect() as db:
        current, hops = entity_id, 0
        while db.execute("SELECT 1 FROM entities WHERE id=?", (current,)).fetchone() is None:
            row = db.execute("SELECT winner_id FROM entity_merge_log WHERE loser_id=? "
                             "ORDER BY id DESC LIMIT 1", (current,)).fetchone()
            hops += 1
            if row is None or hops > _MERGE_HOPS:
                return None
            current = row[0]
        if db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?",
                      (current,)).fetchone():
            return None
    return current, (entity_id if current != entity_id else None)


def render_markdown(store, entity_id: str) -> dict | None:
    resolved = resolve_id(store, entity_id)
    if resolved is None:
        return None
    eid, merged_from = resolved
    ent = store.get_entity(eid)
    rels = store.relations_for(eid)
    others = store.get_entities({r["entity_b"] if r["entity_a"] == eid else r["entity_a"]
                                 for r in rels})
    with store._connect() as db:
        obs = db.execute(
            "SELECT attribute, value, source, valid_from FROM entity_observations "
            "WHERE entity_id=? AND valid_to IS NULL "
            "AND (invalidated_at IS NULL OR invalidated_at='') AND attribute != 'occurrence' "
            "ORDER BY valid_from DESC LIMIT ?", (eid, MAX_OBSERVATIONS)).fetchall()
    role = next((o["value"] for o in obs if o["attribute"] == "role"), "")
    header = ", ".join(x for x in (ent["type"], ent.get("org") or "") if x)
    lines = [f"# {ent['name']}", "", f"*{header}*" + (f" · {role}" if role else "")]
    if merged_from:
        lines += ["", f"Merged from {merged_from}."]
    if ent.get("email_addr"):
        lines += ["", f"Email: {ent['email_addr']}"]
    groups: dict[str, list[str]] = {}
    for r in rels[:MAX_RELATIONS]:
        out = r["entity_a"] == eid
        other_id = r["entity_b"] if out else r["entity_a"]
        other = others.get(other_id, {})
        label = f"{other.get('name', other_id)} ({other_id})"
        groups.setdefault(r["relation"] if out else f"{r['relation']} (from)", []).append(label)
    for rel, labels in sorted(groups.items()):
        lines += ["", f"## {rel}"] + [f"- {x}" for x in labels[:MAX_PER_GROUP]]
        if len(labels) > MAX_PER_GROUP:
            lines.append(f"- … {len(labels) - MAX_PER_GROUP} more")
    if len(rels) > MAX_RELATIONS:
        lines += ["", f"_{len(rels) - MAX_RELATIONS} more relations not shown; "
                      f"use brain_graph for the full neighbourhood._"]
    facts = [o for o in obs if o["attribute"] != "role"]
    if facts:
        lines += ["", "## Recent facts"] + [
            f"- {o['attribute']}: {o['value']} ({o['valid_from'] or 'undated'})" for o in facts]
    return {"id": eid, "markdown": "\n".join(lines) + "\n"}


def reply_needed_ids(store, prefix: str, limit: int = 20) -> list[str]:
    with store._connect() as db:
        rows = db.execute(
            "SELECT message_id FROM email_context WHERE reply_needed=1 AND message_id LIKE ? "
            "ESCAPE '\\' ORDER BY date_iso DESC LIMIT ?",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",
             int(limit))).fetchall()
    return [r[0] for r in rows]
```

Check `Store.path` is the attribute name (graph_view uses `store._path if hasattr(store, "_path") else store.path`); use the same expression if `path` is not always present. Check `relations_for` returns rows with `entity_a`, `entity_b`, `relation` keys (it does for `brain_context`).

- [ ] **Step 4: Routes and client methods**

In `do_GET`, beside `/api/graph/search` (same 503 guard):

```python
                if self.path.split("?")[0] == "/api/resources/entities":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import entity_resource
                    return h_json(self, 200, {"entities": entity_resource.top_entities(server.store)})
                m = re.match(r"^/api/resources/entity/([^/?]+)$", self.path.split("?")[0])
                if m:
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import entity_resource
                    d = entity_resource.render_markdown(server.store, urllib.parse.unquote(m.group(1)))
                    return h_json(self, 200, d) if d else h_json(self, 404, {"error": "not found"})
                if self.path.split("?")[0] == "/api/resources/reply-needed":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import entity_resource
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                    return h_json(self, 200, {"ids": entity_resource.reply_needed_ids(server.store, q)})
```

`ControlClient` (follow `status()`'s use of `_request`; a 404 surfaces however `_request` reports it, so read `_request` and map "not found" to `None`):

```python
    def entity_resources(self) -> list[dict]:
        return self._request("/api/resources/entities").get("entities", [])

    def entity_resource(self, entity_id: str) -> dict | None:
        from urllib.parse import quote
        try:
            return self._request(f"/api/resources/entity/{quote(entity_id, safe='')}")
        except DaemonUnavailable as exc:
            if "404" in str(exc):
                return None
            raise

    def search_entities(self, q: str) -> list[dict]:
        from urllib.parse import quote
        return self._request(f"/api/graph/search?q={quote(q)}")

    def reply_needed(self, q: str) -> list[str]:
        from urllib.parse import quote
        return self._request(f"/api/resources/reply-needed?q={quote(q)}").get("ids", [])
```

If `_request` raises a different exception type for HTTP 404, match that type instead; the unit test for `entity_resource` (404 → `None`) must pin whichever it is.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_entity_resource.py tests/test_control_api_reads.py tests/test_control_client.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/entity_resource.py mcpbrain/control_api.py mcpbrain/control_client.py tests/test_entity_resource.py
git commit -m "feat(daemon): entity resources (top-N, markdown profile, merged-id resolution)"
```

---

### Task 11: MCP server: `mcpbrain://` resources, template and completion

**Files:**
- Modify: `mcpbrain/mcp_server.py` (`list_context_resources` caller, `on_read_resource`, new `on_list_resource_templates` and `on_completion`, `_resource_fingerprint`, `build_server` kwargs)
- Test: `tests/test_mcp_entity_resources.py` (create); extend `tests/test_mcp_sdk_contract.py`

**Interfaces:**
- Consumes: Task 10 `ControlClient` methods.
- Produces: `ENTITY_SCHEME = "mcpbrain://entity/"`; `async def list_entity_resources(client) -> list[types.Resource]`; `async def read_entity_resource(client, uri: str) -> str`; handlers registered on the `Server`.

- [ ] **Step 1: Write the failing tests**

```python
"""mcpbrain://entity resources, template and completion in the MCP server (Task 11)."""
import asyncio

import pytest

from mcpbrain import mcp_server


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    # list_entity_resources caches for 10 minutes; each test starts cold.
    monkeypatch.setattr(mcp_server, "_entity_list_cache", None)


class _Client:
    def __init__(self, down=False):
        self.down = down

    def _check(self):
        if self.down:
            from mcpbrain.control_client import DaemonUnavailable
            raise DaemonUnavailable("down")

    def entity_resources(self):
        self._check()
        return [{"id": "dana-okafor", "name": "Dana Okafor", "type": "person", "org": "Northgate Trust"}]

    def entity_resource(self, eid):
        self._check()
        return {"id": "dana-okafor", "markdown": "# Dana Okafor\n"} if eid in ("dana-okafor", "d-okafor") else None

    def search_entities(self, q):
        return [{"id": "dana-okafor"}, {"id": "dana-smith"}]

    def reply_needed(self, q):
        return ["19a2b3"]


def test_lists_entities_with_titles():
    res = asyncio.run(mcp_server.list_entity_resources(_Client()))
    assert [str(r.uri) for r in res] == ["mcpbrain://entity/dana-okafor"]
    assert res[0].name == "Dana Okafor" and res[0].title == "Dana Okafor, person, Northgate Trust"


def test_daemon_down_lists_nothing_rather_than_failing():
    assert asyncio.run(mcp_server.list_entity_resources(_Client(down=True))) == []


def test_read_entity_and_unknown():
    assert asyncio.run(mcp_server.read_entity_resource(_Client(), "mcpbrain://entity/d-okafor")) == "# Dana Okafor\n"
    with pytest.raises(ValueError, match="unknown entity"):
        asyncio.run(mcp_server.read_entity_resource(_Client(), "mcpbrain://entity/nobody"))


def test_schemes_are_isolated(tmp_path, monkeypatch):
    """A file:// uri never reaches the daemon; a mcpbrain:// uri never reads a file."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    with pytest.raises(ValueError):
        asyncio.run(mcp_server.read_entity_resource(_Client(), f"file://{tmp_path}/config.json"))
    with pytest.raises(ValueError):
        asyncio.run(mcp_server.read_context_resource("mcpbrain://entity/dana-okafor"))
```

Extend `tests/test_mcp_sdk_contract.py`: `build_server(...)` registers handlers for `resources/templates/list` and `completion/complete` (use whatever introspection that file already uses to pin `resources/list`). Add a protocol round-trip in `tests/test_mcp_entity_resources.py` using the `protocol_session` fixture from `tests/conftest.py`: `list_resource_templates()` returns `mcpbrain://entity/{id}`, and `complete(ref=ResourceTemplateReference(uri="mcpbrain://entity/{id}"), argument={"name": "id", "value": "da"})` returns a `CompleteResult` (the subprocess has no daemon, so expect an empty `values` list, which also proves the "empty on failure" rule).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_mcp_entity_resources.py -v`
Expected: FAIL (`AttributeError: list_entity_resources`).

- [ ] **Step 3: Implement**

Module level:

```python
ENTITY_SCHEME = "mcpbrain://entity/"
ENTITY_TEMPLATE = ENTITY_SCHEME + "{id}"
_ENTITY_LIST_TTL_S = 600.0
_entity_list_cache: tuple[float, list] | None = None


async def list_entity_resources(client) -> list:
    """The daemon's top-N entities as resources, cached 10 minutes here (the
    daemon's list itself only changes daily). Daemon unreachable -> [] so the
    file:// resources still list."""
    import asyncio
    import time
    from mcp import types
    global _entity_list_cache
    now = time.monotonic()
    if _entity_list_cache is None or now - _entity_list_cache[0] > _ENTITY_LIST_TTL_S:
        try:
            ents = await asyncio.to_thread(client.entity_resources)
        except Exception:  # noqa: BLE001 -- daemon down: list what we can
            _log.debug("entity resources unavailable", exc_info=True)
            return []
        _entity_list_cache = (now, ents)
    return [
        types.Resource(
            uri=f"{ENTITY_SCHEME}{e['id']}", name=e["name"],
            title=", ".join(x for x in (e["name"], e["type"], e.get("org") or "") if x),
            mimeType="text/markdown")
        for e in _entity_list_cache[1]
    ]


async def read_entity_resource(client, uri) -> str:
    import asyncio
    from urllib.parse import unquote
    uri = str(uri)
    if not uri.startswith(ENTITY_SCHEME):
        raise ValueError(f"not an entity resource: {uri}")
    eid = unquote(uri[len(ENTITY_SCHEME):])
    if not eid or "/" in eid:
        raise ValueError(f"malformed entity resource: {uri}")
    out = await asyncio.to_thread(client.entity_resource, eid)
    if not out:
        raise ValueError(f"unknown entity: {eid}")
    return out["markdown"]
```

`read_context_resource` needs no change for isolation: its allowlist is `file://` paths only, so a `mcpbrain://` uri resolves to a path not in the allowlist and raises. The test pins that.

In `build_server`:

```python
    async def on_list_resources(ctx, params) -> types.ListResourcesResult:
        await _ensure_watcher(ctx)
        return types.ListResourcesResult(
            resources=await list_context_resources() + await list_entity_resources(client))

    async def on_read_resource(ctx, params) -> types.ReadResourceResult:
        # Dispatch on scheme: each reader has its own guard and neither can reach
        # the other's namespace.
        if str(params.uri).startswith(ENTITY_SCHEME):
            text = await read_entity_resource(client, params.uri)
        else:
            text = await read_context_resource(params.uri)
        return types.ReadResourceResult(contents=[
            types.TextResourceContents(uri=params.uri, mimeType="text/markdown", text=text)])

    async def on_list_resource_templates(ctx, params) -> types.ListResourceTemplatesResult:
        return types.ListResourceTemplatesResult(resource_templates=[types.ResourceTemplate(
            uri_template=ENTITY_TEMPLATE, name="entity", title="Person, organisation or project",
            description="A profile from your mcpbrain knowledge graph, by entity id.",
            mime_type="text/markdown")])

    async def on_completion(ctx, params) -> types.CompleteResult:
        import asyncio
        values: list[str] = []
        try:
            ref, arg = params.ref, params.argument
            if getattr(ref, "type", "") == "ref/resource" and ref.uri == ENTITY_TEMPLATE \
                    and arg.name == "id":
                values = [e["id"] for e in await asyncio.to_thread(client.search_entities, arg.value)]
            elif getattr(ref, "type", "") == "ref/prompt" and ref.name == "draft-reply" \
                    and arg.name == "email_id":
                values = await asyncio.to_thread(client.reply_needed, arg.value)
        except Exception:  # noqa: BLE001 -- completion is best-effort, never an error
            _log.debug("completion failed", exc_info=True)
        return types.CompleteResult(completion=types.Completion(values=values[:100]))
```

Register `on_list_resource_templates=on_list_resource_templates, on_completion=on_completion` in the `Server(...)` constructor. Confirm the draft-reply prompt's registered name with `grep -n '"draft-reply"' mcpbrain/mcp_server.py` and use exactly that string.

`_resource_fingerprint()` is synchronous and file-based. Extend it with an optional argument so the watcher also notices the entity set changing:

```python
def _resource_fingerprint(entity_ids: frozenset = frozenset()) -> frozenset[str]:
    return frozenset(str(p) for _, p in _resource_entries()) | {f"{ENTITY_SCHEME}{e}" for e in entity_ids}
```

and in `watch_resources`, compute `ids = frozenset(e["id"] for e in _entity_list_cache[1]) if _entity_list_cache else frozenset()` and pass it. The daily list plus the 10-minute cache means list_changed fires at most about once a day for entities.

- [ ] **Step 4: Run tests (both SDK versions)**

Run: `uv run pytest tests/test_mcp_entity_resources.py tests/test_mcp_resources.py tests/test_mcp_sdk_contract.py tests/test_mcp_server_stdio.py tests/test_mcp_server_no_native.py -q`
Then: `uv run --with "mcp==2.2.0" pytest tests/test_mcp_entity_resources.py tests/test_mcp_sdk_contract.py -q`
Expected: PASS both.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/mcp_server.py tests/test_mcp_entity_resources.py tests/test_mcp_sdk_contract.py
git commit -m "feat(mcp): entities as mcpbrain:// resources, with a template and completion"
```

---

### Task 12: Live verification on this machine

**Files:**
- Modify: `CLAUDE.md` (a short "Graph corrections + entity resources (source-only, NOT released)" section recording what was verified, following the existing sections' style)

Nothing here is released. This task proves the work against the real store and real clients.

- [ ] **Step 1: Snapshot and reinstall**

Confirm a fresh verified backup exists (`mcpbrain doctor` backup line, or take one). Then, per the three install traps in CLAUDE.md:

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain
pgrep -fl "mcpbrain" || echo "no daemon"          # must print "no daemon"
uv tool install --reinstall --force ".[daemon]"
find "$(uv tool dir)/mcpbrain" -name __pycache__ -type d -prune -exec rm -rf {} +
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
```

Verify against the running process: `/api/status` (with `Authorization: Bearer $(cat "$MCPBRAIN_HOME/control_token")`) reports the version, and `grep -n "def _merge_entities_tx" "$(uv tool dir)/mcpbrain/lib/python3.12/site-packages/mcpbrain/store.py"` finds the new code (run it from a neutral directory, not the repo).

- [ ] **Step 2: Migration landed**

Read-only against the live store: `graph_corrections`, `entity_distinct_pairs`, `entity_field_locks` exist and `entity_relations` has `user_verdict`. Run `PRAGMA integrity_check` (expect `ok`) and `PRAGMA foreign_key_check` (expect no rows).

- [ ] **Step 3: Each op once, through a real MCP session, then undone**

In a fresh Claude Code session, pick a low-stakes test entity pair from `brain_graph`/`brain_context`, and run through `brain_graph_correct` with `basis: user_stated`: `hide` → `undo`; `not_same` → `undo`; `set_field` (org) → confirm `update_entity_org` refuses it → `undo`; `reject_relation` → trigger a re-extraction path, or run `graph_write.upsert_relation` against a copy of the store, to confirm it stays rejected → `undo`; `assert_relation` → `undo`; `merge` of two genuine duplicates → inspect → `undo` → confirm both entities and their relation counts match the before numbers. Then `basis: inferred` once: Claude Code should show the elicitation (accept it and confirm `confirmed_via='elicitation'`), and once from Claude Desktop, where it should land as pending. Approve that one on the dashboard. Record every correction id and outcome.

- [ ] **Step 4: Integrity and gold gate**

`PRAGMA integrity_check` ok, `foreign_key_check` 0 rows, `mcpbrain doctor` clean. Run the gold gate the way the repo does (`uv run pytest tests/eval -q` with the tenant gold set present) and confirm recall@10 / MRR are unchanged from before this work: corrections do not touch retrieval.

- [ ] **Step 5: Resources in the real clients**

Claude Code: `@` shows mcpbrain entities; reading one returns the markdown profile. Claude Desktop: the attach menu shows them. Template and completion behave as Task 9 recorded.

- [ ] **Step 6: Record and commit**

Add the CLAUDE.md section: what shipped (source only), the live results from Steps 2-5 with correction ids, integrity and gold numbers, the measured client support, and the rule "a user correction is sticky by design: automated writers skip locked fields and rejected triples; undo is the only way back".

```bash
git add CLAUDE.md
git commit -m "docs: record live verification of graph corrections and entity resources"
```
