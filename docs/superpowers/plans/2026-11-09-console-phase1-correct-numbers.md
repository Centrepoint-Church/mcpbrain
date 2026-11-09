# Console Phase 1: Correct Numbers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The dashboard's stats payload reports correct, bounded, labelled numbers plus freshness and a health verdict, and the current dashboard page reads them, so "153% digested" can never recur.

**Architecture:** A new pure module `mcpbrain/console_stats.py` holds every count and the `health()` verdict, each a function over one read-only SQLite connection (or plain values) so it is testable without a daemon. `dashboard.stats()` opens one connection, calls those functions and assembles the new payload; the existing route is unchanged. `wizard/dashboard.html` is minimally re-pointed at the new fields (the full redesign is Phase 5).

**Tech Stack:** Python 3 stdlib (`sqlite3`, `datetime`), pytest, the existing `ControlServer`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-06-console-redesign-design.md` (sections "What's wrong today", 2a and 2c, 6 "Data layer", Delivery order phase 1)

## Staleness markers

- Last retirement check: 2026-11-09 against state/retired.md (52 entries scanned)
- Last cross-checked against state/decisions.md: 2026-11-09
- Index: `docs/superpowers/plans/2026-11-09-console-redesign-index.md`
- Deviations from the spec, deliberate: (1) spec section 6 has `daemon.status()` gain wall-clock `last_*_at` fields; this plan reads freshness from the store (`sync_cursors.updated_at`, `enrich_payloads.at`) instead, which is the durable record and survives a daemon restart. (2) `needs_you.findings` is a count here; the list with its actions (mark fixed, show the log) arrives in Phase 2, which builds those endpoints.

## Global Constraints

- Public repo: no real names, emails, org names or owner paths anywhere (fixtures use `Acme`, `Dana Okafor`, `Sam Chen`); `tests/test_no_tenant_literals.py` passes.
- No new dependencies.
- Every percentage is bounded 0 to 100; every count is ≥ 0.
- `pct` never reads 100 while anything is waiting.
- Stale threshold: 6 hours, one constant (`STALE_HOURS = 6.0`), used for sources and for enrichment.
- Timestamps in the payload are ISO 8601 UTC strings ending `+00:00`, or `null`.
- Source freshness reads the base cursor keys only: `gmail`, `drive`, `calendar`, `anarlog`. Never the `drive:<id>` shared-drive cursors, never `*_backfill_*`.
- Health precedence: paused, stalled, needs_you, behind, healthy (first match wins).
- Eligible chunks: `enrich_state` is NULL, `''` or `'duplicate'`. `'cold'` is never eligible.

## Review Focus

1. **Mixed timestamp formats in `sync_cursors.updated_at` and `enrich_payloads.at`** (`2026-11-06 01:40:03` from `datetime('now')`, and `2026-11-09T05:53:12.349216+00:00`): both parse as UTC; an unparseable value reads as `null`, never a crash. Test in Task 3.
2. **A shared drive that stopped syncing** (one `drive:<id>` cursor months old): must not mark Drive or the system stale. Test in Task 3.
3. **An older store schema** without `invalidated_at` on relations/observations, or without `enrich_payloads`/`sync_cursors`: counts degrade (all rows live, `null` times), the payload still returns. Tests in Tasks 2 and 3.
4. **A paused daemon**: every timestamp freezes, so staleness must not report Stalled while paused. Test in Task 4.
5. **A snapshot taken mid-cycle** where counts move between queries: one connection per `stats()` call keeps the numbers mutually consistent; `waiting` is computed from the same query as `eligible`. Test in Task 1 (no negative `waiting` for any combination).

---

### Task 1: Index counts that cannot exceed 100%

**Files:**
- Create: `mcpbrain/console_stats.py`
- Test: `tests/test_console_stats.py`

**Interfaces:**
- Produces: `index_counts(db: sqlite3.Connection) -> dict` returning `{"total", "eligible", "cold", "duplicate", "enriched_eligible", "waiting", "pct"}` (all `int`). `pct_of(done: int, eligible: int) -> int`.

- [ ] **Step 1: Write the failing tests**

Fixture helper `_chunks(db, rows)` creates `chunks(rowid INTEGER PRIMARY KEY, enrich_state TEXT, enriched INTEGER DEFAULT 0)` and inserts `(enrich_state, enriched)` pairs.

```python
def test_cold_enriched_chunks_do_not_count_toward_pct(tmp_path):
    # the 153% shape: 6 cold chunks were enriched before being marked cold
    db = _db(tmp_path); _chunks(db, [("cold", 1)] * 6 + [("cold", 0)] * 4 + [(None, 1)] * 3 + [("", 0)] * 1)
    out = index_counts(db)
    assert out == {"total": 14, "eligible": 4, "cold": 10, "duplicate": 0,
                   "enriched_eligible": 3, "waiting": 1, "pct": 75}

