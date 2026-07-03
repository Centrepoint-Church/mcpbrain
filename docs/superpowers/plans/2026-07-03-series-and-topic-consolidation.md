# Series & Topic Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop meeting and topic entities from fragmenting into many near-duplicate nodes by consolidating them at write-time via deterministic keying, and migrate the existing 294 meeting / 412 topic fragments once, under supervision.

**Architecture:** Consolidation moves *upstream* to entity creation — meetings become one `meeting-<org>-<series>` "series" entity with each mention recorded as an append-only `entity_observations` occurrence; topics converge via deterministic normalization (`inflect` singularization + a curated synonym map). No fuzzy post-hoc name-merge (the banned corruption path). Existing fragments are folded by a scoped re-extraction (meetings) and a deterministic id-remap (topics), both attended, backup-gated, and gold-gated.

**Tech Stack:** Python 3, SQLite (`mcpbrain.store.Store`), the enrich block-unit pipeline, `inflect` (new dependency), pytest.

## Global Constraints

- **Never corrupt the graph.** Consolidation is reversible + logged, conservative-default (skip on uncertainty). Going-forward keying does **no** merges; the only destructive ops are the one-shot migrations, each protected by a full DB backup taken first.
- **Gold gate:** recall@10 ≥ 0.55 and MRR ≥ 0.35 must be unaffected after each migration. Harness: `mcpbrain enrich-eval` (→ `mcpbrain/enrich_eval.py:main`, which calls `tests/eval/run_eval.gold_eval` at k=10).
- **Ships ON behind a kill-switch:** new config flags default `True`, disabled only by an explicit `false` in `config.json`. Flag pattern: `bool(read_config(home).get("<key>", True))` (see `mcpbrain/config.py:146-182`).
- **`store.merge_entities` is destructive** — it `DELETE`s the loser row (`store.py:1378`) and logs only the loser name. Never rely on it for going-forward consolidation.
- **`graph_write.write_observation` supersedes** same-`(entity, attribute, source)` rows. Occurrences must use the new append-only path, never `write_observation`.
- **Migrations are attended curator commands** (Josh's machine), never an unattended cadence (C1 role-inbox lesson).
- If the extraction prompt changes, `plugin/agents/enrich-batch.md` must stay byte-identical to `mcpbrain/enrich_prompt.md` via `python bin/sync_agents.py` (see repo CLAUDE.md).
- **Do not push or release.** This plan ends at merged local work + a green gold eval; shipping is a separate explicit step.

## Decisions locked in during brainstorming

- `event` is **folded into** the meeting series scheme (same contract, same keying, stored as `type="meeting"`).
- Each occurrence row carries **date + a short occasion snippet** (`source_span`/summary, capped) as its `value`.
- Singularization uses the **`inflect`** library via a shared `mcpbrain/text_norm.py` helper; applied to **topics only** (person/org names are NOT singularized — see Task 2's assessment).
- Text meetings that don't re-form as a series after migration are **left as single-occurrence series** (non-destructive).

## File Structure

- `mcpbrain/config.py` — MODIFY: add `meeting_series_enabled`, `topic_consolidation_enabled` flags.
- `mcpbrain/text_norm.py` — CREATE: shared `singularize()` helper (wraps `inflect`).
- `mcpbrain/topics.py` — CREATE: `normalize_topic()` + curated synonym map (mirrors `mcpbrain/orgs.py`).
- `mcpbrain/graph_write.py` — MODIFY: `_meeting_series_id()` helper; meeting/event branch + topic normalization in `apply()`.
- `mcpbrain/store.py` — MODIFY: `append_occurrence()`, `meeting_source_doc_ids()`, `reset_enriched()`, `meeting_series_for_old()`.
- `mcpbrain/enrich_prompt.md` — MODIFY: add `series_name`/`occurrence_date` to the meeting entity schema + guidance. Then `bin/sync_agents.py`.
- `mcpbrain/sync/calendar.py` — MODIFY: capture `recurringEventId`; opportunistic `calendar_series` annotation.
- `mcpbrain/consolidate.py` — CREATE: migration logic (`remap_topics`, `reset_meeting_sources`, `retire_meeting_duplicates`).
- `bin/consolidate.py` — CREATE: thin attended CLI (backup + calls into `consolidate.py`).
- Tests: `tests/test_config.py`, `tests/test_text_norm.py`, `tests/test_topics.py`, `tests/test_graph_write.py`, `tests/test_store.py`, `tests/test_calendar.py`, `tests/test_consolidate.py`, `tests/test_contract.py`.

---

### Task 1: Config kill-switches

**Files:**
- Modify: `mcpbrain/config.py` (after `enrich_rich_observations_enabled`, ~line 182)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.meeting_series_enabled(home) -> bool`, `config.topic_consolidation_enabled(home) -> bool` (both default `True`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py — append
import json
from mcpbrain import config

def test_meeting_series_enabled_default_true(tmp_path):
    assert config.meeting_series_enabled(str(tmp_path)) is True

def test_topic_consolidation_enabled_default_true(tmp_path):
    assert config.topic_consolidation_enabled(str(tmp_path)) is True

def test_meeting_series_can_be_disabled(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"meeting_series_enabled": False}))
    assert config.meeting_series_enabled(str(tmp_path)) is False

def test_topic_consolidation_can_be_disabled(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"topic_consolidation_enabled": False}))
    assert config.topic_consolidation_enabled(str(tmp_path)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -k "meeting_series or topic_consolidation" -q`
Expected: FAIL with `AttributeError: module 'mcpbrain.config' has no attribute 'meeting_series_enabled'`

- [ ] **Step 3: Write minimal implementation**

```python
# mcpbrain/config.py — add after enrich_rich_observations_enabled()
def meeting_series_enabled(home) -> bool:
    """Whether graph_write.apply() keys meeting/event entities as one
    'meeting-<org>-<series>' series entity (with per-occurrence
    entity_observations rows) instead of minting a new node per name variant.
    Default TRUE; kill-switch only. Set 'meeting_series_enabled': false in
    config.json to revert to bare slugify(name) meeting entities."""
    return bool(read_config(home).get("meeting_series_enabled", True))


def topic_consolidation_enabled(home) -> bool:
    """Whether topic tags are normalized (singularize + curated synonym map)
    before the topic entity id is derived, so variants converge on one node.
    Default TRUE; kill-switch only. Set 'topic_consolidation_enabled': false in
    config.json to revert to raw lowercased tags."""
    return bool(read_config(home).get("topic_consolidation_enabled", True))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -k "meeting_series or topic_consolidation" -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py tests/test_config.py
git commit -m "feat(config): meeting_series_enabled + topic_consolidation_enabled kill-switches"
```

---

### Task 2: `inflect` dependency + shared singularization helper

`inflect` ships to all users, so it lives in a shared module. **System-wide assessment (record this rationale in the commit body):** the only place plural/singular fragmentation is both common *and* safe to collapse is **topics**. Person surnames ("Jones"→"Jone") and many org names ("Systems", "Ministries") pluralize legitimately, so singularizing `resolve.canonical_key` would over-merge distinct people/orgs — do **not** apply it there. Keep the helper reusable but call it from topic normalization only.

**Files:**
- Modify: `pyproject.toml` (dependencies list, ~line 5-30)
- Create: `mcpbrain/text_norm.py`
- Test: `tests/test_text_norm.py`

**Interfaces:**
- Produces: `text_norm.singularize(word: str) -> str` — lowercases, returns the singular of a simple plural; returns input unchanged when already singular or when `inflect` returns falsy.

- [ ] **Step 1: Add the dependency**

```toml
# pyproject.toml — add to the `dependencies = [` list, keeping alignment
  "inflect>=7",            # singular/plural normalization in text_norm.singularize
```

- [ ] **Step 2: Install it into the working env**

Run: `uv pip install 'inflect>=7'` (or `pip install 'inflect>=7'`)
Expected: installs `inflect` and its dependency `pydantic`/`typeguard` without error.

- [ ] **Step 3: Write the failing test**

```python
# tests/test_text_norm.py
from mcpbrain.text_norm import singularize

def test_simple_plural():
    assert singularize("budgets") == "budget"

def test_es_plural():
    assert singularize("churches") == "church"

def test_already_singular_unchanged():
    assert singularize("budget") == "budget"

def test_lowercases():
    assert singularize("Budgets") == "budget"

def test_empty_returns_empty():
    assert singularize("") == ""

def test_non_plural_word_unchanged():
    # inflect returns False for non-plurals; helper must fall back to input.
    assert singularize("worship") == "worship"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `python -m pytest tests/test_text_norm.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.text_norm'`

- [ ] **Step 5: Write minimal implementation**

```python
# mcpbrain/text_norm.py
"""Shared lexical normalization helpers.

singularize() wraps `inflect` so callers get a safe, lowercased singular form
with a fall-back to the input. Deliberately NOT wired into person/org name
resolution (resolve.canonical_key): surnames and many org names pluralize
legitimately, so singularizing them would over-merge distinct entities. It is
used only for topic-tag normalization (mcpbrain.topics.normalize_topic), where
plural/singular variants of the same concept ('budget'/'budgets') are genuinely
one thing.
"""

import inflect

_ENGINE = inflect.engine()


def singularize(word: str) -> str:
    """Lowercased singular of a simple plural; unchanged if already singular.

    inflect.singular_noun returns False for words it considers already singular
    (or can't analyse); fall back to the lowercased input in that case.
    """
    w = (word or "").strip().lower()
    if not w:
        return ""
    result = _ENGINE.singular_noun(w)
    return result if result else w
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest tests/test_text_norm.py -q`
Expected: PASS (6 passed)

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml mcpbrain/text_norm.py tests/test_text_norm.py
git commit -m "feat(text_norm): inflect-backed singularize() helper

System-wide assessment: singularization is safe/valuable only for topic tags.
NOT applied to resolve.canonical_key — surnames ('Jones') and org names
('Systems','Ministries') pluralize legitimately and would over-merge. Helper is
reusable but topic-scoped by intent."
```

---

### Task 3: `topics.py` — normalize_topic + curated synonym map

**Files:**
- Create: `mcpbrain/topics.py`
- Test: `tests/test_topics.py`

**Interfaces:**
- Consumes: `text_norm.singularize`, `config.read_config`.
- Produces: `topics.normalize_topic(tag: str, home=None) -> str` — returns the canonical lowercased topic tag (whitespace-collapsed, leading-qualifier-stripped, singularized, then synonym-mapped). Returns `""` for tags that collapse to empty.
- Config shape consumed: `read_config(home).get("topic_synonyms")` is an optional `{variant: canonical}` dict (both lowercased at read).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_topics.py
import json
from mcpbrain import topics

def test_lowercase_and_whitespace(tmp_path):
    assert topics.normalize_topic("  Worship  Team ", str(tmp_path)) == "worship team"

def test_strips_leading_qualifier(tmp_path):
    assert topics.normalize_topic("annual budget", str(tmp_path)) == "budget"
    assert topics.normalize_topic("the budget", str(tmp_path)) == "budget"

def test_singularizes(tmp_path):
    assert topics.normalize_topic("budgets", str(tmp_path)) == "budget"

def test_synonym_map(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"topic_synonyms": {"finances": "budget"}}))
    assert topics.normalize_topic("finances", str(tmp_path)) == "budget"

def test_synonym_applied_after_singularize(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"topic_synonyms": {"finance": "budget"}}))
    # 'finances' -> singular 'finance' -> synonym 'budget'
    assert topics.normalize_topic("finances", str(tmp_path)) == "budget"

def test_distinct_concepts_not_merged(tmp_path):
    # No synonym entry: 'prayer' and 'prayer meeting' stay distinct.
    assert topics.normalize_topic("prayer", str(tmp_path)) == "prayer"
    assert topics.normalize_topic("prayer meeting", str(tmp_path)) == "prayer meeting"

def test_empty(tmp_path):
    assert topics.normalize_topic("   ", str(tmp_path)) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_topics.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcpbrain.topics'`

- [ ] **Step 3: Write minimal implementation**

```python
# mcpbrain/topics.py
"""Deterministic topic-tag normalization.

Converges morphological/synonym variants of a topic onto one canonical tag so
the topic entity (id 'topic-<canonical>') doesn't fragment. Deterministic and
reversible — NO LLM merge — so 'prayer' can never silently absorb 'prayer
meeting'; only an explicit curated synonym entry joins two topics.

Mirrors mcpbrain.orgs: the store stays decoupled from this logic; callers
(graph_write.apply) normalize before deriving the entity id.
"""

import re

from mcpbrain import config
from mcpbrain.text_norm import singularize

# Leading throat-clearing words that don't change a topic's identity. Stripped
# only from the FRONT and only when at least one real token remains. Kept small
# and conservative — this is not a general stopword list.
_LEADING_QUALIFIERS = {"the", "a", "an", "our", "annual", "monthly", "weekly"}


def _synonyms(home) -> dict:
    raw = config.read_config(home).get("topic_synonyms") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower()
            for k, v in raw.items() if str(k).strip() and str(v).strip()}


def normalize_topic(tag: str, home=None) -> str:
    """Canonical lowercased topic tag, or '' when it collapses to empty."""
    if home is None:
        home = str(config.app_dir())
    t = re.sub(r"\s+", " ", (tag or "").strip().lower())
    if not t:
        return ""

    # Strip leading qualifiers while a real token remains.
    words = t.split(" ")
    while len(words) > 1 and words[0] in _LEADING_QUALIFIERS:
        words = words[1:]
    t = " ".join(words)

    # Singularize the LAST token only (the head noun); leaves 'youth services'
    # -> 'youth service' but never mangles a leading modifier.
    if words:
        words[-1] = singularize(words[-1])
        t = " ".join(w for w in words if w)

    # Curated synonym map has the final say.
    syn = _synonyms(home)
    return syn.get(t, t)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_topics.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/topics.py tests/test_topics.py
git commit -m "feat(topics): deterministic normalize_topic (singularize + curated synonym map)"
```

---

### Task 4: `store.append_occurrence` — append-only occurrence rows

**Files:**
- Modify: `mcpbrain/store.py` (near `upsert_topic_entity`, ~line 1264)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `store.append_occurrence(entity_id: str, valid_from: str, value: str, source: str) -> bool` — inserts one `entity_observations` row with `attribute='occurrence'`; returns `True` if inserted, `False` if an identical `(entity_id, 'occurrence', valid_from, source)` row already exists (idempotent on thread re-apply). Does NOT supersede prior occurrences.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py — append (assumes a `store` fixture returning an inited Store)
def test_append_occurrence_inserts(store):
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-01-01")
    assert store.append_occurrence("meeting-acme-board", "2026-01-05", "budget review", "m1") is True
    with store._connect() as db:
        rows = db.execute(
            "SELECT valid_from, value, source, attribute FROM entity_observations "
            "WHERE entity_id='meeting-acme-board'").fetchall()
    assert len(rows) == 1
    assert rows[0]["attribute"] == "occurrence"
    assert rows[0]["valid_from"] == "2026-01-05"

def test_append_occurrence_idempotent(store):
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-01-01")
    store.append_occurrence("meeting-acme-board", "2026-01-05", "v", "m1")
    assert store.append_occurrence("meeting-acme-board", "2026-01-05", "v", "m1") is False
    with store._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM entity_observations "
                       "WHERE entity_id='meeting-acme-board'").fetchone()[0]
    assert n == 1

def test_append_occurrence_distinct_dates_coexist(store):
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-01-01")
    store.append_occurrence("meeting-acme-board", "2026-01-05", "a", "m1")
    store.append_occurrence("meeting-acme-board", "2026-01-12", "b", "m2")
    with store._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM entity_observations "
                       "WHERE entity_id='meeting-acme-board'").fetchone()[0]
    assert n == 2  # occurrences accumulate; no supersession
```

If `tests/test_store.py` has no `store` fixture, add one:

```python
import pytest
from mcpbrain.store import Store

@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "brain.db")
    s.init()
    return s
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py -k append_occurrence -q`
Expected: FAIL with `AttributeError: 'Store' object has no attribute 'append_occurrence'`

- [ ] **Step 3: Write minimal implementation**

```python
# mcpbrain/store.py — add after upsert_topic_entity()
def append_occurrence(self, entity_id, valid_from, value, source) -> bool:
    """Append one occurrence row to entity_observations, idempotently.

    Occurrences are ACCUMULATING facts (a recurring meeting's instances), not a
    superseding attribute, so this deliberately does NOT go through
    graph_write.write_observation (which retires prior same-source rows). Keyed
    for idempotency on (entity_id, 'occurrence', valid_from, source): re-applying
    a thread must not duplicate the row. Single-writer daemon, so SELECT-then-
    INSERT is race-free. Returns True on insert, False when the row already
    exists.
    """
    with self._connect() as db:
        exists = db.execute(
            "SELECT 1 FROM entity_observations "
            "WHERE entity_id=? AND attribute='occurrence' AND valid_from=? AND source=?",
            (entity_id, valid_from, source)).fetchone()
        if exists:
            return False
        db.execute(
            "INSERT INTO entity_observations "
            "(entity_id, attribute, value, source, valid_from, confidence_source) "
            "VALUES (?, 'occurrence', ?, ?, ?, 'llm_extraction')",
            (entity_id, value, source, valid_from))
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -k append_occurrence -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): append_occurrence — idempotent, non-superseding occurrence rows"
```

---

### Task 5: `_meeting_series_id` + meeting/event branch in `apply()`

**Files:**
- Modify: `mcpbrain/graph_write.py` — add `_meeting_series_id()` (near other helpers, ~line 1729); add the meeting branch inside `apply()`'s entities loop (`graph_write.py:1076-1146`); compute `_meeting_home` alongside `_dedup_home` (~line 1065).
- Test: `tests/test_graph_write.py`

**Interfaces:**
- Consumes: `config.meeting_series_enabled`, `store.upsert_entity` (low-level, explicit id — `store.py:1217`), `store.append_occurrence` (Task 4), `canonical_org`, `slugify`.
- Produces: `graph_write._meeting_series_id(series_name: str, org: str) -> str` returning `slugify(f"meeting-{org}-{series_name}")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_write.py — append. Uses the module's existing store fixture
# (mirror whatever fixture the file already uses; if none, use the Store fixture
# from Task 4).
from mcpbrain import graph_write

