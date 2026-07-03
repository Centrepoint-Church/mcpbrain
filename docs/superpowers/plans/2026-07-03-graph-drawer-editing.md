# Graph Entity Drawer + Correction Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the entity detail drawer to `/graph` — click a node to see its full picture (identity, relations, backlinks, observations) and **correct its facts** (name/org/email/notes via edit-mode), **merge** duplicates (type-ahead target + role-inbox guard), and **suppress** (reversible), completing the interactive-graph spec.

**Architecture:** New pure functions in `mcpbrain/graph_view.py` (`entity_detail`, `search_entities`, `update_entity`, `merge_entities`, `suppress_entity`) over the store; three new store setters (`rename_entity`, `set_entity_email`, `set_entity_notes`); five new bearer-gated routes in `control_api.py` running in the daemon (single writer) and audited to `change_log`; and a drawer added to the existing `mcpbrain/wizard/graph.html`. No new dependencies.

**Tech Stack:** Python stdlib (`http.server`, read-only SQLite via `dashboard._open_ro`), the store, vanilla JS in `graph.html` (force-graph already vendored).

## Global Constraints

- **Self-contained / offline:** no CDN/npm/build; drawer is inline CSS/JS in `graph.html`.
- **No new graph objects.** Corrections only — edit existing entity fields (name/org/email/notes), merge, suppress. **No** creating entities, relations, or observations.
- **Soft-delete only:** "delete" = `store.suppress_entity` (reversible). No hard delete.
- **Mutations run in the daemon** (the single writer) via `server.store`, exactly like `mark_done`/`resolve_finding`, and each records to `change_log` via `store.record_change`.
- **Merge guard:** refuse self-merge (`loser_id == winner_id`) and refuse if either entity's `email_addr` is a role address (`resolve.is_role_address`) — surfaced as HTTP 409.
- **Bearer-gated:** all `/api/graph/*` require `Authorization: Bearer <token>` (the `/graph` page + `/vendor/*` stay pre-auth).
- **Editing UX:** edit-mode with explicit Save/Cancel (not always-inline). **Merge UX:** type-ahead search (`/api/graph/search`), not click-on-canvas.
- **Verification:** backend is TDD (pytest). Frontend has no unit harness — verify via `node --check`, static markers in `tests/test_graph_page.py`, and a stubbed-endpoint render harness (artifact).

**Verified code facts (live):** `store.get_entity(id)`→dict|None; `store.relations_for(id)` exists but omits `strength`, so `entity_detail` does its own read-only join for names+strength; `store.merge_entities(loser, winner, *, canonical_name=None, method="deterministic")`→None (no-ops on self/missing); `store.suppress_entity(id, reason="")`→bool (writes `entity_suppressions`); `store.update_entity_org(id, org, org_valid_from="")`→bool; `store.record_change(change_type, *, ref_id="", summary="")`; `resolve.is_role_address(email)`→bool. `entities` has `aliases TEXT`, `notes TEXT`, `email_addr TEXT`. `entity_observations(entity_id, attribute, value, source, valid_from, valid_to, confidence)`.

## File Structure

- **Modify** `mcpbrain/store.py` — add `rename_entity`, `set_entity_email`, `set_entity_notes`.
- **Modify** `mcpbrain/graph_view.py` — add `entity_detail`, `search_entities`, `update_entity`, `merge_entities`, `suppress_entity` (the last three are thin, guarded wrappers; note `graph_view.merge_entities`/`suppress_entity` shadow the store names but take `store` as first arg).
- **Modify** `mcpbrain/control_api.py` — five new routes in `do_GET`/`do_POST`/`do_DELETE`.
- **Modify** `mcpbrain/wizard/graph.html` — the drawer (markup + CSS + JS), wired to `onNodeClick`.
- **Modify** `tests/test_graph_view.py`, `tests/test_graph_routes.py`, `tests/test_graph_page.py`, `tests/test_store.py` — coverage.

---

### Task 1: Store setters (`rename_entity`, `set_entity_email`, `set_entity_notes`)

**Files:**
- Modify: `mcpbrain/store.py` (next to `update_entity_org`, ~line 807)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `Store.rename_entity(entity_id, new_name) -> bool` (sets `name`, appends the old name to `aliases` as a `|`-separated, de-duplicated list; no-op alias add if unchanged), `Store.set_entity_email(entity_id, email_addr) -> bool`, `Store.set_entity_notes(entity_id, notes) -> bool`. All return True iff a row was updated.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_store.py`)

```python
def _seed_entity(store, eid="e1", name="Alice", **kw):
    with store._connect() as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES(?,?,'person')", (eid, name))

