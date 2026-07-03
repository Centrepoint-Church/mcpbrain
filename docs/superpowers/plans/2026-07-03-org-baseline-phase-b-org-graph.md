# Org Baseline — Phase B (Org Graph) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build subsystem B of the org-baseline feature: the contribution **edge** (B2), the **curator** pipeline (B3), and the **consumer import** (B4/B4a), all on top of the frozen Phase 0 contracts. Members contribute typed/redacted claims to the fleet; the curator adjudicates and publishes a versioned snapshot; consumers import it as `origin='org'` rows that coexist with local data. Nothing content-shaped ever leaves a machine.

**Architecture:** Three new pure-ish modules — `mcpbrain/org_contrib.py`, `mcpbrain/org_curate.py`, `mcpbrain/org_import.py` — each callable directly (unit-tested against `tests/helpers/org_fleet.py`) and each driven by exactly one of the Phase 0 no-op cadence bodies (`_run_org_contrib_upload`, `_run_org_curate`, `_run_org_import`) that this phase FILLS IN. Plus **B4a guards**: small, clearly-scoped conditionals in `resolve.py` and `graph_write.py` that keep the local merge machinery from fighting the wholesale-replace import. The heavy lifting (fail-closed filtering, corroboration counting, snapshot serialisation, transactional import) lives in the modules; the daemon bodies are thin gates + orchestration.

**Tech Stack:** Python 3, stdlib `sqlite3` + `json` + `gzip` + `hashlib` + `hmac`, the Phase 0 `mcpbrain/org_contracts.py` dataclasses/helpers, `mcpbrain/resolve.py` (deterministic merge + role guard), `mcpbrain/review_apply.py` (verdict-hardening pattern), pytest.

## Global Constraints

- **Do NOT modify the Phase 0 shared surface.** No schema changes (the tables/columns in `store.py` are frozen), no config-accessor signature changes, no cadence registration/defaults/keys edits in `daemon.py`. Phase B may ONLY: (a) fill the bodies of the three `_run_org_*` methods (`daemon.py:1658`, `:1665`, `:1672`) — keeping their existing `_is_due(...)` gate and `dict | None` return; (b) add the three new modules; (c) add the minimal, clearly-scoped B4a guards in `resolve.py` and `graph_write.py` (Task 11).
- **Reuse, do not reinvent.** Deterministic merge + role-address guard come from `resolve.py` (`_deterministic_merges` resolve.py:85, `_email_equality_merges` resolve.py:116, `is_role_address` resolve.py:58, `canonical_key` resolve.py:68, `_candidate_pairs` resolve.py:184, `_pick_winner` resolve.py:226, `resolve_entities` resolve.py:308, `_NAME_MERGEABLE_TYPES` resolve.py:82). The curator's verdict application copies the **0.7.84 hardening** shape from `review_apply.py` (target the finding's OWN stored ref/type, capped, reversible, `pending`/`skip` as the safe default — see `apply_duplicate_verdicts` review_apply.py:317 and the `store.get_finding` guard review_apply.py:43).
- **Fail closed everywhere on the edge.** Any uncertainty in `org_contrib` → the claim is NOT contributed. The contribution filter is the single most safety-critical surface (Phase D egress gate audits it): no non-allowlisted relation type, no non-`{person,org,project}` entity type, no `profile`/`notes`/`mentions`/raw `doc_id`/chunk text, no role-address-keyed entity, no cold-sourced claim, and nothing at all unless the fleet is pinned (`FleetPin.is_pinned`).
- **Contributions carry no content, so the curator adjudicates on structural evidence only** (names, types, emails, aliases, confidence, corroboration counts). When structural evidence can't settle a call, the verdict is `pending`: the claim stays out of the snapshot.
- **Store-read helpers query via `store._connect()` inside the new modules**, following the precedent already in `resolve.py:136` (`_email_equality_merges` runs its own `SELECT`) — this avoids adding methods to the frozen `store.py`. Watermarks/versions use the existing generic `store.set_meta`/`store.get_meta` (store.py:1298/1303).
- **Origin stamping.** New rows the curator materialises and the consumer imports are `origin='org'`; everything a member's own pipeline writes stays `origin='local'` (the column default). Members never contribute `origin='org'` rows (that is the echo guard at the source).
- **FleetStorage only via the Protocol** (`org_contracts.FleetStorage`). The concrete Drive-backed implementation and its factory (`fleet_folder_storage`) are built by subsystem A in `mcpbrain/fleet_storage.py`; Phase B defines no factory of its own. The daemon bodies acquire an instance through a **guarded import** of A's module (Task 10) so B stays build-independent of A — before A merges the import fails and the cadences no-op gracefully (`{"skipped": "no_fleet_storage"}`).
- **Tests:** pytest, flat `tests/test_*.py`, functions `test_*`. Use `tests/helpers/org_fleet.py` (`LocalDirFleetStorage`, `make_install`, `make_fleet`, `FakeInstall`). Construct stores as `Store(tmp_path / "x.sqlite3", dim=4).init()`. Run via `python -m pytest`.
- **No version bump, no release, no push** (per `CLAUDE.md`). Commit locally only.
- Reference spec: `docs/superpowers/specs/2026-07-03-org-baseline-personal-overlay-design.md` (§§ B1–B5). Structure template: `docs/superpowers/plans/2026-07-03-org-baseline-phase-0-foundations.md`.

---

## File Structure

**Created:**
- `mcpbrain/org_contrib.py` — B2 edge. `collect_from_drain` (pure fail-closed filter + redaction → `org_contrib_outbox`), `upload_pending` (outbox → one JSONL batch to `contrib/<email>/<utc>.jsonl`), plus private provenance/cold/source-kind helpers and the `_delta_since_watermark` scanner the daemon body uses.
- `mcpbrain/org_curate.py` — B3 curator. `run` (ingest → deterministic merge → corroboration → AI adjudication → publish snapshot), the injectable `adjudicate` seam (default = all-pending), and the snapshot serialiser.
- `mcpbrain/org_import.py` — B4 consumer. `import_snapshot` (fetch manifest → verify sha256 → transactional wholesale-replace-per-origin + tombstones + demote-not-delete + slug-drift reconciliation). **No FleetStorage factory lives here** — the daemon bodies acquire storage from subsystem A's `mcpbrain/fleet_storage.py` (`fleet_folder_storage`) via a guarded import, so B stays build-independent of A pre-convergence.
- `tests/test_org_contrib.py`, `tests/test_org_curate.py`, `tests/test_org_import.py`, `tests/test_org_b4a_guards.py`, `tests/test_org_daemon_bodies.py`, `tests/test_org_phase_b_gate.py`.

**Modified (guards only):**
- `mcpbrain/resolve.py` — B4a rules 1 & 2: origin-aware guard in `_deterministic_merges`/`_email_equality_merges` + a `curator` bypass; `resolve_entities` threads `curator`.
- `mcpbrain/graph_write.py` — B4a rule 3: `upsert_entity` never overwrites org skeleton fields.
- `mcpbrain/daemon.py` — fill the three `_run_org_*` bodies (bodies only; gate + return shape preserved).

---

## Task 1: B2 edge — `collect_from_drain` fail-closed filter + redaction

**Files:**
- Create: `mcpbrain/org_contrib.py`
- Test: `tests/test_org_contrib.py`

**Interfaces:**
- Consumes: `org_contracts.ContributionRecord`, `org_contracts.source_ref`, `resolve.is_role_address`; `org_contrib_outbox` table (Phase 0, store.py:425); the chunk `enrich_state`/`metadata` columns (read-only).
- Produces: `def collect_from_drain(store, drain_delta, pin, contributor_email) -> int` — filters a drain delta to allowlisted, redacted `ContributionRecord`s and enqueues them into `org_contrib_outbox`; returns the count enqueued. **The drain-delta shape is defined here and is the contract every caller builds.**

`drain_delta` is a dict:
```python
{
  "relations": [ {"entity_a": str, "relation": str, "entity_b": str,
                  "valid_from": str, "valid_to": str, "confidence": float,
                  "origin": str, "source_doc_id": str}, ... ],   # entity_relations rows
  "entities":  { entity_id: {"id": str, "name": str, "type": str, "org": str,
                             "email_addr": str, "aliases": str, "origin": str}, ... },
}
```
Entities are contributed **as endpoints of an allowlisted relation** (a person alongside its `works_at`), sharing the relation's provenance — so a single `source_ref` covers the person, the org, and the edge, and echo-dedup (identical `source_ref` across users of one shared doc) is automatic.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_contrib.py`:

```python
import json

from mcpbrain import org_contrib
from mcpbrain.org_contracts import FleetPin, ContributionRecord, source_ref
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


def _pin():
    return FleetPin(fleet_secret="s3cret", relation_allowlist=("works_at", "member_of", "mentioned_with"))


def _delta(relation="works_at", *, a_type="person", b_type="org",
           a_email="joel@acme.org", origin="local", valid_to="",
           source_doc_id="msg-1"):
    return {
        "relations": [{"entity_a": "joel", "relation": relation, "entity_b": "acme",
                       "valid_from": "2026-01-01", "valid_to": valid_to,
                       "confidence": 0.9, "origin": origin,
                       "source_doc_id": source_doc_id}],
        "entities": {
            "joel": {"id": "joel", "name": "Joel Chelliah", "type": a_type,
                     "org": "Acme", "email_addr": a_email, "aliases": "",
                     "origin": origin},
            "acme": {"id": "acme", "name": "Acme", "type": b_type, "org": "",
                     "email_addr": "", "aliases": "", "origin": origin},
        },
    }


def _outbox(store):
    with store._connect() as db:
        return [json.loads(r["record"])
                for r in db.execute("SELECT record FROM org_contrib_outbox ORDER BY id").fetchall()]


def test_allowlisted_relation_contributes_edge_and_both_endpoints(tmp_path):
    s = _store(tmp_path)
    n = org_contrib.collect_from_drain(s, _delta(), _pin(), "alice@x.org")
    recs = _outbox(s)
    assert n == 3                                   # joel entity, acme entity, works_at relation
    kinds = sorted(r["claim"]["kind"] for r in recs)
    assert kinds == ["entity", "entity", "relation"]
    # source_ref is HMAC(secret, doc_id) — identical for all three, hides the doc id
    assert {r["source_ref"] for r in recs} == {source_ref("s3cret", "msg-1")}
    # NOTHING content-shaped leaks
    blob = json.dumps(recs)
    assert "msg-1" not in blob and "profile" not in blob and "mentions" not in blob


def test_unpinned_fleet_contributes_nothing(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(s, _delta(), FleetPin(), "alice@x.org") == 0
    assert _outbox(s) == []


def test_non_allowlisted_relation_dropped(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(s, _delta(relation="reports_to"), _pin(), "a@x.org") == 0


def test_role_address_endpoint_drops_whole_claim(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(
        s, _delta(a_email="office@acme.org"), _pin(), "a@x.org") == 0


def test_non_layer1_entity_type_dropped(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(
        s, _delta(b_type="document"), _pin(), "a@x.org") == 0


def test_org_origin_rows_never_re_contributed(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(s, _delta(origin="org"), _pin(), "a@x.org") == 0


def test_cold_sourced_claim_dropped(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO chunks(doc_id, text, content_hash, metadata, enrich_state) "
                   "VALUES('msg-cold','t','h','{}','cold')")
    assert org_contrib.collect_from_drain(
        s, _delta(source_doc_id="msg-cold"), _pin(), "a@x.org") == 0


def test_missing_provenance_fails_closed(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(
        s, _delta(source_doc_id=""), _pin(), "a@x.org") == 0


def test_supersession_carries_valid_to(tmp_path):
    s = _store(tmp_path)
    org_contrib.collect_from_drain(s, _delta(valid_to="2026-06-01"), _pin(), "a@x.org")
    rel = [r for r in _outbox(s) if r["claim"]["kind"] == "relation"][0]
    assert rel["valid_to"] == "2026-06-01"


def test_records_round_trip_through_contribution_record(tmp_path):
    s = _store(tmp_path)
    org_contrib.collect_from_drain(s, _delta(), _pin(), "a@x.org")
    for raw in _outbox(s):
        assert ContributionRecord.from_dict(raw).contributor_email == "a@x.org"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_contrib.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.org_contrib'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/org_contrib.py`:

