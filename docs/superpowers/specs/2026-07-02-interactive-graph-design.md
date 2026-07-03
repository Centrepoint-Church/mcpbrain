# Interactive Knowledge Graph — Design

**Date:** 2026-07-02 (revised 2026-07-03)
**Status:** Explore + readability shipped (0.7.80–0.7.83 and the clustered-map
release); **one phase remaining: the entity drawer with editing.**
**Context:** Bring back the interactive knowledge-graph explorer that ran on the
predecessor project `ops-brain` (`itsjoshuakemp/ops-brain`), adapted to
mcpbrain's self-contained, daemon-served dashboard. Opened from the dashboard's
"Explore graph" button at `/graph`.

## Goals

- A full-page, interactive graph of the knowledge graph (`entities` +
  `entity_relations`), reachable from the dashboard, rendered with **force-graph +
  d3-force** (a live force simulation — see the renderer note below and the
  companion readability spec).
- A **readable map** (clustered "islands" by community/org/type, labelled hulls,
  semantic zoom) — see `2026-07-03-graph-readability-design.md`.
- An **entity drawer** that shows an entity's full picture and lets you **correct
  its facts** in place (rename, org / works-at, email, notes) and **curate** the
  graph (merge duplicates, suppress).
- Stay true to mcpbrain's constraints: **self-contained and offline** (no CDN, no
  npm/build step), served by the daemon's stdlib control server, gated by the
  existing loopback bearer token.

## Non-goals (deliberate decisions, not deferrals)

- **No authoring of new graph objects.** You can correct facts on entities that
  already exist, but you cannot create brand-new entities, hand-draw new relations
  between previously-unrelated entities, or add observations. The graph is
  *derived* from ingested email/Drive/calendar; the drawer is a **correction and
  curation** surface, not a manual graph editor. (This keeps the mental model and
  the write surface simple.)
- **Soft-delete only.** "Delete" suppresses an entity (reversible via
  `entity_suppressions`); there is no hard delete. Permanently destroying derived
  graph rows from a UI is the wrong default — suppress is the correct choice, not a
  reduced one.
- **No new build toolchain** (no Vite/Svelte). We adapt the ops-brain *concept and
  data shape*, not its code.

## Renderer note (supersedes the original Sigma plan)

The original design specified **Sigma.js + graphology + a ForceAtlas2 web-worker**
layout. That shipped briefly and was **replaced** because its one-shot static
layout lacked the live, legible movement we wanted. The renderer is now a direct
port of ops-brain's `canvas.ts`: **`force-graph` (1.51.4) + full `d3` (7.9.0)**
driving a live d3-force simulation, vendored as UMD under `mcpbrain/wizard/vendor/`
(`force-graph.min.js`, `d3.min.js`). Node **colour is fixed to entity type**; a
switchable **"Group by"** (community/org/type) drives spatial clustering. Details
live in the readability spec; this spec covers the data/mutation layer and the
drawer.

## Source mapping (ops-brain → mcpbrain)

The data layer ports almost verbatim because the schemas match:

| ops-brain | mcpbrain | Notes |
|---|---|---|
| `_graph_canvas()` | `graph_view.graph_canvas()` | `{nodes, links, communities}`; degree filter; 5000 cap — **shipped** |
| `entities` (id/name/type/org/degree/email_count/email_addr/last_seen/aliases/notes) | same columns exist | verified on live store |
| `entity_relations` (entity_a/entity_b/relation/strength) | same columns | verified |
| `entity_communities`, `community_summaries` | same | community colouring/titles |
| `POST /api/entity/{id}` (name/org/notes/email) | `graph_view.update_entity()` + store setters | correction surface |
| `POST /graph/merge` | `store.merge_entities(loser, winner)` | exists |
| `DELETE /graph/entity/{id}` | `store.suppress_entity(id)` | soft-delete; exists |
| force-graph + d3 (ops-brain via Vite) | vendored UMD builds, no build | shipped |

## Architecture — three isolated units

