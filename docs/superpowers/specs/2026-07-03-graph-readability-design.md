# Graph Readability — Map-first Clustered Layout with Semantic Zoom

**Date:** 2026-07-03
**Status:** Approved (design); implementation to be planned next.
**Context:** The `/graph` explorer (force-graph + d3-force, shipped 0.7.83) renders
a live but *undifferentiated* layout — ~2,937 nodes at degree≥7 settle into one
colourful "hairball". Communities exist (34 Leiden clusters, titled) but are only
*coloured*, not *spatially grouped*. This design makes the graph read as a
**map**: legible groups you can point at and name, with detail revealed on zoom.

## Decisions (from brainstorming)

- **Primary job:** map-first — open on a legible overview, then dive in to explore.
- **Grouping:** switchable — **community / org / type** (a "Group by" control that
  drives *spatial clustering only*).
- **Colour:** fixed to **entity type** (person / org / project / document / topic /
  meeting). Colour always tells you *what kind* of thing a node is, independent of how
  the map is grouped — so re-grouping never repaints the nodes.
- **Density:** semantic zoom — overview shows cluster regions + names + top hubs;
  zooming in progressively reveals member nodes and their labels.

## Chosen approach

**A — Force-clustered "islands" + semantic zoom.** Keep the live d3-force
simulation, add a per-group attractor so groups settle into visible islands, draw
labelled translucent region-hulls behind them, and drive level-of-detail from the
zoom scale. Rejected: **B** deterministic grouped grid (stable but static, reads
like a dashboard, loses the motion the user valued); **C** bubble-overview→expand
(user explicitly chose semantic zoom over bubbles).

This is a **`graph.html`-only evolution** — the backend `/api/graph/canvas` already
returns `community`, `org`, `type`, and `connections`, which is everything the
clustering, colouring, hub-ranking and edge-typing need. No backend change.

## Design detail

### 1. Clustered layout (the grouping)
- A node's **group key** = the active dimension's value: `community` (id), `org`
  (string; empty → "unassigned"), or `type`.
- Compute a **stable anchor per group**: collect distinct group keys, sort them
  deterministically (by descending member count, tie-break by key) and place their
  anchors evenly on a circle in layout space, radius `R = 40 * sqrt(numGroups)` (so
  more groups → bigger ring). Anchor `i`: `(R*cos(2πi/G), R*sin(2πi/G))`.
- Apply attraction with d3 accessors: `d3.forceX(n => anchor(n).x).strength(0.28)`
  and `d3.forceY(n => anchor(n).y).strength(0.28)`, alongside the existing
  `forceManyBody` (charge) and `forceCollide`. **Drop the global `forceCenter`** in
  clustered mode (the per-group X/Y forces provide centring); keep charge weaker
  (`strength(-40)`) so clusters don't blow apart.
- **"Group by" control** (community/org/type) recomputes group keys → anchors →
  hubs → hull membership, updates the forceX/forceY accessors, and
  `d3ReheatSimulation()`. It changes **only the spatial grouping**.
- **Node colour is fixed to entity type** (`--graph-<type>` tokens) and never changes
  when you regroup. **Hull tint** encodes the active group so islands stay visually
  distinct: `--graph-community-*` when grouped by community, a hashed palette when by
  org, the type colour when by type.

### 2. Labelled region hulls
- Each group's members define a **convex hull** via `d3.polygonHull` (full d3 is
  vendored). Draw in `onRenderFramePre` (behind nodes): filled path in the group
  colour at low alpha (~0.08) + a faint stroke, padded outward ~18px.
- **Cluster label** = the group's name (community `title` from the payload's
  `communities` map; org string; type name) drawn at the hull **centroid**.
- **Performance:** recompute hull polygons on a **throttle (~150 ms)**, cache them,
  and redraw the cached polygons every frame. Groups with < 3 nodes skip the hull
  (draw a small pill behind the single/pair instead).

### 3. Semantic zoom (level-of-detail)
Driven by `globalScale` (passed to `nodeCanvasObject`) and the current `zoom()`:
- **Hub** = top `K=4` nodes by `connections` within each group (precomputed on load/
  regroup).
- **Overview (low zoom, `globalScale < 1.5`):** draw hulls + cluster names + **hub
  nodes only** (non-hub nodes hidden or drawn as faint 1px dots). No per-node labels.
- **Mid zoom:** all nodes drawn; labels for hubs + hovered/selected.
- **In (high zoom, `globalScale > 3`):** all nodes + all in-viewport labels (with
  label-collision skipping). **Cluster names fade out** (opacity ramps from 1→0
  across `globalScale` 1.5→3) so they don't fight node labels.