def test_null_and_empty_state_are_both_eligible(tmp_path): ...   # (None,0) and ("",0) -> eligible 2
def test_duplicates_are_eligible(tmp_path): ...                   # ("duplicate",1) -> eligible 1, duplicate 1, pct 100
def test_pct_floors_while_anything_waits(tmp_path):
    # 999 done of 1000 is 99.9; must read 99, never 100
    ...; assert out["pct"] == 99 and out["waiting"] == 1
def test_pct_is_100_only_when_nothing_waits(tmp_path): ...       # all eligible enriched -> 100
def test_empty_or_all_cold_store_is_zero_not_error(tmp_path): ... # eligible 0 -> pct 0, waiting 0
def test_waiting_never_negative(tmp_path): ...                    # every (state, enriched) combo -> waiting >= 0, 0 <= pct <= 100
def test_missing_chunks_table_degrades_to_zeros(tmp_path): ...
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_console_stats.py -q`
Expected: FAIL, `ModuleNotFoundError: mcpbrain.console_stats`

- [ ] **Step 3: Implement `index_counts` and `pct_of` in `mcpbrain/console_stats.py`**

One `SELECT` with `SUM(CASE …)` columns so every figure comes from the same snapshot. Eligible is `enrich_state IS NULL OR enrich_state IN ('', 'duplicate')`. `pct_of`: `0` when `eligible == 0`; `100` when `done >= eligible`; otherwise `floor(100 * done / eligible)`, capped at 99. Guard a missing `chunks` table or `enriched` column by returning zeros (read `sqlite_master` / `PRAGMA table_info` the way `dashboard._graph_counts` does).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_console_stats.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/console_stats.py tests/test_console_stats.py
git commit -m "feat(console): index counts measured against eligible chunks only"
```

### Task 2: Graph counts people would recognise

**Files:**
- Modify: `mcpbrain/console_stats.py`
- Test: `tests/test_console_stats.py`

**Interfaces:**
- Produces: `graph_counts(db) -> dict` returning `{"by_type": {"person","org","project","topic","meeting","document","thread","unknown": int}, "relations_live": int, "observations_live": int, "unlinked": int}`. Every one of the eight type keys is always present.

- [ ] **Step 1: Write the failing tests**

Fixture creates `entities(id TEXT PRIMARY KEY, type TEXT)`, `entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, entity_b TEXT, invalidated_at TEXT)`, `entity_observations(id INTEGER PRIMARY KEY, entity_id TEXT, invalidated_at TEXT)`.

```python
def test_by_type_always_has_all_eight_keys(tmp_path): ...        # one person -> other seven are 0
def test_null_or_unrecognised_type_counts_as_unknown(tmp_path):  # type None and "gizmo" -> unknown 2
def test_invalidated_relations_and_observations_excluded(tmp_path):
    # 3 relations, 1 invalidated; 2 observations, 1 invalidated
    assert out["relations_live"] == 2 and out["observations_live"] == 1
def test_unlinked_counts_entities_with_no_live_relation_either_side(tmp_path):
    # acme-a -> acme-b live; acme-c only in an invalidated relation; acme-d in none
    assert out["unlinked"] == 2
def test_schema_without_invalidated_at_counts_every_row_live(tmp_path): ...
def test_missing_graph_tables_degrade_to_zeros(tmp_path): ...
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_console_stats.py -q -k graph`
Expected: FAIL, `ImportError: cannot import name 'graph_counts'`

- [ ] **Step 3: Implement `graph_counts(db)`**