def test_rename_entity_sets_name_and_keeps_alias(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init(); _seed_entity(s)
    assert s.rename_entity("e1", "Alice Smith") is True
    e = s.get_entity("e1")
    assert e["name"] == "Alice Smith"
    assert "Alice" in e["aliases"].split("|")

def test_rename_entity_unknown_id(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    assert s.rename_entity("nope", "X") is False

def test_set_entity_email_and_notes(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init(); _seed_entity(s)
    assert s.set_entity_email("e1", "alice@acme.com") is True
    assert s.set_entity_notes("e1", "prefers email") is True
    e = s.get_entity("e1")
    assert e["email_addr"] == "alice@acme.com" and e["notes"] == "prefers email"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_store.py -k "rename_entity or set_entity" -q`
Expected: FAIL (`AttributeError: 'Store' object has no attribute 'rename_entity'`).

- [ ] **Step 3: Implement the setters** in `mcpbrain/store.py` (place immediately after `update_entity_org`)

```python
    def rename_entity(self, entity_id: str, new_name: str) -> bool:
        """Rename an entity, preserving the old name as an alias.

        aliases is a '|'-separated, de-duplicated list. The old name is appended
        (case-preserving) unless it already equals the new name or is already
        present. Returns True iff the entity exists and was updated.
        """
        new_name = (new_name or "").strip()
        if not new_name:
            return False
        with self._connect() as db:
            row = db.execute("SELECT name, COALESCE(aliases,'') AS aliases "
                             "FROM entities WHERE id=?", (entity_id,)).fetchone()
            if row is None:
                return False
            old = (row["name"] or "").strip()
            parts = [a for a in row["aliases"].split("|") if a]
            if old and old != new_name and old not in parts:
                parts.append(old)
            db.execute("UPDATE entities SET name=?, aliases=? WHERE id=?",
                       (new_name, "|".join(parts), entity_id))
            return True

    def set_entity_email(self, entity_id: str, email_addr: str) -> bool:
        """Set an entity's email_addr. Returns True iff a row was updated."""
        with self._connect() as db:
            cur = db.execute("UPDATE entities SET email_addr=? WHERE id=?",
                             (email_addr or "", entity_id))
            return cur.rowcount > 0

    def set_entity_notes(self, entity_id: str, notes: str) -> bool:
        """Set an entity's notes. Returns True iff a row was updated."""
        with self._connect() as db:
            cur = db.execute("UPDATE entities SET notes=? WHERE id=?",
                             (notes or "", entity_id))
            return cur.rowcount > 0
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_store.py -k "rename_entity or set_entity" -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): rename_entity (name+alias), set_entity_email, set_entity_notes"
```

---

### Task 2: `entity_detail` + `search_entities` (read layer)

**Files:**
- Modify: `mcpbrain/graph_view.py`
- Test: `tests/test_graph_view.py`

**Interfaces:**
- Consumes: `dashboard._open_ro` (already imported in `graph_view`).
- Produces:
  - `entity_detail(store, entity_id) -> dict | None` → `{id, name, type, org, email_addr, aliases, notes, connections, relations:[{other_id,other_name,relation,strength,direction}], backlinks:[{other_id,other_name,relation,strength,direction}], observations:[{attribute,value,valid_from,valid_to,source}]}`; `None` if the entity is unknown or suppressed.
  - `search_entities(store, q, limit=10) -> list[dict]` → `[{id,name,type,org}]`; `[]` if `q` is blank.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_view.py`; reuse the file's `_Store` shim — a class exposing `_path`)

```python
def _seed_detail(path):
    import sqlite3
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', email_addr TEXT DEFAULT '', aliases TEXT DEFAULT '', "
                   "notes TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1, invalidated_at TEXT)")
        db.execute("CREATE TABLE entity_observations(id INTEGER PRIMARY KEY, entity_id TEXT, "
                   "attribute TEXT, value TEXT, source TEXT, valid_from TEXT, valid_to TEXT)")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.executemany("INSERT INTO entities(id,name,type,org,email_addr,degree) VALUES(?,?,?,?,?,?)", [
            ("e1","Alice","person","Acme","alice@acme.com",5),
            ("e2","Bob","person","Acme","",3),
            ("e3","Acme","org","","",9)])
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) VALUES(1,'e1','works_at','e3',4)")
        db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) VALUES(2,'e2','manages','e1',2)")
        db.execute("INSERT INTO entity_observations(id,entity_id,attribute,value,valid_from) VALUES(1,'e1','role','Lead','2026-01')")

def test_entity_detail_shape(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    d = graph_view.entity_detail(_Store(p), "e1")
    assert d["name"] == "Alice" and d["org"] == "Acme" and d["email_addr"] == "alice@acme.com"
    assert {r["other_id"] for r in d["relations"]} == {"e3"}          # e1 -> e3 (out)
    assert d["relations"][0]["other_name"] == "Acme" and d["relations"][0]["relation"] == "works_at"
    assert {b["other_id"] for b in d["backlinks"]} == {"e2"}          # e2 -> e1 (in)
    assert d["observations"][0]["attribute"] == "role"

def test_entity_detail_unknown_is_none(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    assert graph_view.entity_detail(_Store(p), "nope") is None

def test_entity_detail_suppressed_is_none(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    import sqlite3
    with sqlite3.connect(str(p)) as db:
        db.execute("INSERT INTO entity_suppressions(entity_id,reason,suppressed_at) VALUES('e1','x','t')")
    assert graph_view.entity_detail(_Store(p), "e1") is None

def test_search_entities(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed_detail(p)
    ids = {r["id"] for r in graph_view.search_entities(_Store(p), "a")}
    assert "e1" in ids and "e3" in ids          # Alice, Acme
    assert graph_view.search_entities(_Store(p), "") == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_graph_view.py -k "entity_detail or search_entities" -q`
Expected: FAIL (`AttributeError: module 'mcpbrain.graph_view' has no attribute 'entity_detail'`).

- [ ] **Step 3: Implement** — add to `mcpbrain/graph_view.py` (after `graph_canvas`)

```python
def _table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (name,)).fetchone() is not None


def entity_detail(store, entity_id: str) -> dict | None:
    """Full drawer payload for one entity, or None if unknown/suppressed."""
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            if _table_exists(db, "entity_suppressions"):
                if db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?",
                              (entity_id,)).fetchone():
                    return None
            e = db.execute("SELECT * FROM entities WHERE id=?", (entity_id,)).fetchone()
            if e is None:
                return None
            rels, backs = [], []
            rows = db.execute(
                "SELECT r.entity_a, r.relation, r.entity_b, COALESCE(r.strength,1) AS strength, "
                "       ea.name AS a_name, eb.name AS b_name "
                "FROM entity_relations r "
                "LEFT JOIN entities ea ON ea.id=r.entity_a "
                "LEFT JOIN entities eb ON eb.id=r.entity_b "
                "WHERE (r.entity_a=:id OR r.entity_b=:id) AND r.invalidated_at IS NULL "
                "ORDER BY r.strength DESC",
                {"id": entity_id}).fetchall()
            for r in rows:
                if r["entity_a"] == entity_id:
                    rels.append({"other_id": r["entity_b"], "other_name": r["b_name"] or r["entity_b"],
                                 "relation": r["relation"], "strength": r["strength"], "direction": "out"})
                else:
                    backs.append({"other_id": r["entity_a"], "other_name": r["a_name"] or r["entity_a"],
                                  "relation": r["relation"], "strength": r["strength"], "direction": "in"})
            obs = []
            if _table_exists(db, "entity_observations"):
                obs = [dict(o) for o in db.execute(
                    "SELECT attribute, value, valid_from, valid_to, source "
                    "FROM entity_observations WHERE entity_id=? "
                    "ORDER BY COALESCE(valid_from,'') DESC, id DESC", (entity_id,)).fetchall()]
            return {
                "id": e["id"], "name": e["name"], "type": e["type"],
                "org": e["org"] or "", "email_addr": e["email_addr"] or "",
                "aliases": e["aliases"] or "", "notes": e["notes"] or "",
                "connections": e["degree"] or 0,
                "relations": rels, "backlinks": backs, "observations": obs,
            }
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        log.warning("entity_detail: read failed (%s)", exc)
        return None


def search_entities(store, q: str, limit: int = 10) -> list[dict]:
    """Name search for the merge type-ahead. [] on blank q; excludes suppressed."""
    q = (q or "").strip()
    if not q:
        return []
    path = store._path if hasattr(store, "_path") else store.path
    try:
        db = _open_ro(Path(path))
        try:
            supp = ("LEFT JOIN entity_suppressions s ON s.entity_id = e.id"
                    if _table_exists(db, "entity_suppressions") else "")
            where_supp = "AND s.entity_id IS NULL" if supp else ""
            rows = db.execute(
                f"SELECT e.id, e.name, e.type, COALESCE(e.org,'') AS org "
                f"FROM entities e {supp} "
                f"WHERE lower(e.name) LIKE :q {where_supp} "
                f"ORDER BY (lower(e.name)=lower(:exact)) DESC, length(e.name) ASC "
                f"LIMIT :lim",
                {"q": f"%{q.lower()}%", "exact": q, "lim": int(limit)}).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        log.warning("search_entities: read failed (%s)", exc)
        return []
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_graph_view.py -k "entity_detail or search_entities" -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/graph_view.py tests/test_graph_view.py
git commit -m "feat(graph): entity_detail + search_entities read layer"
```

---

### Task 3: `graph_view` mutations — `update_entity`, `merge_entities`, `suppress_entity`

**Files:**
- Modify: `mcpbrain/graph_view.py`
- Test: `tests/test_graph_view.py`

**Interfaces:**
- Consumes: store setters (Task 1), `store.update_entity_org`, `store.merge_entities`, `store.suppress_entity`, `store.get_entity`, `store.record_change`, `resolve.is_role_address`, `entity_detail` (Task 2).
- Produces:
  - `update_entity(store, entity_id, *, name=None, org=None, email_addr=None, notes=None) -> dict | None` — applies only the provided fields via the store setters, records a `change_log` entry, and returns the fresh `entity_detail` (or `None` if the entity is unknown).
  - `merge_entities(store, loser_id, winner_id) -> dict` — `{ "ok": True }` on success (after `store.merge_entities` + `record_change`), or `{ "ok": False, "error": "self_merge" | "role_inbox", "message": str }` on refusal.
  - `suppress_entity(store, entity_id) -> dict` — `{ "ok": bool }` (calls `store.suppress_entity`, records change).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_view.py`). These use the real `Store` (mutations need write methods), not the RO shim:

```python
def _rw_store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "rw.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.executemany("INSERT INTO entities(id,name,type,org,email_addr) VALUES(?,?,?,?,?)", [
            ("alice","Alice","person","Acme","alice@acme.com"),
            ("al","Alice A.","person","","alice@acme.com"),
            ("office","Office","person","Acme","office@acme.com"),
            ("front","Front Desk","person","Acme","office@acme.com")])
    return s