- **Click a hull / cluster label / legend row → drill into that cluster.** Re-fetch
  the cluster's *complete* membership from `/api/graph/canvas` filtered to that group
  (`community=<id>` / `org=<name>` / `type=<name>`, with `min_conn=1`), render just
  that cluster (its members + internal links), and `zoomToFit`. A **"← Back to map"**
  breadcrumb returns to the overview. This is what lets the overview stay
  degree-filtered for legibility *without* hiding anything — a cluster's full detail
  is always one click away, and it needs no backend change (the filter params already
  exist).
- Thresholds live in named constants at the top of the script for easy tuning.

### 4. Edges that teach structure
- Precompute each node's group key → an edge is **intra** (endpoints same group) or
  **inter** (different groups).
- Default: **intra-cluster edges → a whisper** (`--graph-link-mute`, width 0.3);
  **inter-cluster edges stay visible** (`--graph-link-idle`, width 0.6) so you can
  *see how groups connect* — the point of an overview map.
- Hover/select still lights that node's incident edges **hot** at full width
  (existing behaviour, unchanged).

### 5. Declutter, legend, counts
- `forceCollide` radius = node radius + 2 (already present) prevents overlap.
- **Label-collision skip:** when drawing labels, skip one if its box overlaps an
  already-drawn label this frame (cheap AABB check against a per-frame list).
- **Legend** (top-right overlay): the **entity-type colour key** (person / org /
  project / document / topic / meeting → their `--graph-<type>` colours) so colours
  are always decodable. Group identities are read from the **hull labels** on the
  map; clicking a hull or its label drills into that cluster (§3).
- Header **count** stays: "N nodes · M links · G groups".

### 6. Overview framing
- On load and on regroup, after a short settle (or `cooldownTime` end), call
  `zoomToFit(400ms, 60px)` so the whole map frames itself.
- `prefers-reduced-motion`: skip the animated settle (higher `warmupTicks`,
  `cooldownTime(0)`); hulls/labels/LOD still render on the static layout.

## Constraints (unchanged from the graph feature)
- Self-contained / offline: force-graph + full d3 already vendored under
  `wizard/vendor/`; `d3.polygonHull` comes from the full d3 bundle (no new lib).
- `graph.html` stays a single inline-CSS/JS page served token-injected at `/graph`.
- Bearer-gated `/api/graph/canvas` unchanged.

## Testing
- **Backend:** unchanged — existing `test_graph_view` / `test_graph_routes` cover it.
- **Static markers** (`test_graph_page.py`): assert the new hooks exist — a
  `Group by` control, `polygonHull`, `zoomToFit`, `onRenderFramePre`, the LOD
  constants, and the legend container.
- **JS syntax:** `node --check` on the inline script.
- **Visual:** a stubbed-fetch render harness (inlined vendored libs + mock clusters)
  published as an artifact to confirm islands + hulls + labels + semantic zoom +
  group-by actually render and animate. (Curl can't see a blank canvas; this is the
  real check — the lesson from 0.7.80/0.7.83.)

## Behaviour completeness

This ships as one complete implementation, not a reduced first cut:
- **Overview** shows the whole-graph map, degree-filtered (≤5000) purely for
  legibility — this is the readable bird's-eye, not a limitation.
- **Drilling into any cluster loads its complete membership** (the click behaviour
  in §3, via the existing filter params), so no detail is permanently hidden behind
  a degree threshold.
- **Layout runs in-browser** — the correct choice at ~3k nodes; positions are not
  persisted server-side (no server infra is warranted for this scale). This is a
  decision, not a deferral.

## Out of scope (genuinely separate features, not reduced versions of this one)
- **Entity detail drawer** on node click — its own feature (graph "detail" work).
- **Graph editing** (rename/merge/delete) — its own feature.
- No backend change is required for anything in this spec.
- Release version is chosen at release time (not fixed in the spec).

## Implementation order

Built in this sequence for testability; ships together as one complete change:
1. **Cluster layout + group-by control** — anchors, forceX/forceY, colour-follows-
   group, regroup+reheat, overview `zoomToFit`.
2. **Hulls + cluster labels + legend** — throttled `polygonHull`, centroid labels,
   clickable legend.
3. **Semantic zoom + drill-in + edge typing** — LOD by `globalScale`, cluster-label
   fade, cluster drill-in (full membership) + back-to-map, intra/inter edge
   treatment, label-collision skip.