### 1. Backend data + mutation layer — `mcpbrain/graph_view.py`

Pure functions over a `store` (unit-testable without a daemon, mirroring
`dashboard.stats`).

- `graph_canvas(store, *, min_conn, org, community, types, recency_days, max_links=5000) -> dict` — **shipped.** `{nodes, links, communities}`, degree/org/community/type/recency filters, 5000 cap → `too_large` (413), tolerant of a missing `entity_suppressions` table.
- `entity_detail(store, entity_id) -> dict | None` — **new.**
  `{id, name, type, org, email_addr, aliases, notes, connections, relations: [{other_id, other_name, relation, strength, direction}], observations: [{attribute, value, valid_from, valid_to, source}], backlinks: [{id, name, relation}]}`. Read-only pass over `entities` + `entity_relations` (joined to the other endpoint for names/strength) + `entity_observations`. `None` when the id is unknown/suppressed → 404.
- `search_entities(store, q, limit=10) -> list[dict]` — **new.** Powers the merge
  type-ahead: `[{id, name, type, org}]` for entities whose name matches `q`
  (case-insensitive prefix/substring), excluding suppressed. Empty `q` → `[]`.
- Mutations (thin wrappers; the route layer stays declarative). Every mutation
  records to `change_log` (audit; consistent with existing dashboard mutations):
  - `update_entity(store, id, *, name?, org?, email_addr?, notes?) -> dict` — corrects
    existing fields only. Renaming (a `name` change) preserves the old name as an
    **alias**. Uses store setters: `rename_entity` (new: name + alias),
    `update_entity_org` (exists), `set_entity_email` (new), `set_entity_notes` (new).
  - `merge_entities(store, loser_id, winner_id) -> dict` — delegates to
    `store.merge_entities`, but **first enforces the role-inbox guard** (0.7.77):
    refuse if either entity is keyed on / shares a role address (`office@`,
    `info@`, …) via `is_role_address()`; also refuse a self-merge. Returns a
    structured refusal the UI shows.
  - `suppress_entity(store, id) -> dict` — `store.suppress_entity(id, reason="graph-ui")` (reversible soft-delete).
- **New store methods needed:** `Store.rename_entity(id, new_name)` (set name, append
  old name to `aliases`), `Store.set_entity_email(id, email)`, `Store.set_entity_notes(id, notes)`. (`update_entity_org`, `merge_entities`, `suppress_entity` already exist.)

### 2. Control API routes — `mcpbrain/control_api.py`

Bearer-token gated (loopback + `Authorization`), except `/graph` + `/vendor/*`
which are served pre-auth (page injects the token; assets carry none). Mutations
run in the daemon process (the single writer) through `server.store`, identical to
the existing `mark_done` / `resolve_finding` routes.

| Method | Path | Handler | Status |
|---|---|---|---|
| GET | `/graph` | serve `wizard/graph.html` token-injected | shipped |
| GET | `/vendor/<name>.js` | serve vendored libs (allowlisted) | shipped |
| GET | `/api/graph/canvas` | `graph_view.graph_canvas(...)`; 413 too_large; 503 no store | shipped |
| GET | `/api/graph/entity/{id}` | `graph_view.entity_detail(...)`; 404 if unknown | **new** |
| GET | `/api/graph/search?q=` | `graph_view.search_entities(...)`; `[]` on empty `q` | **new** (merge type-ahead) |
| POST | `/api/graph/entity/{id}` | `graph_view.update_entity(...)`; body `{name?, org?, email_addr?, notes?}`; 400 on empty/invalid | **new** |
| POST | `/api/graph/merge` | `graph_view.merge_entities(...)`; `{loser_id, winner_id}`; 409 on role-inbox / self-merge refusal | **new** |
| DELETE | `/api/graph/entity/{id}` | `graph_view.suppress_entity(...)`; 404 if unknown | **new** |

### 3. Frontend — `mcpbrain/wizard/graph.html`

