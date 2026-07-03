# Graph Readability (Clustered Map + Semantic Zoom) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the `/graph` explorer from an undifferentiated force "hairball" into a legible map — spatially clustered islands (grouped by community/org/type), translucent labelled region-hulls, entity-type colours, semantic-zoom level-of-detail, click-to-drill-into-a-cluster, and structure-revealing edges.

**Architecture:** A single-file evolution of `mcpbrain/wizard/graph.html` (force-graph + vendored d3). Add a per-group attractor force (`d3.forceX/forceY` toward computed cluster anchors) so groups form islands; draw hulls via `d3.polygonHull` in `onRenderFramePre`; drive level-of-detail from `globalScale`; drill into a cluster by re-fetching it filtered. **No backend change** — `/api/graph/canvas` already returns `community`, `org`, `type`, `connections`, and the `communities` title map, and already supports `community=`/`org=`/`type=`/`min_conn=` filters for drill-in.

**Tech Stack:** Vanilla JS in one HTML file; vendored `force-graph` 1.51.4 + full `d3` 7.9.0 (already under `mcpbrain/wizard/vendor/`, includes `d3-polygon` → `d3.polygonHull`/`d3.polygonCentroid`); served token-injected at `/graph`.

## Global Constraints

- **Self-contained / offline:** no CDN, no npm, no build step. All JS is vendored (`force-graph.min.js`, `d3.min.js`) and served at `/vendor/<name>.js`. `graph.html` keeps inline CSS/JS only.
- **No backend change.** `/api/graph/canvas` is unchanged; drill-in reuses its existing `community=`/`org=`/`type=` + `min_conn=` params.
- **Colour is fixed to entity type** (person/org/project/document/topic/meeting via `--graph-<type>` tokens). Regrouping never repaints nodes.
- **"Group by" drives spatial grouping only** (community / org / type).
- **Hull tint encodes the active group** (community → `--graph-community-*`; org → hashed community palette; type → the type colour).
- **Overview stays degree-filtered for legibility** (the `min_conn` slider, ≤5000 cap). **Drilling into a cluster loads its complete membership** (`min_conn=1` + the group filter). No detail is permanently hidden.
- **Reduced motion:** `prefers-reduced-motion` → higher `warmupTicks`, `cooldownTime(0)`; hulls/labels/LOD still render on the static layout.
- **Verification reality:** there is no JS unit-test harness in this repo. Frontend tasks are verified by (a) `node --check` on the inline script, (b) static-marker assertions in `tests/test_graph_page.py`, and (c) a **stubbed-fetch render harness** published as an artifact (curl cannot see a blank canvas — this caught real bugs in 0.7.80/0.7.83).

**Current file state:** `mcpbrain/wizard/graph.html` currently renders the force-graph view shipped in 0.7.83 (colour-by control, free/ego forces, hover tooltip, degree/org/type filters). Task 1 **replaces it wholesale** with the clustered-map version below.

## File Structure

- **Modify (replace):** `mcpbrain/wizard/graph.html` — the whole renderer.
- **Modify:** `tests/test_graph_page.py` — swap the Sigma-era/plain-force markers for the clustered-map markers.
- No other files change. Vendored libs, routes, backend, and `tests/test_graph_view.py` / `tests/test_graph_routes.py` / `tests/test_graph_assets.py` are untouched.

---

### Task 1: Replace `graph.html` with the clustered-map renderer

**Files:**
- Modify (replace entire file): `mcpbrain/wizard/graph.html`

**Interfaces:**
- Consumes: `GET /api/graph/canvas` (payload `{nodes:[{id,name,type,org,connections,community,...}], links:[{source,target,relation,strength}], communities:{ "<id>": "<title>" }}`); vendored globals `window.ForceGraph`, `window.d3` (incl. `d3.polygonHull`, `d3.polygonCentroid`).
- Produces: the `/graph` page. Markers later tasks assert: `id="f-group"`, `id="legend"`, `id="back-btn"`, `forceX`, `forceY`, `polygonHull`, `zoomToFit`, `onRenderFramePre`, `screen2GraphCoords`.