def _extraction(entities, thread_id="t1", org="Acme"):
    return {
        "thread_id": thread_id, "org": org, "content_type": "update",
        "summary": "s", "topics": [], "actions": [], "relations": [],
        "entities": entities,
        "messages": [{"message_id": "m1", "sender": "A <a@acme.org>",
                      "date": "2026-01-05", "subject": "sub"}],
    }

def test_meeting_series_id_is_org_scoped():
    a = graph_write._meeting_series_id("Board Meeting", "Acme")
    b = graph_write._meeting_series_id("Board Meeting", "Beta")
    assert a == "meeting-acme-board-meeting"
    assert a != b  # same name, different org -> different series

def test_meeting_mentions_converge_on_one_series(store):
    ext = _extraction([{"name": "College Meeting 12 May", "type": "meeting",
                        "series_name": "College Meeting", "occurrence_date": "2026-05-12"}])
    graph_write.apply(store, ext, doc_ids=["gmail-m1-body-0"], home=str(store.db_path.parent))
    ext2 = _extraction([{"name": "College Meeting 19 May", "type": "meeting",
                         "series_name": "College Meeting", "occurrence_date": "2026-05-19"}],
                       thread_id="t2")
    ext2["messages"][0]["message_id"] = "m2"
    graph_write.apply(store, ext2, doc_ids=["gmail-m2-body-0"], home=str(store.db_path.parent))
    with store._connect() as db:
        meetings = db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()
        occ = db.execute("SELECT COUNT(*) FROM entity_observations "
                         "WHERE entity_id='meeting-acme-college-meeting' "
                         "AND attribute='occurrence'").fetchone()[0]
    assert [m["id"] for m in meetings] == ["meeting-acme-college-meeting"]
    assert occ == 2  # two occurrences on the one series