Single self-contained page (shipped): force-graph clustered map, filters, hover
tooltip, type legend, semantic zoom, cluster drill-in. **This phase adds the
entity drawer:**
- **Open on node click** → fetch `/api/graph/entity/{id}` → drawer with: identity
  (name, type, org, email, aliases), the **relations** list (other entity ·
  relation · strength · direction), the **observations** timeline, and
  **backlinks**.
- **Editing in the drawer** (correction/curation only):
  - **Edit fields via edit-mode** — the drawer is read-only until an **"Edit"**
    button turns name, org (works-at), email, and notes into inputs with explicit
    **Save / Cancel**. Save → `POST /api/graph/entity/{id}`. (No accidental writes.)
  - **Merge via type-ahead** — a "Merge into…" box queries `GET /api/graph/search?q=`,
    you pick the target, then a **confirm modal** ("*this* merges into *target* —
    its links move, and *this* becomes an alias") → `POST /api/graph/merge`. Server
    refuses role-inbox / self-merge → 409 shown inline.
  - **Suppress** — confirm → `DELETE /api/graph/entity/{id}` (reversible).
  - On success, patch the in-memory graph and/or re-fetch the canvas so the map
    reflects the change.
- Relations/observations are **shown but not individually edited** (that would be
  authoring new graph objects — out of scope by decision).

## Error handling
- No store / daemon down → page shows "brain not running"; API 503.
- `too_large` → 413 + prompt to tighten filters (shipped).
- Merge role-inbox / self-merge refusal → 409 + explanatory message in the drawer.
- Unknown/suppressed entity id → 404; the drawer shows "not found".
- Invalid/empty update body → 400; the field keeps its prior value.

## Testing
- **Backend unit** (`tests/test_graph_view.py`, seeded store): `entity_detail`
  fields (relations split into out/backlinks, observations) + unknown-id `None`;
  `search_entities` (name match, excludes suppressed, empty-`q` → `[]`);
  `update_entity` (name→alias, org, email, notes); `merge_entities` happy path,
  **role-inbox refusal**, **self-merge refusal**; `suppress_entity`.
  (`graph_canvas` tests already exist.)
- **Route** (`ControlServer`): 200/400/401/404/409 across the new endpoints
  (incl. `GET /api/graph/search`).
- **Frontend:** `node --check` on the inline script; static-marker assertions for
  the drawer hooks; a stubbed-fetch render harness (as used for the dashboard and
  the graph) confirming the drawer opens, shows detail, and the edit/merge/suppress
  flows call the right endpoints and update the map.

## Remaining phase (single, complete)

Explore + readability are **shipped**. The one remaining phase delivers the whole
of the rest of this spec at once:

**Entity drawer with editing** — `entity_detail` + `search_entities` +
`update_entity` + `merge` + `suppress` in `graph_view.py`; the five new routes
(entity, search, update, merge, delete); new store setters (`rename_entity`,
`set_entity_email`, `set_entity_notes`); and the drawer UI (view + edit-mode
fields with Save/Cancel + type-ahead merge + suppress, with confirmations and the
role-inbox guard). When this ships, the spec is done.

## Decisions (resolved — formerly "open questions")
- **Hard-delete:** not built. Suppress (reversible) is the correct, safe default.
- **Layout positions:** computed in-browser (correct at this scale); not persisted
  server-side — no infra warranted.
- **Overview density:** the degree-filtered overview is legibility, not a limit —
  drilling into a cluster loads its complete membership (readability spec).
- **Authoring:** correcting existing entity facts (name/org/email/notes) + merge +
  suppress only; no new entities/relations/observations. Confirmed with Josh
  2026-07-03.
- **Merge target selection:** type-ahead search in the drawer (`GET /api/graph/search`),
  not click-on-canvas — works even when the duplicate isn't on-screen. (Josh 2026-07-03.)
- **Field editing:** explicit edit-mode with Save/Cancel, not always-inline —
  intentional writes on a curation surface. (Josh 2026-07-03.)
- **Release versions** are chosen at release time, not fixed in the spec.