def test_update_entity_applies_fields(tmp_path):
    s = _rw_store(tmp_path)
    d = graph_view.update_entity(s, "alice", name="Alice Smith", org="Beta", notes="vip")
    assert d["name"] == "Alice Smith" and d["org"] == "Beta" and d["notes"] == "vip"
    assert "Alice" in s.get_entity("alice")["aliases"].split("|")

def test_update_entity_unknown(tmp_path):
    s = _rw_store(tmp_path)
    assert graph_view.update_entity(s, "nope", name="X") is None

def test_merge_ok(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "al", "alice")
    assert out["ok"] is True
    assert s.get_entity("al") is None and s.get_entity("alice") is not None

def test_merge_self_refused(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "alice", "alice")
    assert out["ok"] is False and out["error"] == "self_merge"

def test_merge_role_inbox_refused(tmp_path):
    s = _rw_store(tmp_path)
    out = graph_view.merge_entities(s, "front", "office")   # both office@ -> role address
    assert out["ok"] is False and out["error"] == "role_inbox"
    assert s.get_entity("front") is not None                # not merged

def test_suppress(tmp_path):
    s = _rw_store(tmp_path)
    assert graph_view.suppress_entity(s, "al")["ok"] is True
    assert graph_view.entity_detail(s, "al") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_graph_view.py -k "update_entity or merge_ or merge_self or role_inbox or test_suppress" -q`
Expected: FAIL (attributes not defined).

- [ ] **Step 3: Implement** — add to `mcpbrain/graph_view.py`

```python
def update_entity(store, entity_id: str, *, name=None, org=None,
                  email_addr=None, notes=None) -> dict | None:
    """Correct existing fields on an entity. Returns the fresh detail, or None
    if the entity is unknown. Only provided (non-None) fields are written."""
    if store.get_entity(entity_id) is None:
        return None
    changed = []
    if name is not None and name.strip():
        store.rename_entity(entity_id, name); changed.append("name")
    if org is not None:
        store.update_entity_org(entity_id, org); changed.append("org")
    if email_addr is not None:
        store.set_entity_email(entity_id, email_addr); changed.append("email")
    if notes is not None:
        store.set_entity_notes(entity_id, notes); changed.append("notes")
    if changed:
        store.record_change("entity_edited", ref_id=entity_id,
                            summary=f"edited {', '.join(changed)}")
    return entity_detail(store, entity_id)