- [ ] **Step 1: Replace the file** with exactly this content:

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
    --graph-bg:#ffffff; --graph-label:#12151b; --graph-fade:#c9ced7;
    --graph-link-idle:#dfe3ea; --graph-link-mute:#eef1f5; --graph-link-hot:#0b5cff;
    --graph-ring:#cdbff5; --graph-ring-text:#697280; --graph-node-fallback:#7a828d;
    --graph-person:#0b5cff; --graph-org:#6a4cff; --graph-project:#1f9d54;
    --graph-document:#b7791f; --graph-topic:#0e7490; --graph-meeting:#db2777;
    --graph-community-0:#0b5cff;  --graph-community-1:#6a4cff;  --graph-community-2:#1f9d54;
    --graph-community-3:#b7791f;  --graph-community-4:#0e7490;  --graph-community-5:#d64545;
    --graph-community-6:#9333ea;  --graph-community-7:#c2410c;  --graph-community-8:#0891b2;
    --graph-community-9:#4d7c0f;  --graph-community-10:#db2777; --graph-community-11:#7c3aed;
    --graph-community-12:#059669; --graph-community-13:#ca8a04; --graph-community-14:#2563eb;
    --graph-community-15:#dc2626; --graph-community-16:#0d9488; --graph-community-17:#a21caf;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 var(--sans);
    -webkit-font-smoothing:antialiased;display:flex;flex-direction:column}
  header{display:flex;align-items:center;gap:14px;padding:12px 18px;
    border-bottom:1px solid var(--line);background:var(--card);flex:0 0 auto}
  .wordmark{font-weight:680;letter-spacing:-.01em}
  .eyebrow{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.11em;color:var(--faint)}
  a.back,.linkbtn{color:var(--muted);text-decoration:none;font-size:13px;background:none;border:0;cursor:pointer;font:inherit}
  a.back:hover,.linkbtn:hover{color:var(--ink)}
  #back-btn{display:none}
  .count{margin-left:auto;font-family:var(--mono);font-size:12.5px;color:var(--muted)}
  .layout{flex:1 1 auto;display:flex;min-height:0}
  .filters{flex:0 0 230px;border-right:1px solid var(--line);background:var(--card);
    padding:16px;display:flex;flex-direction:column;gap:16px;overflow-y:auto}
  .filters label{display:flex;flex-direction:column;gap:6px;font-size:11px;
    text-transform:uppercase;letter-spacing:.07em;color:var(--muted)}
  .filters input[type=range]{width:100%}
  .filters select,.filters input[type=text]{font:inherit;font-size:14px;padding:6px 8px;
    border:1px solid var(--line);border-radius:8px;background:#fff;text-transform:none;color:var(--ink)}
  .deg-val{font-family:var(--mono);color:var(--ink)}
  .hint{font-size:11px;color:var(--faint);line-height:1.4;text-transform:none;letter-spacing:0}
  #stage{flex:1 1 auto;position:relative;min-width:0;background:var(--graph-bg)}
  #graph{position:absolute;inset:0}
  #overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    color:var(--muted);font-size:14px;background:var(--paper);z-index:5;text-align:center;padding:24px}
  #overlay.hidden{display:none}
  #tooltip{position:absolute;z-index:10;pointer-events:none;display:none;background:#1a1f27;color:#fff;
    border-radius:8px;padding:7px 10px;font-size:12.5px;max-width:260px;line-height:1.4}
  #tooltip b{font-weight:640}
  #tooltip .sub{color:#c9ced7}
  #legend{position:absolute;top:12px;right:12px;z-index:8;background:rgba(255,255,255,.92);
    border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12px;
    box-shadow:0 2px 8px rgba(20,24,29,.06)}
  #legend h4{margin:0 0 7px;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--faint);font-weight:700}
  #legend .row{display:flex;align-items:center;gap:7px;padding:1.5px 0;color:var(--ink)}
  #legend .sw{width:10px;height:10px;border-radius:3px;flex:0 0 auto}
