# Interactive Knowledge Graph — Phase 1 (Read-only Explore) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a full-page, self-contained interactive knowledge-graph explorer (Sigma.js) reachable from the dashboard's "Explore graph" button, rendering a filtered subgraph of `entities`/`entity_relations` with pan/zoom, hover tooltips, and degree/org/type filters.

**Architecture:** A new pure `mcpbrain/graph_view.py` shapes Sigma-style `{nodes, links, communities}` from the store (read-only SQLite). The daemon's stdlib control server gains three GET routes: `/graph` (token-injected HTML), `/vendor/<name>.js` (vendored libs), and `/api/graph/canvas` (data). The frontend `wizard/graph.html` loads vendored graphology + sigma + forceatlas2, builds the graph, runs a bounded ForceAtlas2 layout, and renders with Sigma.

**Tech Stack:** Python 3.12 stdlib (`http.server`), SQLite (read-only URI), vendored UMD builds of graphology / sigma / graphology-layout-forceatlas2, vanilla JS.

## Global Constraints

- **Self-contained / offline:** no CDN, no npm, no build step. All JS is vendored under `mcpbrain/wizard/vendor/` and served locally. Inline all page CSS/JS in `graph.html` (matches `dashboard.html`).
- **Auth:** `/api/graph/*` requires the loopback bearer token (`Authorization: Bearer <token>`). `/graph` and `/vendor/*` are served *before* the auth gate (mirrors `/dashboard` and `/img/`) — the page injects the token; assets carry none.
- **Bind:** control server is already `127.0.0.1`-only; Host header is checked in `_auth_ok` for token routes.
- **Reads are read-only:** graph data reads use the read-only SQLite URI helper `dashboard._open_ro`, never a write connection. (No mutations in Phase 1.)
- **Degrade, never crash:** any DB/read error returns an empty `{"nodes": [], "links": [], "communities": {}}` payload; the page shows a clear empty/error state, never a broken render.
- **Node cap:** hard cap 5000 nodes → `{"error": "too_large", "cap": 5000, "candidate_count": N}` surfaced as HTTP 413.
- **Design tokens:** reuse `dashboard.html`'s palette/type (`--ink #12151b`, `--signal #0b5cff`, `--graph #6a4cff`, `--mono` stack, etc.) so `/graph` reads as the same product.

**Verified facts (live store, 2026-07-02):** `entities.degree` is populated (21,161 > 0; 2,937 ≥ 7; max 11,350). Suppression is a separate table `entity_suppressions(entity_id, reason, suppressed_at)`. `entity_communities(entity_id, community_id, level)`, `community_summaries(community_id, level, title, …)`. `entity_relations(entity_a, entity_b, relation, strength, …)`.

## File Structure

- **Create** `mcpbrain/graph_view.py` — data-shaping (`graph_canvas`). Pure over a store; unit-testable without a daemon.
- **Create** `mcpbrain/wizard/graph.html` — the full-page Sigma explorer (inline CSS/JS).
- **Create** `mcpbrain/wizard/vendor/graphology.umd.min.js`, `sigma.min.js`, `graphology-layout-forceatlas2.min.js`, `README.md` — vendored libs + provenance.
- **Modify** `mcpbrain/control_api.py` — add `_serve_graph`, `_serve_vendor`, `/api/graph/canvas` handling in `do_GET`.
- **Modify** `mcpbrain/wizard/dashboard.html` — enable the "Explore graph" button to link to `/graph`.
- **Create** `tests/test_graph_view.py`, `tests/test_graph_routes.py`, `tests/test_graph_assets.py`, `tests/test_graph_page.py`.

Global UMD names the frontend relies on (verified in Task 1): graphology → `window.graphology` (Graph constructor); sigma → `window.Sigma`; forceatlas2 → `window.graphologyLayoutForceAtlas2` (with `.assign(graph, opts)`).

---

### Task 1: Vendor Sigma.js + graphology (assets + provenance)

**Files:**
- Create: `mcpbrain/wizard/vendor/graphology.umd.min.js`
- Create: `mcpbrain/wizard/vendor/sigma.min.js`
- Create: `mcpbrain/wizard/vendor/graphology-layout-forceatlas2.min.js`
- Create: `mcpbrain/wizard/vendor/README.md`
- Test: `tests/test_graph_assets.py`

**Interfaces:**
- Produces: three JS files under `mcpbrain/wizard/vendor/` exposing browser globals `graphology`, `Sigma`, `graphologyLayoutForceAtlas2`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_assets.py
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "vendor"
FILES = ["graphology.umd.min.js", "sigma.min.js",
         "graphology-layout-forceatlas2.min.js"]

def test_vendor_files_present_and_nonempty():
    for name in FILES:
        p = VENDOR / name
        assert p.is_file(), f"missing vendored lib: {name}"
        assert p.stat().st_size > 1000, f"suspiciously small: {name}"