```python
"""Subsystem B2 — the contribution edge.

After an enrichment drain, allowlisted entity/relation deltas are filtered
fail-closed, redacted into org_contracts.ContributionRecords, and queued in the
local org_contrib_outbox. A daily cadence (_run_org_contrib_upload) uploads the
pending rows as one JSONL batch to the fleet folder. Nothing content-shaped ever
leaves the machine: no chunk text, no profile/notes/mentions, no raw doc_id
(only its HMAC), no non-allowlisted type, no role-address-keyed person, no
cold-sourced claim, and nothing at all until the fleet is pinned.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from mcpbrain.org_contracts import ContributionRecord, FleetPin, source_ref
from mcpbrain.resolve import is_role_address

log = logging.getLogger(__name__)

# Layer-1 entity types (spec B1). Fixed allowlist — a new/unknown type is never
# contributed (fail-safe), mirroring resolve._NAME_MERGEABLE_TYPES.
_LAYER1_ENTITY_TYPES = frozenset({"person", "org", "project"})


def _is_cold(store, doc_id: str) -> bool:
    """True if the provenance doc is a cold (salience-gated) chunk. Cold chunks
    skip graph-extraction, so this is belt-and-suspenders — but the edge filter
    must fail closed on them regardless of how a row got written."""
    if not doc_id:
        return True
    with store._connect() as db:
        r = db.execute(
            "SELECT 1 FROM chunks WHERE doc_id=? AND enrich_state='cold' LIMIT 1",
            (doc_id,)).fetchone()
    return r is not None


def _source_kind(store, doc_id: str) -> str:
    """Map a doc's chunk metadata source_type to a contribution source_kind
    (email|drive|calendar). Never reveals the doc id itself."""
    with store._connect() as db:
        r = db.execute("SELECT metadata FROM chunks WHERE doc_id=? LIMIT 1",
                       (doc_id,)).fetchone()
    st = ""
    if r and r["metadata"]:
        try:
            st = (json.loads(r["metadata"]) or {}).get("source_type", "") or ""
        except (ValueError, TypeError):
            st = ""
    return {"gmail": "email", "drive": "drive", "calendar": "calendar"}.get(st, "email")


def collect_from_drain(store, drain_delta, pin: FleetPin, contributor_email: str) -> int:
    """Filter a drain delta to allowlisted, redacted ContributionRecords and
    enqueue them into org_contrib_outbox. Returns the number of records enqueued.

    drain_delta = {"relations": [entity_relations rows], "entities": {id: row}}.
    See module docstring / plan for the exact shape. Fail-closed at every step.
    """
    if not pin.is_pinned:
        return 0                                   # no fleet_secret => nothing leaves
    allow = set(pin.relation_allowlist)
    entities = drain_delta.get("entities") or {}
    records: list[ContributionRecord] = []
    seen: set = set()                              # dedup identical (source_ref, claim)

    def _emit(rec: ContributionRecord) -> None:
        key = (rec.source_ref, json.dumps(rec.claim, sort_keys=True), rec.valid_to)
        if key in seen:
            return
        seen.add(key)
        records.append(rec)

    for rel in drain_delta.get("relations") or []:
        if (rel.get("origin") or "local") != "local":
            continue                               # never re-contribute org rows (echo guard)
        relation = rel.get("relation") or ""
        if relation not in allow:
            continue                               # allowlist — fail closed
        doc_id = rel.get("source_doc_id") or ""
        if not doc_id or _is_cold(store, doc_id):
            continue                               # no/ cold provenance — fail closed
        a = entities.get(rel.get("entity_a"))
        b = entities.get(rel.get("entity_b"))
        if not a or not b:
            continue
        if a.get("type") not in _LAYER1_ENTITY_TYPES or b.get("type") not in _LAYER1_ENTITY_TYPES:
            continue                               # non-layer-1 type — fail closed
        if is_role_address(a.get("email_addr", "")) or is_role_address(b.get("email_addr", "")):
            continue                               # role inbox never keys a person (0.7.77)
        if (a.get("origin") or "local") != "local" or (b.get("origin") or "local") != "local":
            continue
        sref = source_ref(pin.fleet_secret, doc_id)
        skind = _source_kind(store, doc_id)
        vfrom = rel.get("valid_from") or ""
        for e in (a, b):
            _emit(ContributionRecord(
                claim={"kind": "entity", "id": e["id"], "name": e.get("name", ""),
                       "type": e.get("type", ""), "org": e.get("org", "") or "",
                       "email_addr": e.get("email_addr", "") or "",
                       "aliases": e.get("aliases", "") or ""},
                confidence=1.0, valid_from=vfrom,
                contributor_email=contributor_email, source_kind=skind, source_ref=sref))
        _emit(ContributionRecord(
            claim={"kind": "relation", "entity_a": rel["entity_a"],
                   "relation": relation, "entity_b": rel["entity_b"]},
            confidence=float(rel.get("confidence", 1.0) or 1.0),
            valid_from=vfrom, valid_to=rel.get("valid_to") or "",
            contributor_email=contributor_email, source_kind=skind, source_ref=sref))

    if not records:
        return 0
    with store._connect() as db:
        for rec in records:
            db.execute("INSERT INTO org_contrib_outbox(record) VALUES(?)",
                       (json.dumps(rec.to_dict(), sort_keys=True),))
    return len(records)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_contrib.py -v`
