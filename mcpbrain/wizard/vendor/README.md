# Vendored graph libraries

Browser UMD builds, committed so the dashboard graph works fully offline
(no CDN, no build step). Served by the daemon at `/vendor/<name>.js`.

| File | Package | Version | Source |
|---|---|---|---|
| graphology.umd.min.js | graphology | 0.25.4 | jsDelivr npm dist |
| sigma.min.js | sigma | 2.4.0 | jsDelivr npm build (UMD; v3 is ESM-only) |
| graphology-layout-forceatlas2.min.js | graphology-layout-forceatlas2 | 0.4.4 | jsDelivr npm build |

Globals exposed: `window.graphology` (Graph constructor), `window.Sigma`,
`window.forceAtlas2` (`.assign(graph, opts)`).

**Deviation from the original plan:** `graphology-layout-forceatlas2` stopped
publishing a `build/*.min.js` (UMD) bundle after 0.5.x — every version from
0.6.0 through the current 0.10.1 ships only CommonJS source (`index.js`,
`require()`-based, no bundled dist). 0.4.4 is the newest published version
that still has a UMD build under `build/`, so it's what's vendored here; its
exposed global is `window.forceAtlas2` (not `graphologyLayoutForceAtlas2` as
originally assumed, and it has no separate `.min.js` name distinct from the
`build/` path). Verified compatible with the vendored graphology 0.25.4:
built a `graphology.Graph`, ran `forceAtlas2.assign(graph, {iterations, settings})`
against it, and confirmed node positions moved as expected — the algorithm's
public API (`assign`/`inferSettings`, `graph.forEachNode`/attribute
getters-setters) has been stable across these graphology versions.

To update: re-fetch the same paths at a new pinned version, re-run
`tests/test_graph_assets.py` and `tests/test_graph_page.py`, and update this table.
If a future `graphology-layout-forceatlas2` release restores a UMD dist,
prefer that over 0.4.4 and update the global name accordingly.