</style>
</head>
<body>
  <header>
    <span class="wordmark">mcpbrain</span>
    <span class="eyebrow">Knowledge graph</span>
    <a class="back" href="/dashboard">← Dashboard</a>
    <button id="back-btn" class="linkbtn">← Back to map</button>
    <span id="count" class="count"></span>
  </header>
  <div class="layout">
    <aside class="filters">
      <label>Group by
        <select id="f-group">
          <option value="community" selected>Community</option>
          <option value="org">Organisation</option>
          <option value="type">Type</option>
        </select>
      </label>
      <label>Min connections <span class="deg-val" id="deg-val">7</span>
        <input type="range" id="f-degree" min="1" max="50" value="7">
      </label>
      <label>Organisation
        <select id="f-org"><option value="">All</option></select>
      </label>
      <label>Type
        <select id="f-type"><option value="">All</option></select>
      </label>
      <label>Search
        <input type="text" id="f-search" placeholder="highlight by name" autocomplete="off">
      </label>
      <div class="hint">Drag to pan · scroll to zoom · drag a node to move it · click a cluster to open it · right-click a node to focus its neighbourhood · double-click empty space to reset.</div>
    </aside>
    <div id="stage">
      <div id="graph"></div>
      <div id="legend"></div>
      <div id="overlay">Loading graph…</div>
      <div id="tooltip"></div>
    </div>
  </div>

  <script src="/vendor/d3.min.js"></script>
  <script src="/vendor/force-graph.min.js"></script>
  <script>
    const TOKEN = "__MCPBRAIN_TOKEN__";
    const H = { headers: { Authorization: "Bearer " + TOKEN } };
    const reduce = window.matchMedia("(prefers-reduced-motion:reduce)").matches;
    const $ = (id) => document.getElementById(id);

    // ---- level-of-detail thresholds (tune here) ----
    const LOD_NODES_MIN = 1.5;   // below this zoom: hubs only
    const LOD_LABELS_MIN = 3.0;  // above this zoom: all in-view labels
    const HUBS_PER_GROUP = 4;
    const HULL_PAD = 18, HULL_THROTTLE_MS = 150;

    const TYPE_TOKEN = { person:"--graph-person", org:"--graph-org", project:"--graph-project",
                         document:"--graph-document", topic:"--graph-topic", meeting:"--graph-meeting" };
    const TYPE_ORDER = ["person","org","project","document","topic","meeting"];
    function readVar(n, fb=""){ const v = getComputedStyle(document.documentElement).getPropertyValue(n).trim(); return v || fb; }
    function readPalette(){
      const byType = {}; for (const [k,t] of Object.entries(TYPE_TOKEN)) byType[k] = readVar(t);
      const community = []; for (let i=0;i<18;i++) community.push(readVar(`--graph-community-${i}`));
      return { bg:readVar("--graph-bg","#fff"), label:readVar("--graph-label","#12151b"),
        fade:readVar("--graph-fade","#c9ced7"), linkIdle:readVar("--graph-link-idle","#dfe3ea"),
        linkMute:readVar("--graph-link-mute","#eef1f5"), linkHot:readVar("--graph-link-hot","#0b5cff"),
        ring:readVar("--graph-ring","#cdbff5"), ringText:readVar("--graph-ring-text","#697280"),
        nodeFallback:readVar("--graph-node-fallback","#7a828d"), byType, community };
    }
    const palette = readPalette();
    function hashIndex(s, n){ let h=0; for (let i=0;i<s.length;i++) h=(h*31+s.charCodeAt(i))>>>0; return h % n; }

    // colour is ALWAYS by entity type
    function nodeColour(n){ return palette.byType[n.type] || palette.nodeFallback; }
    function nodeRadius(n){
      const conn = n.connections || 0, scale = window.innerWidth <= 768 ? 1.4 : 1;
      if (n.type === "org" || n.type === "project") return (8 + Math.min(conn*0.15, 12)) * scale;
      return (3 + Math.min(conn*0.08, 8)) * scale;
    }

    const state = { hoveredNodeId:null, selectedNodeId:null, mode:"map", egoNodeId:null,
                    egoDepth:1, egoHops:{}, groupBy:"community", searchTerm:"",
                    drill:null /* {dim,key,name} */ };
    let CURRENT = { nodes:[], links:[], communities:{} };
    let ANCHORS = {};           // groupKey -> {x,y}
    let NODE_GROUP = new Map(); // id -> groupKey
    let HUBS = new Set();       // hub node ids
    let HULLS = [];             // [{poly,cx,cy,name,colour,key}]
    let lastHull = 0;

    function groupKeyOf(n){
      if (state.groupBy === "org") return n.org || "unassigned";
      if (state.groupBy === "type") return n.type || "unknown";
      return n.community == null ? "—" : String(n.community);  // community
    }
    function groupName(key){
      if (state.groupBy === "community") return CURRENT.communities[key] || (key === "—" ? "No community" : "Community " + key);
      if (state.groupBy === "org") return key === "unassigned" ? "Unassigned" : key;
      return key.charAt(0).toUpperCase() + key.slice(1);  // type
    }
    function groupColour(key){
      if (state.groupBy === "type") return palette.byType[key] || palette.nodeFallback;
      if (state.groupBy === "community"){ const i = /^\d+$/.test(key) ? (+key % 18) : hashIndex(key,18); return palette.community[i]; }
      return palette.community[hashIndex(key,18)];  // org
    }

    function recomputeGroups(){
      NODE_GROUP = new Map();
      const counts = new Map();
      for (const n of CURRENT.nodes){ const k = groupKeyOf(n); NODE_GROUP.set(n.id, k); counts.set(k,(counts.get(k)||0)+1); }
      // anchors: groups on a circle, largest first, radius grows with group count
      const keys = [...counts.keys()].sort((a,b)=> (counts.get(b)-counts.get(a)) || (a<b?-1:1));
      const G = keys.length, R = 40 * Math.sqrt(Math.max(1, G));
      ANCHORS = {};
      keys.forEach((k,i)=>{ const a = 2*Math.PI*i/Math.max(1,G); ANCHORS[k] = { x:R*Math.cos(a), y:R*Math.sin(a) }; });
      // hubs: top HUBS_PER_GROUP by connections within each group
      HUBS = new Set();
      const byGroup = new Map();
      for (const n of CURRENT.nodes){ const k = NODE_GROUP.get(n.id); (byGroup.get(k) || byGroup.set(k,[]).get(k)).push(n); }
      for (const arr of byGroup.values()){
        arr.sort((a,b)=>(b.connections||0)-(a.connections||0));
        for (let i=0;i<Math.min(HUBS_PER_GROUP, arr.length);i++) HUBS.add(arr[i].id);
      }
    }
    function anchorOf(n){ return ANCHORS[NODE_GROUP.get(n.id)] || {x:0,y:0}; }

    // ---- hulls (throttled compute, drawn behind nodes) ----
    function recomputeHulls(){
      const byGroup = new Map();
      for (const n of CURRENT.nodes){
        if (n.x == null) continue;
        const k = NODE_GROUP.get(n.id); (byGroup.get(k) || byGroup.set(k,[]).get(k)).push([n.x, n.y]);
      }
      HULLS = [];
      for (const [k, pts] of byGroup){
        if (pts.length < 3) continue;
        const hull = d3.polygonHull(pts); if (!hull) continue;
        const c = d3.polygonCentroid(hull);
        const padded = hull.map(([x,y]) => { const dx=x-c[0], dy=y-c[1], d=Math.hypot(dx,dy)||1;
          return [x + dx/d*HULL_PAD, y + dy/d*HULL_PAD]; });
        HULLS.push({ poly:padded, cx:c[0], cy:c[1], name:groupName(k), colour:groupColour(k), key:k });
      }
    }
    function drawHulls(ctx, globalScale){
      const now = performance.now();
      if (now - lastHull > HULL_THROTTLE_MS){ recomputeHulls(); lastHull = now; }
      for (const h of HULLS){
        ctx.beginPath(); ctx.moveTo(h.poly[0][0], h.poly[0][1]);
        for (let i=1;i<h.poly.length;i++) ctx.lineTo(h.poly[i][0], h.poly[i][1]);
        ctx.closePath();
        ctx.globalAlpha = 0.08; ctx.fillStyle = h.colour; ctx.fill();
        ctx.globalAlpha = 0.5; ctx.strokeStyle = h.colour; ctx.lineWidth = 1/globalScale; ctx.stroke();
        ctx.globalAlpha = 1;
      }
      // cluster labels fade out as you zoom in (overview -> detail)
      const t = (LOD_LABELS_MIN - globalScale) / (LOD_LABELS_MIN - LOD_NODES_MIN);
      const op = Math.max(0, Math.min(1, t));
      if (op > 0.02){
        for (const h of HULLS){
          ctx.save(); ctx.globalAlpha = op * 0.9; ctx.fillStyle = h.colour;
          ctx.font = `700 ${13/globalScale}px ${getComputedStyle(document.body).fontFamily}`;
          ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText(h.name, h.cx, h.cy); ctx.restore();
        }
      }
    }

    // ---- ego (right-click focus) ----
    function computeEgoHops(nodes, links, focusId, depth){
      const hops = { [focusId]:0 }, adj = new Map();
      for (const l of links){ const s = typeof l.source==="object"?l.source.id:l.source, t = typeof l.target==="object"?l.target.id:l.target;
        (adj.get(s)||adj.set(s,new Set()).get(s)).add(t); (adj.get(t)||adj.set(t,new Set()).get(t)).add(s); }
      const q=[[focusId,0]];
      while(q.length){ const [id,d]=q.shift(); if(d>=depth) continue;
        for(const nb of adj.get(id)||[]) if(hops[nb]===undefined){ hops[nb]=d+1; q.push([nb,d+1]); } }
      return hops;
    }

    // ---- node + edge drawing ----
    let labelBoxes = [];  // per-frame label collision boxes
    function drawNode(node, ctx, globalScale){
      const r = node._r ?? nodeRadius(node);
      const isHub = HUBS.has(node.id);
      // overview: hubs only; below LOD_NODES_MIN non-hubs draw as faint dots
      if (globalScale < LOD_NODES_MIN && !isHub && node.id !== state.hoveredNodeId && node.id !== state.selectedNodeId){
        ctx.beginPath(); ctx.arc(node.x, node.y, Math.max(0.6, r*0.5), 0, 2*Math.PI);
        ctx.fillStyle = palette.fade; ctx.globalAlpha = 0.5; ctx.fill(); ctx.globalAlpha = 1; return;
      }
      const isSel = node.id === state.selectedNodeId, isHov = node.id === state.hoveredNodeId;
      const isEgo = state.mode === "ego" && state.egoNodeId === node.id;
      const searching = state.searchTerm.length > 0;
      const match = !searching || node.name.toLowerCase().includes(state.searchTerm);
      const fill = isSel ? palette.label : (searching && !match ? palette.fade : nodeColour(node));
      ctx.beginPath(); ctx.arc(node.x, node.y, r, 0, 2*Math.PI); ctx.fillStyle = fill; ctx.fill();
      if (isHov || (searching && match)){ ctx.strokeStyle = palette.label; ctx.lineWidth = 1.5/globalScale; ctx.stroke(); }
      const wantLabel = isEgo || isHub || isHov || isSel || (searching && match) || globalScale >= LOD_LABELS_MIN;
      if (!wantLabel) return;
      // label-collision skip (screen-space AABB)
      const fs = isEgo ? 14 : Math.max(9, Math.min(12, r*globalScale*1.2)) / globalScale;
      ctx.font = `${isEgo?"bold ":""}${fs}px ${getComputedStyle(document.body).fontFamily}`;
      const w = ctx.measureText(node.name).width, lx = node.x, ly = node.y + r + 3/globalScale;
      const box = [lx - w/2, ly, lx + w/2, ly + fs];
      for (const b of labelBoxes){ if (box[0]<b[2]&&box[2]>b[0]&&box[1]<b[3]&&box[3]>b[1]){ if(!(isHub||isHov||isSel)) return; } }
      labelBoxes.push(box);
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      ctx.save(); ctx.globalAlpha = searching && !match ? 0.30 : 0.85; ctx.fillStyle = palette.label;
      ctx.fillText(node.name, lx, ly); ctx.restore();
    }
    function edgeKind(l){
      const s = typeof l.source==="object"?l.source.id:l.source, t = typeof l.target==="object"?l.target.id:l.target;
      return NODE_GROUP.get(s) === NODE_GROUP.get(t) ? "intra" : "inter";
    }
    function linkColourFor(l){
      const focus = state.hoveredNodeId || state.selectedNodeId;
      const sid = typeof l.source==="object"?l.source.id:l.source, tid = typeof l.target==="object"?l.target.id:l.target;
      if (focus) return (sid===focus||tid===focus) ? palette.linkHot : palette.linkMute;
      if (state.searchTerm){ const sn=l.source?.name?.toLowerCase().includes(state.searchTerm), tn=l.target?.name?.toLowerCase().includes(state.searchTerm);
        return (sn||tn) ? palette.linkHot : palette.linkMute; }
      return edgeKind(l) === "inter" ? palette.linkIdle : palette.linkMute;
    }
    function linkWidthFor(l){
      const focus = state.hoveredNodeId || state.selectedNodeId;
      const sid = typeof l.source==="object"?l.source.id:l.source, tid = typeof l.target==="object"?l.target.id:l.target;
      if (focus) return (sid===focus||tid===focus) ? 1.6 : 0.4;
      return edgeKind(l) === "inter" ? 0.6 : 0.3;
    }
    function drawEgoRings(ctx){
      if (state.mode !== "ego" || !state.egoNodeId) return;
      const ringR = 90; ctx.save(); ctx.strokeStyle = palette.ring; ctx.lineWidth = 1;
      for (let hop=1; hop<=state.egoDepth; hop++){ ctx.beginPath(); ctx.arc(0,0,hop*ringR,0,2*Math.PI); ctx.stroke();
        ctx.fillStyle = palette.ringText; ctx.font = "11px "+getComputedStyle(document.body).fontFamily;
        ctx.textAlign="center"; ctx.textBaseline="bottom"; ctx.fillText(`${hop} hop${hop>1?"s":""}`, 0, -hop*ringR-4); }
      ctx.restore();
    }

    // ---- force-graph init ----
    const container = $("graph");
    const fg = new ForceGraph(container)
      .backgroundColor(palette.bg).nodeId("id").linkSource("source").linkTarget("target")
      .nodeRelSize(1).nodeVal((n)=>Math.pow(n._r ?? nodeRadius(n),2)).nodeLabel(()=> "")
      .linkColor(linkColourFor).linkWidth(linkWidthFor)
      .onRenderFramePre((ctx, gs)=>{ labelBoxes = []; drawHulls(ctx, gs); })
      .nodeCanvasObject((node, ctx, gs)=> drawNode(node, ctx, gs))
      .onRenderFramePost((ctx)=> drawEgoRings(ctx))
      .onNodeHover((node)=>{ state.hoveredNodeId = node?node.id:null; container.style.cursor = node?"pointer":"default"; showTooltip(node); })
      .onNodeDrag(()=> hideTooltip())
      .onNodeClick((node)=>{ state.selectedNodeId = node.id; })
      .onNodeRightClick((node)=> enterEgo(node.id))
      .onBackgroundClick((ev)=> onBg(ev))
      .warmupTicks(reduce ? 80 : 30).cooldownTime(reduce ? 0 : 4000);

    fg.d3Force("charge", d3.forceManyBody().strength(-40).distanceMax(300));
    fg.d3Force("collide", d3.forceCollide().radius((n)=>(n._r ?? nodeRadius(n)) + 2));
    const linkF = fg.d3Force("link");
    if (linkF) linkF.distance(60).strength((l)=>{ const s=(l.source.connections)||1, t=(l.target.connections)||1; return 0.1/Math.min(s,t); });
    fg.d3AlphaDecay(0.02);

    function applyClusterForces(){
      fg.d3Force("center", null); fg.d3Force("radial", null);
      fg.d3Force("x", d3.forceX((n)=>anchorOf(n).x).strength(0.28));
      fg.d3Force("y", d3.forceY((n)=>anchorOf(n).y).strength(0.28));
      for (const n of CURRENT.nodes){ n.fx = undefined; n.fy = undefined; }
    }
    function applyEgoForces(){
      fg.d3Force("x", null); fg.d3Force("y", null); fg.d3Force("center", null);
      const f = CURRENT.nodes.find((n)=>n.id===state.egoNodeId); if (f){ f.fx=0; f.fy=0; }
      for (const n of CURRENT.nodes) if (n.id!==state.egoNodeId){ n.fx=undefined; n.fy=undefined; }
      fg.d3Force("radial", d3.forceRadial((n)=>(state.egoHops[n.id]||0)*90,0,0).strength(0.8));
    }
    function relayout(){
      if (state.mode === "ego" && state.egoNodeId) applyEgoForces(); else applyClusterForces();
      fg.d3ReheatSimulation();
    }
    function enterEgo(id){
      state.mode="ego"; state.egoNodeId=id; state.selectedNodeId=id;
      state.egoHops = computeEgoHops(CURRENT.nodes, CURRENT.links, id, state.egoDepth);
      applyEgoForces(); fg.d3ReheatSimulation();
    }
    function exitEgo(){ if (state.mode!=="ego") return; state.mode="map"; state.egoNodeId=null; state.egoHops={}; applyClusterForces(); fg.d3ReheatSimulation(); }

    // background click: exit ego, else hit-test hulls -> drill into a cluster
    function onBg(ev){
      if (state.mode === "ego"){ exitEgo(); return; }
      if (state.drill){ return; }  // already inside a cluster
      const p = fg.screen2GraphCoords(ev.offsetX, ev.offsetY);
      for (const h of HULLS){ if (pointInPoly([p.x,p.y], h.poly)){ drillInto(h.key); return; } }
      state.selectedNodeId = null;
    }
    function pointInPoly(pt, poly){
      let inside=false; for (let i=0,j=poly.length-1;i<poly.length;j=i++){
        const xi=poly[i][0],yi=poly[i][1],xj=poly[j][0],yj=poly[j][1];
        if (((yi>pt[1])!==(yj>pt[1])) && (pt[0] < (xj-xi)*(pt[1]-yi)/(yj-yi)+xi)) inside=!inside; }
      return inside;
    }
    function drillInto(key){ state.drill = { dim: state.groupBy, key, name: groupName(key) }; $("back-btn").style.display="inline"; load(); }
    $("back-btn").addEventListener("click", ()=>{ state.drill=null; $("back-btn").style.display="none"; load(); });

    // ---- tooltip ----
    const tip = $("tooltip");
    function showTooltip(node){
      if (!node){ hideTooltip(); return; }
      const hop = state.mode==="ego" ? state.egoHops[node.id] : undefined;
      tip.innerHTML = ""; const b=document.createElement("b"); b.textContent=node.name; tip.appendChild(b);
      const sub=document.createElement("div"); sub.className="sub";
      sub.textContent = (node.type||"")+" · "+(node.org||"no org")+" · "+(node.connections||0)+" connections"+(hop!==undefined?" · hop "+hop:"");
      tip.appendChild(sub); tip.style.display="block";
    }
    function hideTooltip(){ tip.style.display="none"; }
    $("stage").addEventListener("mousemove", (e)=>{ if(tip.style.display==="block"){ tip.style.left=(e.offsetX+14)+"px"; tip.style.top=(e.offsetY+14)+"px"; } });
    container.addEventListener("dblclick", ()=>{ if(!state.hoveredNodeId) exitEgo(); });

    // ---- legend (fixed entity-type colour key) ----
    function renderLegend(){
      const el = $("legend"); el.innerHTML = "<h4>Type</h4>";
      for (const t of TYPE_ORDER){ const row=document.createElement("div"); row.className="row";
        const sw=document.createElement("span"); sw.className="sw"; sw.style.background=palette.byType[t]||palette.nodeFallback;
        const lb=document.createElement("span"); lb.textContent=t;
        row.appendChild(sw); row.appendChild(lb); el.appendChild(row); }
    }
    renderLegend();

    // ---- sizing ----
    function fit(){ const r=$("stage").getBoundingClientRect(); fg.width(r.width).height(r.height); }
    fit(); new ResizeObserver(fit).observe($("stage"));

    // ---- filters + data ----
    function populateOptions(nodes){
      const orgs=new Set(), types=new Set();
      for (const n of nodes){ if(n.org) orgs.add(n.org); if(n.type) types.add(n.type); }
      for (const [sel,vals] of [["f-org",orgs],["f-type",types]]){ const el=$(sel), cur=el.value;
        el.innerHTML='<option value="">All</option>'; [...vals].sort().forEach((v)=>el.add(new Option(v,v))); el.value=cur; }
    }
    function filterQuery(){
      const p=new URLSearchParams();
      if (state.drill){ p.set(state.drill.dim, state.drill.key); p.set("min_conn","1"); }
      else { p.set("min_conn", $("f-degree").value);
        if ($("f-org").value) p.set("org", $("f-org").value);
        if ($("f-type").value) p.set("type", $("f-type").value); }
      return p.toString();
    }
    let loading=false;
    async function load(){
      if (loading) return; loading=true;
      const ov=$("overlay"); ov.textContent = state.drill ? ("Opening “"+state.drill.name+"”…") : "Loading graph…"; ov.classList.remove("hidden");
      try{
        const res = await fetch("/api/graph/canvas?"+filterQuery(), H);
        if (res.status===413){ const i=await res.json(); ov.textContent="Too many nodes ("+(i.candidate_count||"5000+")+") — raise “Min connections”."; loading=false; return; }
        if (!res.ok) throw new Error("HTTP "+res.status);
        const data = await res.json();
        CURRENT = { nodes:data.nodes, links:data.links, communities:data.communities||{} };
        populateOptions(data.nodes);
        const G = new Set(data.nodes.map(groupKeyOf)).size;
        $("count").textContent = data.nodes.length+" nodes · "+data.links.length+" links · "+G+" groups";
        if (!data.nodes.length){ ov.textContent="No entities match these filters."; loading=false; return; }
        for (const n of data.nodes) n._r = nodeRadius(n);
        state.mode="map"; state.egoNodeId=null; state.egoHops={};
        recomputeGroups(); applyClusterForces();
        fg.graphData({ nodes:data.nodes, links:data.links });
        setTimeout(()=> fg.zoomToFit(400, 60), reduce ? 50 : 1200);
        ov.classList.add("hidden");
      }catch(e){ ov.textContent="Couldn’t load the graph. Is the daemon running?"; }
      finally{ loading=false; }
    }

    const deg=$("f-degree");
    deg.addEventListener("input", ()=>{ $("deg-val").textContent=deg.value; });
    deg.addEventListener("change", load);
    $("f-org").addEventListener("change", load);
    $("f-type").addEventListener("change", load);
    $("f-group").addEventListener("change", (e)=>{ state.groupBy=e.target.value; recomputeGroups(); relayout(); });
    let searchT=null;
    $("f-search").addEventListener("input", (e)=>{ clearTimeout(searchT);
      searchT=setTimeout(()=>{ state.searchTerm=e.target.value.trim().toLowerCase(); fg.nodeColor(fg.nodeColor()); }, 150); });

    load();
  </script>