`GROUP BY type` folded into the eight keys (anything else or NULL goes to `unknown`). Live means `invalidated_at IS NULL` when the column exists, else every row. Unlinked: entities whose `id` is in neither `entity_a` nor `entity_b` of a live relation (`NOT EXISTS` against a `UNION` of the two columns).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_console_stats.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/console_stats.py tests/test_console_stats.py
git commit -m "feat(console): graph counts by kind, live links only, unlinked entities"
```

### Task 3: Freshness from sync cursors and enrichment pushes

**Files:**
- Modify: `mcpbrain/console_stats.py`
- Test: `tests/test_console_stats.py`

**Interfaces:**
- Produces: `parse_utc(value: str | None) -> datetime | None`; `source_times(db) -> dict[str, str | None]` keyed `gmail`, `drive`, `calendar`, `anarlog` (ISO UTC or `None`); `enrich_activity(db, now: datetime) -> {"last_push_at": str | None, "pushes_24h": int}`; constant `SOURCE_KEYS = ("gmail", "drive", "calendar", "anarlog")`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_utc_accepts_sqlite_and_iso_forms():
    assert parse_utc("2026-11-06 01:40:03").isoformat() == "2026-11-06T01:40:03+00:00"
    assert parse_utc("2026-11-09T05:53:12.349216+00:00").isoformat() == "2026-11-09T05:53:12.349216+00:00"
    assert parse_utc("not a time") is None and parse_utc(None) is None

def test_source_times_reads_base_keys_only(tmp_path):
    # rows: gmail fresh, drive fresh, drive:0AFAKEID stale since July, gmail_backfill_until, calendar absent
    out = source_times(db)
    assert out["drive"] == "2026-11-09T05:53:13+00:00"   # the stale shared drive is ignored
    assert out["calendar"] is None and set(out) == {"gmail", "drive", "calendar", "anarlog"}

def test_enrich_activity_counts_last_24h(tmp_path):
    now = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)
    # pushes at 01:40 today, 23:00 yesterday, and 3 days ago
    assert enrich_activity(db, now) == {"last_push_at": "2026-11-06T01:40:03+00:00", "pushes_24h": 2}

def test_missing_tables_give_none_and_zero(tmp_path): ...
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_console_stats.py -q -k "parse_utc or source_times or enrich_activity or missing_tables"`
Expected: FAIL on import

- [ ] **Step 3: Implement the three functions**

`parse_utc` tries `datetime.fromisoformat` after replacing a single space separator with `T`; a naive result gets `tzinfo=timezone.utc`; anything that raises returns `None`. `source_times` selects `source, updated_at FROM sync_cursors WHERE source IN SOURCE_KEYS`. `enrich_activity` reads `at` from `enrich_payloads` and compares parsed values in Python (the column holds both formats, so a SQL string comparison is wrong).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_console_stats.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/console_stats.py tests/test_console_stats.py
git commit -m "feat(console): source and enrichment freshness across both timestamp formats"
```

### Task 4: The health verdict

**Files:**
- Modify: `mcpbrain/console_stats.py`
- Test: `tests/test_console_stats.py`

**Interfaces:**
- Produces: `STALE_HOURS = 6.0`; `health(*, now: datetime, paused: bool, stalled: str | None, watchdog_limit_reached: bool, connections: list[dict], open_findings: int, corrections_pending: int, sources: dict[str, str | None], waiting: int, last_push_at: str | None) -> {"state": str, "reasons": list[str]}`. `state` is one of `"paused"`, `"stalled"`, `"needs_you"`, `"behind"`, `"healthy"`. `connections` items carry `name`, `label`, `state`.

- [ ] **Step 1: Write the failing table test**

```python
NOW = datetime(2026, 11, 6, 2, 0, tzinfo=timezone.utc)
FRESH = {k: "2026-11-06T01:55:00+00:00" for k in SOURCE_KEYS}
OK = dict(now=NOW, paused=False, stalled=None, watchdog_limit_reached=False, connections=[],
          open_findings=0, corrections_pending=0, sources=FRESH, waiting=0, last_push_at="2026-11-06T01:40:00+00:00")

@pytest.mark.parametrize("over,state", [
    ({}, "healthy"),
    ({"paused": True, "stalled": "cycle", "open_findings": 3}, "paused"),          # paused wins
    ({"stalled": "cycle"}, "stalled"),
    ({"watchdog_limit_reached": True}, "stalled"),
    ({"sources": {**FRESH, "gmail": "2026-11-05T19:00:00+00:00"}}, "stalled"),   # 7 h old
    ({"sources": {**FRESH, "anarlog": None}}, "healthy"),                         # never synced is not stale
    ({"open_findings": 2}, "needs_you"),
    ({"corrections_pending": 1}, "needs_you"),
    ({"connections": [{"name": "google", "label": "Google", "state": "needs_action"}]}, "needs_you"),
    ({"waiting": 3578, "last_push_at": "2026-11-05T12:00:00+00:00"}, "behind"),  # 14 h, work waiting
    ({"waiting": 3578, "last_push_at": None}, "behind"),
    ({"waiting": 0, "last_push_at": None}, "healthy"),                           # nothing to do is not behind
    ({"open_findings": 1, "waiting": 9, "last_push_at": None}, "needs_you"),    # precedence
])
def test_health_state(over, state):
    assert health(**{**OK, **over})["state"] == state