Expected: PASS (all 10 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_contrib.py tests/test_org_contrib.py
git commit -m "feat(org): B2 edge — fail-closed contribution filter + redaction (collect_from_drain)"
```

---

## Task 2: B2 edge — `upload_pending` (outbox → fleet JSONL batch)

**Files:**
- Modify: `mcpbrain/org_contrib.py`
- Test: `tests/test_org_contrib.py`

**Interfaces:**
- Consumes: `org_contrib_outbox` (Phase 0), `org_contracts.FleetStorage` (Protocol).
- Produces: `def upload_pending(store, fleet_storage, contributor_email) -> dict` — uploads all pending (`uploaded_at=''`) outbox rows as ONE append-only JSONL batch to `contrib/<email>/<utc-timestamp>.jsonl`, stamps them uploaded, returns `{"uploaded": n, "batch": path}` (`{"uploaded": 0, "batch": ""}` when nothing pending).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_org_contrib.py`:

```python
def test_upload_pending_writes_one_batch_and_marks_uploaded(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    org_contrib.collect_from_drain(s, _delta(), _pin(), "alice@x.org")
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    res = org_contrib.upload_pending(s, fs, "alice@x.org")
    assert res["uploaded"] == 3
    assert res["batch"].startswith("contrib/alice@x.org/") and res["batch"].endswith(".jsonl")
    body = fs.get_bytes(res["batch"]).decode().strip().splitlines()
    assert len(body) == 3
    # second call has nothing pending
    assert org_contrib.upload_pending(s, fs, "alice@x.org") == {"uploaded": 0, "batch": ""}


def test_upload_pending_empty_is_noop(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    assert org_contrib.upload_pending(s, LocalDirFleetStorage(tmp_path / "f"), "a@x.org") == {
        "uploaded": 0, "batch": ""}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_contrib.py -k upload -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.org_contrib' has no attribute 'upload_pending'`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/org_contrib.py`:

```python
def upload_pending(store, fleet_storage, contributor_email: str) -> dict:
    """Upload all pending outbox rows as one JSONL batch to
    contrib/<email>/<utc>.jsonl, then mark them uploaded. Idempotent-safe: only
    rows with uploaded_at=='' are taken, so a re-run after a successful upload is
    a no-op."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT id, record FROM org_contrib_outbox WHERE uploaded_at='' ORDER BY id"
        ).fetchall()
    if not rows:
        return {"uploaded": 0, "batch": ""}
    payload = ("\n".join(r["record"] for r in rows) + "\n").encode("utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = f"contrib/{contributor_email}/{ts}.jsonl"
    fleet_storage.put_bytes(path, payload)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with store._connect() as db:
        db.executemany("UPDATE org_contrib_outbox SET uploaded_at=? WHERE id=?",
                       [(now, r["id"]) for r in rows])
    return {"uploaded": len(rows), "batch": path}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_contrib.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_contrib.py tests/test_org_contrib.py
git commit -m "feat(org): B2 edge — upload_pending drains outbox to a fleet JSONL batch"
```

---

## Task 3: B2 edge — watermark delta scanner (`_delta_since_watermark`)

**Files:**
- Modify: `mcpbrain/org_contrib.py`
- Test: `tests/test_org_contrib.py`

**Interfaces:**
- Consumes: `entity_relations`/`entities` rows (read-only), `store.get_meta`/`set_meta`.
- Produces: `def _delta_since_watermark(store) -> tuple[dict, dict]` returning `(drain_delta, new_watermark)`. Because Phase B may not add a drain-path hook (only the `_run_org_*` bodies), the daily `_run_org_contrib_upload` cadence does BOTH collection and upload: it scans `entity_relations` for rows written OR superseded since the last watermark (new `id` beyond the high-water mark, or `invalidated_at`/`last_seen` after the last timestamp — this catches new edges, supersessions, and re-observations), gathers their endpoint entities, and hands the delta to `collect_from_drain`. `collect_from_drain` itself stays a standalone drain-delta-driven function, so a future per-drain hook (or Phase D) can call it unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_org_contrib.py`:

```python
def test_delta_since_watermark_picks_up_new_and_superseded(tmp_path):
    from mcpbrain import graph_write
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name, typ in (("joel", "Joel", "person"), ("acme", "Acme", "org"),
                               ("beta", "Beta", "org")):
            db.execute("INSERT INTO entities(id,name,type,origin) VALUES(?,?,?,'local')",
                       (eid, name, typ))
    graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                source_doc_id="msg-1")
    delta, wm = org_contrib._delta_since_watermark(s)
    rels = {(r["entity_a"], r["relation"], r["entity_b"]) for r in delta["relations"]}
    assert ("joel", "works_at", "acme") in rels
    assert "joel" in delta["entities"] and "acme" in delta["entities"]
    # advancing the watermark then re-scanning yields nothing new
    s.set_meta("org_contrib_hwm", str(wm["hwm"]))
    s.set_meta("org_contrib_ts", wm["ts"])
    delta2, _ = org_contrib._delta_since_watermark(s)
    assert delta2["relations"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_contrib.py -k watermark -v`
Expected: FAIL — no attribute `_delta_since_watermark`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/org_contrib.py`:

```python
def _delta_since_watermark(store) -> tuple[dict, dict]:
    """Build a drain_delta of entity_relations changed since the stored
    watermark (org_contrib_hwm = max row id last seen, org_contrib_ts = last
    scan timestamp), plus their endpoint entities. Returns (drain_delta,
    new_watermark) where new_watermark = {"hwm": int, "ts": iso}."""
    hwm = int(store.get_meta("org_contrib_hwm") or 0)
    last_ts = store.get_meta("org_contrib_ts") or ""
    with store._connect() as db:
        rel_rows = db.execute(
            "SELECT id, entity_a, relation, entity_b, valid_from, valid_to, "
            "       confidence, origin, source_doc_id "
            "FROM entity_relations "
            "WHERE id > ? "
            "   OR (invalidated_at IS NOT NULL AND invalidated_at > ?) "
            "   OR (last_seen IS NOT NULL AND last_seen > ?) "
            "ORDER BY id",
            (hwm, last_ts, last_ts)).fetchall()
        max_id = db.execute("SELECT COALESCE(MAX(id),0) m FROM entity_relations").fetchone()["m"]
    relations = [dict(r) for r in rel_rows]
    ent_ids = {r["entity_a"] for r in relations} | {r["entity_b"] for r in relations}
    entities: dict = {}
    if ent_ids:
        placeholders = ",".join("?" * len(ent_ids))
        with store._connect() as db:
            for e in db.execute(
                f"SELECT id, name, type, org, email_addr, aliases, origin "
                f"FROM entities WHERE id IN ({placeholders})", tuple(ent_ids)).fetchall():
                entities[e["id"]] = dict(e)
    new_wm = {"hwm": int(max_id),
              "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return {"relations": relations, "entities": entities}, new_wm
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_contrib.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_contrib.py tests/test_org_contrib.py
git commit -m "feat(org): B2 edge — watermark delta scanner for cadence-driven collection"
```

---

## Task 4: B3 curator — ingest contrib JSONL into staging (idempotent)

**Files:**
- Create: `mcpbrain/org_curate.py`
- Test: `tests/test_org_curate.py`

**Interfaces:**
- Consumes: `FleetStorage.list_paths`/`get_bytes`, `org_contracts.ContributionRecord`, `org_contrib_staging` (Phase 0, UNIQUE(contributor_email, source_ref, claim)).
- Produces: `def _ingest(store, fleet_storage) -> dict` — reads every `contrib/**/*.jsonl` batch, parses each line as a `ContributionRecord`, and `INSERT OR IGNORE`s into `org_contrib_staging`; the UNIQUE makes re-ingest of the same batch a no-op. Returns `{"batches": n, "ingested": rows_new}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_curate.py`:

```python
import gzip
import json

from mcpbrain import org_curate
from mcpbrain.org_contracts import ContributionRecord
from mcpbrain.store import Store


def _store(tmp_path, name="curator"):
    s = Store(tmp_path / f"{name}.sqlite3", dim=4)
    s.init()
    return s


def _rec(claim, sref="ref1", email="alice@x.org", **kw):
    return ContributionRecord(claim=claim, confidence=kw.get("confidence", 1.0),
                              valid_from=kw.get("valid_from", "2026-01-01"),
                              valid_to=kw.get("valid_to", ""), contributor_email=email,
                              source_kind="email", source_ref=sref)


def _write_batch(fs, path, recs):
    body = ("\n".join(json.dumps(r.to_dict(), sort_keys=True) for r in recs) + "\n").encode()
    fs.put_bytes(path, body)


def test_ingest_is_idempotent(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/alice@x.org/1.jsonl",
                 [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
                        "org": "", "email_addr": "", "aliases": ""})])
    r1 = org_curate._ingest(s, fs)
    r2 = org_curate._ingest(s, fs)                 # same batch again
    assert r1["ingested"] == 1
    assert r2["ingested"] == 0                     # UNIQUE dedups
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM org_contrib_staging").fetchone()["c"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_curate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.org_curate'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/org_curate.py`:

```python
"""Subsystem B3 — the curator.

A standard install with config.role='org_curator'. It curates claims, it does
not extract. Pipeline (daily cadence): ingest contribution JSONL from the fleet
into staging, deterministically merge (reusing resolve.py, role-address
guarded), count corroboration (distinct source_ref / contributor), adjudicate
what determinism can't settle on STRUCTURAL evidence only (verdict 'pending'
when it can't decide), and publish a versioned snapshot (manifest written LAST).
Reversible + capped, per the 0.7.84 brain-review hardening.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from datetime import datetime, timezone

from mcpbrain import orgs
from mcpbrain.org_contracts import (ContributionRecord, SnapshotManifest, Tombstone)

log = logging.getLogger(__name__)


def _ingest(store, fleet_storage) -> dict:
    """Read every contrib/**/*.jsonl batch into org_contrib_staging. Idempotent
    via the UNIQUE(contributor_email, source_ref, claim) constraint."""
    batches = 0
    ingested = 0
    for path in fleet_storage.list_paths("contrib/"):
        if not path.endswith(".jsonl"):
            continue
        blob = fleet_storage.get_bytes(path)
        if not blob:
            continue
        batches += 1
        with store._connect() as db:
            for line in blob.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = ContributionRecord.from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("curate: skipping malformed contrib line in %s: %s", path, exc)
                    continue
                cur = db.execute(
                    "INSERT OR IGNORE INTO org_contrib_staging"
                    "(contributor_email, source_ref, claim, confidence, valid_from, "
                    " valid_to, source_kind, batch_file) VALUES(?,?,?,?,?,?,?,?)",
                    (rec.contributor_email, rec.source_ref,
                     json.dumps(rec.claim, sort_keys=True), rec.confidence,
                     rec.valid_from, rec.valid_to, rec.source_kind, path))
                ingested += cur.rowcount
    return {"batches": batches, "ingested": ingested}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_curate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_curate.py tests/test_org_curate.py
git commit -m "feat(org): B3 curator — idempotent contrib ingest into staging"
```

---

## Task 5: B3 curator — deterministic materialise + corroboration guard

**Files:**
- Modify: `mcpbrain/org_curate.py`
- Test: `tests/test_org_curate.py`

**Interfaces:**
- Consumes: `graph_write.upsert_entity`/`upsert_relation` (materialise), `resolve.is_role_address`, `org_contrib_staging`.
- Produces: `def _materialise(store, pin_allowlist=None) -> dict` — turns staged claims into `origin='org'` graph rows in the curator's own store, applying the per-type corroboration guard (`mentioned_with` needs ≥2 distinct `source_ref`; other allowlisted relations may be singletons) and the role-address guard (never materialise a person keyed on a role inbox). Supersessions (a claim's max `valid_to`) are applied. Returns `{"entities": n, "relations": n, "pending": n}`. `pending` counts guard-withheld `mentioned_with` singletons.

Corroboration rule (spec B3.2): a claim is corroborated by `≥2 distinct source_ref` **or** `≥2 distinct contributor_email`; `mentioned_with` additionally requires `≥2 distinct source_ref` specifically (independent sources, not just independent people).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_org_curate.py`:

```python
def _stage(store, recs):
    with store._connect() as db:
        for r in recs:
            db.execute(
                "INSERT OR IGNORE INTO org_contrib_staging"
                "(contributor_email, source_ref, claim, confidence, valid_from, valid_to, source_kind)"
                " VALUES(?,?,?,?,?,?,?)",
                (r.contributor_email, r.source_ref, json.dumps(r.claim, sort_keys=True),
                 r.confidence, r.valid_from, r.valid_to, r.source_kind))


def test_materialise_writes_org_rows(tmp_path):
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "joel", "name": "Joel Chelliah", "type": "person",
              "org": "Acme", "email_addr": "joel@acme.org", "aliases": ""}),
        _rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"}),
    ])
    res = org_curate._materialise(s)
    assert res["entities"] >= 2 and res["relations"] == 1
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities WHERE origin='org'").fetchone()["c"] >= 2
        assert db.execute("SELECT COUNT(*) c FROM entity_relations WHERE origin='org'").fetchone()["c"] == 1


def test_mentioned_with_singleton_stays_pending(tmp_path):
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person", "org": "",
              "email_addr": "", "aliases": ""}),
        _rec({"kind": "entity", "id": "mary", "name": "Mary", "type": "person", "org": "",
              "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "mentioned_with", "entity_b": "mary"}),
    ])
    res = org_curate._materialise(s)
    assert res["pending"] >= 1
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entity_relations "
                          "WHERE relation='mentioned_with'").fetchone()["c"] == 0


def test_mentioned_with_two_sources_materialises(tmp_path):
    s = _store(tmp_path)
    ents = [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person", "org": "",
                  "email_addr": "", "aliases": ""}, sref="r1"),
            _rec({"kind": "entity", "id": "mary", "name": "Mary", "type": "person", "org": "",
                  "email_addr": "", "aliases": ""}, sref="r1")]
    rel = {"kind": "relation", "entity_a": "joel", "relation": "mentioned_with", "entity_b": "mary"}
    _stage(s, ents + [_rec(rel, sref="r1", email="a@x.org"),
                      _rec(rel, sref="r2", email="b@x.org")])
    res = org_curate._materialise(s)
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entity_relations "
                          "WHERE relation='mentioned_with'").fetchone()["c"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_curate.py -k materialise -v`
Expected: FAIL — no attribute `_materialise`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/org_curate.py`:

```python
from mcpbrain import graph_write
from mcpbrain.resolve import is_role_address

# Relations that need independent-source corroboration before entering layer 1
# (spec B1/B3): co-occurrence in a single mailbox never surfaces org-wide.
_CORROBORATION_GUARDED = {"mentioned_with"}


def _staged_claims(store) -> list[dict]:
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT contributor_email, source_ref, claim, confidence, valid_from, valid_to "
            "FROM org_contrib_staging ORDER BY id").fetchall()]


def _stamp_origin(store, *, entity_ids=(), relation_ids=()) -> None:
    with store._connect() as db:
        for eid in entity_ids:
            db.execute("UPDATE entities SET origin='org' WHERE id=?", (eid,))
        for rid in relation_ids:
            db.execute("UPDATE entity_relations SET origin='org' WHERE id=?", (rid,))


def _materialise(store, pin_allowlist=None) -> dict:
    """Materialise corroborated staged claims into origin='org' rows. Entities
    first (so relation endpoints exist), then relations under the corroboration +
    role guards. Supersessions apply the claim's latest valid_to."""
    rows = _staged_claims(store)
    entity_claims: dict = {}         # id -> merged entity claim
    # (a, rel, b) -> {"srefs": set, "contribs": set, "valid_from": min, "valid_to": max, "conf": max}
    relation_claims: dict = {}
    for r in rows:
        claim = json.loads(r["claim"])
        if claim.get("kind") == "entity":
            eid = claim["id"]
            prev = entity_claims.get(eid, {})
            # Never key a person on a role inbox (0.7.77).
            if claim.get("type") == "person" and is_role_address(claim.get("email_addr", "")):
                continue
            entity_claims[eid] = {**prev, **{k: (claim.get(k) or prev.get(k) or "")
                                             for k in ("name", "type", "org", "email_addr", "aliases")}}
        elif claim.get("kind") == "relation":
            key = (claim["entity_a"], claim["relation"], claim["entity_b"])
            agg = relation_claims.setdefault(key, {"srefs": set(), "contribs": set(),
                                                   "valid_from": "", "valid_to": "", "conf": 0.0})
            agg["srefs"].add(r["source_ref"])
            agg["contribs"].add(r["contributor_email"])
            agg["conf"] = max(agg["conf"], float(r["confidence"] or 1.0))
            vf = r["valid_from"] or ""
            if vf and (not agg["valid_from"] or vf < agg["valid_from"]):
                agg["valid_from"] = vf
            vt = r["valid_to"] or ""
            if vt > agg["valid_to"]:
                agg["valid_to"] = vt

    n_ent = 0
    for eid, e in entity_claims.items():
        got = graph_write.upsert_entity(store, name=e.get("name") or eid,
                                        entity_type=e.get("type") or "person",
                                        org=e.get("org", ""), email_addr=e.get("email_addr", ""),
                                        aliases=e.get("aliases", ""))
        if got:
            _stamp_origin(store, entity_ids=[got])
            n_ent += 1

    n_rel = 0
    pending = 0
    for (a, rel, b), agg in relation_claims.items():
        distinct_sources = len(agg["srefs"])
        corroborated = distinct_sources >= 2 or len(agg["contribs"]) >= 2
        if rel in _CORROBORATION_GUARDED and distinct_sources < 2:
            pending += 1
            continue
        if rel in _CORROBORATION_GUARDED and not corroborated:
            pending += 1
            continue
        if store.get_entity(a) is None or store.get_entity(b) is None:
            continue
        rid = graph_write.upsert_relation(
            store, a, rel, b, valid_from=agg["valid_from"] or _today(),
            confidence=agg["conf"] or 1.0, source_doc_id="org-curated")
        if rid is not None:
            _stamp_origin(store, relation_ids=[rid])
            n_rel += 1
            if agg["valid_to"]:
                with store._connect() as db:
                    db.execute("UPDATE entity_relations SET valid_to=? WHERE id=?",
                               (agg["valid_to"], rid))
    return {"entities": n_ent, "relations": n_rel, "pending": pending}


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_curate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_curate.py tests/test_org_curate.py
git commit -m "feat(org): B3 curator — deterministic materialise with corroboration + role guards"
```

---

## Task 6: B3 curator — structural-only AI adjudication (pending-safe, capped)

**Files:**
- Modify: `mcpbrain/org_curate.py`
- Test: `tests/test_org_curate.py`

**Interfaces:**
- Consumes: `resolve._candidate_pairs` (resolve.py:184), `resolve._pick_winner`, `resolve.is_role_address`, `resolve._NAME_MERGEABLE_TYPES`, `store.merge_entities`.
- Produces:
  - `def _build_adjudication_units(store) -> list[dict]` — fuzzy same-type name-pair candidates among `origin='org'` entities, each as a STRUCTURAL-ONLY packet `{"pair_id": "a|b", "a": {...}, "b": {...}}` (names/types/emails/aliases only — contributions carry no content).
  - `adjudicate(units, *, home=None) -> list[dict]` — the injectable adjudication seam. **Default returns `[]`** (no verdicts → everything stays pending, the safe default). Tests and a future Haiku-wired curator replace this.
  - `def _apply_merge_verdicts(store, verdicts, *, cap) -> dict` — applies `{"pair_id", "verdict": "merge"|"pending"|"skip", "canonical"?}` with the 0.7.84 hardening: re-fetch both entities (missing → skip), enforce `_NAME_MERGEABLE_TYPES` + role-address guards, cap merges, and treat anything not strictly `"merge"` (including `"pending"`) as a no-op. Returns `{"merged", "guarded", "capped", "pending", "skipped"}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_org_curate.py`:

```python
def test_adjudicate_default_is_all_pending(tmp_path):
    s = _store(tmp_path)
    assert org_curate.adjudicate([{"pair_id": "a|b"}]) == []