def test_vendor_readme_records_versions():
    readme = (VENDOR / "README.md").read_text()
    for pkg in ("graphology", "sigma", "graphology-layout-forceatlas2"):
        assert pkg in readme
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_graph_assets.py -q`
Expected: FAIL (files/README missing).

- [ ] **Step 3: Fetch the pinned UMD builds** (network required — run with sandbox disabled)

```bash
cd mcpbrain/wizard && mkdir -p vendor && cd vendor
curl -fsSL -o graphology.umd.min.js \
  https://cdn.jsdelivr.net/npm/graphology@0.25.4/dist/graphology.umd.min.js
curl -fsSL -o sigma.min.js \
  https://cdn.jsdelivr.net/npm/sigma@2.4.0/build/sigma.min.js
curl -fsSL -o graphology-layout-forceatlas2.min.js \
  https://cdn.jsdelivr.net/npm/graphology-layout-forceatlas2@0.10.1/dist/graphology-layout-forceatlas2.min.js
```

- [ ] **Step 4: Verify each file is a real UMD bundle exposing its global**

Run:
```bash
grep -c "graphology" graphology.umd.min.js
grep -c "Sigma\|sigma" sigma.min.js
grep -c "forceAtlas2\|ForceAtlas2\|assign" graphology-layout-forceatlas2.min.js
node -e "global.window=global;global.self=global;require('./graphology.umd.min.js');console.log('graphology OK:',typeof (global.graphology))"
```
Expected: non-zero grep counts; `graphology OK: function` (or `object` exposing `Graph`). If a pinned URL 404s, bump to the nearest published patch of the same MAJOR.MINOR and re-verify; record the actual version used in the README (Step 5). Sigma v2.x UMD is required (v3 is ESM-first and has no single-file UMD).

- [ ] **Step 5: Write the provenance README**

```markdown
# Vendored graph libraries

Browser UMD builds, committed so the dashboard graph works fully offline
(no CDN, no build step). Served by the daemon at `/vendor/<name>.js`.

| File | Package | Version | Source |
|---|---|---|---|
| graphology.umd.min.js | graphology | 0.25.4 | jsDelivr npm dist |
| sigma.min.js | sigma | 2.4.0 | jsDelivr npm build (UMD; v3 is ESM-only) |
| graphology-layout-forceatlas2.min.js | graphology-layout-forceatlas2 | 0.10.1 | jsDelivr npm dist |

Globals exposed: `window.graphology` (Graph constructor), `window.Sigma`,
`window.graphologyLayoutForceAtlas2` (`.assign(graph, opts)`).