def test_event_folds_into_meeting_series(store):
    ext = _extraction([{"name": "Youth Camp", "type": "event",
                        "series_name": "Youth Camp", "occurrence_date": "2026-05-12"}])
    graph_write.apply(store, ext, doc_ids=["gmail-m1-body-0"], home=str(store.db_path.parent))
    with store._connect() as db:
        row = db.execute("SELECT id, type FROM entities WHERE id='meeting-acme-youth-camp'").fetchone()
    assert row is not None and row["type"] == "meeting"

def test_meeting_series_disabled_falls_back(store):
    (store.db_path.parent / "config.json").write_text('{"meeting_series_enabled": false}')
    ext = _extraction([{"name": "College Meeting 12 May", "type": "meeting",
                        "series_name": "College Meeting", "occurrence_date": "2026-05-12"}])
    graph_write.apply(store, ext, doc_ids=["gmail-m1-body-0"], home=str(store.db_path.parent))
    with store._connect() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()]
    assert ids == ["college-meeting-12-may"]  # legacy bare slugify(name)
```

> Note: if the `store` fixture lacks `db_path`, pass `home=str(tmp_path)` explicitly and construct the Store on that path. Match the file's existing fixture conventions.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph_write.py -k "meeting_series or event_folds or converge" -q`
Expected: FAIL (`AttributeError: module 'mcpbrain.graph_write' has no attribute '_meeting_series_id'`)