def test_apply_merge_verdict_merges_only_on_merge(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name in (("joel-c", "Joel C"), ("joel-chelliah", "Joel Chelliah")):
            db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                       "VALUES(?,?,'person','org',1)", (eid, name))
    res = org_curate._apply_merge_verdicts(
        s, [{"pair_id": "joel-c|joel-chelliah", "verdict": "merge", "canonical": "Joel Chelliah"}],
        cap=10)
    assert res["merged"] == 1
    assert s.get_entity("joel-c") is None or s.get_entity("joel-chelliah") is None


def test_apply_merge_verdict_pending_is_noop(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        for eid in ("a", "b"):
            db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                       "VALUES(?,?,'person','org',1)", (eid, eid))
    res = org_curate._apply_merge_verdicts(s, [{"pair_id": "a|b", "verdict": "pending"}], cap=10)
    assert res["pending"] == 1 and res["merged"] == 0
    assert s.get_entity("a") is not None and s.get_entity("b") is not None


def test_apply_merge_verdict_role_address_guarded(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('office-a','Office','person','office@x.org','org')")
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('office-b','Office B','person','office@y.org','org')")
    res = org_curate._apply_merge_verdicts(
        s, [{"pair_id": "office-a|office-b", "verdict": "merge"}], cap=10)
    assert res["guarded"] == 1 and res["merged"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_curate.py -k "adjudicate or merge_verdict" -v`
Expected: FAIL — attributes missing.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/org_curate.py`:

```python
from mcpbrain.resolve import (_NAME_MERGEABLE_TYPES, _candidate_pairs, _pick_winner)


def _org_entities(store) -> list[dict]:
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, name, type, org, email_addr, aliases, mentions "
            "FROM entities WHERE origin='org' ORDER BY id").fetchall()]


def _build_adjudication_units(store) -> list[dict]:
    """Fuzzy same-type name-pair candidates among org entities, structural-only.
    Reuses resolve._candidate_pairs (blocking + token similarity, name-identity
    types only) — the exact machinery the local fuzzy-review queue uses."""
    ents = _org_entities(store)
    units = []
    for a, b in _candidate_pairs(ents):
        pair_id = "|".join(sorted((a["id"], b["id"])))
        units.append({"pair_id": pair_id,
                      "a": {k: a.get(k, "") for k in ("id", "name", "type", "email_addr", "aliases")},
                      "b": {k: b.get(k, "") for k in ("id", "name", "type", "email_addr", "aliases")}})
    return units


def adjudicate(units, *, home=None) -> list[dict]:
    """Adjudication seam. Default: no verdicts -> everything pending (safe
    default, spec B3.3). A Haiku-wired curator or a test monkeypatches this to
    return [{"pair_id", "verdict": "merge"|"pending"|"skip", "canonical"?}, ...]."""
    return []


def _apply_merge_verdicts(store, verdicts, *, cap) -> dict:
    """Apply curator merge verdicts with the 0.7.84 hardening: authoritative
    entity lookup (missing -> skip), type + role-address guards, capped merges,
    and anything not strictly 'merge' (incl. 'pending') is a no-op."""
    result = {"merged": 0, "guarded": 0, "capped": 0, "pending": 0, "skipped": 0}
    for v in verdicts or []:
        verdict = v.get("verdict")
        ids = (v.get("pair_id") or "").split("|")
        if len(ids) != 2 or not all(ids) or ids[0] == ids[1]:
            result["skipped"] += 1
            continue
        if verdict == "pending":
            result["pending"] += 1
            continue
        if verdict != "merge":
            result["skipped"] += 1
            continue
        a = store.get_entity(ids[0])
        b = store.get_entity(ids[1])
        if a is None or b is None:
            result["skipped"] += 1
            continue
        if a["type"] not in _NAME_MERGEABLE_TYPES or b["type"] not in _NAME_MERGEABLE_TYPES:
            result["guarded"] += 1
            continue
        if is_role_address(a.get("email_addr", "")) or is_role_address(b.get("email_addr", "")):
            result["guarded"] += 1
            continue
        if result["merged"] >= cap:
            result["capped"] += 1
            continue
        winner, loser = _pick_winner(a, b)
        store.merge_entities(loser["id"], winner["id"],
                             canonical_name=v.get("canonical") or None, method="curator")
        result["merged"] += 1
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_curate.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_curate.py tests/test_org_curate.py
git commit -m "feat(org): B3 curator — structural-only adjudication seam + hardened merge appliers"
```

---

## Task 7: B3 curator — publish snapshot + `run()` orchestration

**Files:**
- Modify: `mcpbrain/org_curate.py`
- Test: `tests/test_org_curate.py`

**Interfaces:**
- Consumes: everything above, `store.list_entity_merges` (store.py:1835), `orgs.taxonomy_from_config`.
- Produces:
  - `def _publish(store, fleet_storage, home) -> SnapshotManifest` — serialises `origin='org'` entities + relations + org taxonomy into `org-graph/snapshot.jsonl.gz`, tombstones (from `entity_merge_log`) into `org-graph/tombstones.jsonl`, then writes `org-graph/manifest.json` **LAST** with `version = prev+1` and `snapshot_sha256 = sha256(gzip_bytes)`. Version tracked in `meta['org_curator_version']`.
  - `def run(store, fleet_storage, home) -> dict` — the full pipeline: ingest → materialise → deterministic dedup via `resolve.resolve_entities(store, curator=True)` (org↔org merges permitted only here) → build units → `adjudicate` → `_apply_merge_verdicts` (capped by `config.review_max_apply_per_run`) → `_publish`. Returns a summary dict.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_org_curate.py`:

```python
def test_run_end_to_end_publishes_snapshot(tmp_path, monkeypatch):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    from mcpbrain.org_contracts import SnapshotManifest
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/alice@x.org/1.jsonl", [
        _rec({"kind": "entity", "id": "joel", "name": "Joel Chelliah", "type": "person",
              "org": "Acme", "email_addr": "joel@acme.org", "aliases": ""}),
        _rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"}),
    ])
    summary = org_curate.run(s, fs, str(tmp_path))
    assert summary["published"] is True and summary["version"] == 1
    man = SnapshotManifest.from_dict(json.loads(fs.get_bytes("org-graph/manifest.json")))
    assert man.entity_count >= 2 and man.relation_count == 1
    gz = fs.get_bytes("org-graph/snapshot.jsonl.gz")
    assert hashlib_sha(gz) == man.snapshot_sha256
    lines = gzip.decompress(gz).decode().splitlines()
    kinds = {json.loads(x)["kind"] for x in lines}
    assert {"entity", "relation"} <= kinds


def hashlib_sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