To update: re-fetch the same paths at a new pinned version, re-run
`tests/test_graph_assets.py` and `tests/test_graph_page.py`, and update this table.
```

(Adjust the version numbers to the ones actually fetched in Step 4.)

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_graph_assets.py -q`
Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/wizard/vendor/ tests/test_graph_assets.py
git commit -m "feat(graph): vendor graphology + sigma + forceatlas2 UMD builds"
```

---

### Task 2: `graph_view.graph_canvas()` — Sigma-shaped data

**Files:**
- Create: `mcpbrain/graph_view.py`
- Test: `tests/test_graph_view.py`

**Interfaces:**
- Consumes: `mcpbrain.dashboard._open_ro(path: Path) -> sqlite3.Connection` (existing).
- Produces: `graph_canvas(store, *, min_conn=7, org="", community="", types=None, recency_days=0, max_links=5000) -> dict` returning either `{"nodes": list, "links": list, "communities": dict}` or `{"error": "too_large", "cap": 5000, "candidate_count": int}`. Node keys: `id, name, type, org, email_count, email_addr, connections, community, first_seen, last_seen`. Link keys: `source, target, relation, strength`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_graph_view.py
import sqlite3
from mcpbrain import graph_view


class _Store:
    def __init__(self, path):
        self._path = str(path)


def _seed(path, entities, relations, *, suppressed=(), communities=(), summaries=()):
    """entities: list of (id, name, type, org, degree, last_seen).
       relations: list of (a, b, relation, strength).
       communities: list of (entity_id, community_id, level).
       summaries: list of (community_id, level, title)."""
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        for (eid, name, typ, org, degree, last_seen) in entities:
            db.execute("INSERT INTO entities(id,name,type,org,degree,last_seen) VALUES(?,?,?,?,?,?)",
                       (eid, name, typ, org, degree, last_seen))
        for i, (a, b, rel, st) in enumerate(relations):
            db.execute("INSERT INTO entity_relations(id,entity_a,relation,entity_b,strength) VALUES(?,?,?,?,?)",
                       (i, a, rel, b, st))
        for eid in suppressed:
            db.execute("INSERT INTO entity_suppressions(entity_id,reason,suppressed_at) VALUES(?,?,?)",
                       (eid, "test", "2026-01-01"))
        for row in communities:
            db.execute("INSERT INTO entity_communities(entity_id,community_id,level) VALUES(?,?,?)", row)
        for row in summaries:
            db.execute("INSERT INTO community_summaries(community_id,level,title,summary,member_count,key_entities,updated) "
                       "VALUES(?,?,?,?,?,?,?)", (row[0], row[1], row[2], "", 0, "", ""))


def test_canvas_nodes_and_links(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p,
          entities=[("e1", "Alice", "person", "Acme", 9, "2026-06-01"),
                    ("e2", "Bob", "person", "Acme", 8, "2026-06-02"),
                    ("e3", "Low", "person", "", 2, "2026-06-03")],
          relations=[("e1", "e2", "works_with", 5), ("e1", "e3", "knows", 3)])
    out = graph_view.graph_canvas(_Store(p), min_conn=7)
    ids = {n["id"] for n in out["nodes"]}
    assert ids == {"e1", "e2"}                       # e3 (degree 2) filtered out
    assert out["links"] == [{"source": "e1", "target": "e2",
                             "relation": "works_with", "strength": 5}]  # e1-e3 dropped (e3 not a node)
    n = next(n for n in out["nodes"] if n["id"] == "e1")
    assert n["name"] == "Alice" and n["type"] == "person" and n["connections"] == 9


def test_canvas_excludes_suppressed(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p,
          entities=[("e1", "Alice", "person", "", 9, ""), ("e2", "Bob", "person", "", 9, "")],
          relations=[], suppressed=["e2"])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert {n["id"] for n in out["nodes"]} == {"e1"}


def test_canvas_org_and_type_filters(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "Acme", 9, ""),
                       ("e2", "Beta Co", "org", "Acme", 9, ""),
                       ("e3", "Carol", "person", "Other", 9, "")],
          relations=[])
    assert {n["id"] for n in graph_view.graph_canvas(_Store(p), min_conn=1, org="Acme")["nodes"]} == {"e1", "e2"}
    assert {n["id"] for n in graph_view.graph_canvas(_Store(p), min_conn=1, types=["person"])["nodes"]} == {"e1", "e3"}


def test_canvas_communities(tmp_path):
    p = tmp_path / "b.sqlite3"
    _seed(p, entities=[("e1", "Alice", "person", "", 9, "")], relations=[],
          communities=[("e1", 3, 0)], summaries=[(3, 0, "Leadership")])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out["communities"] == {"3": "Leadership"}
    assert out["nodes"][0]["community"] == 3


def test_canvas_too_large(tmp_path):
    p = tmp_path / "b.sqlite3"
    ents = [(f"e{i}", f"N{i}", "person", "", 9, "") for i in range(5001)]
    _seed(p, entities=ents, relations=[])
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out.get("error") == "too_large" and out["cap"] == 5000


def test_canvas_degrades_on_bad_store(tmp_path):
    p = tmp_path / "empty.sqlite3"
    with sqlite3.connect(str(p)):
        pass  # no tables
    out = graph_view.graph_canvas(_Store(p), min_conn=1)
    assert out == {"nodes": [], "links": [], "communities": {}}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_graph_view.py -q`
Expected: FAIL (`No module named 'mcpbrain.graph_view'`).

- [ ] **Step 3: Implement `graph_view.py`**

