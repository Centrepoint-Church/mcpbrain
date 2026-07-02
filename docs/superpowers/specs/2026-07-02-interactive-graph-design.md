# Interactive Knowledge Graph — Design

**Date:** 2026-07-02
**Status:** Approved (design); implementation planned in phases.
**Context:** Bring back the interactive knowledge-graph explorer that ran on the
predecessor project `ops-brain` (`itsjoshuakemp/ops-brain`), adapted to
mcpbrain's self-contained, daemon-served dashboard. The dashboard already
reserves an "Explore graph" affordance in the hero graph panel; this feature is
what it opens.

## Goals

- A full-page, interactive graph of the knowledge graph (`entities` +
  `entity_relations`), rendered with **Sigma.js**, reachable from the dashboard.
- **Full parity** with the ops-brain graph over three phases: read-only explore,
  entity detail drawer, and in-graph editing (rename / merge / delete).
- Stay true to mcpbrain's constraints: **self-contained and offline** (no CDN, no
  npm/build step), served by the daemon's stdlib control server, gated by the
  existing loopback bearer token.

## Non-goals

- No new build toolchain (no Vite/Svelte). The ops-brain frontend is a Svelte 5
  SPA; we adapt the *concept and data shape*, not the code.
- No hard delete in v1 (soft-delete / suppress only; reversible).
- No graph-write operations beyond rename / merge / delete (no add-observation,
  no manual relation authoring) in this design.

## Source mapping (ops-brain → mcpbrain)

The data layer ports almost verbatim because the schemas match:

| ops-brain | mcpbrain | Notes |
|---|---|---|
| `_graph_canvas()` (`workstation/routers/knowledge.py`) | new `graph_view.graph_canvas()` | Sigma-shaped `{nodes, links, communities}`; degree filter; 5000 cap |
| `entities` (id/name/type/org/degree/email_count/email_addr/last_seen) | same columns exist | verified on live store (25,883 entities) |
| `entity_relations` (entity_a/entity_b/relation/strength) | same columns | verified (78,566 relations) |
| `entity_communities`, `community_summaries` | same | community coloring/titles |
| `POST /graph/entity/{id}/rename` | `store` name update + alias | no dedicated method yet; add one |
| `POST /graph/merge` | `store.merge_entities(loser, winner)` | exists |
| `DELETE /graph/entity/{id}` | `store.suppress_entity(id)` | soft-delete; exists |
| Svelte + Sigma + graphology (Vite build) | vendored UMD builds, no build | see Vendoring |

## Architecture — three isolated units

### 1. Backend data + mutation layer — `mcpbrain/graph_view.py` (new)

Pure functions over a `store`, so they are unit-testable without a daemon
(mirrors `dashboard.stats`).

- `graph_canvas(store, *, min_conn, org, community, types, recency_days, max_links=5000) -> dict`
  - Returns `{"nodes": [...], "links": [...], "communities": {...}}`.
  - Node: `{id, name, type, org, email_count, email_addr, connections, community, first_seen, last_seen}`.
  - Link: `{source, target, relation, strength}` (only where both endpoints are in the node set).
  - Filters: `min_conn` on `degree`; optional `org` (`unassigned` = empty), `community`, `types`, `recency_days` on `last_seen`.
  - Excludes suppressed entities.
  - Hard cap: if the filtered node set exceeds 5000, return `{"error": "too_large", "cap": 5000, "candidate_count": N}` (surfaced as HTTP 413).
- `entity_detail(store, entity_id) -> dict | None`
  - `{id, name, type, org, email_addr, connections, aliases, notes, relations: [{other_id, other_name, relation, strength, direction}], observations: [...], backlinks: [...]}`.
  - `None` when the id is unknown/suppressed → 404.
- Mutations (thin wrappers so the route layer stays declarative):
  - `rename_entity(store, id, new_name)` — updates `name`, appends the old name to `aliases`. New store method `Store.rename_entity`.
  - `merge_entities(store, loser_id, winner_id)` — delegates to `store.merge_entities`, but **first enforces the role-inbox guard** (0.7.77): refuse if either entity is keyed on / shares a role address (`office@`, `info@`, …) via `is_role_address()`. Returns a structured refusal the UI shows.
  - `delete_entity(store, id)` — `store.suppress_entity(id, reason="graph-ui")`.
- Every mutation records to `change_log` (audit; consistent with existing dashboard mutations).

### 2. Control API routes — `mcpbrain/control_api.py`

All behind the existing bearer-token gate (loopback + `Authorization`), except
the HTML page and the vendor assets which are served pre-auth like `/dashboard`
and `/img/` (the page injects the token; assets carry none).

| Method | Path | Handler |
|---|---|---|
| GET | `/graph` | serve `wizard/graph.html` with `__MCPBRAIN_TOKEN__` injected (mirror `_serve_dashboard`) |
| GET | `/vendor/<name>.js` | serve `wizard/vendor/<name>.js` (allowlisted names; `text/javascript`), mirror `_serve_image` |
| GET | `/api/graph/canvas` | `graph_view.graph_canvas(...)`; 413 on `too_large`; 503 if no store |
| GET | `/api/graph/entity/{id}` | `graph_view.entity_detail(...)`; 404 if unknown |
| POST | `/api/graph/entity/{id}/rename` | `{name}`; 400 on empty/invalid |
| POST | `/api/graph/merge` | `{loser_id, winner_id}`; 409 on role-inbox refusal |
| DELETE | `/api/graph/entity/{id}` | soft-delete; 404 if unknown |

