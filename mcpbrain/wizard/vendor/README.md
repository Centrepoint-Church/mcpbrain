# Vendored graph libraries

Browser UMD builds, committed so the dashboard graph works fully offline
(no CDN, no build step). Served by the daemon at `/vendor/<name>.js` and loaded
by `wizard/graph.html`.

| File | Package | Version | Source |
|---|---|---|---|
| force-graph.min.js | force-graph | 1.51.4 | jsDelivr npm dist |
| d3.min.js | d3 | 7.9.0 | jsDelivr npm dist (full bundle; provides `d3-force`) |

Globals exposed: `window.ForceGraph` (constructor: `new ForceGraph(el)`),
`window.d3` (used for `forceManyBody`/`forceCollide`/`forceLink`/`forceRadial`/
`forceCenter`).

The graph renderer in `graph.html` is a direct port of the ops-brain
`workstation-frontend/src/workspaces/graph/explore/canvas.ts` — force-graph
driving a live d3-force simulation, retuned to mcpbrain's palette via the
`--graph-*` CSS tokens defined in `graph.html`.

The full `d3` bundle is vendored (rather than the standalone `d3-force`, which
is not self-contained — it `require`s `d3-quadtree`/`d3-dispatch`/`d3-timer`).

To update: re-fetch the same paths at a new pinned version, re-run
`tests/test_graph_assets.py` and `tests/test_graph_page.py`, and update this table.