- [ ] **Step 3: Add the helper**

```python
# mcpbrain/graph_write.py — add near _bump_email_count (~line 1729)
def _meeting_series_id(series_name: str, org: str) -> str:
    """Deterministic, org-scoped series id for a meeting/event.

    Org-scoping is the structural guard against the 'Staff Meeting across two
    orgs' collision: distinct orgs -> distinct ids, no heuristic needed.
    """
    return slugify(f"meeting-{org}-{series_name}")
```

- [ ] **Step 4: Compute `_meeting_home` near the dedup-home block**

```python
# mcpbrain/graph_write.py — alongside _dedup_home (~line 1065)
    _meeting_home = str(home) if home is not None else str(config.app_dir())
    _meeting_series = config.meeting_series_enabled(_meeting_home)
```

- [ ] **Step 5: Add the meeting/event branch in the entities loop**

Insert immediately after the owner/junk guards and BEFORE the write-time-dedup block (i.e. right after the `if etype == "person" and is_junk_entity(...)` guard, ~line 1089):

```python
        # ── Meeting/event series (Task 5) ─────────────────────────────────
        # A meeting/event is not a name-identity entity; each mention names it
        # differently ('College Meeting 12 May'). Key it on an org-scoped SERIES
        # id and record this mention as an append-only occurrence, so variants
        # converge on one node instead of fragmenting. 'event' folds into the
        # same scheme (stored as type 'meeting').
        if etype in ("meeting", "event") and _meeting_series:
            series = (ent.get("series_name") or ename).strip()
            if not series:
                continue
            m_org = canonical_org(eorg or org, taxonomy)
            eid = _meeting_series_id(series, m_org)
            store.upsert_entity(eid, series, "meeting", m_org, lead_date_iso)
            name_to_id[ename] = eid
            if eid not in linked:
                if store.link_email_entity(lead_msg_id, eid, role="about"):
                    _bump_email_count(store, eid)
                linked.add(eid)
            occ_date = _parse_date_iso(ent.get("occurrence_date") or "") or lead_date_iso or today
            occ_value = (ent.get("source_span") or summary or "")[:200]
            store.append_occurrence(eid, occ_date, occ_value, lead_msg_id or prov_doc_id)
            continue
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/test_graph_write.py -k "meeting_series or event_folds or converge or disabled_falls_back" -q`
Expected: PASS (4 passed)

- [ ] **Step 7: Run the full graph_write suite (no regressions)**