Mutations run in the daemon process (the single writer) through `server.store`,
identical to the existing `mark_done` / `resolve_finding` routes, so no new
concurrency surface is introduced.

### 3. Frontend — `mcpbrain/wizard/graph.html` + `mcpbrain/wizard/vendor/`

A single self-contained page (styles + script inline, matching `dashboard.html`
conventions and design tokens). Layout: filters sidebar · Sigma canvas ·
entity drawer (opens on node click).

- Load flow: fetch `/api/graph/canvas` (bearer) → build a `graphology` graph →
  run `graphology-layout-forceatlas2` in a **Web Worker** (bounded iterations) →
  `Sigma` renders. Circular-layout fallback if the worker errors/times out.
- Interactions: pan/zoom (Sigma built-in); hover → tooltip (name · type · org ·
  connections); click node → fetch `entity/{id}` → drawer; background click →
  deselect.
- Node encoding: size by `connections` (degree); color by community (fallback by
  type); label on zoom / for high-degree nodes.
- Filters: degree slider (`min_conn`), org select, type multiselect, community
  select, recency. Changing a filter re-fetches canvas and re-lays-out.
- Editing (Phase 3): drawer has Rename (inline), Merge (pick/confirm a target),
  Delete (confirm). On success, patch the in-memory graph and/or re-fetch.
- `too_large` (413): the canvas shows "Too many nodes — raise the degree filter",
  never a broken render.
- Respect `prefers-reduced-motion`: skip the layout animation, render final
  positions directly.

## Vendoring (Sigma + graphology)

Pinned **UMD** browser builds committed under `mcpbrain/wizard/vendor/`:

- `graphology.umd.min.js`
- `sigma.min.js`
- `graphology-layout-forceatlas2.min.js` (+ its worker helper if used)

Fetched once author-side (with network) and committed — no install-time or
runtime network. Served locally via `GET /vendor/<name>.js` (allowlisted). The
wheel already ships `wizard/**` as package data, so the vendor dir is included; a
test asserts the files exist and serve with the right content-type. Versions are
pinned and recorded in this spec / a `vendor/README.md` for provenance.

## Data flow (summary)

```
/graph (HTML, token-injected)
   │  loads /vendor/*.js
   ▼
fetch /api/graph/canvas?min_conn=…&org=…       ── daemon: graph_view.graph_canvas(store, …)
   ▼
graphology graph ── ForceAtlas2 (web worker) ── Sigma render
   │ hover → tooltip
   │ click node → fetch /api/graph/entity/{id} → drawer
   │ edit/merge/delete → POST/DELETE → daemon store mutation (single writer) → change_log
   ▼                                            └─ re-fetch/patch graph
```

## Error handling

- No store / daemon not running → the page renders a clear "brain not running"
  state; API returns 503.
- `too_large` → 413 + UI prompt to tighten filters.
- Layout worker failure/timeout → circular-layout fallback, no crash.
- Merge role-inbox refusal → 409 + explanatory message in the drawer.
- Unknown entity id → 404; the drawer shows "not found".

## Testing

- **Backend unit** (`tests/test_graph_view.py`, seeded store like
  `test_dashboard_stats`): `graph_canvas` node/link shaping, each filter, the
  5000 cap → `too_large`, suppressed-entity exclusion; `entity_detail` fields +
  unknown-id `None`; mutations — rename (name+alias), merge happy path, **merge
  role-inbox refusal**, delete (suppress).
- **Route** (`ControlServer`): 200/401/413/404/409 across the new endpoints;
  `/vendor/<name>.js` serves 200 + correct content-type and rejects unknown/
  traversal names.
- **Packaging:** a test asserting the vendored files exist under `wizard/vendor/`.
- **Frontend:** `node --check` on the inline script; element-ID cross-check; a
  stubbed-fetch render harness (as used to verify the dashboard).

## Phasing (one design, shipped incrementally)

Each phase is independently shippable; **release versions are intentionally not
fixed here** (other work may land between phases).

- **Phase 1 — Explore (read-only):** `graph_view.graph_canvas`, `GET /graph`,
  `GET /api/graph/canvas`, `GET /vendor/*`, vendored Sigma, filters, hover,
  pan/zoom, layout worker. The dashboard "Explore graph" button links here.
- **Phase 2 — Detail drawer:** `entity_detail` + `GET /api/graph/entity/{id}` +
  drawer UI (relations, observations, backlinks).
- **Phase 3 — Editing:** rename/merge/delete store methods + routes + drawer
  controls + confirmations + role-inbox guard + `change_log` audit.

## Open questions / follow-ups

- Hard-delete (vs. suppress) — deferred; revisit if soft-delete proves
  insufficient.
- Persisted layout positions (precompute during the daily resolve cadence) — a
  future optimization if in-browser layout of ~5k nodes feels slow.
- Add-observation / manual relation authoring — out of scope; open a follow-up if
  wanted.