```python
# mcpbrain/graph_view.py
"""Sigma-shaped knowledge-graph data for the dashboard graph explorer.

graph_canvas() returns {nodes, links, communities} for the entities/relations
graph, filtered to a manageable subset (degree threshold + optional org/type/
community/recency) and capped at 5000 nodes. Read-only; degrades to an empty
payload on any DB error so the page never breaks.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain.dashboard import _open_ro

log = logging.getLogger(__name__)

_MAX_NODES = 5000
_EMPTY = {"nodes": [], "links": [], "communities": {}}


def graph_canvas(store, *, min_conn: int = 7, org: str = "", community: str = "",
                 types: list[str] | None = None, recency_days: int = 0,
                 max_links: int = 5000) -> dict:
    """Return Sigma-shaped {nodes, links, communities} or a too_large marker."""
    path = store._path if hasattr(store, "_path") else store.path

    where = ["COALESCE(e.degree, 0) >= :min_conn", "s.entity_id IS NULL"]
    params: dict = {"min_conn": int(min_conn)}
    if org:
        where.append("COALESCE(e.org, '') = :org")
        params["org"] = "" if org == "unassigned" else org
    if community:
        where.append("ec.community_id = :community")
        try:
            params["community"] = int(community)
        except (TypeError, ValueError):
            return dict(_EMPTY)
    if types:
        where.append("e.type IN (" + ",".join(f":t{i}" for i in range(len(types))) + ")")
        for i, t in enumerate(types):
            params[f"t{i}"] = t
    if recency_days and int(recency_days) > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(recency_days))).strftime("%Y-%m-%d")
        where.append("COALESCE(e.last_seen, '') >= :cutoff")
        params["cutoff"] = cutoff

    try:
        db = _open_ro(Path(path))
        try:
            rows = db.execute(f"""
                SELECT e.id, e.name, e.type, COALESCE(e.org, '') AS org,
                       COALESCE(e.email_count, 0) AS email_count,
                       COALESCE(e.email_addr, '') AS email_addr,
                       COALESCE(e.first_seen, '') AS first_seen,
                       COALESCE(e.last_seen, '') AS last_seen,
                       ec.community_id, cs.title AS community_title,
                       COALESCE(e.degree, 0) AS degree
                FROM entities e
                LEFT JOIN entity_suppressions s ON s.entity_id = e.id
                LEFT JOIN entity_communities ec ON ec.entity_id = e.id AND ec.level = 0
                LEFT JOIN community_summaries cs
                       ON cs.community_id = ec.community_id AND cs.level = 0
                WHERE {' AND '.join(where)}
                LIMIT 5001
            """, params).fetchall()

            if len(rows) > _MAX_NODES:
                return {"error": "too_large", "cap": _MAX_NODES, "candidate_count": len(rows)}

            node_ids = {r["id"] for r in rows}
            nodes = [{
                "id": r["id"], "name": r["name"], "type": r["type"] or "person",
                "org": r["org"], "email_count": r["email_count"],
                "email_addr": r["email_addr"], "connections": r["degree"],
                "community": r["community_id"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            } for r in rows]

            link_cap = max(100, min(50000, int(max_links)))
            links = []
            for e in db.execute(
                "SELECT entity_a AS source, entity_b AS target, "
                "COALESCE(relation, '') AS relation, COALESCE(strength, 1) AS strength "
                "FROM entity_relations WHERE COALESCE(strength, 0) > 0 "
                "ORDER BY strength DESC"
            ):
                if e["source"] in node_ids and e["target"] in node_ids:
                    links.append({"source": e["source"], "target": e["target"],
                                  "relation": e["relation"], "strength": e["strength"]})
                    if len(links) >= link_cap:
                        break

            communities: dict = {}
            for r in rows:
                cid = r["community_id"]
                if cid is not None and str(cid) not in communities:
                    communities[str(cid)] = r["community_title"] or f"Community {cid}"

            return {"nodes": nodes, "links": links, "communities": communities}
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        log.warning("graph_canvas: read failed (%s) — returning empty", exc)
        return dict(_EMPTY)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_graph_view.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run ruff check mcpbrain/graph_view.py
git add mcpbrain/graph_view.py tests/test_graph_view.py
git commit -m "feat(graph): graph_view.graph_canvas — Sigma-shaped subgraph"
```

---

### Task 3: `GET /api/graph/canvas` route

**Files:**
- Modify: `mcpbrain/control_api.py` (in `do_GET`, after the existing `/api/dashboard/stats` block)
- Test: `tests/test_graph_routes.py`

**Interfaces:**
- Consumes: `graph_view.graph_canvas(store, **filters)` (Task 2); `h_json` (existing).
- Produces: `GET /api/graph/canvas` → 200 `{nodes,links,communities}`; 413 on `too_large`; 503 if `server.store is None`. Query params: `min_conn` (int), `org` (str), `community` (str), `type` (repeatable → list), `recency_days` (int), `max_links` (int).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_graph_routes.py
import json
import sqlite3
import urllib.error
import urllib.request

from mcpbrain.control_api import ControlServer


class _Store:
    def __init__(self, path):
        self._path = str(path)


class _Daemon:
    def status(self):
        return {"paused": False}


def _seed(path, n_entities=3):
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, "
                   "org TEXT DEFAULT '', first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '', "
                   "email_count INTEGER DEFAULT 0, email_addr TEXT DEFAULT '', degree INTEGER DEFAULT 0)")
        db.execute("CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, "
                   "relation TEXT, entity_b TEXT, strength REAL DEFAULT 1)")
        db.execute("CREATE TABLE entity_suppressions(entity_id TEXT PRIMARY KEY, reason TEXT, suppressed_at TEXT)")
        db.execute("CREATE TABLE entity_communities(entity_id TEXT, community_id INTEGER, level INTEGER)")
        db.execute("CREATE TABLE community_summaries(community_id INTEGER, level INTEGER, title TEXT, "
                   "summary TEXT, member_count INTEGER, key_entities TEXT, updated TEXT)")
        for i in range(n_entities):
            db.execute("INSERT INTO entities(id,name,type,degree) VALUES(?,?,?,?)",
                       (f"e{i}", f"N{i}", "person", 9))