</body>
</html>
```

- [ ] **Step 2: `node --check` the inline script**

Run:
```bash
python3 - <<'PY'
import re,subprocess
js=re.findall(r"<script>(.*?)</script>", open("mcpbrain/wizard/graph.html").read(), re.S)[-1]
open("/tmp/_g.js","w").write(js)
print(subprocess.run(["node","--check","/tmp/_g.js"],capture_output=True,text=True).stderr or "OK")
```
Expected: `OK`.

- [ ] **Step 3: Visually verify with a render harness** (this is the real gate — a global/logic bug shows as a blank or hairball canvas that curl can't detect)

Build a harness that inlines the vendored libs + this page body and stubs `fetch` with ~140 mock nodes across ~8 communities (varied `type`, `org`, `connections`, `community`), publish it as an artifact, and confirm: islands form and drift into place; each island shows a translucent hull + its name; node colours are by type (per the legend); zooming out hides non-hubs and shows cluster names; zooming in reveals members and fades cluster names; clicking a hull re-queries (mock returns the same set — confirm the "Opening…"/back-button path runs and "← Back to map" appears); "Group by" → org/type re-clusters without recolouring nodes. (Reuse the assembly approach from the 0.7.83 verify harness: read `graph.html`, inline `vendor/d3.min.js` and `vendor/force-graph.min.js` in place of the `<script src>` tags, replace `__MCPBRAIN_TOKEN__`, prepend a `window.fetch` stub.)

- [ ] **Step 4: Commit**

```bash
git add mcpbrain/wizard/graph.html
git commit -m "feat(graph): clustered map — group-by islands, hulls, semantic zoom, drill-in"
```

---

### Task 2: Update the page's static-marker tests

**Files:**
- Modify: `tests/test_graph_page.py`

**Interfaces:**
- Consumes: `mcpbrain/wizard/graph.html` from Task 1.

- [ ] **Step 1: Replace the markers test** — find `test_graph_html_has_expected_hooks` and set its marker list to:

```python
    for marker in ['/vendor/force-graph.min.js', '/vendor/d3.min.js',
                   '/api/graph/canvas', 'id="graph"', 'new ForceGraph',
                   'd3.forceManyBody', 'id="f-group"', 'polygonHull',
                   'onRenderFramePre', 'zoomToFit', 'screen2GraphCoords',
                   'id="legend"', 'id="back-btn"', '__MCPBRAIN_TOKEN__']:
        assert marker in html, f"missing: {marker}"