def merge_entities(store, loser_id: str, winner_id: str) -> dict:
    """Merge loser into winner, guarded. Refuses self-merge and role-inbox pairs."""
    from mcpbrain.resolve import is_role_address
    if loser_id == winner_id:
        return {"ok": False, "error": "self_merge",
                "message": "Can't merge an entity into itself."}
    loser, winner = store.get_entity(loser_id), store.get_entity(winner_id)
    if loser is None or winner is None:
        return {"ok": False, "error": "not_found", "message": "Entity not found."}
    if is_role_address(loser.get("email_addr", "")) or is_role_address(winner.get("email_addr", "")):
        return {"ok": False, "error": "role_inbox",
                "message": "One of these is keyed on a shared/role inbox "
                           "(e.g. office@) — merging could fuse distinct people. Refused."}
    store.merge_entities(loser_id, winner_id)
    store.record_change("entity_merged", ref_id=winner_id,
                        summary=f"merged {loser_id} into {winner_id}")
    return {"ok": True}


def suppress_entity(store, entity_id: str) -> dict:
    """Soft-delete (reversible) an entity."""
    ok = store.suppress_entity(entity_id, reason="graph-ui")
    if ok:
        store.record_change("entity_suppressed", ref_id=entity_id, summary="suppressed via graph")
    return {"ok": bool(ok)}
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_graph_view.py -q`
Expected: PASS (all graph_view tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/graph_view.py tests/test_graph_view.py
git commit -m "feat(graph): guarded update/merge/suppress mutations"
```