def _get(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def test_canvas_route_returns_nodes(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed(p)
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        code, body = _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?min_conn=1", srv.token)
        assert code == 200
        assert len(body["nodes"]) == 3
        assert set(body) == {"nodes", "links", "communities"}
    finally:
        srv.stop()


def test_canvas_route_413_when_too_large(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed(p, n_entities=5001)
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas?min_conn=1", srv.token)
            assert False, "expected 413"
        except urllib.error.HTTPError as e:
            assert e.code == 413
    finally:
        srv.stop()


def test_canvas_route_503_without_store(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        try:
            _get(f"http://127.0.0.1:{srv.port}/api/graph/canvas", srv.token)
            assert False, "expected 503"
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        srv.stop()


def test_canvas_route_requires_token(tmp_path):
    p = tmp_path / "b.sqlite3"; _seed(p)
    srv = ControlServer(_Daemon(), str(tmp_path), store=_Store(p)); srv.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/graph/canvas")
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_graph_routes.py -q`
Expected: FAIL (route returns 404).

- [ ] **Step 3: Add the route handler** in `mcpbrain/control_api.py`

At the top of the file, add `import urllib.parse` alongside the existing imports (check it isn't already imported). Then, inside `do_GET`, immediately **after** the `if self.path == "/api/dashboard/stats":` block and its handler, insert:

```python
                if self.path.split("?")[0] == "/api/graph/canvas":
                    if server.store is None:
                        return h_json(self, 503, {"error": "dashboard not available"})
                    try:
                        from mcpbrain import graph_view
                        qs = urllib.parse.urlparse(self.path).query
                        q = urllib.parse.parse_qs(qs)
                        def _int(name, default):
                            try:
                                return int(q.get(name, [default])[0])
                            except (TypeError, ValueError):
                                return default
                        result = graph_view.graph_canvas(
                            server.store,
                            min_conn=_int("min_conn", 7),
                            org=q.get("org", [""])[0],
                            community=q.get("community", [""])[0],
                            types=q.get("type", []),
                            recency_days=_int("recency_days", 0),
                            max_links=_int("max_links", 5000),
                        )
                        if isinstance(result, dict) and result.get("error") == "too_large":
                            return h_json(self, 413, result)
                        return h_json(self, 200, result)
                    except Exception as exc:
                        log.exception("graph canvas failed")
                        return h_json(self, 500, {"error": str(exc)})
```

Note: the existing exact-match checks like `if self.path == "/api/status"` still work because `/api/graph/canvas?...` never equals them; this handler matches on the path without its query string.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_graph_routes.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/control_api.py tests/test_graph_routes.py
git commit -m "feat(graph): GET /api/graph/canvas route"
```

---

### Task 4: Serve `/graph` page + `/vendor/<name>.js`

**Files:**
- Modify: `mcpbrain/control_api.py` (`do_GET` pre-auth block + two serve helpers)
- Test: `tests/test_graph_page.py`

**Interfaces:**
- Consumes: vendored files (Task 1); `mcpbrain/wizard/graph.html` (Task 5 — for the page test, create a minimal placeholder first if graph.html doesn't exist yet; Task 5 replaces it).
- Produces: `GET /graph` → 200 HTML with `__MCPBRAIN_TOKEN__` replaced by the live token; `GET /vendor/<name>.js` → 200 `text/javascript` for allowlisted names, 404 otherwise.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_graph_page.py
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

from mcpbrain.control_api import ControlServer


class _Daemon:
    def status(self):
        return {"paused": False}


def _raw(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def test_graph_page_injects_token(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        code, ctype, body = _raw(f"http://127.0.0.1:{srv.port}/graph")
        text = body.decode()
        assert code == 200 and "text/html" in ctype
        assert "__MCPBRAIN_TOKEN__" not in text
        assert srv.token in text
    finally:
        srv.stop()


def test_vendor_serves_js(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        code, ctype, body = _raw(f"http://127.0.0.1:{srv.port}/vendor/sigma.min.js")
        assert code == 200 and "javascript" in ctype and len(body) > 1000
    finally:
        srv.stop()


def test_vendor_rejects_unknown(tmp_path):
    srv = ControlServer(_Daemon(), str(tmp_path), store=None); srv.start()
    try:
        try:
            _raw(f"http://127.0.0.1:{srv.port}/vendor/../secrets.js")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: FAIL (routes 404). (If `graph.html` doesn't exist yet, create a one-line placeholder `mcpbrain/wizard/graph.html` containing `__MCPBRAIN_TOKEN__` so this task's page test can pass independently; Task 5 overwrites it.)

- [ ] **Step 3: Wire the pre-auth routes** in `do_GET`

In `mcpbrain/control_api.py`, find the pre-auth GET block (where `/`, `/dashboard`, `/img/` are served before `_auth_ok`). Add, alongside them:

```python
                if self.path == "/graph": return server._serve_graph(self)
                if self.path.startswith("/vendor/"):
                    m = re.match(r"^/vendor/([A-Za-z0-9._-]+\.js)$", self.path)
                    return server._serve_vendor(self, m.group(1) if m else "")
```

- [ ] **Step 4: Add the serve helpers** to `ControlServer` (next to `_serve_dashboard` / `_serve_image`)

```python
    def _serve_graph(self, h):
        p = Path(__file__).parent / "wizard" / "graph.html"
        if not p.exists():
            b = b"wizard/graph.html not found (packaging error)"
            h.send_response(500); h.send_header("Content-Type", "text/plain")
            h.send_header("Content-Length", str(len(b))); h.end_headers(); h.wfile.write(b)
            return
        html = p.read_text().replace("__MCPBRAIN_TOKEN__", self.token).encode()
        h.send_response(200); h.send_header("Content-Type", "text/html")
        h.send_header("Content-Length", str(len(html))); h.end_headers(); h.wfile.write(html)

    def _serve_vendor(self, h, name):
        root = (Path(__file__).parent / "wizard" / "vendor").resolve()
        p = (root / name).resolve()
        if root not in p.parents or not p.is_file() or p.suffix != ".js":
            h.send_response(404); h.end_headers(); return
        data = p.read_bytes()
        h.send_response(200); h.send_header("Content-Type", "text/javascript")
        h.send_header("Content-Length", str(len(data))); h.end_headers(); h.wfile.write(data)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/control_api.py tests/test_graph_page.py mcpbrain/wizard/graph.html
git commit -m "feat(graph): serve /graph page + /vendor/<name>.js"
```

---

### Task 5: `graph.html` — the Sigma explorer frontend

**Files:**
- Create (replace placeholder): `mcpbrain/wizard/graph.html`
- Test: extend `tests/test_graph_page.py` with static-markup assertions + a JS `node --check`.

**Interfaces:**
- Consumes: `/vendor/graphology.umd.min.js`, `/vendor/sigma.min.js`, `/vendor/graphology-layout-forceatlas2.min.js`; `GET /api/graph/canvas`. Globals: `graphology`, `Sigma`, `graphologyLayoutForceAtlas2`.

- [ ] **Step 1: Write the failing static tests** (append to `tests/test_graph_page.py`)

```python
def test_graph_html_has_expected_hooks():
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "graph.html").read_text()
    for marker in ['/vendor/graphology.umd.min.js', '/vendor/sigma.min.js',
                   '/vendor/graphology-layout-forceatlas2.min.js',
                   '/api/graph/canvas', 'id="graph"', 'new Sigma',
                   'forceAtlas2', '__MCPBRAIN_TOKEN__']:
        assert marker in html, f"missing: {marker}"


def test_graph_html_js_syntax():
    import re, subprocess, shutil
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "graph.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)  # inline only (no src)
    assert scripts, "expected an inline <script>"
    if shutil.which("node"):
        for i, js in enumerate(scripts):
            f = Path(f"/tmp/_graph_{i}.js"); f.write_text(js)
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: FAIL (placeholder graph.html lacks the markers).

- [ ] **Step 3: Write `mcpbrain/wizard/graph.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Graph — mcpbrain</title>
<style>
  :root{
    --ink:#12151b; --muted:#697280; --faint:#98a0ab; --line:#e6e8ee;
    --paper:#f4f6f9; --card:#ffffff; --signal:#0b5cff; --graph:#6a4cff;
    --mono:ui-monospace,"SF Mono","JetBrains Mono","Fira Mono","Consolas",monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--sans);
    -webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
  header{display:flex;align-items:center;gap:14px;padding:12px 18px;
    border-bottom:1px solid var(--line);background:var(--card);flex:0 0 auto}
  .wordmark{font-weight:680;letter-spacing:-.01em}
  .eyebrow{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.11em;color:var(--faint)}
  a.back{color:var(--muted);text-decoration:none;font-size:13px}
  a.back:hover{color:var(--ink)}
  .layout{flex:1 1 auto;display:flex;min-height:0}
  .filters{flex:0 0 220px;border-right:1px solid var(--line);background:var(--card);
    padding:16px;display:flex;flex-direction:column;gap:16px;overflow-y:auto}
  .filters label{display:flex;flex-direction:column;gap:6px;font-size:12px;
    text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
  .filters input[type=range]{width:100%}
  .filters select,.filters input[type=text]{font:inherit;padding:6px 8px;border:1px solid var(--line);
    border-radius:8px;background:#fff}
  .deg-val{font-family:var(--mono);color:var(--ink)}
  #stage{flex:1 1 auto;position:relative;min-width:0}
  #graph{position:absolute;inset:0}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:var(--muted);font-size:14px;background:var(--paper);z-index:5}
  #overlay.hidden{display:none}
  #tooltip{position:absolute;z-index:10;pointer-events:none;display:none;
    background:#1a1f27;color:#fff;border-radius:8px;padding:7px 10px;font-size:12.5px;
    max-width:260px;line-height:1.4}
  #tooltip b{font-weight:640}
  .count{margin-left:auto;font-family:var(--mono);font-size:12.5px;color:var(--muted)}
</style>
</head>
<body>
  <header>
    <span class="wordmark">mcpbrain</span>
    <span class="eyebrow">Knowledge graph</span>
    <a class="back" href="/dashboard">← Dashboard</a>
    <span id="count" class="count"></span>
  </header>
  <div class="layout">
    <aside class="filters">
      <label>Min connections <span class="deg-val" id="deg-val">7</span>
        <input type="range" id="f-degree" min="1" max="50" value="7">
      </label>
      <label>Organisation
        <select id="f-org"><option value="">All</option></select>
      </label>
      <label>Type
        <select id="f-type"><option value="">All</option></select>
      </label>
    </aside>
    <div id="stage">
      <div id="graph"></div>
      <div id="overlay">Loading graph…</div>
      <div id="tooltip"></div>
    </div>
  </div>

  <script src="/vendor/graphology.umd.min.js"></script>
  <script src="/vendor/sigma.min.js"></script>
  <script src="/vendor/graphology-layout-forceatlas2.min.js"></script>
  <script>
    const TOKEN = "__MCPBRAIN_TOKEN__";
    const H = { headers: { Authorization: "Bearer " + TOKEN } };
    const reduce = window.matchMedia("(prefers-reduced-motion:reduce)").matches;
    const $ = (id) => document.getElementById(id);
    const PALETTE = ["#6a4cff","#0b5cff","#1f9d54","#b7791f","#d64545","#0e7490",
                     "#9333ea","#c2410c","#0891b2","#4d7c0f"];

    let renderer = null;

    function colorFor(node){
      if (node.community == null) return "#9aa1ab";
      return PALETTE[Math.abs(Number(node.community)) % PALETTE.length];
    }
    function sizeFor(conn){ return Math.max(2, Math.min(18, 2 + Math.sqrt(conn || 1))); }

    function populateFilterOptions(nodes){
      const orgs = new Set(), types = new Set();
      for (const n of nodes){ if (n.org) orgs.add(n.org); if (n.type) types.add(n.type); }
      const orgSel = $("f-org"), typeSel = $("f-type");
      const cur = { org: orgSel.value, type: typeSel.value };
      orgSel.innerHTML = '<option value="">All</option>';
      [...orgs].sort().forEach(o => orgSel.add(new Option(o, o)));
      typeSel.innerHTML = '<option value="">All</option>';
      [...types].sort().forEach(t => typeSel.add(new Option(t, t)));
      orgSel.value = cur.org; typeSel.value = cur.type;
    }

    function buildGraph(data){
      const g = new graphology.Graph();
      for (const n of data.nodes){
        g.addNode(n.id, {
          label: n.name, x: Math.random(), y: Math.random(),
          size: sizeFor(n.connections), color: colorFor(n),
          _type: n.type, _org: n.org, _conn: n.connections,
        });
      }
      for (const e of data.links){
        if (g.hasNode(e.source) && g.hasNode(e.target) && !g.hasEdge(e.source, e.target)){
          try { g.addEdge(e.source, e.target, { size: 0.4, color: "#d8dbe2" }); }
          catch (err) { /* parallel/self edges ignored */ }
        }
      }
      const iters = reduce ? 60 : Math.min(400, 120 + g.order);
      try { graphologyLayoutForceAtlas2.assign(g, { iterations: iters,
        settings: { gravity: 1, scalingRatio: 8, slowDown: 2, barnesHutOptimize: g.order > 800 } }); }
      catch (err) { /* keep random positions on layout failure */ }
      return g;
    }

    function render(g){
      if (renderer){ renderer.kill(); renderer = null; }
      renderer = new Sigma(g, $("graph"), {
        renderLabels: true, labelRenderedSizeThreshold: 9,
        defaultEdgeColor: "#d8dbe2",
      });
      const tip = $("tooltip");
      renderer.on("enterNode", ({ node }) => {
        const a = g.getNodeAttributes(node);
        tip.innerHTML = "<b>" + a.label + "</b><br>" + (a._type || "") +
          " · " + (a._org || "no org") + " · " + (a._conn || 0) + " connections";
        tip.style.display = "block";
      });
      renderer.getMouseCaptor().on("mousemovebody", (e) => {
        tip.style.left = (e.x + 14) + "px"; tip.style.top = (e.y + 14) + "px";
      });
      renderer.on("leaveNode", () => { tip.style.display = "none"; });
    }

    function currentFilters(){
      const p = new URLSearchParams();
      p.set("min_conn", $("f-degree").value);
      if ($("f-org").value) p.set("org", $("f-org").value);
      if ($("f-type").value) p.set("type", $("f-type").value);
      return p.toString();
    }

    let loading = false;
    async function load(){
      if (loading) return; loading = true;
      const ov = $("overlay"); ov.textContent = "Loading graph…"; ov.classList.remove("hidden");
      try {
        const res = await fetch("/api/graph/canvas?" + currentFilters(), H);
        if (res.status === 413){
          const info = await res.json();
          ov.textContent = "Too many nodes (" + (info.candidate_count || "5000+") +
            ") — raise “Min connections” to narrow the view.";
          loading = false; return;
        }
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        populateFilterOptions(data.nodes);
        $("count").textContent = data.nodes.length + " nodes · " + data.links.length + " links";
        if (!data.nodes.length){ ov.textContent = "No entities match these filters."; loading = false; return; }
        ov.textContent = "Laying out…";
        const g = buildGraph(data);
        render(g);
        ov.classList.add("hidden");
      } catch (e){
        ov.textContent = "Couldn’t load the graph. Is the daemon running?";
      } finally { loading = false; }
    }

    const deg = $("f-degree");
    deg.addEventListener("input", () => { $("deg-val").textContent = deg.value; });
    deg.addEventListener("change", load);
    $("f-org").addEventListener("change", load);
    $("f-type").addEventListener("change", load);
    load();
  </script>
</body>
</html>
```

- [ ] **Step 4: Run the static + syntax tests**

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: PASS (all page tests, incl. markers + `node --check`).

- [ ] **Step 5: Manual smoke via stubbed-fetch harness** (optional but recommended)

Build a scratch copy that stubs `fetch` with mock `{nodes,links,communities}` and open it in a browser (or screenshot) to confirm Sigma renders, hover works, and the degree slider re-queries. Mirrors the dashboard verify-harness technique. No commit.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/wizard/graph.html tests/test_graph_page.py
git commit -m "feat(graph): Sigma explorer page (pan/zoom, hover, filters)"
```

---

### Task 6: Link the dashboard "Explore graph" button to `/graph`

**Files:**
- Modify: `mcpbrain/wizard/dashboard.html` (the `.explore` button in the graph panel)
- Test: extend `tests/test_dashboard_circles.py` (already reads `dashboard.html`) or add a small assertion in `tests/test_graph_page.py`.

**Interfaces:**
- Consumes: the `/graph` route (Task 4).
- Produces: an enabled "Explore graph" control that navigates to `/graph`.

- [ ] **Step 1: Write the failing test** (add to `tests/test_graph_page.py`)

```python
def test_dashboard_links_to_graph():
    html = (Path(__file__).resolve().parents[1] / "mcpbrain" / "wizard" / "dashboard.html").read_text()
    assert "/graph" in html                       # explore button targets the page
    assert "Explore graph" in html
    # the button is no longer a disabled "soon" teaser
    assert 'class="explore" disabled' not in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_graph_page.py::test_dashboard_links_to_graph -q`
Expected: FAIL (button is currently `disabled` with a "soon" pill).

- [ ] **Step 3: Update the button** in `mcpbrain/wizard/dashboard.html`

Replace the existing explore button:

```html
        <button class="explore" disabled title="Interactive graph coming soon">Explore graph <span class="soon">soon</span></button>
```

with a navigating button:

```html
        <button class="explore" onclick="location.href='/graph'">Explore graph →</button>
```

(Leave the `.explore` CSS as-is; the `.soon` pill rule can stay unused or be removed.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/wizard/dashboard.html tests/test_graph_page.py
git commit -m "feat(graph): link dashboard Explore-graph button to /graph"
```

---

## Final verification (before any release)

- [ ] `uv run pytest -q` — full suite green.
- [ ] `uv run ruff check mcpbrain/` — clean.
- [ ] Manual: `mcpbrain` daemon running → open `http://127.0.0.1:<control_port>/graph` (token injected via the dashboard link) → graph renders, degree slider re-queries, hover tooltips work, org/type filters work, 413 message appears at `min_conn=1` on the full store.
- [ ] Release is a **separate, explicit step** (follow `docs/RELEASE-RUNBOOK.md`); version number chosen at release time, not fixed in this plan.

## Notes / deviations from the spec

- **Layout runs synchronously** (bounded ForceAtlas2 `.assign`, with a "Laying out…" overlay) rather than in a Web Worker. Rationale: the worker build needs a separate vendored worker script + Blob wiring; synchronous bounded iterations on the default degree≥7 subset (~2.9k nodes) is simple, dependency-light, and acceptable. Moving layout to a worker is a clean follow-up if it feels heavy.
- **Click-to-select / entity drawer is Phase 2** — Phase 1 shows entity facts via the hover tooltip only.