def test_run_second_publish_bumps_version(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/a@x.org/1.jsonl",
                 [_rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
                        "org": "", "email_addr": "", "aliases": ""})])
    v1 = org_curate.run(s, fs, str(tmp_path))["version"]
    v2 = org_curate.run(s, fs, str(tmp_path))["version"]
    assert (v1, v2) == (1, 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_curate.py -k "run_" -v`
Expected: FAIL — no attribute `run`.

- [ ] **Step 3: Write minimal implementation**

Append to `mcpbrain/org_curate.py`:

```python
from mcpbrain import config, resolve

SNAPSHOT_PATH = "org-graph/snapshot.jsonl.gz"
TOMBSTONES_PATH = "org-graph/tombstones.jsonl"
MANIFEST_PATH = "org-graph/manifest.json"


def _snapshot_lines(store, home) -> list[str]:
    lines = []
    with store._connect() as db:
        for e in db.execute(
            "SELECT id, name, type, org, email_addr, aliases FROM entities "
            "WHERE origin='org' ORDER BY id").fetchall():
            lines.append(json.dumps({"kind": "entity", **dict(e)}, sort_keys=True))
        for r in db.execute(
            "SELECT entity_a, relation, entity_b, valid_from, valid_to, confidence "
            "FROM entity_relations WHERE origin='org' AND invalidated_at IS NULL "
            "ORDER BY id").fetchall():
            lines.append(json.dumps({"kind": "relation", **dict(r)}, sort_keys=True))
    lines.append(json.dumps({"kind": "org_taxonomy",
                             "names": list(orgs.taxonomy_from_config(home).names)}, sort_keys=True))
    return lines


def _tombstones(store) -> list[Tombstone]:
    # Every merged-away id becomes a tombstone pointing at its winner, so a
    # consumer re-import never resurrects it (spec B3.4 / B5.4).
    return [Tombstone(entity_id=m["loser_id"], merged_into=m["winner_id"])
            for m in store.list_entity_merges()]


def _publish(store, fleet_storage, home) -> SnapshotManifest:
    prev = int(store.get_meta("org_curator_version") or 0)
    version = prev + 1
    lines = _snapshot_lines(store, home)
    gz = gzip.compress(("\n".join(lines) + "\n").encode("utf-8"))
    tombs = _tombstones(store)
    n_ent = sum(1 for x in lines if json.loads(x)["kind"] == "entity")
    n_rel = sum(1 for x in lines if json.loads(x)["kind"] == "relation")
    manifest = SnapshotManifest(
        version=version, created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        entity_count=n_ent, relation_count=n_rel, tombstone_count=len(tombs),
        snapshot_sha256=hashlib.sha256(gz).hexdigest())
    # Order matters: snapshot + tombstones FIRST, manifest LAST, so a crash mid-
    # publish never leaves consumers a manifest pointing at a missing snapshot.
    fleet_storage.put_bytes(SNAPSHOT_PATH, gz)
    fleet_storage.put_bytes(
        TOMBSTONES_PATH,
        ("\n".join(json.dumps(t.to_dict(), sort_keys=True) for t in tombs) + "\n").encode()
        if tombs else b"")
    fleet_storage.put_bytes(MANIFEST_PATH,
                            json.dumps(manifest.to_dict(), sort_keys=True).encode())
    store.set_meta("org_curator_version", str(version))
    return manifest


def run(store, fleet_storage, home) -> dict:
    """Full curator pass. Safe to run repeatedly (ingest is idempotent; publish
    versions monotonically)."""
    ing = _ingest(store, fleet_storage)
    mat = _materialise(store)
    # Org-layer dedup is the curator's job — resolve with curator=True so the
    # B4a org<->org guard (Task 11) is bypassed here (and only here).
    resolve.resolve_entities(store, home=home, curator=True)
    units = _build_adjudication_units(store)
    cap = config.review_max_apply_per_run(home)
    verdicts = adjudicate(units, home=home)
    adj = _apply_merge_verdicts(store, verdicts, cap=cap)
    manifest = _publish(store, fleet_storage, home)
    return {"published": True, "version": manifest.version,
            "ingested": ing["ingested"], "materialised": mat,
            "adjudicated": adj, "units": len(units)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_curate.py -v`
Expected: PASS. (Depends on Task 11's `resolve_entities(curator=...)` param — if run before Task 11, temporarily call `resolve.resolve_entities(store, home=home)`; the final wiring uses `curator=True`.)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_curate.py tests/test_org_curate.py
git commit -m "feat(org): B3 curator — versioned snapshot publish + run() orchestration"
```

---

## Task 8: B4 consumer — transactional import core (wholesale-replace + tombstones + demote)

**Files:**
- Create: `mcpbrain/org_import.py`
- Test: `tests/test_org_import.py`

**Interfaces:**
- Consumes: `FleetStorage`, `org_contracts.SnapshotManifest`/`Tombstone`, `store.set_meta`/`get_meta`.
- Produces:
  - `def import_snapshot(store, fleet_storage) -> dict` — fetch `manifest.json`; if absent → `{"status": "no_snapshot"}`; if version isn't newer than `meta['org_snapshot_version']` → `{"status": "unchanged", "version": <current>}`; fetch `snapshot.jsonl.gz`, verify `snapshot_sha256` (mismatch → `{"status": "error", "reason": "sha_mismatch"}`, previous layer intact); then in ONE transaction: upsert snapshot rows as `origin='org'`; wholesale-replace (delete `origin='org'` rows absent from the snapshot — but **demote to `origin='local'`** instead of deleting when local relations/observations are attached); apply tombstones (re-point local references to `merged_into`, else delete/demote); set the new version. `origin='local'` rows are never touched. On success → `{"status": "imported", "version", "entities", "relations", "tombstoned", "demoted"}`. **Consumed by subsystem C onboarding too — C branches on `status in {"imported", "unchanged", "no_snapshot"}`, so the `status` string is the frozen contract.**

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_import.py`:

```python
import gzip
import hashlib
import json

from mcpbrain import org_import
from mcpbrain.org_contracts import SnapshotManifest, Tombstone
from mcpbrain.store import Store


def _store(tmp_path, name="consumer"):
    s = Store(tmp_path / f"{name}.sqlite3", dim=4)
    s.init()
    return s


def _publish(fs, entities, relations, *, version, tombstones=()):
    lines = ([json.dumps({"kind": "entity", **e}, sort_keys=True) for e in entities] +
             [json.dumps({"kind": "relation", **r}, sort_keys=True) for r in relations])
    gz = gzip.compress(("\n".join(lines) + "\n").encode())
    man = SnapshotManifest(version=version, created_at="t", entity_count=len(entities),
                           relation_count=len(relations), tombstone_count=len(tombstones),
                           snapshot_sha256=hashlib.sha256(gz).hexdigest())
    fs.put_bytes("org-graph/snapshot.jsonl.gz", gz)
    fs.put_bytes("org-graph/tombstones.jsonl",
                 ("\n".join(json.dumps(t.to_dict(), sort_keys=True) for t in tombstones) + "\n").encode()
                 if tombstones else b"")
    fs.put_bytes("org-graph/manifest.json", json.dumps(man.to_dict(), sort_keys=True).encode())


def _ent(id, name, type="person", **kw):
    return {"id": id, "name": name, "type": type, "org": kw.get("org", ""),
            "email_addr": kw.get("email_addr", ""), "aliases": kw.get("aliases", "")}


def test_import_writes_org_rows(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("joel", "Joel"), _ent("acme", "Acme", "org")],
             [{"entity_a": "joel", "relation": "works_at", "entity_b": "acme",
               "valid_from": "2026-01-01", "valid_to": "", "confidence": 1.0}], version=1)
    res = org_import.import_snapshot(s, fs)
    assert res["status"] == "imported" and res["version"] == 1
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities WHERE origin='org'").fetchone()["c"] == 2
        assert db.execute("SELECT origin FROM entity_relations WHERE entity_a='joel'").fetchone()["origin"] == "org"


def test_not_newer_is_skipped(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    again = org_import.import_snapshot(s, fs)
    assert again["status"] == "unchanged" and again["version"] == 1


def test_sha_mismatch_aborts_and_leaves_layer_intact(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    # publish v2 but corrupt the gzip after the manifest is written
    _publish(fs, [_ent("acme", "Acme", "org"), _ent("beta", "Beta", "org")], [], version=2)
    fs.put_bytes("org-graph/snapshot.jsonl.gz", b"corrupt-not-matching-sha")
    res = org_import.import_snapshot(s, fs)
    assert res["status"] == "error" and res["reason"] == "sha_mismatch"
    with s._connect() as db:                       # v1 layer survives
        assert db.execute("SELECT COUNT(*) c FROM entities WHERE origin='org'").fetchone()["c"] == 1


def test_wholesale_replace_removes_absent_org_rows_but_keeps_local(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('mine','Mine','person','local')")
    _publish(fs, [_ent("acme", "Acme", "org"), _ent("beta", "Beta", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=2)   # beta gone
    org_import.import_snapshot(s, fs)
    assert s.get_entity("beta") is None            # absent org row removed
    assert s.get_entity("acme") is not None        # still present
    assert s.get_entity("mine") is not None        # local untouched


def test_removal_demotes_when_local_data_attached(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("beta", "Beta", "org")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:                        # attach a LOCAL relation to the org node
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('me','Me','person','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('me','mentioned_with','beta','local')")
    _publish(fs, [_ent("acme", "Acme", "org")], [], version=2)   # beta absent
    org_import.import_snapshot(s, fs)
    beta = s.get_entity("beta")
    assert beta is not None and beta["origin"] == "local"   # demoted, not deleted


def test_tombstone_repoints_local_references(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _publish(fs, [_ent("dup", "Dup"), _ent("joel-chelliah", "Joel Chelliah")], [], version=1)
    org_import.import_snapshot(s, fs)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('doc','Doc','document','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin) "
                   "VALUES('doc','mentioned_with','dup','local')")
    _publish(fs, [_ent("joel-chelliah", "Joel Chelliah")], [], version=2,
             tombstones=[Tombstone(entity_id="dup", merged_into="joel-chelliah")])
    org_import.import_snapshot(s, fs)
    assert s.get_entity("dup") is None
    with s._connect() as db:
        row = db.execute("SELECT entity_b FROM entity_relations WHERE entity_a='doc'").fetchone()
    assert row["entity_b"] == "joel-chelliah"       # local ref re-pointed to the survivor
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_import.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.org_import'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/org_import.py`:

```python
"""Subsystem B4 — consumer import.

Fetches the curator's versioned snapshot and imports it as origin='org' rows
that coexist with local data. Wholesale-replace per origin (the fleet.merge_org_
config semantics applied to data): org rows absent from a newer snapshot are
removed — but DEMOTED to origin='local' rather than deleted when local
relations/observations hang off them, so import never orphans the user's own
knowledge. Tombstones re-point local references onto merge survivors so a stale
import can't resurrect a merged-away node. Single transaction; sha256-verified;
origin='local' rows are never touched.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging

from mcpbrain.org_contracts import SnapshotManifest, Tombstone

log = logging.getLogger(__name__)

MANIFEST_PATH = "org-graph/manifest.json"
SNAPSHOT_PATH = "org-graph/snapshot.jsonl.gz"
TOMBSTONES_PATH = "org-graph/tombstones.jsonl"


def _parse_snapshot(gz: bytes):
    entities, relations, taxonomy = [], [], []
    for line in gzip.decompress(gz).decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        kind = obj.get("kind")
        if kind == "entity":
            entities.append(obj)
        elif kind == "relation":
            relations.append(obj)
        elif kind == "org_taxonomy":
            taxonomy = obj.get("names", [])
    return entities, relations, taxonomy


def _has_local_attachments(db, entity_id) -> bool:
    r = db.execute(
        "SELECT 1 FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='local' LIMIT 1",
        (entity_id, entity_id)).fetchone()
    if r:
        return True
    return db.execute(
        "SELECT 1 FROM entity_observations WHERE entity_id=? LIMIT 1", (entity_id,)).fetchone() is not None


def _repoint_local_refs(db, from_id, to_id) -> None:
    db.execute("UPDATE OR IGNORE entity_relations SET entity_a=? WHERE entity_a=? AND origin='local'",
               (to_id, from_id))
    db.execute("UPDATE OR IGNORE entity_relations SET entity_b=? WHERE entity_b=? AND origin='local'",
               (to_id, from_id))
    db.execute("DELETE FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='local'",  # admin-delete-ok
               (from_id, from_id))
    db.execute("UPDATE entity_observations SET entity_id=? WHERE entity_id=?", (to_id, from_id))


def import_snapshot(store, fleet_storage) -> dict:
    """See module docstring. Signature frozen — consumed by C onboarding too."""
    raw_manifest = fleet_storage.get_bytes(MANIFEST_PATH)
    if not raw_manifest:
        return {"status": "no_snapshot"}
    manifest = SnapshotManifest.from_dict(json.loads(raw_manifest))
    local_version = int(store.get_meta("org_snapshot_version") or 0)
    if manifest.version <= local_version:
        return {"status": "unchanged", "version": local_version}
    gz = fleet_storage.get_bytes(SNAPSHOT_PATH)
    if not gz or hashlib.sha256(gz).hexdigest() != manifest.snapshot_sha256:
        log.warning("org_import: snapshot sha256 mismatch (v%s); aborting", manifest.version)
        return {"status": "error", "reason": "sha_mismatch"}
    entities, relations, _taxonomy = _parse_snapshot(gz)
    raw_tombs = fleet_storage.get_bytes(TOMBSTONES_PATH) or b""
    tombstones = [Tombstone.from_dict(json.loads(x))
                  for x in raw_tombs.decode("utf-8").splitlines() if x.strip()]

    snapshot_ids = {e["id"] for e in entities}
    demoted = tombstoned = 0
    with store._connect() as db:
        # (1) Upsert snapshot entities as origin='org' (skeleton authoritative;
        #     a same-slug local row keeps its local flesh via COALESCE-add only).
        for e in entities:
            db.execute(
                "INSERT INTO entities(id,name,type,org,email_addr,aliases,origin,first_seen,last_seen) "
                "VALUES(?,?,?,?,?,?, 'org', '', '') "
                "ON CONFLICT(id) DO UPDATE SET "
                "  name=excluded.name, type=excluded.type, org=excluded.org, "
                "  email_addr=excluded.email_addr, "
                "  aliases=CASE WHEN entities.aliases='' THEN excluded.aliases ELSE entities.aliases END, "
                "  origin='org'",
                (e["id"], e.get("name", ""), e.get("type", "person"), e.get("org", ""),
                 e.get("email_addr", ""), e.get("aliases", "")))
        # (2) Upsert snapshot relations as origin='org'.
        for r in relations:
            db.execute(
                "INSERT OR IGNORE INTO entity_relations"
                "(entity_a,relation,entity_b,valid_from,valid_to,confidence,origin,source_doc_id) "
                "VALUES(?,?,?,?,?,?, 'org', 'org-snapshot')",
                (r["entity_a"], r["relation"], r["entity_b"], r.get("valid_from", ""),
                 r.get("valid_to", ""), r.get("confidence", 1.0)))
            db.execute("UPDATE entity_relations SET origin='org', valid_to=? "
                       "WHERE entity_a=? AND relation=? AND entity_b=?",
                       (r.get("valid_to", ""), r["entity_a"], r["relation"], r["entity_b"]))
        # (3) Wholesale-replace: org rows absent from the snapshot go — but demote
        #     (not delete) when local data is attached (spec B4).
        org_rows = [row["id"] for row in db.execute(
            "SELECT id FROM entities WHERE origin='org'").fetchall()]
        for eid in org_rows:
            if eid in snapshot_ids:
                continue
            if _has_local_attachments(db, eid):
                db.execute("UPDATE entities SET origin='local' WHERE id=?", (eid,))
                demoted += 1
            else:
                db.execute("DELETE FROM entity_relations WHERE (entity_a=? OR entity_b=?) AND origin='org'",  # admin-delete-ok
                           (eid, eid))
                db.execute("DELETE FROM entities WHERE id=?", (eid,))  # admin-delete-ok
        db.execute("DELETE FROM entity_relations WHERE origin='org' AND source_doc_id='org-snapshot' "  # admin-delete-ok
                   "AND (entity_a NOT IN (SELECT id FROM entities) OR entity_b NOT IN (SELECT id FROM entities))")
        # (4) Tombstones: re-point local references to the survivor, then remove
        #     the tombstoned node (spec B3.4 / B4).
        for t in tombstones:
            if store.get_entity(t.entity_id) is None:
                continue
            if t.merged_into and store.get_entity(t.merged_into) is not None:
                _repoint_local_refs(db, t.entity_id, t.merged_into)
                store.record_change("org_repoint", ref_id=t.entity_id,
                                    summary=f"tombstone -> {t.merged_into}", source="org_import")
            db.execute("DELETE FROM entity_relations WHERE entity_a=? OR entity_b=?",  # admin-delete-ok
                       (t.entity_id, t.entity_id))
            db.execute("DELETE FROM entities WHERE id=?", (t.entity_id,))  # admin-delete-ok
            tombstoned += 1
    store.set_meta("org_snapshot_version", str(manifest.version))
    return {"status": "imported", "version": manifest.version,
            "entities": len(entities), "relations": len(relations),
            "tombstoned": tombstoned, "demoted": demoted}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_import.py -v`
Expected: PASS (all 6 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_import.py tests/test_org_import.py
git commit -m "feat(org): B4 consumer — transactional snapshot import (replace + tombstones + demote)"
```

---

## Task 9: B4 consumer — slug-drift reconciliation + restore-from-repoint-log

**Files:**
- Modify: `mcpbrain/org_import.py`
- Test: `tests/test_org_import.py`

**Interfaces:**
- Consumes: `resolve.is_role_address`, `resolve.canonical_key`/`_tokens`/`_token_set_ratio`, `store.merge_entities`, `org_repoint_log` (Phase 0), `entity_merge_log`.
- Produces:
  - `def _reconcile_slug_drift(store, entities) -> int` — before wholesale-replace, reconcile each incoming org entity against existing **local** entities: email-equality merge (role-address guarded) and org-supplied-alias / canonical-key token match. The local node merges **into** the org node (org survives — spec B4a rule 2); each re-point is logged to `org_repoint_log`. Ambiguous name-only pairs are left for the local fuzzy-review queue (never auto-merged). Called from `import_snapshot` at the top of the transaction.
  - `def _restore_from_repoint_log(store, entities) -> int` — when a newer snapshot re-introduces an id previously merged away (a curator SPLIT), consult `org_repoint_log` to re-attach the local flesh that was moved onto the merge target back to the resurrected node.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_org_import.py`:

```python
def test_slug_drift_email_equality_merges_local_into_org(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                        # local variant with a private observation
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-c','Joel C','person','joel@acme.org','local',3)")
        db.execute("INSERT INTO entity_observations(entity_id,attribute,value,source,valid_from) "
                   "VALUES('joel-c','note','private','local','2026-01-01')")
    _publish(fs, [_ent("joel-chelliah", "Joel Chelliah", email_addr="joel@acme.org")],
             [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("joel-c") is None            # local merged away
    surv = s.get_entity("joel-chelliah")
    assert surv is not None and surv["origin"] == "org"   # org node survives
    with s._connect() as db:
        obs = db.execute("SELECT entity_id FROM entity_observations WHERE attribute='note'").fetchone()
        rep = db.execute("SELECT from_entity_id,to_entity_id FROM org_repoint_log").fetchone()
    assert obs["entity_id"] == "joel-chelliah"       # private flesh re-attached
    assert (rep["from_entity_id"], rep["to_entity_id"]) == ("joel-c", "joel-chelliah")


def test_role_address_pair_never_auto_merges(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('office-local','Office','person','office@acme.org','local')")
    _publish(fs, [_ent("office-org", "Office Org", email_addr="office@acme.org")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("office-local") is not None  # NOT merged (role inbox)


def test_ambiguous_name_only_pair_left_for_fuzzy_queue(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:                         # same-ish name, no shared email/alias
        db.execute("INSERT INTO entities(id,name,type,origin) "
                   "VALUES('jsmith','J Smith','person','local')")
    _publish(fs, [_ent("john-smith", "John Smith")], [], version=1)
    org_import.import_snapshot(s, fs)
    assert s.get_entity("jsmith") is not None         # not auto-merged
    assert s.get_entity("john-smith") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_import.py -k "slug_drift or role_address_pair or ambiguous" -v`
Expected: FAIL — no reconciliation yet (local `joel-c` survives, observation not re-attached).

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/org_import.py`, add the reconciliation helpers and call them at the top of the transaction in `import_snapshot` (immediately before step (1) "Upsert snapshot entities"):

```python
from mcpbrain.resolve import canonical_key, is_role_address, _token_set_ratio, _tokens


def _log_repoint(store, from_id, to_id, version, reason) -> None:
    with store._connect() as db:
        db.execute("INSERT INTO org_repoint_log(from_entity_id,to_entity_id,snapshot_version,reason) "
                   "VALUES(?,?,?,?)", (from_id, to_id, version, reason))


def _reconcile_slug_drift(store, entities, version) -> int:
    """Merge existing LOCAL entities into their incoming org twin (org survives)
    on a shared email (role-guarded) or an alias/canonical-key/token match.
    Ambiguous name-only pairs are left for the local fuzzy queue. Logs each
    re-point to org_repoint_log (spec B4/B4a)."""
    merged = 0
    with store._connect() as db:
        local = [dict(r) for r in db.execute(
            "SELECT id,name,type,email_addr,aliases FROM entities WHERE origin='local'").fetchall()]
    local_by_email = {}
    for l in local:
        em = (l.get("email_addr") or "").strip().lower()
        if em and not is_role_address(em):
            local_by_email.setdefault(em, []).append(l)
    for e in entities:
        org_id = e["id"]
        # (a) email-equality (deterministic, role-guarded)
        em = (e.get("email_addr") or "").strip().lower()
        target = None
        if em and not is_role_address(em):
            cands = [l for l in local_by_email.get(em, []) if l["id"] != org_id]
            if len(cands) == 1:
                target = cands[0]
        # (b) alias / canonical-key token match (same-type only)
        if target is None:
            org_toks = _tokens(e.get("name", ""))
            org_key = canonical_key(e.get("name", ""))
            org_aliases = {a.strip().lower() for a in (e.get("aliases") or "").split(",") if a.strip()}
            matches = []
            for l in local:
                if l["id"] == org_id or l["type"] != e.get("type"):
                    continue
                if is_role_address(l.get("email_addr", "")):
                    continue
                lk = canonical_key(l["name"])
                lname = l["name"].strip().lower()
                if lk and lk == org_key:
                    matches.append(l)
                elif lname in org_aliases:
                    matches.append(l)
                elif _token_set_ratio(org_toks, _tokens(l["name"])) >= 0.8:
                    matches.append(l)
            if len(matches) == 1:            # single unambiguous match only
                target = matches[0]
        if target is not None and store.get_entity(org_id) is not None:
            # Local merges INTO org (org id survives — B4a rule 2). If the org row
            # isn't materialised yet (upsert happens after), create a stub first.
            store.merge_entities(target["id"], org_id, method="slug_drift")
            _log_repoint(store, target["id"], org_id, version, "slug_drift")
            merged += 1
    return merged
```

Then in `import_snapshot`, before step (1), materialise org id stubs and reconcile so `merge_entities` has a target:

```python
    with store._connect() as db:
        for e in entities:                          # stub org ids so reconcile can merge into them
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin,first_seen,last_seen) "
                       "VALUES(?,?,?, 'org', '', '')",
                       (e["id"], e.get("name", ""), e.get("type", "person")))
    reconciled = _reconcile_slug_drift(store, entities, manifest.version)
    restored = _restore_from_repoint_log(store, entities)
```

Add `_restore_from_repoint_log` (curator split recovery — B4a rule 4):

```python
def _restore_from_repoint_log(store, entities) -> int:
    """When a snapshot re-introduces an id previously merged away (curator
    split), re-attach the local flesh that was moved onto the merge target back
    to the resurrected node, using org_repoint_log."""
    incoming = {e["id"] for e in entities}
    restored = 0
    with store._connect() as db:
        logs = [dict(r) for r in db.execute(
            "SELECT from_entity_id, to_entity_id FROM org_repoint_log").fetchall()]
    for lg in logs:
        resurrected = lg["from_entity_id"]
        target = lg["to_entity_id"]
        if resurrected in incoming and store.get_entity(resurrected) is None:
            with store._connect() as db:
                db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin,first_seen,last_seen) "
                           "VALUES(?,?, 'person', 'org', '', '')", (resurrected, resurrected))
                db.execute("UPDATE entity_observations SET entity_id=? WHERE entity_id=?",
                           (resurrected, target))
            restored += 1
    return restored
```

Include `reconciled`/`restored` in the returned dict (`"reconciled": reconciled, "restored": restored`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_import.py -v`
Expected: PASS (all reconciliation + earlier tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/org_import.py tests/test_org_import.py
git commit -m "feat(org): B4 consumer — slug-drift reconciliation + split recovery via repoint log"
```

---

## Task 10: Fill the three `_run_org_*` daemon bodies

**Files:**
- Modify: `mcpbrain/daemon.py` (bodies of `_run_org_contrib_upload` daemon.py:1658, `_run_org_import` daemon.py:1665, `_run_org_curate` daemon.py:1672 — **bodies only**; keep each `_is_due(...)` gate and `dict | None` return)
- Test: `tests/test_org_daemon_bodies.py`

**Interfaces:**
- Consumes: `org_contrib`, `org_curate`, `org_import`, `config` (`app_dir`, `org_contrib_enabled`, `org_import_enabled`, `is_org_curator`, `owner_email`, `fleet_pin`), subsystem A's `fleet_storage.fleet_folder_storage` (via guarded import), and `self.ensure_services()` (daemon.py:592-620) for the Drive service.
- Produces: three cadence bodies that gate on their config flag + a live `FleetStorage`, advance `_last_*` when they run, and delegate to the modules. Each returns a small summary dict or `{"skipped": <reason>}`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_daemon_bodies.py`:

```python
from mcpbrain import daemon as d


def _daemon():
    # A bare Daemon-like object is heavy to build; assert the bodies gate on the
    # module-level seams by patching them. Use the real class with a minimal shim.
    class Shim(d.Daemon):
        def __init__(self):
            self._org_contrib_upload_interval_s = 1.0
            self._last_org_contrib_upload = None
            self._org_import_interval_s = 1.0
            self._last_org_import = None
            self._org_curate_interval_s = 1.0
            self._last_org_curate = None
            self._clock = lambda: 1000.0

        def ensure_services(self):          # real daemon resolves services here
            return {"drive_service": None}
    return Shim()


def test_contrib_upload_skips_when_unpinned(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    dm = _daemon()
    res = dm._run_org_contrib_upload()
    assert res == {"skipped": "unpinned"} or res == {"skipped": "disabled"}
    assert dm._last_org_contrib_upload == 1000.0   # advanced despite skip


def test_curate_skips_when_not_curator(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)   # role defaults to 'member'
    dm = _daemon()
    assert dm._run_org_curate() == {"skipped": "not_curator"}


def test_import_noops_without_fleet_storage(tmp_path, monkeypatch):
    # Simulate the pre-A state: subsystem A's fleet_storage module either isn't
    # importable, or its factory returns None (no Drive service). Inject a fake
    # that returns None so the assertion holds regardless of whether A has landed.
    import sys
    import types
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    fake = types.ModuleType("mcpbrain.fleet_storage")
    fake.fleet_folder_storage = lambda home, drive_service=None: None
    monkeypatch.setitem(sys.modules, "mcpbrain.fleet_storage", fake)
    dm = _daemon()
    assert dm._run_org_import() == {"skipped": "no_fleet_storage"}
    assert dm._last_org_import == 1000.0           # advanced despite skip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_daemon_bodies.py -v`
Expected: FAIL — the stub bodies return `None`, not the skip dicts.

- [ ] **Step 3: Write minimal implementation**

Replace the three stub bodies in `mcpbrain/daemon.py` (keep the `_is_due` gate + `now = self._clock()` advance pattern from `_run_review` daemon.py:1617):

```python
    def _run_org_contrib_upload(self) -> dict | None:
        """Collect allowlisted deltas since the watermark and upload the outbox
        to the fleet folder. Both steps run here because Phase B may not add a
        drain-path hook — collect_from_drain stays reusable for a future one."""
        if not self._is_due("_org_contrib_upload_interval_s", "_last_org_contrib_upload"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_contrib
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            if not _config.org_contrib_enabled(home):
                self._last_org_contrib_upload = now
                return {"skipped": "disabled"}
            pin = _config.fleet_pin(home)
            if not pin.is_pinned:
                self._last_org_contrib_upload = now
                return {"skipped": "unpinned"}
            # FleetStorage is built by subsystem A (mcpbrain/fleet_storage.py). Guarded
            # import keeps B build-independent of A pre-convergence; the Drive service
            # lives in the services dict (daemon.py:592-620), not on self.
            try:
                from mcpbrain import fleet_storage
                fs = fleet_storage.fleet_folder_storage(
                    home, drive_service=self.ensure_services().get("drive_service"))
            except ImportError:
                fs = None
            if fs is None:
                self._last_org_contrib_upload = now
                return {"skipped": "no_fleet_storage"}
            email = _config.owner_email(home)
            delta, wm = org_contrib._delta_since_watermark(self._store)
            n = org_contrib.collect_from_drain(self._store, delta, pin, email)
            self._store.set_meta("org_contrib_hwm", str(wm["hwm"]))
            self._store.set_meta("org_contrib_ts", wm["ts"])
            up = org_contrib.upload_pending(self._store, fs, email)
            log.info("org_contrib: collected=%d uploaded=%d", n, up["uploaded"])
        except Exception as exc:  # noqa: BLE001 — a cadence must never crash the loop
            log.warning("org_contrib pass failed: %s", exc, exc_info=True)
            return {"org_contrib": False, "error": str(exc)}
        self._last_org_contrib_upload = now
        return {"collected": n, **up}

    def _run_org_import(self) -> dict | None:
        """Import a newer org-graph snapshot into origin='org' rows."""
        if not self._is_due("_org_import_interval_s", "_last_org_import"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_import
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            if not _config.org_import_enabled(home):
                self._last_org_import = now
                return {"skipped": "disabled"}
            try:
                from mcpbrain import fleet_storage
                fs = fleet_storage.fleet_folder_storage(
                    home, drive_service=self.ensure_services().get("drive_service"))
            except ImportError:
                fs = None
            if fs is None:
                self._last_org_import = now
                return {"skipped": "no_fleet_storage"}
            res = org_import.import_snapshot(self._store, fs)
            log.info("org_import: %s", res)
        except Exception as exc:  # noqa: BLE001
            log.warning("org_import pass failed: %s", exc, exc_info=True)
            return {"org_import": False, "error": str(exc)}
        self._last_org_import = now
        return res

    def _run_org_curate(self) -> dict | None:
        """Curator-only: ingest contributions, adjudicate, publish a snapshot."""
        if not self._is_due("_org_curate_interval_s", "_last_org_curate"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_curate
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            if not _config.is_org_curator(home):
                self._last_org_curate = now
                return {"skipped": "not_curator"}
            try:
                from mcpbrain import fleet_storage
                fs = fleet_storage.fleet_folder_storage(
                    home, drive_service=self.ensure_services().get("drive_service"))
            except ImportError:
                fs = None
            if fs is None:
                self._last_org_curate = now
                return {"skipped": "no_fleet_storage"}
            res = org_curate.run(self._store, fs, home)
            log.info("org_curate: %s", {k: res[k] for k in ("version", "ingested") if k in res})
        except Exception as exc:  # noqa: BLE001
            log.warning("org_curate pass failed: %s", exc, exc_info=True)
            return {"org_curate": False, "error": str(exc)}
        self._last_org_curate = now
        return res
```

> **Note:** the Drive service is resolved via `self.ensure_services().get("drive_service")` — the real daemon keeps services in the `self._services` dict (daemon.py:592-620, `_filter_services` keys `gmail_service`/`calendar_service`/`drive_service`); there is **no** `self._drive` attribute. The `FleetStorage` implementation is subsystem A's `mcpbrain/fleet_storage.py` (`fleet_folder_storage`); the guarded `try/except ImportError` means that before A merges, the import fails, `fs` is `None`, and every body cleanly no-ops with `{"skipped": "no_fleet_storage"}`. Once A lands, storage flows through with no further B edit.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_daemon_bodies.py tests/test_org_cadence_stubs.py -v`
Expected: PASS (bodies gate correctly; the Phase 0 cadence-registration tests still green).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/daemon.py tests/test_org_daemon_bodies.py
git commit -m "feat(daemon): fill org contrib_upload/import/curate cadence bodies (bodies only)"
```

---

## Task 11: B4a cross-layer merge guards (resolve.py + graph_write.py)

**Files:**
- Modify: `mcpbrain/resolve.py` (`_deterministic_merges` resolve.py:85, `_email_equality_merges` resolve.py:116, `resolve_entities` resolve.py:308)
- Modify: `mcpbrain/graph_write.py` (`upsert_entity` existing-row branch, graph_write.py:1920)
- Test: `tests/test_org_b4a_guards.py`

**Interfaces:**
- Consumes: `entities.origin`.
- Produces the four B4a invariants:
  1. **Local machinery never merges org↔org** — the local resolve tiers skip pairs/groups where both rows are `origin='org'` (that is the curator's job; a local merge would be resurrected next import). A `curator=True` bypass lets `org_curate.run` (Task 7) reuse the same code to dedup the org layer.
  2. **Any local↔org merge leaves the org node surviving** — the survivor is forced to the org-origin row regardless of mentions.
  3. **Local writes never overwrite org skeleton fields** — `upsert_entity` landing on an `origin='org'` row may add aliases/notes but never rewrites `name`/`type`/`org`/`email_addr`.
  4. **Re-points are logged** — merges already log to `entity_merge_log`; the import path additionally logs to `org_repoint_log` (Task 9).

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_b4a_guards.py`:

```python
from mcpbrain import graph_write, resolve
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_local_never_merges_org_org(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.write_time_dedup_enabled", lambda h: True)
    s = _store(tmp_path)
    with s._connect() as db:                         # two org rows, same canonical name
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme-inc','Acme','org','org',2)")
    resolve.resolve_entities(s, home=str(tmp_path))   # local (curator=False)
    assert s.get_entity("acme") is not None and s.get_entity("acme-inc") is not None


def test_curator_bypass_merges_org_org(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) VALUES('acme-inc','Acme','org','org',2)")
    resolve.resolve_entities(s, home=str(tmp_path), curator=True)
    survivors = [e for e in (s.get_entity("acme"), s.get_entity("acme-inc")) if e]
    assert len(survivors) == 1                        # curator dedups org layer


def test_local_org_merge_org_survives(tmp_path, monkeypatch):
    monkeypatch.setattr("mcpbrain.config.write_time_dedup_enabled", lambda h: True)
    s = _store(tmp_path)
    with s._connect() as db:                          # local has MORE mentions, but org must win
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-local','Joel','person','joel@x.org','local',9)")
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin,mentions) "
                   "VALUES('joel-org','Joel','person','joel@x.org','org',1)")
    resolve.resolve_entities(s, home=str(tmp_path))
    assert s.get_entity("joel-org") is not None        # org survivor
    assert s.get_entity("joel-local") is None


def test_upsert_never_overwrites_org_skeleton(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,origin) "
                   "VALUES('joel','Joel Chelliah','person','Acme','joel@acme.org','org')")
    graph_write.upsert_entity(s, name="Joel Chelliah", entity_type="person",
                              org="Beta", email_addr="joel@beta.org", notes="local note")
    e = s.get_entity("joel")
    assert e["org"] == "Acme" and e["email_addr"] == "joel@acme.org"   # skeleton unchanged
    assert "local note" in (e["notes"] or "")                          # flesh added
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_org_b4a_guards.py -v`
Expected: FAIL — no origin awareness (`curator` kwarg unknown; org↔org merges happen; skeleton overwritten).

- [ ] **Step 3: Write minimal implementation**

**(3a)** In `mcpbrain/resolve.py`, add an origin helper and thread `curator` through the two merge tiers + `resolve_entities`.

Add after `canonical_key` (resolve.py:73):

```python
def _origin_map(store) -> dict:
    """id -> origin ('local'|'org'). Queried here (not via entities_for_resolution)
    to keep the frozen store surface untouched, per the B4a guard scope."""
    with store._connect() as db:
        return {r["id"]: (r["origin"] or "local")
                for r in db.execute("SELECT id, origin FROM entities").fetchall()}


def _org_survivor(members, origins):
    """B4a rule 2: when a group mixes local + org rows, the org row must survive
    (the import would otherwise resurrect the org node with local flesh stranded).
    Returns the forced survivor, or None to fall back to the mentions rule."""
    org_members = [m for m in members if origins.get(m["id"]) == "org"]
    if len(org_members) == 1:
        return org_members[0]
    return None
```

In `_deterministic_merges` (resolve.py:85), change the signature and guard each group:

```python
def _deterministic_merges(store, *, curator: bool = False) -> int:
    ents = store.entities_for_resolution()
    origins = _origin_map(store)
    groups = {}
    ...
    for (_type, _key), members in groups.items():
        if len(members) < 2:
            continue
        org_count = sum(1 for m in members if origins.get(m["id"]) == "org")
        if not curator and org_count >= 2:
            _emit_merge_candidates(store, members, origins)   # B4a rule 1
            continue
        forced = None if curator else _org_survivor(members, origins)
        survivor = forced or max(members, key=lambda m: (m.get("mentions", 0), len(m["name"]), m["id"]))
        for m in members:
            if m["id"] != survivor["id"]:
                store.merge_entities(m["id"], survivor["id"], method="deterministic")
                merged += 1
    return merged
```

In `_email_equality_merges` (resolve.py:116), add `curator` and the same guard (after the existing role-address `continue` at resolve.py:151):

```python
def _email_equality_merges(store, home=None, *, curator: bool = False) -> int:
    ...
    origins = _origin_map(store)
    ...
    for _email, members in groups.items():
        if len(members) < 2:
            continue
        if is_role_address(_email):
            continue
        org_count = sum(1 for m in members if origins.get(m["id"]) == "org")
        if not curator and org_count >= 2:
            _emit_merge_candidates(store, members, origins)
            continue
        forced = None if curator else _org_survivor(members, origins)
        survivor = forced or max(members, key=lambda m: (m.get("mentions", 0), len(m["name"]), m["id"]))
        for m in members:
            if m["id"] != survivor["id"]:
                store.merge_entities(m["id"], survivor["id"], method="email")
                merged += 1
    return merged
```

Add the merge-candidate emitter (B4a rule 1 — contribute upstream instead of merging locally; lazy import avoids an org_contrib↔resolve cycle):

```python
def _emit_merge_candidates(store, members, origins) -> None:
    """A local pass found two ORG rows it thinks are duplicates. Local must not
    merge them (the next import would resurrect them); instead contribute the
    pair upstream as a merge-candidate signal for the curator (spec B4a rule 1)."""
    try:
        import json
        from mcpbrain import config as _config
        from mcpbrain.org_contracts import ContributionRecord, source_ref
        home = str(_config.app_dir())
        pin = _config.fleet_pin(home)
        if not pin.is_pinned:
            return
        org_ids = sorted(m["id"] for m in members if origins.get(m["id"]) == "org")
        email = _config.owner_email(home)
        for i in range(len(org_ids)):
            for j in range(i + 1, len(org_ids)):
                claim = {"kind": "merge_candidate", "a": org_ids[i], "b": org_ids[j]}
                rec = ContributionRecord(
                    claim=claim, confidence=0.5, valid_from="",
                    contributor_email=email, source_kind="local",
                    source_ref=source_ref(pin.fleet_secret, f"{org_ids[i]}|{org_ids[j]}"))
                with store._connect() as db:
                    db.execute("INSERT INTO org_contrib_outbox(record) VALUES(?)",
                               (json.dumps(rec.to_dict(), sort_keys=True),))
    except Exception:  # noqa: BLE001 — a best-effort signal must never break resolution
        log.debug("resolve: merge-candidate emit failed", exc_info=True)
```

In `resolve_entities` (resolve.py:308) thread the flag:

```python
def resolve_entities(store, client=None, *, max_adjudications: int = 200, home=None,
                     curator: bool = False) -> dict:
    auto = _deterministic_merges(store, curator=curator)
    auto += _email_equality_merges(store, home=home, curator=curator)
    return {"mode": "deterministic", "auto_merges": auto, "llm_merges": 0,
            "llm_calls": 0, "kept_distinct": 0}
```

**(3b)** In `mcpbrain/graph_write.py`, guard the existing-row update branch of `upsert_entity` (graph_write.py:1920, `if existing:`). Add at the top of that branch:

```python
        if existing:
            _is_org = ("origin" in existing.keys() and existing["origin"] == "org")
            updates: dict = {"last_seen": today}
            if org and not _is_org:                 # B4a rule 3: never overwrite org skeleton
                ...                                  # (existing org-recency logic, unchanged)
            if email_addr and not existing["email_addr"] and not _is_org:
                updates["email_addr"] = email_addr
            if notes:                                # flesh (notes/aliases) is always allowed
                ...
```

Concretely, wrap the two skeleton-field mutations (`org` and `email_addr`) in `and not _is_org`; leave `notes`, `title_alias` appends, and `last_seen` unconditional. (The email/alias dedup branches at graph_write.py:1854 and :1909 already keep the winner's id, so they don't rewrite skeleton fields.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_org_b4a_guards.py tests/test_resolve*.py tests/test_graph_write*.py -v`
Expected: PASS (B4a guards green; existing resolve/graph_write suites still green — the `curator` kwarg defaults False, so local behaviour is unchanged except the new org-aware guards).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/resolve.py mcpbrain/graph_write.py tests/test_org_b4a_guards.py
git commit -m "feat(org): B4a cross-layer merge guards (no org<->org locally, org survives, skeleton frozen)"
```

---

## Task 12: Phase B exit gate — member→curator→consumer round trip

**Files:**
- Test: `tests/test_org_phase_b_gate.py`

**Interfaces:**
- Consumes: everything above + `tests/helpers/org_fleet.py` (`make_fleet`).
- Produces: one end-to-end test proving the three subsystems compose on the shared harness (a member contributes → curator publishes → a second consumer imports the same claims as `origin='org'` rows), plus the full-suite green check.

- [ ] **Step 1: Write the failing test**

Create `tests/test_org_phase_b_gate.py`:

```python
import json

from mcpbrain import org_contrib, org_curate, org_import
from mcpbrain.org_contracts import FleetPin


def _pin():
    return FleetPin(fleet_secret="s3cret",
                    relation_allowlist=("works_at", "member_of", "mentioned_with"))


def test_member_curator_consumer_round_trip(tmp_path, monkeypatch):
    from tests.helpers.org_fleet import make_fleet
    members, curator, fs = make_fleet(tmp_path, n_members=2)
    alice, bob = members
    # pin every install's config so contribution is enabled
    from mcpbrain import config
    for inst in (alice, bob, curator):
        config.write_config(str(inst.home), {"org_config": {"org_pin": {
            "fleet_secret": "s3cret",
            "relation_allowlist": ["works_at", "member_of", "mentioned_with"]}}})

    # (1) alice's local graph learns joel works_at acme; contribute + upload
    a = alice.store
    with a._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('joel','Joel Chelliah','person','joel@acme.org','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('acme','Acme','org','local')")
    from mcpbrain import graph_write
    graph_write.upsert_relation(a, "joel", "works_at", "acme", valid_from="2026-01-01",
                                source_doc_id="msg-1")
    with a._connect() as db:                          # give the provenance chunk a source_type
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,enrich_state) "
                   "VALUES('msg-1','t','h','{\"source_type\":\"gmail\"}','')")
    delta, wm = org_contrib._delta_since_watermark(a)
    assert org_contrib.collect_from_drain(a, delta, _pin(), "alice@x.org") == 3
    org_contrib.upload_pending(a, fs, "alice@x.org")

    # (2) curator ingests + publishes
    summary = org_curate.run(curator.store, fs, str(curator.home))
    assert summary["published"] is True and summary["version"] == 1

    # (3) bob imports the snapshot as origin='org'
    res = org_import.import_snapshot(bob.store, fs)
    assert res["status"] == "imported"
    joel = bob.store.get_entity("joel")
    assert joel is not None and joel["origin"] == "org"
    with bob.store._connect() as db:
        rel = db.execute("SELECT origin FROM entity_relations WHERE entity_a='joel' "
                         "AND relation='works_at'").fetchone()
    assert rel is not None and rel["origin"] == "org"
```

- [ ] **Step 2: Run test to verify it fails, then passes**

Run: `python -m pytest tests/test_org_phase_b_gate.py -v`
Expected: PASS once Tasks 1–11 are complete (if red, the failing subsystem — not this gate — is the fix site).

- [ ] **Step 3: Run the full suite as the exit gate**

Run: `python -m pytest tests/ -q`
Expected: whole suite green (no regressions from the resolve/graph_write guards or the daemon bodies). If red, fix the offending task before proceeding.

- [ ] **Step 4: Commit**

```bash
git add tests/test_org_phase_b_gate.py
git commit -m "test(org): Phase B exit gate — member->curator->consumer round trip"
```

- [ ] **Step 5: Phase B complete**

Subsystem B is green in isolation against the shared harness. It merges into the Phase-D convergence work (A↔B echo-dedup integration, egress gate, rollout ordering) once A and C land. No push/release — that remains a separate explicit instruction.

---

## Self-Review

**Spec coverage** (spec §§ B1–B5 + interaction-matrix notes):

| Spec item | Task(s) |
|---|---|
| **B1** layer-1 contents (person/org/project + allowlisted relations; never profile/mentions) | Enforced by `_LAYER1_ENTITY_TYPES` + the redacted claim shape in `collect_from_drain` (Task 1); snapshot serialises only these fields (Task 7); org taxonomy carried in the snapshot (Task 7). |
| **B2** edge: allowlist + redaction + HMAC source_ref + fail-closed + role/cold guards | Task 1 (`collect_from_drain`), Task 3 (watermark scan), Task 10 (cadence body). |
| **B2** bitemporal supersessions contributed | Task 1 (`valid_to` on the relation claim), Task 3 (scan picks up `invalidated_at`/`last_seen` changes). |
| **B2** echo-dedup (identical `source_ref` across users) | Task 1 (source_ref = HMAC(doc_id), shared by entity+relation claims of one doc); curator counts distinct source_ref (Task 5). |
| **B2** outbox → daily JSONL batch to `contrib/<email>/<utc>.jsonl` | Task 2 (`upload_pending`), Task 10 (body). |
| **B3** curator: ingest → staging (idempotent) | Task 4 (`_ingest`, UNIQUE-dedup). |
| **B3** deterministic merge via resolve.py, role-guarded | Task 5 (`_materialise` role guard) + Task 7 (`resolve_entities(curator=True)`). |
| **B3** corroboration counting (distinct source_ref/contributor; `mentioned_with` ≥2 sources) | Task 5 (`_CORROBORATION_GUARDED`, distinct-source counting). |
| **B3** AI adjudication reusing review pattern, structural-only, pending verdicts, capped, 0.7.84 ref/type hardening | Task 6 (`_build_adjudication_units` structural-only, `adjudicate` default-pending, `_apply_merge_verdicts` capped + guard + get_entity re-fetch). |
| **B3** publish versioned snapshot (manifest LAST, snapshot_sha256, tombstones) | Task 7 (`_publish`). |
| **B4** consumer: fetch manifest, import-if-newer, sha256 verify, transactional | Task 8 (`import_snapshot`). |
| **B4** wholesale-replace per origin; local rows never touched | Task 8 (steps 1–3). |
| **B4** demote-not-delete when local data attached; tombstone re-point | Task 8 (`_has_local_attachments`, `_repoint_local_refs`). |
| **B4** slug-drift reconciliation via resolve.py (email + alias/token), ambiguous → fuzzy queue | Task 9 (`_reconcile_slug_drift`). |
| **B4** restore-from-repoint-log on curator split | Task 9 (`_restore_from_repoint_log`). |
| **B4a** rules 1–4 (no org↔org locally / local↔org survives as org / skeleton frozen / repoints logged) | Task 11 (resolve + graph_write guards) + Tasks 8–9 (`org_repoint_log`). |
| **B5** source-of-truth rules | Curator-only writes (Task 7); every fact traces to staged contributions (Task 4); capped/reversible/logged/versioned (Tasks 6–7); tombstones (Tasks 7–8); local freshness wins + disagreement flows back (Task 11 skeleton guard + merge-candidate emit). |

**Cross-subsystem interface — exact signatures exposed (verbatim):**
- `mcpbrain/org_contrib.py`: `def collect_from_drain(store, drain_delta, pin, contributor_email) -> int` (Task 1) ✓; `def upload_pending(store, fleet_storage, contributor_email) -> dict` (Task 2) ✓.
- `mcpbrain/org_curate.py`: `def run(store, fleet_storage, home) -> dict` (Task 7) ✓.
- `mcpbrain/org_import.py`: `def import_snapshot(store, fleet_storage) -> dict` (Task 8) ✓ — returns a `status` string (`"imported"|"unchanged"|"no_snapshot"|"error"`) that C branches on; consumed by C onboarding.
- FleetStorage consumed only via the `org_contracts.FleetStorage` Protocol (never the concrete class); the concrete instance comes from subsystem A's `mcpbrain/fleet_storage.fleet_folder_storage`, acquired in the daemon bodies via a guarded import (Task 10). B defines no factory of its own. ✓

**Type/contract consistency:**
- `FleetPin` used read-only: `.is_pinned`, `.fleet_secret`, `.relation_allowlist` (Tasks 1, 3, 10, 11) — matches org_contracts.py:133.
- `ContributionRecord` constructed with the frozen field set (Tasks 1, 4, 11) and round-tripped via `.to_dict()`/`.from_dict()` (Tasks 1, 4, 12) — matches org_contracts.py:70.
- `SnapshotManifest`/`Tombstone` produced by the curator (Task 7) and consumed by the importer (Tasks 8–9) with identical fields — matches org_contracts.py:96/108.
- `source_ref(secret, doc_id)` (org_contracts.py:158) used identically edge-side (Task 1) and in the merge-candidate emit (Task 11).
- Fleet paths agree across producer/consumer: `org-graph/manifest.json`, `org-graph/snapshot.jsonl.gz`, `org-graph/tombstones.jsonl`, `contrib/<email>/<utc>.jsonl` (Tasks 2, 4, 7, 8).
- `resolve_entities(..., curator: bool=False)` default preserves all existing local callers (`daemon._run_resolve_entities` daemon.py:1607 calls it without `curator`); only `org_curate.run` passes `curator=True` (Tasks 7, 11).
- Daemon edits are body-only: the `_is_due` gate + `_last_*` advance pattern of `_run_review` (daemon.py:1617) is preserved; no cadence registration/defaults/keys touched (Task 10 vs the Phase 0 frozen blocks).

**Placeholder scan:** No TBD/TODO; every code step shows complete code. The one deliberate seam — the guarded `from mcpbrain import fleet_storage` in the daemon bodies (subsystem A's `fleet_folder_storage`) — makes the cadences no-op gracefully (`{"skipped": "no_fleet_storage"}`) before A lands, rather than being an unfinished stub; B ships no factory of its own. The `adjudicate` seam returning `[]` is the spec-mandated pending-safe default, not a placeholder.

**Reused (not reinvented):** `is_role_address`, `canonical_key`, `_tokens`, `_token_set_ratio`, `_candidate_pairs`, `_pick_winner`, `_NAME_MERGEABLE_TYPES`, `resolve_entities` (resolve.py); the `store.get_finding`/capped/`pending`-safe applier shape (review_apply.py); `graph_write.upsert_entity`/`upsert_relation` for materialise + import; `store.merge_entities` for every cross-layer merge.