---

### Task 4: Control API routes

**Files:**
- Modify: `mcpbrain/control_api.py`
- Test: `tests/test_graph_routes.py`

**Interfaces:**
- Consumes: `graph_view.entity_detail/search_entities/update_entity/merge_entities/suppress_entity`.
- Produces routes: `GET /api/graph/entity/<id>` (200/404), `GET /api/graph/search?q=` (200), `POST /api/graph/entity/<id>` (200/400/404), `POST /api/graph/merge` (200/409/404), `DELETE /api/graph/entity/<id>` (200/404). All 503 if `server.store is None`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_routes.py`; reuse its `_Store`/`_Daemon`/`_get` helpers, and add a POST/DELETE helper)

```python
def _req(url, token, method, body=None):
    import json, urllib.request
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": f"Bearer {token}",
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=5) as resp:
        return resp.status, json.loads(resp.read() or b"{}")

def _rw(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "rw.sqlite3", dim=4); s.init()
    with s._connect() as db:
        db.executemany("INSERT INTO entities(id,name,type,org,email_addr) VALUES(?,?,?,?,?)",
                       [("alice","Alice","person","Acme","a@x.com"),
                        ("al","Alice A.","person","","a@x.com")])
    return s

def test_entity_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/entity/alice", srv.token)
        assert code == 200 and body["name"] == "Alice"
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/entity/nope", srv.token); assert False
        except urllib.error.HTTPError as e: assert e.code == 404
    finally: srv.stop()

def test_search_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/search?q=alice", srv.token)
        assert code == 200 and any(r["id"] == "alice" for r in body)
    finally: srv.stop()

def test_update_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _req(f"http://127.0.0.1:{srv.port}/api/graph/entity/alice", srv.token,
                          "POST", {"org": "Beta"})
        assert code == 200 and body["org"] == "Beta"
    finally: srv.stop()