```

(Leave the `test_graph_html_js_syntax` test and all other tests in the file as-is — they still apply. If a `new Sigma` / `forceAtlas2` marker lingers anywhere in the file from an earlier era, remove it.)

- [ ] **Step 2: Run the graph page tests**

Run: `uv run pytest tests/test_graph_page.py -q`
Expected: PASS (all tests in the file, including the JS `node --check` test).

- [ ] **Step 3: Commit**

```bash
git add tests/test_graph_page.py
git commit -m "test(graph): assert clustered-map page hooks"
```

---

### Task 3: Release gate — full suite, ruff, wheel packaging

**Files:** none changed (verification only).

- [ ] **Step 1: Full test suite**

Run: `uv run pytest -q`
Expected: all pass (1 skipped is normal). Confirm `tests/test_graph_view.py`, `tests/test_graph_routes.py`, `tests/test_graph_assets.py` are green (backend + vendoring unaffected).

- [ ] **Step 2: Lint**

Run: `uv run ruff check mcpbrain/`
Expected: `All checks passed!`

- [ ] **Step 3: Confirm the wheel still ships `graph.html` + vendored libs**

Run:
```bash
uv build --wheel 2>&1 | tail -1
python3 - <<'PY'
import zipfile,glob
z=zipfile.ZipFile(sorted(glob.glob("dist/*.whl"))[-1])
need=["mcpbrain/wizard/graph.html","mcpbrain/wizard/vendor/force-graph.min.js","mcpbrain/wizard/vendor/d3.min.js"]
for n in need: print(n, "OK" if n in z.namelist() else "MISSING")
PY
```
Expected: all three `OK`.

- [ ] **Step 4: (No commit)** — this task only gates. Release (version bump across the four files, dist wheel, plugin sync, daemon update) is a **separate, explicit step** per `docs/RELEASE-RUNBOOK.md`; the version number is chosen at release time, not fixed in this plan.

---

## Final verification (before release)

- [ ] `uv run pytest -q` green; `uv run ruff check mcpbrain/` clean.
- [ ] Render-harness artifact reviewed: islands + hulls + labels + type-colours + semantic zoom + group-by + drill-in/back all work.
- [ ] Manual on the live daemon after install: open `/graph` → clustered map renders; "Group by" re-clusters; clicking a cluster opens its full membership and "← Back to map" returns; right-click ego-focus works; reduced-motion renders a static clustered map.

## Notes / decisions (from the spec)

- **Colour is fixed to entity type**; **Group by** changes spatial grouping only. Regrouping never repaints nodes; hull tint carries group identity.
- **Overview is degree-filtered for legibility; drilling into a cluster loads its complete membership** (`min_conn=1` + the group filter) — no backend change, and no detail permanently hidden.
- **Layout is in-browser** (correct at ~3k nodes); positions are not persisted server-side.
- Entity detail drawer and graph editing are **separate features**, not part of this change.