Run: `python -m pytest tests/test_graph_write.py -q`
Expected: PASS (all existing tests still green)

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/graph_write.py tests/test_graph_write.py
git commit -m "feat(graph_write): org-scoped meeting/event series with occurrence observations"
```

---

### Task 6: Topic normalization wired into `apply()`

Normalize the whole `topics_list` at the point it's read, so the stored `email_context.topics` string, the min-2-distinct-org gate (`_topic_distinct_orgs`, which LIKE-matches that stored string), and the topic entity id all use the *same* normalized tags.

**Files:**
- Modify: `mcpbrain/graph_write.py` — where `topics_list`/`topics_str` are computed in `apply()` (`graph_write.py:955-956`); import `topics`.
- Test: `tests/test_graph_write.py`

**Interfaces:**
- Consumes: `topics.normalize_topic` (Task 3), `config.topic_consolidation_enabled` (Task 1).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_graph_write.py — append
def test_topic_variants_converge(store):
    home = str(store.db_path.parent)
    # Two orgs must mention a topic before the min-2-org gate opens it; three
    # applies seed prior rows then create the entity (see apply() topic gate).
    def ext(tid, mid, org, topics):
        return {"thread_id": tid, "org": org, "content_type": "update", "summary": "s",
                "topics": topics, "actions": [], "relations": [], "entities": [],
                "messages": [{"message_id": mid, "sender": "A <a@acme.org>",
                              "date": "2026-01-05", "subject": "s"}]}
    graph_write.apply(store, ext("t1", "m1", "Acme", ["budgets"]), doc_ids=["d1"], home=home)
    graph_write.apply(store, ext("t2", "m2", "Beta", ["the budget"]), doc_ids=["d2"], home=home)
    graph_write.apply(store, ext("t3", "m3", "Acme", ["budget"]), doc_ids=["d3"], home=home)
    with store._connect() as db:
        ids = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='topic'").fetchall()]
    assert ids == ["topic-budget"]  # 'budgets'/'the budget'/'budget' -> one node
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_graph_write.py -k topic_variants_converge -q`
Expected: FAIL (multiple topic ids, e.g. `topic-budgets`, because normalization isn't wired)

- [ ] **Step 3: Wire normalization**

Add the import at the top of `graph_write.py` (with the other `from mcpbrain import` lines, ~line 25):

```python
from mcpbrain import config, orgs, topics
```

Replace the `topics_list`/`topics_str` assignment (`graph_write.py:955-956`):

```python
    topics_list = extraction.get("topics", []) or []
    _topic_home = str(home) if home is not None else str(config.app_dir())
    if config.topic_consolidation_enabled(_topic_home):
        topics_list = [topics.normalize_topic(t, _topic_home) for t in topics_list]
        topics_list = [t for t in topics_list if t]
    topics_str = ", ".join(topics_list)
```

(The existing topic loop at `graph_write.py:1179` keeps its own `.strip().lower()` — harmless on already-normalized tags — and continues to gate on `_topic_distinct_orgs`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_graph_write.py -k topic_variants_converge -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the full graph_write suite**

Run: `python -m pytest tests/test_graph_write.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/graph_write.py tests/test_graph_write.py
git commit -m "feat(graph_write): normalize topic tags before id derivation (converge variants)"
```

---

### Task 7: Extraction contract & prompt — meeting series fields

`validate_extraction`/`sanitize_extraction` already pass unknown entity-dict keys through untouched (they validate list shape + entity *type*, not entity fields — `contract.py:88-92, 255-260`). So the contract needs only a **prompt** change plus a regression test proving the new fields survive validation and sanitization.

**Files:**
- Modify: `mcpbrain/enrich_prompt.md` (entity schema ~line 52-54; field notes ~line 82)
- Test: `tests/test_contract.py`
- Then: `python bin/sync_agents.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract.py — append
from mcpbrain import contract

def test_meeting_series_fields_pass_validation():
    ext = {"thread_id": "t1", "org": "Acme", "content_type": "update",
           "summary": "s", "entities": [
               {"name": "Board 12 May", "type": "meeting",
                "series_name": "Board", "occurrence_date": "2026-05-12"}],
           "topics": [], "actions": [], "relations": []}
    assert contract.validate_extraction(ext) == []

def test_meeting_series_fields_survive_sanitize():
    ext = {"thread_id": "t1", "org": "Acme", "content_type": "update",
           "summary": "s", "entities": [
               {"name": "Board 12 May", "type": "meeting",
                "series_name": "Board", "occurrence_date": "2026-05-12"}],
           "topics": [], "actions": [], "relations": []}
    cleaned, dropped = contract.sanitize_extraction(ext)
    assert dropped == 0
    ent = cleaned["entities"][0]
    assert ent["series_name"] == "Board"
    assert ent["occurrence_date"] == "2026-05-12"
```

- [ ] **Step 2: Run test to verify it passes already (documents the contract)**

Run: `python -m pytest tests/test_contract.py -k meeting_series -q`
Expected: PASS (2 passed) — confirming the lenient contract already accepts the fields. If it FAILS, stop and reconcile before touching the prompt.

- [ ] **Step 3: Update the meeting entity schema in the prompt**

In `mcpbrain/enrich_prompt.md`, extend the `entities` schema line (~line 52-54) and add a field note. Change the entities example to document the meeting-only fields:

```json
  "entities": [{"name": "Person Name", "type": "person|org|project|meeting",
                "org": "<org tag>", "role": "Job title",
                "series_name": "Board Meeting", "occurrence_date": "YYYY-MM-DD",
                "source_span": "exact short phrase from the text"}],
```

Add to the field notes (after the `entities, topics, ...` bullet, ~line 82):

```markdown
- **Meetings/events.** For a `meeting` entity, also set `series_name` — the
  recurring series identity with the specific-occasion parts removed (drop
  dates, "weekly", week numbers, "#3"): "College Board Meeting — 12 May" →
  `series_name` "College Board Meeting". Set `occurrence_date` (YYYY-MM-DD) to
  the date of THIS occurrence. These let the graph record one meeting series
  with each mention as a dated occurrence, instead of a new node per mention.
  Omit both for a genuine one-off meeting.
```

- [ ] **Step 4: Sync the plugin agent copy**

Run: `python bin/sync_agents.py`
Expected: reports `plugin/agents/enrich-batch.md` updated (or already in sync). Confirm no diff remains:
Run: `git diff --stat plugin/agents/enrich-batch.md`

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/enrich_prompt.md plugin/agents/enrich-batch.md tests/test_contract.py
git commit -m "feat(contract): document meeting series_name/occurrence_date extraction fields"
```

---

### Task 8: Calendar — capture recurringEventId

**Files:**
- Modify: `mcpbrain/sync/calendar.py` — `normalise_calendar` meta dict (`calendar.py:58-67`)
- Test: `tests/test_calendar.py`

**Interfaces:**
- Produces: calendar chunks whose `metadata["recurring_event_id"]` holds the event's `recurringEventId` (`""` for non-recurring events).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar.py — append (or create)
from mcpbrain.sync.calendar import normalise_calendar

def test_recurring_event_id_captured():
    ev = {"id": "occ123", "recurringEventId": "series999", "status": "confirmed",
          "summary": "Standup", "start": {"date": "2026-05-12"}, "end": {"date": "2026-05-12"}}
    chunks = normalise_calendar(ev)
    assert chunks[0].metadata["recurring_event_id"] == "series999"

def test_non_recurring_event_id_blank():
    ev = {"id": "e1", "status": "confirmed", "summary": "One-off",
          "start": {"date": "2026-05-12"}, "end": {"date": "2026-05-12"}}
    chunks = normalise_calendar(ev)
    assert chunks[0].metadata["recurring_event_id"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calendar.py -k recurring -q`
Expected: FAIL with `KeyError: 'recurring_event_id'`

- [ ] **Step 3: Add the field to the meta dict**

```python
# mcpbrain/sync/calendar.py — inside normalise_calendar's `meta = {...}` (line ~58)
        "recurring_event_id": event.get("recurringEventId", ""),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calendar.py -k recurring -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/calendar.py tests/test_calendar.py
git commit -m "feat(calendar): capture recurringEventId in chunk metadata"
```

---

### Task 9: Calendar — opportunistic `calendar_series` annotation

When a calendar sync sees a recurring event whose `(normalized summary, org)` matches an existing meeting **series** entity, stamp the series with a `calendar_series` observation (the `recurringEventId`). This is an *annotation upgrade*, never a re-key — it can't mis-merge.

**Files:**
- Modify: `mcpbrain/sync/calendar.py` — add `_annotate_series_from_event(store, event, owner)`, call it in `sync_calendar` and `backfill_calendar_window` next to `_apply_attendees_to_graph`.
- Modify: `mcpbrain/store.py` — add `find_meeting_series(name_slug_prefix)` reader (or reuse a direct query in the annotator).
- Test: `tests/test_calendar.py`

**Interfaces:**
- Consumes: `graph_write._meeting_series_id`, `org_from_email`/`owner`, `store.append/observation`.
- Produces: `calendar._annotate_series_from_event(store, event, owner) -> bool` — writes one `entity_observations` row (`attribute='calendar_series'`, `value=recurringEventId`) on the matching series when exactly one exists; returns `True` if annotated.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar.py — append
from mcpbrain.sync import calendar as cal
from mcpbrain.graph_write import owner_identity_from_config

def test_annotate_series_from_recurring_event(store):
    # Seed a meeting series the way apply() would (org 'external' — no owner org).
    store.upsert_entity("meeting-external-standup", "Standup", "meeting", "external", "2026-05-01")
    ev = {"id": "occ1", "recurringEventId": "series999", "status": "confirmed",
          "summary": "Standup", "start": {"date": "2026-05-12"}, "end": {"date": "2026-05-12"}}
    owner = owner_identity_from_config()
    assert cal._annotate_series_from_event(store, ev, owner) is True
    with store._connect() as db:
        row = db.execute(
            "SELECT value FROM entity_observations "
            "WHERE entity_id='meeting-external-standup' AND attribute='calendar_series'"
        ).fetchone()
    assert row["value"] == "series999"

def test_annotate_noop_without_matching_series(store):
    ev = {"id": "occ1", "recurringEventId": "series999", "status": "confirmed",
          "summary": "Nonexistent Meeting", "start": {"date": "2026-05-12"}}
    owner = owner_identity_from_config()
    assert cal._annotate_series_from_event(store, ev, owner) is False

def test_annotate_noop_for_non_recurring(store):
    store.upsert_entity("meeting-external-standup", "Standup", "meeting", "external", "2026-05-01")
    ev = {"id": "e1", "status": "confirmed", "summary": "Standup", "start": {"date": "2026-05-12"}}
    owner = owner_identity_from_config()
    assert cal._annotate_series_from_event(store, ev, owner) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calendar.py -k annotate -q`
Expected: FAIL (`AttributeError: module ... has no attribute '_annotate_series_from_event'`)

- [ ] **Step 3: Implement the annotator**

```python
# mcpbrain/sync/calendar.py — add import at top
from mcpbrain.graph_write import _meeting_series_id
# (keep existing imports)

def _annotate_series_from_event(store, event, owner) -> bool:
    """Stamp a matching meeting series with this recurring event's id.

    Conservative: only fires for a recurring event whose (normalized summary,
    org) resolves to an EXISTING series entity. Writes a 'calendar_series'
    observation (value=recurringEventId). Never creates or re-keys an entity, so
    it cannot mis-merge two series. Org is unknown from a bare calendar event, so
    we try the owner's default org first, then 'external' — the two buckets a
    calendar-derived series would have been keyed under.
    """
    rec_id = event.get("recurringEventId", "")
    summary = (event.get("summary") or "").strip()
    if not rec_id or not summary:
        return False
    candidate_orgs = []
    # owner's configured org (if any alias carries one) then external fallback.
    candidate_orgs.append("external")
    for org in candidate_orgs:
        eid = _meeting_series_id(summary, org)
        with store._connect() as db:
            exists = db.execute(
                "SELECT 1 FROM entities WHERE id=? AND type='meeting'", (eid,)).fetchone()
            if not exists:
                continue
            already = db.execute(
                "SELECT 1 FROM entity_observations WHERE entity_id=? "
                "AND attribute='calendar_series' AND value=?", (eid, rec_id)).fetchone()
            if already:
                return False
            db.execute(
                "INSERT INTO entity_observations "
                "(entity_id, attribute, value, source, valid_from, confidence_source) "
                "VALUES (?, 'calendar_series', ?, ?, ?, 'gdrive')",
                (eid, rec_id, f"cal-{event.get('id','')}",
                 (event.get("start") or {}).get("date")
                 or (event.get("start") or {}).get("dateTime", "")[:10] or ""))
        return True
    return False
```

> The org-matching is deliberately minimal (`external` only) because a text meeting's org came from the thread, not the calendar. Extending `candidate_orgs` with the owner's configured org names is a safe future tweak; keep it conservative now to avoid false annotations.

- [ ] **Step 4: Call it during sync**

In `sync_calendar` (`calendar.py:278-284`) and `backfill_calendar_window` (`calendar.py:213-221`), add the annotation call right after `_apply_attendees_to_graph(store, ev, owner)`:

```python
            _apply_attendees_to_graph(store, ev, owner)
            _annotate_series_from_event(store, ev, owner)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_calendar.py -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/sync/calendar.py tests/test_calendar.py
git commit -m "feat(calendar): opportunistic calendar_series annotation on matching meeting series"
```

---

### Task 10: Store readers for the scoped meeting migration

**Files:**
- Modify: `mcpbrain/store.py` — add `meeting_source_doc_ids()`, `reset_enriched()`, `meeting_series_for_old()`.
- Test: `tests/test_store.py`

**Interfaces:**
- Produces:
  - `store.meeting_source_doc_ids() -> list[str]` — deduped doc_ids of chunks that produced any `type='meeting'` entity (via `email_entities` → `doc_ids_for_messages`, unioned with `entity_relations.source_doc_id`).
  - `store.reset_enriched(doc_ids: list[str]) -> int` — sets `enriched=0, enriched_version=0` on those chunks; returns rows affected.
  - `store.meeting_series_for_old(old_id: str) -> str | None` — the single `meeting-*` series id now linked to the same messages as `old_id`; `None` if zero or >1 (ambiguous → leave alone).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py — append
def test_meeting_source_doc_ids_via_email_link(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("gmail-m1-body-0", "text", "h1", {"message_id": "m1"})
    store.link_email_entity("m1", "board-12-may", role="about")
    assert store.meeting_source_doc_ids() == ["gmail-m1-body-0"]

def test_reset_enriched(store):
    store.upsert_chunk("d1", "t", "h", {})
    store.mark_enriched(["d1"])
    assert store.reset_enriched(["d1"]) == 1
    with store._connect() as db:
        row = db.execute("SELECT enriched, enriched_version FROM chunks WHERE doc_id='d1'").fetchone()
    assert row["enriched"] == 0 and row["enriched_version"] == 0

def test_meeting_series_for_old_unique_match(store):
    # old bare entity + new series both linked to message m1 -> maps old->series.
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-05-12")
    store.link_email_entity("m1", "board-12-may", role="about")
    store.link_email_entity("m1", "meeting-acme-board", role="about")
    assert store.meeting_series_for_old("board-12-may") == "meeting-acme-board"

def test_meeting_series_for_old_ambiguous_returns_none(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-staff", "Staff", "meeting", "Acme", "2026-05-12")
    store.link_email_entity("m1", "board-12-may", role="about")
    store.link_email_entity("m1", "meeting-acme-board", role="about")
    store.link_email_entity("m1", "meeting-acme-staff", role="about")
    assert store.meeting_series_for_old("board-12-may") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_store.py -k "meeting_source or reset_enriched or series_for_old" -q`
Expected: FAIL (`AttributeError` on `meeting_source_doc_ids`)

- [ ] **Step 3: Implement the readers**

```python
# mcpbrain/store.py — add near doc_ids_for_messages()
def meeting_source_doc_ids(self) -> list[str]:
    """Doc_ids of chunks that produced any type='meeting' entity.

    Union of two provenance paths: email_entities links (message -> chunk via
    doc_ids_for_messages) and entity_relations.source_doc_id. Used by the scoped
    meeting migration to reset exactly those chunks for re-extraction."""
    with self._connect() as db:
        msg_ids = [r["message_id"] for r in db.execute(
            "SELECT DISTINCT ee.message_id FROM email_entities ee "
            "JOIN entities e ON e.id = ee.entity_id WHERE e.type='meeting'").fetchall()]
        rel_docs = [r["source_doc_id"] for r in db.execute(
            "SELECT DISTINCT er.source_doc_id FROM entity_relations er "
            "JOIN entities e ON (e.id=er.entity_a OR e.id=er.entity_b) "
            "WHERE e.type='meeting' AND COALESCE(er.source_doc_id,'')!=''").fetchall()]
    docs = set(self.doc_ids_for_messages(msg_ids)) | set(rel_docs)
    return sorted(docs)

def reset_enriched(self, doc_ids) -> int:
    """Set enriched=0, enriched_version=0 on the given chunks so the daemon
    re-extracts them. Returns rows affected."""
    ids = [d for d in (doc_ids or []) if d]
    if not ids:
        return 0
    ph = ",".join("?" * len(ids))
    with self._connect() as db:
        cur = db.execute(
            f"UPDATE chunks SET enriched=0, enriched_version=0 WHERE doc_id IN ({ph})", ids)
        return cur.rowcount

def meeting_series_for_old(self, old_id) -> str | None:
    """The single 'meeting-*' series entity now linked to the same messages as
    old_id, or None when zero or more than one match (ambiguous -> leave alone,
    per the non-destructive migration policy)."""
    with self._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT e2.id FROM email_entities ee1 "
            "JOIN email_entities ee2 ON ee1.message_id = ee2.message_id "
            "JOIN entities e2 ON e2.id = ee2.entity_id "
            "WHERE ee1.entity_id = ? AND e2.type='meeting' "
            "AND e2.id != ? AND e2.id LIKE 'meeting-%'",
            (old_id, old_id)).fetchall()
    series = [r["id"] for r in rows]
    return series[0] if len(series) == 1 else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_store.py -k "meeting_source or reset_enriched or series_for_old" -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_store.py
git commit -m "feat(store): scoped-migration readers (meeting_source_doc_ids, reset_enriched, meeting_series_for_old)"
```

---

### Task 11: `consolidate.py` — migration logic

**Files:**
- Create: `mcpbrain/consolidate.py`
- Test: `tests/test_consolidate.py`

**Interfaces:**
- Consumes: `topics.normalize_topic`, `store.merge_entities`, `store.meeting_source_doc_ids`, `store.reset_enriched`, `store.meeting_series_for_old`.
- Produces:
  - `consolidate.remap_topics(store, home) -> dict` — folds each existing `type='topic'` entity into its `topic-<normalize_topic(name)>` id via `merge_entities`; returns `{"merged": int, "canonical": int}`.
  - `consolidate.reset_meeting_sources(store) -> dict` — snapshots current meeting-entity ids and resets their source chunks; returns `{"pre_ids": [...], "chunks_reset": int}`.
  - `consolidate.retire_meeting_duplicates(store, pre_ids) -> dict` — merges each pre-migration bare meeting id into its unique new series (if any); returns `{"retired": int, "left": int}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consolidate.py
from mcpbrain import consolidate

def test_remap_topics_folds_variants(store, tmp_path):
    home = str(tmp_path)
    store.upsert_entity("topic-budgets", "budgets", "topic", "", "2026-01-01")
    store.upsert_entity("topic-budget", "budget", "topic", "", "2026-01-01")
    out = consolidate.remap_topics(store, home)
    with store._connect() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM entities WHERE type='topic'").fetchall()]
    assert ids == ["topic-budget"]
    assert out["merged"] == 1

def test_remap_topics_leaves_distinct(store, tmp_path):
    store.upsert_entity("topic-prayer", "prayer", "topic", "", "2026-01-01")
    store.upsert_entity("topic-prayer-meeting", "prayer meeting", "topic", "", "2026-01-01")
    consolidate.remap_topics(store, str(tmp_path))
    with store._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM entities WHERE type='topic'").fetchone()[0]
    assert n == 2  # no synonym entry -> stay distinct

def test_reset_meeting_sources(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("gmail-m1-body-0", "t", "h", {"message_id": "m1"})
    store.mark_enriched(["gmail-m1-body-0"])
    store.link_email_entity("m1", "board-12-may", role="about")
    out = consolidate.reset_meeting_sources(store)
    assert "board-12-may" in out["pre_ids"]
    assert out["chunks_reset"] == 1

def test_retire_meeting_duplicates(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-05-12")
    store.link_email_entity("m1", "board-12-may", role="about")
    store.link_email_entity("m1", "meeting-acme-board", role="about")
    out = consolidate.retire_meeting_duplicates(store, ["board-12-may"])
    with store._connect() as db:
        remaining = db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()
    assert [r["id"] for r in remaining] == ["meeting-acme-board"]
    assert out["retired"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_consolidate.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'mcpbrain.consolidate'`)

- [ ] **Step 3: Implement**

```python
# mcpbrain/consolidate.py
"""One-shot, attended consolidation migrations for meetings and topics.

These are the ONLY destructive operations in the series/topic feature (they call
store.merge_entities, which deletes the loser row). Always run behind a full DB
backup + gold eval — see bin/consolidate.py. Going-forward consolidation
(graph_write.apply) does no merges and needs none of this.
"""

import logging

from mcpbrain import topics
from mcpbrain.chunking import slugify

log = logging.getLogger(__name__)


def remap_topics(store, home) -> dict:
    """Fold each topic entity into its normalized topic-<canonical> id."""
    with store._connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, name FROM entities WHERE type='topic'").fetchall()]
    merged = 0
    canon_ids = set()
    for r in rows:
        canonical = topics.normalize_topic(r["name"], home)
        if not canonical:
            continue
        new_id = slugify(f"topic-{canonical}")
        canon_ids.add(new_id)
        if new_id == r["id"]:
            continue
        # Ensure the canonical entity exists, then fold the variant into it.
        store.upsert_entity(new_id, canonical, "topic", "", "")
        store.merge_entities(r["id"], new_id, method="topic_consolidation")
        merged += 1
    return {"merged": merged, "canonical": len(canon_ids)}


def reset_meeting_sources(store) -> dict:
    """Snapshot current meeting ids and reset their source chunks for re-extract."""
    with store._connect() as db:
        pre_ids = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='meeting'").fetchall()]
    doc_ids = store.meeting_source_doc_ids()
    chunks_reset = store.reset_enriched(doc_ids)
    log.info("reset_meeting_sources: %d meeting entities, %d chunks reset",
             len(pre_ids), chunks_reset)
    return {"pre_ids": pre_ids, "chunks_reset": chunks_reset}


def retire_meeting_duplicates(store, pre_ids) -> dict:
    """Merge each pre-migration bare meeting id into its unique new series.

    Runs AFTER re-extraction has produced the meeting-<org>-<series> nodes. A
    pre-id with zero or ambiguous series matches is LEFT as a single-occurrence
    entity (non-destructive policy). Skips ids that are already series ids."""
    retired = 0
    left = 0
    for old_id in pre_ids:
        if old_id.startswith("meeting-"):
            continue  # already a series id (e.g. a re-run)
        with store._connect() as db:
            still = db.execute("SELECT 1 FROM entities WHERE id=?", (old_id,)).fetchone()
        if not still:
            continue
        series = store.meeting_series_for_old(old_id)
        if series:
            store.merge_entities(old_id, series, method="meeting_series")
            retired += 1
        else:
            left += 1
    log.info("retire_meeting_duplicates: retired=%d left=%d", retired, left)
    return {"retired": retired, "left": left}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_consolidate.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/consolidate.py tests/test_consolidate.py
git commit -m "feat(consolidate): topic remap + scoped meeting reset/retire migration logic"
```

---

### Task 12: `bin/consolidate.py` — attended CLI with backup

Thin orchestration wrapper: backs up the DB, then runs a phase. No new logic (all tested in Task 11), so it carries a smoke test only.

**Files:**
- Create: `bin/consolidate.py`
- Test: `tests/test_consolidate.py` (smoke test of the backup helper)

**Interfaces:**
- Consumes: `consolidate.*`, `config.app_dir`, `store.Store`.
- Produces: CLI `python bin/consolidate.py {topics|meetings-reset|meetings-retire} [--home DIR]`; `_backup_db(db_path) -> Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consolidate.py — append
import importlib.util, pathlib

def _load_bin():
    path = pathlib.Path(__file__).resolve().parents[1] / "bin" / "consolidate.py"
    spec = importlib.util.spec_from_file_location("bin_consolidate", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod

def test_backup_db_creates_copy(tmp_path):
    db = tmp_path / "brain.db"; db.write_bytes(b"sqlitedata")
    mod = _load_bin()
    backup = mod._backup_db(db)
    assert backup.exists() and backup.read_bytes() == b"sqlitedata"
    assert backup != db
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_consolidate.py -k backup_db -q`
Expected: FAIL (`FileNotFoundError` / no `bin/consolidate.py`)

- [ ] **Step 3: Implement the CLI**

```python
# bin/consolidate.py
"""Attended, backup-gated consolidation migrations (curator-run).

Usage:
  python bin/consolidate.py topics            # fold the 412 topic variants
  python bin/consolidate.py meetings-reset    # reset meeting-source chunks
  # ... let the daemon drain/re-extract, then:
  python bin/consolidate.py meetings-retire   # fold old meeting nodes into series

Every phase takes a full DB backup FIRST. If the post-run gold eval regresses
(recall@10 < 0.55 or MRR < 0.35), restore the printed backup path. meetings-reset
writes the pre-migration id snapshot to <home>/consolidate_pre_ids.json for the
later meetings-retire phase.
"""
import argparse, json, shutil, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcpbrain import config, consolidate            # noqa: E402
from mcpbrain.store import Store                     # noqa: E402


def _backup_db(db_path: Path) -> Path:
    db_path = Path(db_path)
    backup = db_path.with_suffix(db_path.suffix + f".bak-{int(time.time())}")
    shutil.copy2(db_path, backup)
    return backup


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["topics", "meetings-reset", "meetings-retire"])
    ap.add_argument("--home", default=None)
    ns = ap.parse_args(argv)

    home = ns.home or str(config.app_dir())
    db_path = Path(config.db_path(home)) if hasattr(config, "db_path") else Path(home) / "brain.db"
    store = Store(db_path); store.init()

    backup = _backup_db(db_path)
    print(f"[consolidate] backup written: {backup}")
    snap = Path(home) / "consolidate_pre_ids.json"

    if ns.phase == "topics":
        print("[consolidate] topics:", consolidate.remap_topics(store, home))
    elif ns.phase == "meetings-reset":
        out = consolidate.reset_meeting_sources(store)
        snap.write_text(json.dumps(out["pre_ids"]))
        print(f"[consolidate] meetings-reset: {out['chunks_reset']} chunks reset; "
              f"{len(out['pre_ids'])} pre-ids saved to {snap}")
        print("[consolidate] now let the daemon re-extract, then run meetings-retire.")
    elif ns.phase == "meetings-retire":
        if not snap.exists():
            print("[consolidate] ERROR: no pre-id snapshot; run meetings-reset first.")
            return 1
        pre_ids = json.loads(snap.read_text())
        print("[consolidate] meetings-retire:", consolidate.retire_meeting_duplicates(store, pre_ids))

    print("[consolidate] Run `mcpbrain enrich-eval` now. If recall@10 < 0.55 or "
          f"MRR < 0.35, restore: cp {backup} {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> If `config` has no `db_path()` helper, the fallback `<home>/brain.db` matches the Store default; confirm the real DB filename in `config.py`/`store.py` and adjust the one line if needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_consolidate.py -k backup_db -q`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/consolidate.py tests/test_consolidate.py
git commit -m "feat(bin): attended backup-gated consolidate CLI (topics / meetings-reset / meetings-retire)"
```

---

### Task 13: Full-suite regression + gold-eval baseline

**Files:** none (verification task).

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS (no regressions across the repo).

- [ ] **Step 2: Capture the gold-eval baseline (pre-migration)**

Run: `mcpbrain enrich-eval`
Expected: prints `Gold set: recall@10=<r> MRR=<m> ...`. Record both numbers; both must satisfy recall@10 ≥ 0.55 and MRR ≥ 0.35 already (baseline sanity).

- [ ] **Step 3: Document the migration runbook in the PR description**

The attended migration sequence (curator, on the live store):
1. `python bin/consolidate.py topics` → then `mcpbrain enrich-eval`; if regressed, restore the printed backup.
2. `python bin/consolidate.py meetings-reset` → let the daemon drain/re-extract the reset chunks (watch the spool empty).
3. `python bin/consolidate.py meetings-retire` → then `mcpbrain enrich-eval`; if regressed, restore.
4. Count checks: `SELECT type, COUNT(*) FROM entities GROUP BY type` — meeting + topic node counts should drop; verify occurrence rows exist (`SELECT COUNT(*) FROM entity_observations WHERE attribute='occurrence'`).

- [ ] **Step 4: Commit any doc/runbook note (if added) and finish**

```bash
git add -A
git commit -m "test: full-suite green + gold-eval baseline for series/topic consolidation" --allow-empty
```

---

## Self-Review

**Spec coverage:**
- Meeting series model (name+org key, occurrences as observations) → Tasks 4, 5.
- Extraction emits series_name + occurrence_date → Task 7.
- recurringEventId capture + opportunistic upgrade → Tasks 8, 9.
- Topic deterministic normalization + curated synonym map → Tasks 2, 3, 6.
- Topics NOT added to `_NAME_MERGEABLE_TYPES` → honored (no change to resolve.py).
- Scoped meeting migration (not full-corpus reflow) → Tasks 10, 11, 12.
- Topic id-remap migration → Tasks 11, 12.
- Attended + backup-gated + gold-gated → Task 12 (backup), Task 13 (gold gate).
- Ships ON behind kill-switches → Task 1.
- `event` folded in → Task 5. Occurrence value = date + snippet → Task 5. inflect + system-wide assessment → Task 2. Unmatched left as single-occurrence → Task 11 (`retire_meeting_duplicates` leaves ambiguous/unmatched).

**Placeholder scan:** No TBD/TODO; every code step shows complete code; commands have expected output.

**Type consistency:** `_meeting_series_id(series_name, org)` used identically in Tasks 5, 9. `append_occurrence(entity_id, valid_from, value, source)` defined in Task 4, used in Tasks 5. `normalize_topic(tag, home)` defined Task 3, used in Tasks 6, 11. `meeting_source_doc_ids`/`reset_enriched`/`meeting_series_for_old` defined Task 10, used Task 11. `remap_topics`/`reset_meeting_sources`/`retire_meeting_duplicates` defined Task 11, used Task 12.

**Known integration caveats flagged for the implementer:** the `store` test fixture shape (Task 4 note), `db_path` filename in the CLI (Task 12 note), and matching the existing `tests/test_graph_write.py` fixture conventions (Task 5 note).