def test_merge_route_409_role_or_self(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        try:
            _req(f"http://127.0.0.1:{srv.port}/api/graph/merge", srv.token, "POST",
                 {"loser_id": "alice", "winner_id": "alice"}); assert False
        except urllib.error.HTTPError as e: assert e.code == 409
    finally: srv.stop()

def test_delete_route(tmp_path):
    s = _rw(tmp_path); srv = ControlServer(_Daemon(), str(tmp_path), store=s); srv.start()
    try:
        code, body = _req(f"http://127.0.0.1:{srv.port}/api/graph/entity/al", srv.token, "DELETE")
        assert code == 200 and body["ok"] is True
    finally: srv.stop()
```

(Add `import urllib.error` at the top of the test file if not already present.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_graph_routes.py -k "entity_route or search_route or update_route or merge_route or delete_route" -q`
Expected: FAIL (routes 404 / method not handled).

- [ ] **Step 3: Implement the routes** in `mcpbrain/control_api.py`

In `do_GET`, after the existing `/api/graph/canvas` block, add:

```python
                m = re.match(r"^/api/graph/entity/([^/?]+)$", self.path.split("?")[0])
                if m:
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    d = graph_view.entity_detail(server.store, urllib.parse.unquote(m.group(1)))
                    return h_json(self, 200, d) if d else h_json(self, 404, {"error": "not found"})
                if self.path.split("?")[0] == "/api/graph/search":
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
                    return h_json(self, 200, graph_view.search_entities(server.store, q))
```

Add a `do_DELETE` method to the handler class `H` (mirror `do_POST`'s auth gate):

```python
            def do_DELETE(self):
                if not self._auth_ok(): return
                m = re.match(r"^/api/graph/entity/([^/?]+)$", self.path)
                if m:
                    if server.store is None: return h_json(self, 503, {"error": "dashboard not available"})
                    from mcpbrain import graph_view
                    eid = urllib.parse.unquote(m.group(1))
                    if server.store.get_entity(eid) is None:
                        return h_json(self, 404, {"error": "not found"})
                    return h_json(self, 200, graph_view.suppress_entity(server.store, eid))
                self.send_response(404); self.end_headers()
```

In `_handle_post` (the POST dispatcher), add near the other `/api/...` handlers (inside the try):

```python
            m = re.match(r"^/api/graph/entity/([^/?]+)$", h.path)
            if m:
                if self.store is None: return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_view
                eid = urllib.parse.unquote(m.group(1))
                d = graph_view.update_entity(
                    self.store, eid,
                    name=body.get("name"), org=body.get("org"),
                    email_addr=body.get("email_addr"), notes=body.get("notes"))
                return h_json(h, 200, d) if d else h_json(h, 404, {"error": "not found"})
            if h.path == "/api/graph/merge":
                if self.store is None: return h_json(h, 503, {"error": "dashboard not available"})
                from mcpbrain import graph_view
                out = graph_view.merge_entities(self.store, body.get("loser_id", ""), body.get("winner_id", ""))
                if out.get("ok"): return h_json(h, 200, out)
                code = 404 if out.get("error") == "not_found" else 409
                return h_json(h, code, out)
```

Ensure `import urllib.parse` is present at the top of `control_api.py` (it is, from the graph-canvas work).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_graph_routes.py -q`
Expected: PASS (all, including the new 5).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/control_api.py tests/test_graph_routes.py
git commit -m "feat(graph): entity/search/update/merge/delete control routes"
```

---

### Task 5: Drawer UI in `graph.html`

**Files:**
- Modify: `mcpbrain/wizard/graph.html`
- Test: `tests/test_graph_page.py`

**Interfaces:**
- Consumes: `GET /api/graph/entity/{id}`, `GET /api/graph/search`, `POST /api/graph/entity/{id}`, `POST /api/graph/merge`, `DELETE /api/graph/entity/{id}`; the page's existing `fg`, `H` (auth headers), `load()`, and `state`.

- [ ] **Step 1: Read the current file** so the additions integrate with the shipped clustered-map version:

Run: `sed -n '1,40p' mcpbrain/wizard/graph.html` and locate (a) the `#stage` markup, (b) the `.onNodeClick(` handler, (c) the `H` headers constant.

- [ ] **Step 2: Add the drawer markup** — inside `#stage` (after `<div id="tooltip"></div>`), add:

```html
      <aside id="drawer" class="drawer" hidden>
        <button id="dw-close" class="dw-x" aria-label="Close">×</button>
        <div id="dw-body"></div>
      </aside>
```

- [ ] **Step 3: Add the drawer CSS** — inside the `<style>` block, append:

```css
  .drawer{position:absolute;top:0;right:0;bottom:0;width:340px;z-index:9;background:var(--card);
    border-left:1px solid var(--line);box-shadow:-4px 0 18px rgba(20,24,29,.06);
    overflow-y:auto;padding:16px 18px;font-size:14px}
  .drawer[hidden]{display:none}
  .dw-x{position:absolute;top:10px;right:12px;border:0;background:none;font-size:20px;color:var(--muted);cursor:pointer}
  .dw-x:hover{color:var(--ink)}
  .dw-name{font-size:18px;font-weight:680;margin:2px 40px 2px 0}
  .dw-meta{color:var(--muted);font-size:12.5px;margin-bottom:12px}
  .dw-sec{margin-top:16px}
  .dw-sec h4{font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);margin:0 0 6px}
  .dw-row{display:flex;gap:8px;padding:3px 0;font-size:13px;border-top:1px solid var(--line)}
  .dw-row:first-of-type{border-top:0}
  .dw-row .rel{color:var(--muted);flex:0 0 auto}
  .dw-field{display:flex;flex-direction:column;gap:3px;margin:6px 0}
  .dw-field label{font-size:11px;color:var(--muted)}
  .dw-field input,.dw-field textarea{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:8px}
  .dw-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
  .dw-actions button{font:inherit;font-weight:600;border-radius:8px;padding:7px 12px;cursor:pointer;border:1px solid var(--line);background:#fff}
  .dw-actions .primary{background:var(--signal);color:#fff;border-color:transparent}
  .dw-actions .danger{color:var(--err,#d64545);border-color:#f0c9c9}
  .dw-search{position:relative}
  .dw-search input{width:100%}
  .dw-hits{list-style:none;margin:4px 0 0;padding:0;border:1px solid var(--line);border-radius:8px;max-height:160px;overflow:auto}
  .dw-hits li{padding:6px 9px;cursor:pointer;font-size:13px}
  .dw-hits li:hover{background:var(--idle-bg,#eef1f5)}
  .dw-err{color:var(--err,#d64545);font-size:12.5px;margin-top:8px}
```

- [ ] **Step 4: Add the drawer JS** — inside the main `<script>`, append (uses the page's `H` and `fg`/`load`):

```javascript
  // ---- entity drawer ----
  const dw = $("drawer"), dwBody = $("dw-body");
  let dwId = null, dwData = null;
  $("dw-close").addEventListener("click", closeDrawer);
  function closeDrawer(){ dw.hidden = true; dwId = null; dwData = null; }
  function esc(s){ const d = document.createElement("div"); d.textContent = s == null ? "" : s; return d.innerHTML; }

  async function openDrawer(id){
    dwId = id; dw.hidden = false; dwBody.textContent = "Loading…";
    try{
      const r = await fetch("/api/graph/entity/" + encodeURIComponent(id), H);
      if (r.status === 404){ dwBody.textContent = "Entity not found."; return; }
      if (!r.ok) throw new Error("HTTP " + r.status);
      dwData = await r.json(); renderDrawer();
    }catch(e){ dwBody.textContent = "Couldn’t load this entity."; }
  }

  function relList(items){
    if (!items.length) return '<div class="dw-row" style="color:var(--muted)">none</div>';
    return items.map(x => `<div class="dw-row"><span class="rel">${esc(x.relation)}</span>`
      + `<span>${esc(x.other_name)}</span></div>`).join("");
  }
  function renderDrawer(){
    const d = dwData;
    dwBody.innerHTML =
      `<div class="dw-name">${esc(d.name)}</div>`
      + `<div class="dw-meta">${esc(d.type)} · ${esc(d.org || "no org")} · ${d.connections} connections`
      + (d.email_addr ? " · " + esc(d.email_addr) : "") + `</div>`
      + `<div class="dw-actions"><button id="dw-edit" class="primary">Edit</button>`
      + `<button id="dw-merge-btn">Merge…</button>`
      + `<button id="dw-suppress" class="danger">Suppress</button></div>`
      + `<div id="dw-inline"></div>`
      + `<div class="dw-sec"><h4>Relations</h4>${relList(d.relations)}</div>`
      + `<div class="dw-sec"><h4>Backlinks</h4>${relList(d.backlinks)}</div>`
      + `<div class="dw-sec"><h4>Observations</h4>`
      + (d.observations.length ? d.observations.map(o =>
          `<div class="dw-row"><span class="rel">${esc(o.attribute)}</span><span>${esc(o.value)}`
          + (o.valid_from ? ` <span style="color:var(--faint)">(${esc(o.valid_from)})</span>` : "")
          + `</span></div>`).join("")
        : '<div class="dw-row" style="color:var(--muted)">none</div>') + `</div>`;
    $("dw-edit").onclick = renderEdit;
    $("dw-merge-btn").onclick = renderMerge;
    $("dw-suppress").onclick = doSuppress;
  }

  function renderEdit(){
    const d = dwData, box = $("dw-inline");
    box.innerHTML =
      field("Name","dw-f-name",d.name) + field("Organisation","dw-f-org",d.org)
      + field("Email","dw-f-email",d.email_addr) + field("Notes","dw-f-notes",d.notes,true)
      + `<div class="dw-actions"><button id="dw-save" class="primary">Save</button>`
      + `<button id="dw-cancel">Cancel</button></div><div id="dw-edit-err" class="dw-err"></div>`;
    $("dw-cancel").onclick = renderDrawer;
    $("dw-save").onclick = doSave;
  }
  function field(label,id,val,area){
    const input = area ? `<textarea id="${id}" rows="2">${esc(val)}</textarea>`
                       : `<input id="${id}" value="${esc(val)}">`;
    return `<div class="dw-field"><label>${label}</label>${input}</div>`;
  }
  async function doSave(){
    const body = { name: $("dw-f-name").value.trim(), org: $("dw-f-org").value.trim(),
                   email_addr: $("dw-f-email").value.trim(), notes: $("dw-f-notes").value };
    try{
      const r = await fetch("/api/graph/entity/" + encodeURIComponent(dwId),
        { method:"POST", headers:{...H.headers,"Content-Type":"application/json"}, body:JSON.stringify(body) });
      if (!r.ok) throw new Error("HTTP " + r.status);
      dwData = await r.json(); renderDrawer(); load();   // refresh map (name/colour/labels)
    }catch(e){ const el = $("dw-edit-err"); if (el) el.textContent = "Save failed."; }
  }

  function renderMerge(){
    const box = $("dw-inline");
    box.innerHTML = `<div class="dw-sec"><h4>Merge “${esc(dwData.name)}” into…</h4>`
      + `<div class="dw-search"><input id="dw-q" placeholder="search entity to keep" autocomplete="off">`
      + `<ul id="dw-hits" class="dw-hits" hidden></ul></div>`
      + `<div id="dw-merge-err" class="dw-err"></div>`
      + `<div class="dw-actions"><button id="dw-merge-cancel">Cancel</button></div></div>`;
    $("dw-merge-cancel").onclick = renderDrawer;
    let t = null;
    $("dw-q").addEventListener("input", (e)=>{ clearTimeout(t);
      t = setTimeout(()=> searchTargets(e.target.value.trim()), 180); });
  }
  async function searchTargets(q){
    const ul = $("dw-hits");
    if (!q){ ul.hidden = true; return; }
    try{
      const r = await fetch("/api/graph/search?q=" + encodeURIComponent(q), H);
      const hits = (await r.json()).filter(h => h.id !== dwId).slice(0, 8);
      ul.innerHTML = hits.map(h => `<li data-id="${esc(h.id)}">${esc(h.name)} `
        + `<span style="color:var(--faint)">${esc(h.type)}${h.org ? " · " + esc(h.org) : ""}</span></li>`).join("");
      ul.hidden = hits.length === 0;
      [...ul.children].forEach(li => li.onclick = ()=> confirmMerge(li.dataset.id, li.textContent));
    }catch(e){ ul.hidden = true; }
  }
  async function confirmMerge(winnerId, winnerLabel){
    if (!window.confirm(`Merge “${dwData.name}” into “${winnerLabel}”?\n\nIts links move to the kept entity, and “${dwData.name}” becomes an alias. Reversible only via the store.`)) return;
    try{
      const r = await fetch("/api/graph/merge",
        { method:"POST", headers:{...H.headers,"Content-Type":"application/json"},
          body: JSON.stringify({ loser_id: dwId, winner_id: winnerId }) });
      const out = await r.json();
      if (r.ok && out.ok){ closeDrawer(); load(); }
      else { const el = $("dw-merge-err"); if (el) el.textContent = out.message || "Merge refused."; }
    }catch(e){ const el = $("dw-merge-err"); if (el) el.textContent = "Merge failed."; }
  }

  async function doSuppress(){
    if (!window.confirm(`Suppress “${dwData.name}”? It’s hidden from the graph (reversible).`)) return;
    try{
      const r = await fetch("/api/graph/entity/" + encodeURIComponent(dwId), { method:"DELETE", headers: H.headers });
      if (r.ok){ closeDrawer(); load(); }
    }catch(e){ /* leave drawer open on failure */ }
  }
```

- [ ] **Step 5: Wire node-click to open the drawer.** Find the existing `.onNodeClick((node)=>{ state.selectedNodeId = node.id; })` and change it to:

```javascript
      .onNodeClick((node)=>{ state.selectedNodeId = node.id; openDrawer(node.id); })
```

- [ ] **Step 6: `node --check` + update page markers.** In `tests/test_graph_page.py`, add to the `test_graph_html_has_expected_hooks` marker list: `'id="drawer"'`, `'/api/graph/entity/'`, `'/api/graph/search'`, `'/api/graph/merge'`, `'openDrawer'`.

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: PASS (markers + the `node --check` test).

- [ ] **Step 7: Render-harness smoke.** Rebuild the graph render harness (inline vendored libs + page body) but also stub the new endpoints (`/api/graph/entity/<id>` returns a mock detail; `/api/graph/search` returns 2 mocks; POST/DELETE return `{ok:true}`/updated detail). Publish as an artifact and confirm: clicking a node opens the drawer with identity/relations/observations; **Edit → Save** posts and refreshes; **Merge…** type-ahead lists hits and the confirm modal fires; **Suppress** confirms and closes. A role-inbox 409 shows the inline message.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/wizard/graph.html tests/test_graph_page.py
git commit -m "feat(graph): entity drawer — detail view + edit-mode + type-ahead merge + suppress"
```

---

### Task 6: Release gate

**Files:** none (verification only).

- [ ] **Step 1: Full suite** — `uv run pytest -q` → all pass (1 skipped ok).
- [ ] **Step 2: Lint** — `uv run ruff check mcpbrain/` → clean.
- [ ] **Step 3: Wheel packaging** — `uv build --wheel` then confirm `mcpbrain/wizard/graph.html` + `mcpbrain/wizard/vendor/*.js` are in the wheel (unchanged, but re-check).
- [ ] **Step 4: No commit** — release (version bump across the four files, dist wheel, plugin sync, daemon update) is a separate explicit step per `docs/RELEASE-RUNBOOK.md`.

---

## Final verification (before release)
- [ ] `uv run pytest -q` green; `uv run ruff check mcpbrain/` clean.
- [ ] Render-harness artifact reviewed: drawer open, edit/save, merge type-ahead + confirm + role-inbox refusal, suppress.
- [ ] Manual on the live daemon: click a node → drawer; correct an org → map updates; merge a duplicate (and confirm a role-inbox pair is refused with a clear message); suppress → node leaves the map.

## Notes / decisions (from the spec)
- Corrections only (name/org/email/notes) + merge + suppress; **no** creating entities/relations/observations.
- Merge = type-ahead search target; edit = explicit edit-mode Save/Cancel.
- Soft-delete (suppress, reversible); mutations run in the daemon and audit to `change_log`.
- Backend is TDD; the drawer is verified by markers + `node --check` + a render harness.