def test_health_reasons_name_the_cause():
    r = health(**{**OK, "sources": {**FRESH, "gmail": "2026-11-05T19:00:00+00:00"}})["reasons"]
    assert r == ["Gmail has not synced for 7 hours"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_console_stats.py -q -k health`
Expected: FAIL on import

- [ ] **Step 3: Implement `health`**

First match wins in the Global Constraints order. Source labels for reasons: `gmail` Gmail, `drive` Drive, `calendar` Calendar, `anarlog` Meetings. Hours are whole hours, rounded down. Reason strings, exactly: `"Paused"`; `"<Label> has not synced for <n> hours"`; `"The daemon stalled in <phase>"`; `"The watchdog restart limit was reached"`; `"<n> open finding(s)"` (singular at 1); `"<n> graph correction(s) waiting"`; `"<Label> needs attention"`; `"<n> chunks waiting, no enrichment for <n> hours"` or `"…, no enrichment yet"` when `last_push_at` is `None`. `reasons` lists every cause found at the winning state's level.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_console_stats.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/console_stats.py tests/test_console_stats.py
git commit -m "feat(console): health verdict with fixed precedence and plain reasons"
```

### Task 5: Assemble the new payload in `dashboard.stats()`

**Files:**
- Modify: `mcpbrain/dashboard.py` (`stats()`, delete `_graph_counts` once unused; keep `queue_state`)
- Test: `tests/test_dashboard_stats.py` (rewrite the four tests that pin the old shape; keep the route tests)

**Interfaces:**
- Consumes: Tasks 1 to 4; `store.pending_corrections()`; the `status` dict from `daemon.status()` (`chunk_count`, `paused`, `stalled`, `watchdog_limit_reached`, `connections`, `open_findings`, `spool`, `backfill`, `version`, `reflow`).
- Produces: `stats(store, home, status: dict, *, now: datetime | None = None) -> dict` with exactly these top-level keys: `as_of`, `index`, `enrich`, `graph`, `sources`, `health`, `needs_you`, `connections`, `spool`, `system`. Shapes:
  - `index`: Task 1's dict.
  - `enrich`: `{"queue_state": queue_state(queued=spool.pending, unenriched_eligible=index.waiting), "units_pending": int, "last_push_at", "pushes_24h"}`.
  - `graph`: Task 2's dict plus `"circles": len(store.list_communities())`.
  - `sources`: list in order gmail, drive, calendar, anarlog: `{"name", "label", "last_synced_at", "history": {"done": bool, "reached": str | None}}`, with `history` from `status["backfill"]` (anarlog: `{"done": True, "reached": None}`).
  - `needs_you`: `{"connections": [connections whose state is "needs_action"], "findings": int, "corrections": int}`.
  - `system`: `{"version", "paused", "watchdog_exits", "reflow"}` where `reflow` is `status["reflow"]` only when it is halted, blocked or has work queued or remaining, else `None`.
  - `enrich_enabled` and `links_per_entity` are gone.

- [ ] **Step 1: Rewrite the shape tests**

Extend `_seed` to Task 1–3's schema (chunks with `enriched`, typed entities, relations with `entity_a`, `entity_b` and `invalidated_at`, `sync_cursors`, `enrich_payloads`). `_Store` gains `pending_corrections()` returning a list. Replace `test_stats_index_and_graph_counts`, `test_stats_daemon_and_spool_passthrough`, `test_stats_eligible_clamps_at_zero_rather_than_going_negative` and `test_stats_queued_defaults_to_zero_when_spool_key_is_absent` with:

```python
def test_stats_regression_153_percent(tmp_path):
    # 56 cold-and-enriched, 44 cold, 96 eligible enriched, 4 eligible waiting
    out = dashboard.stats(_Store(p), str(tmp_path), _status(), now=NOW)
    assert out["index"]["pct"] == 96 and out["index"]["waiting"] == 4

def test_stats_top_level_keys(tmp_path):
    assert set(out) == {"as_of", "index", "enrich", "graph", "sources", "health",
                        "needs_you", "connections", "spool", "system"}
    assert "enrich_enabled" not in json.dumps(out) and "links_per_entity" not in json.dumps(out)

def test_stats_sources_include_meetings_with_freshness(tmp_path): ...  # 4 entries, anarlog label "Meetings"
def test_stats_starved_queue_is_reported(tmp_path): ...                # waiting > 0, spool pending 0 -> "starved"
def test_stats_health_needs_you_from_findings(tmp_path): ...           # open_findings=2 -> health.state "needs_you"
def test_stats_reflow_hidden_when_finished(tmp_path): ...              # reflow done, nothing queued -> system.reflow is None
def test_stats_pending_corrections_failure_degrades_to_zero(tmp_path): ...  # store raises -> needs_you.corrections 0
```

`test_stats_sources_from_backfill`, `test_stats_connections_ordered_and_no_clickup`, `test_stats_degrades_on_empty_store` and both route tests stay, with assertions moved to the new keys (`sources[*].history`, `index.pct`).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_dashboard_stats.py -q`
Expected: FAIL on the new keys

- [ ] **Step 3: Rewrite `stats()`**

Open one connection with `_open_ro(path)` and pass it to every `console_stats` function, so all counts come from one snapshot; close it in `finally`. `now` defaults to `datetime.now(timezone.utc)`. Wrap `store.list_communities()` and `store.pending_corrections()` the way `list_communities` is wrapped today (log and degrade to 0). Delete `_graph_counts`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_dashboard_stats.py tests/test_console_stats.py tests/test_dashboard_queue.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/dashboard.py tests/test_dashboard_stats.py
git commit -m "feat(console): stats payload with correct pct, freshness and health"
```

### Task 6: Point the current dashboard at the corrected fields

**Files:**
- Modify: `mcpbrain/wizard/dashboard.html` (`render`, `renderSources`, `renderSpool`, `renderDaemon` and the tile and card markup only; styling waits for Phase 5)
- Test: `tests/test_dashboard_routes.py`

**Interfaces:**
- Consumes: Task 5's payload.

- [ ] **Step 1: Write the failing test**

```python
def test_dashboard_html_reads_the_new_payload():
    html = (Path(dashboard.__file__).parent / "wizard" / "dashboard.html").read_text()
    for gone in ("enriched_pct", "links_per_entity", "enrich_enabled", "Links / entity", "Spool depth"):
        assert gone not in html
    for used in ("idx.pct", "idx.waiting", "data.health", "last_synced_at", "enrich.last_push_at"):
        assert used in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_dashboard_routes.py -q -k new_payload`
Expected: FAIL

- [ ] **Step 3: Re-point the page**

- **Meter:** `idx.pct`, labelled "<pct>% of what's worth enriching is in the graph". The sub-line reads "<waiting> waiting · <units_pending> queued for Claude"; the tooltip is unchanged.
- **"Enriched" stat:** shows `idx.enriched_eligible`.
- **Links / entity tile:** replace with "Unlinked" showing `graph.unlinked`.
- **Entities stat:** shows people + orgs + projects + topics + meetings, with the sub-line "people, organisations, projects, topics and meetings".
- **Sources:** each source adds "synced HH:MM" from `last_synced_at`, shown in the configured timezone the way `asofLabel` already formats. Meetings is included.
- **Spool row:** remove it.
- **Daemon card:** the "Enrichment" row shows the last push time and `pushes_24h`. A new "Health" row shows `health.state` and its first reason.
- **Live chip:** reads the health state.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_dashboard_routes.py tests/test_dashboard_stats.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/wizard/dashboard.html tests/test_dashboard_routes.py
git commit -m "fix(dashboard): read corrected stats (no more >100% digested)"
```

### Task 7: Phase gate, live verification

**Files:** none changed unless a check fails.

- [ ] **Step 1: Full suite, lint, tenant guard**

Run: `uv run pytest -q && uv run ruff check . && uv run pytest tests/test_no_tenant_literals.py -q`
Expected: all pass, with `mcpbrain/tenant_people.json` present (the person-name check runs, not skips).

- [ ] **Step 2: Numbers against the real store, read-only**

Run against the live store read-only (the daemon keeps running; nothing writes):

```bash
uv run python -c "
import sqlite3, os
from mcpbrain import console_stats as cs
p = os.path.expanduser('~/Library/Application Support/mcpbrain/brain.sqlite3')
db = sqlite3.connect(f'file:{p}?mode=ro', uri=True)
i = cs.index_counts(db); print(i); assert 0 <= i['pct'] <= 100 and i['waiting'] >= 0
print(cs.graph_counts(db)); print(cs.source_times(db))"
```

Expected: `pct` between 0 and 100 and in line with the spec's measured 96% (it moves as enrichment runs); `relations_live` below the total relation count; all four sources have a time within the last few hours.

- [ ] **Step 3: The page itself**

Use the `run` skill to start the daemon from this branch (or a ControlServer on a read-only copy) and open `/dashboard`. Confirm the meter reads at or below 100%, Meetings appears under Sources, and the health chip names a state.

- [ ] **Step 4: Record and hand back**

Append a dated line to the index's Phase 1 row (`Status: done <date>, <short sha>`). Commit. Do not push or release; tell the owner Phase 1 is ready and that phases 2, 3 and 4 can start.
