# Memory "keep" Re-review Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `memory_distil`'s `keep` verdict from permanently parking a note — resurface it for reconsideration after 30 days, while `expire`/`promote` stay permanent exactly as they are today.

**Architecture:** A new `distilled_verdict` chunk-metadata field records which verdict produced a `distilled_at` stamp. `store.note_chunks`'s `exclude_distilled` filter re-includes a note only when its `distilled_verdict` is `"keep"` and its `distilled_at` is older than `keep_review_days` (default 30). `expire`/`promote` stay excluded regardless of age.

**Tech Stack:** Python 3.12, SQLite chunk-metadata JSON (`mcpbrain/store.py`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-memory-keep-review-window-design.md`

## Global Constraints

- Do not push or release. Commit locally only.
- Do not bump versions. No changes to the five version files.
- `ruff` must be clean on every file touched.
- No new `config.py` getter for the window length — `keep_review_days` is a plain parameter
  with a default, matching how `build_distil_requests`'s own `cap` is handled today.
- `mcpbrain/memory_index.py`'s `note_chunks` call stays untouched — it never sets
  `exclude_distilled`, so it is unaffected either way.
- `expire`/`promote` permanence is unchanged. Only `keep` becomes time-boxed.
- Timestamp parsing uses `.replace("Z", "+00:00")` before `datetime.fromisoformat`, matching
  this codebase's established convention (`decay.py`, `fleet.py`, `importance.py`, `feedback.py`,
  `auto_enable.py`, `probes.py`).
- A malformed or missing `distilled_at` on a `distilled_verdict="keep"` row fails safe: treated
  as not-yet-stale (stays excluded), never raises.

---

## File Structure

| File | Responsibility |
|---|---|
| `mcpbrain/store.py` | Modify — `note_chunks` gains `keep_review_days` and age-aware exclusion |
| `mcpbrain/memory_distil.py` | Modify — `drain_distil` stamps `distilled_verdict`; `build_distil_requests` threads `keep_review_days` through |
| `tests/test_store_schema_p3.py` | Modify — age-boundary tests for `note_chunks` |
| `tests/test_memory_distil.py` | Modify — resurface/stay-excluded tests for `build_distil_requests` |

This is one task: both files change together, and `note_chunks`'s new filter is inert without
`memory_distil` stamping `distilled_verdict`, while `memory_distil`'s stamp is inert without
`note_chunks` reading it. A reviewer needs both halves in view to judge correctness, so
splitting them would just create an artificial red-then-red gate.

---

### Task 1: Time-box the `keep` verdict

**Files:**
- Modify: `mcpbrain/store.py:1593-1627` (`note_chunks`)
- Modify: `mcpbrain/memory_distil.py` (module docstring, `build_distil_requests`, `drain_distil`)
- Test: `tests/test_store_schema_p3.py` (append)
- Test: `tests/test_memory_distil.py` (append)

**Interfaces:**
- Consumes: nothing from outside this task — this is a standalone follow-up to the already-shipped
  `distilled_at`/`exclude_distilled` mechanism (`mcpbrain/store.py`, `mcpbrain/memory_distil.py`,
  both currently in the state shown below).
- Produces: `Store.note_chunks(..., keep_review_days: int = 30, ...)`;
  `memory_distil.build_distil_requests(store, *, cap=30, keep_review_days=30)`. Nothing
  downstream consumes these — this task's own tests are the proof.

**Background — current state of both files (post the closure-durability branch):**

`mcpbrain/store.py`'s `note_chunks` currently reads:

```python
    def note_chunks(self, *, observation_type: str | None = None,
                    include_expired: bool = False, exclude_distilled: bool = False,
                    limit: int = 500) -> list[dict]:
        """Return capture-note chunks (doc_id starting with 'note-'), with parsed metadata.

        Excludes expired chunks (meta["expired"] is truthy) unless include_expired=True.
        Excludes chunks memory_distil has already classified (meta["distilled_at"] is
        truthy) when exclude_distilled=True — pass this only from memory_distil's own
        selection query; callers like memory_index.py that render the live memory set
        must NOT set it, since a distilled note still belongs in the index. Filters by
        observation_type if provided. Returns the newest `limit` live results (ORDER BY
        rowid DESC). The limit is applied AFTER the Python-side expired/distilled/
        observation_type filter, so a store full of expired or already-distilled notes
        never truncates live/fresh ones — we iterate the cursor and stop once `limit`
        live rows are collected rather than pre-truncating in SQL.
        """
        sql = ("SELECT doc_id, text, metadata FROM chunks "
               "WHERE doc_id LIKE 'note-%' ORDER BY rowid DESC")
        results = []
        with self._connect() as db:
            for r in db.execute(sql):
                try:
                    meta = json.loads(r["metadata"])
                except Exception:
                    continue
                if not include_expired and meta.get("expired"):
                    continue
                if exclude_distilled and meta.get("distilled_at"):
                    continue
                if observation_type is not None and meta.get("observation_type") != observation_type:
                    continue
                results.append({
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": meta,
                })
                if len(results) == limit:
                    break
        return results
```

`mcpbrain/memory_distil.py` currently reads in full:

```python
"""memory_distil: review, expire, and promote session memory notes.

Block contract:
  build_distil_requests(store, *, cap) -> list[dict]
      Returns live memory notes ready for LLM review.

  drain_distil(store, inbox_obj) -> {"expired": N, "promotions_flagged": N}
      Applies verdicts (all three stamp distilled_at so build_distil_requests
      stops re-submitting an already-classified note):
        "keep"    — stamps distilled_at only
        "expire"  — patches chunk metadata expired=True, records memory_expired
        "promote" — records a memory_promotion finding; keeps the note live
      Unknown doc_id or unknown verdict: silently skipped.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_VALID_VERDICTS = {"keep", "expire", "promote"}


def build_distil_requests(store, *, cap: int = 30) -> list[dict]:
    """Return up to `cap` live memory notes as LLM-ready request dicts.

    Each dict contains:
        {doc_id, title, content, captured_at}
    content is the body after the first blank line, capped at 300 chars.
    """
    chunks = store.note_chunks(observation_type="memory", exclude_distilled=True, limit=cap)
    results = []
    for c in chunks:
        text = c["text"] or ""
        meta = c.get("metadata") or {}

        # Split on first double-newline to get body; fall back to full text.
        parts = text.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else text
        content = body[:300]

        results.append({
            "doc_id": c["doc_id"],
            "title": meta.get("title", ""),
            "content": content,
            "captured_at": meta.get("captured_at", ""),
        })
    return results


def drain_distil(store, inbox_obj: dict) -> dict:
    """Apply LLM verdicts to memory notes.

    Expects inbox_obj["memory_distil"] to be a list of:
        {doc_id, verdict, reason?, target_hint?}

    Returns {"expired": N, "promotions_flagged": N}.
    """
    items = inbox_obj.get("memory_distil") or []
    expired_count = 0
    promoted_count = 0

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for item in items:
        doc_id = item.get("doc_id", "")
        verdict = item.get("verdict", "")

        # Skip unknown verdicts immediately.
        if verdict not in _VALID_VERDICTS:
            log.debug("memory_distil: skipping doc_id=%s unknown verdict=%s", doc_id, verdict)
            continue

        if verdict == "keep":
            # Stamp distilled_at even on a no-op verdict: nothing about an
            # unchanged note will produce a different answer on the next
            # distil run, so re-asking Haiku about it forever is pure
            # recurring cost. patch_chunk_metadata's own existence guard
            # (returns False, harmlessly) covers a doc_id gone stale between
            # listing and draining, so no separate get_chunk check is needed
            # for this branch.
            store.patch_chunk_metadata(doc_id, distilled_at=now)
            continue

        # Verify the chunk exists before acting.
        chunk = store.get_chunk(doc_id)
        if chunk is None:
            log.debug("memory_distil: doc_id=%s not found, skipping", doc_id)
            continue

        if verdict == "expire":
            ok = store.patch_chunk_metadata(doc_id, expired=True, distilled_at=now)
            if ok:
                reason = item.get("reason", "")
                store.record_change(
                    "memory_expired",
                    ref_id=doc_id,
                    summary=f"Memory note expired: {doc_id}",
                    detail=reason,
                    source="memory_distil",
                )
                expired_count += 1

        elif verdict == "promote":
            reason = item.get("reason", "")
            target_hint = item.get("target_hint", "")
            # get_chunk returns metadata already parsed to a dict; guard for a
            # raw JSON string defensively.
            meta = chunk.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            store.record_finding(
                "memory_promotion",
                ref_id=doc_id,
                org=meta.get("org", ""),
                summary=f"Memory note flagged for promotion: {doc_id}",
                detail=f"reason={reason} target_hint={target_hint}",
            )
            ok = store.patch_chunk_metadata(doc_id, distilled_at=now)
            if ok:
                promoted_count += 1

    return {"expired": expired_count, "promotions_flagged": promoted_count}


# Register with drain.py so it is called automatically when this module is imported.
def _register():
    try:
        from mcpbrain.drain import BLOCK_DRAINERS  # noqa: PLC0415

        BLOCK_DRAINERS["memory_distil"] = drain_distil
    except ImportError:
        log.debug("drain module not available; memory_distil drainer not registered")


_register()
```

`tests/test_memory_distil.py` currently has `_store(tmp_path)` and `_note(s, doc_id, title)`
helpers near the top (the latter sets `observation_type: "memory"` and `captured_at:
"2026-06-01T00:00:00Z"` in the chunk metadata) — reuse both, don't invent new ones.

`tests/test_store_schema_p3.py` currently has a `_store(tmp_path)` helper and, from the prior
branch, `test_note_chunks_exclude_distilled` and
`test_note_chunks_exclude_distilled_applies_before_limit` — both currently stamp metadata with
`distilled_at` but no `distilled_verdict`. **These two existing tests must be updated** in this
task: they assert on `exclude_distilled=True` excluding ANY `distilled_at`-stamped note, which
is no longer true for `keep`-verdicted ones — after this task, a bare `distilled_at` with no
`distilled_verdict` (or a `distilled_verdict` other than `"keep"`) still excludes permanently,
so if those two tests stamp metadata with `distilled_verdict="expire"` (or any non-`"keep"`
value) explicitly, their existing assertions hold unchanged. Do this rather than leaving them
implicitly relying on undefined behavior.

- [ ] **Step 1: Write the failing tests**

First, update the two existing `note_chunks` tests in `tests/test_store_schema_p3.py` so they
keep testing permanent exclusion (unrelated to `keep`'s new time-boxing) by giving their stamped
notes an explicit non-`"keep"` verdict. Find `test_note_chunks_exclude_distilled` and change its
second `upsert_chunk` call's metadata from:

```python
    s.upsert_chunk(doc_id="note-b", text="B\n\nbody", content_hash="note-b",
                   metadata={"source": "note", "title": "B",
                             "observation_type": "memory",
                             "captured_at": "2026-06-01T00:00:00Z",
                             "distilled_at": "2026-07-01T00:00:00Z"})
```

to:

```python
    s.upsert_chunk(doc_id="note-b", text="B\n\nbody", content_hash="note-b",
                   metadata={"source": "note", "title": "B",
                             "observation_type": "memory",
                             "captured_at": "2026-06-01T00:00:00Z",
                             "distilled_at": "2026-07-01T00:00:00Z",
                             "distilled_verdict": "expire"})
```

Find `test_note_chunks_exclude_distilled_applies_before_limit` and change its `note-old`
`upsert_chunk` call's metadata the same way — add `"distilled_verdict": "expire"` to the dict
that already has `"distilled_at": "2026-06-02T00:00:00Z"`. Both tests' existing assertions stay
the same; only the fixture metadata gains one key each.

Now append the new age-boundary tests to `tests/test_store_schema_p3.py`:

```python
def test_note_chunks_exclude_distilled_reincludes_stale_keep(tmp_path):
    """A keep-verdicted note past the re-review window must resurface — keep is a
    deferral, not a decision, and permanently parking it means most notes get
    looked at exactly once, at their freshest and least-informative moment."""
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-stale-keep", text="Stale\n\nbody", content_hash="note-stale-keep",
                   metadata={"source": "note", "title": "Stale",
                             "observation_type": "memory",
                             "captured_at": "2026-05-01T00:00:00Z",
                             "distilled_at": "2026-06-01T00:00:00Z",
                             "distilled_verdict": "keep"})

    ids = {c["doc_id"] for c in
           s.note_chunks(observation_type="memory", exclude_distilled=True,
                        keep_review_days=30, limit=500)}

    assert ids == {"note-stale-keep"}


def test_note_chunks_exclude_distilled_keeps_excluding_fresh_keep(tmp_path):
    """A keep-verdicted note still inside the window stays excluded."""
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-fresh-keep", text="Fresh\n\nbody", content_hash="note-fresh-keep",
                   metadata={"source": "note", "title": "Fresh",
                             "observation_type": "memory",
                             "captured_at": "2026-07-20T00:00:00Z",
                             "distilled_at": "2026-07-24T00:00:00Z",
                             "distilled_verdict": "keep"})

    ids = {c["doc_id"] for c in
           s.note_chunks(observation_type="memory", exclude_distilled=True,
                        keep_review_days=30, limit=500)}

    assert ids == set()


def test_note_chunks_exclude_distilled_never_reincludes_expire_or_promote(tmp_path):
    """expire/promote stay permanently excluded no matter how old the stamp is —
    only keep is time-boxed."""
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-old-expire", text="X\n\nbody", content_hash="note-old-expire",
                   metadata={"source": "note", "title": "X",
                             "observation_type": "memory",
                             "captured_at": "2026-01-01T00:00:00Z",
                             "expired": True,
                             "distilled_at": "2026-01-01T00:00:00Z",
                             "distilled_verdict": "expire"})
    s.upsert_chunk(doc_id="note-old-promote", text="Y\n\nbody", content_hash="note-old-promote",
                   metadata={"source": "note", "title": "Y",
                             "observation_type": "memory",
                             "captured_at": "2026-01-01T00:00:00Z",
                             "distilled_at": "2026-01-01T00:00:00Z",
                             "distilled_verdict": "promote"})

    ids = {c["doc_id"] for c in
           s.note_chunks(observation_type="memory", exclude_distilled=True,
                        keep_review_days=30, limit=500)}

    assert ids == set()


def test_note_chunks_exclude_distilled_keep_with_bad_timestamp_fails_safe(tmp_path):
    """A malformed distilled_at on a keep-verdicted row must not crash and must
    not spuriously resurface — the safe default is to stay excluded."""
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-bad-ts", text="Bad\n\nbody", content_hash="note-bad-ts",
                   metadata={"source": "note", "title": "Bad",
                             "observation_type": "memory",
                             "captured_at": "2026-01-01T00:00:00Z",
                             "distilled_at": "not-a-timestamp",
                             "distilled_verdict": "keep"})

    ids = {c["doc_id"] for c in
           s.note_chunks(observation_type="memory", exclude_distilled=True,
                        keep_review_days=30, limit=500)}

    assert ids == set()
```

Then append to `tests/test_memory_distil.py`:

```python
def test_drain_stamps_distilled_verdict_alongside_distilled_at(tmp_path):
    """distilled_verdict must record WHICH verdict produced the stamp, so
    note_chunks can tell a deferral (keep) apart from a decision
    (expire/promote) when deciding whether to re-include it."""
    s = _store(tmp_path)
    _note(s, "note-k", "Keep me")
    _note(s, "note-e", "Expire me")
    _note(s, "note-p", "Promote me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-k", "verdict": "keep"},
        {"doc_id": "note-e", "verdict": "expire", "reason": "stale"},
        {"doc_id": "note-p", "verdict": "promote",
         "reason": "stated 4 times", "target_hint": "preferences.md"},
    ]})

    assert s.get_chunk("note-k")["metadata"]["distilled_verdict"] == "keep"
    assert s.get_chunk("note-e")["metadata"]["distilled_verdict"] == "expire"
    assert s.get_chunk("note-p")["metadata"]["distilled_verdict"] == "promote"


def test_build_distil_requests_resurfaces_stale_keep_note(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-stale", text="Stale\n\nbody", content_hash="note-stale",
                   metadata={"source": "note", "title": "Stale",
                             "observation_type": "memory",
                             "captured_at": "2026-05-01T00:00:00Z",
                             "distilled_at": "2026-06-01T00:00:00Z",
                             "distilled_verdict": "keep"})

    reqs = memory_distil.build_distil_requests(s, cap=30, keep_review_days=30)

    assert {r["doc_id"] for r in reqs} == {"note-stale"}


def test_build_distil_requests_leaves_fresh_keep_note_excluded(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-fresh", text="Fresh\n\nbody", content_hash="note-fresh",
                   metadata={"source": "note", "title": "Fresh",
                             "observation_type": "memory",
                             "captured_at": "2026-07-20T00:00:00Z",
                             "distilled_at": "2026-07-24T00:00:00Z",
                             "distilled_verdict": "keep"})

    reqs = memory_distil.build_distil_requests(s, cap=30, keep_review_days=30)

    assert reqs == []
```

Note: `test_build_distil_requests_resurfaces_stale_keep_note` and
`test_build_distil_requests_leaves_fresh_keep_note_excluded` use fixed calendar dates
(`"2026-06-01T00:00:00Z"` / `"2026-07-24T00:00:00Z"`) rather than computing an offset from
`datetime.now()` at test-write time, matching this session's date context (today is 2026-07-25)
so the "30 days ago" and "1 day ago" relationships are stable and don't depend on when the test
actually runs.

- [ ] **Step 2: Run the tests to verify they fail for the right reason**

Run: `pytest tests/test_store_schema_p3.py -k "reincludes_stale_keep or keeps_excluding_fresh_keep or never_reincludes or bad_timestamp" -v -p no:xdist`
Expected: FAIL — `TypeError: note_chunks() got an unexpected keyword argument 'keep_review_days'`.

Run: `pytest tests/test_memory_distil.py -k "distilled_verdict or resurfaces_stale or leaves_fresh" -v -p no:xdist`
Expected: `test_drain_stamps_distilled_verdict_alongside_distilled_at` FAILs with `KeyError: 'distilled_verdict'` (or `assert None == 'keep'` depending on how `.get`/`[]` is used — either way, the key is absent today); the other two FAIL with `TypeError: build_distil_requests() got an unexpected keyword argument 'keep_review_days'`.

Also run the two UPDATED pre-existing tests to confirm they still pass with their new
`distilled_verdict` fixture key (they should — you haven't changed `note_chunks` yet, and the
old behavior — exclude on any `distilled_at` — is unaffected until Step 3):

Run: `pytest tests/test_store_schema_p3.py -k "test_note_chunks_exclude_distilled and not reincludes and not fails_safe" -v -p no:xdist`
Expected: PASS (both, unchanged behavior pre-Step-3).

- [ ] **Step 3: Update `note_chunks`**

In `mcpbrain/store.py`, replace the whole `note_chunks` method (currently lines 1593-1621) with:

```python
    def note_chunks(self, *, observation_type: str | None = None,
                    include_expired: bool = False, exclude_distilled: bool = False,
                    keep_review_days: int = 30, limit: int = 500) -> list[dict]:
        """Return capture-note chunks (doc_id starting with 'note-'), with parsed metadata.

        Excludes expired chunks (meta["expired"] is truthy) unless include_expired=True.
        Excludes chunks memory_distil has already classified (meta["distilled_at"] is
        truthy) when exclude_distilled=True — pass this only from memory_distil's own
        selection query; callers like memory_index.py that render the live memory set
        must NOT set it, since a distilled note still belongs in the index.

        A "keep" verdict is a deferral, not a decision, so it is time-boxed: a
        distilled_verdict="keep" chunk is RE-INCLUDED once distilled_at is more than
        keep_review_days old, so memory_distil reconsiders it. "expire"/"promote" (or a
        distilled_at with no distilled_verdict at all) stay excluded permanently — those
        are genuine terminal decisions. A malformed or missing distilled_at on a "keep"
        row fails safe (stays excluded) rather than raising or spuriously resurfacing.

        Filters by observation_type if provided. Returns the newest `limit` live results
        (ORDER BY rowid DESC). The limit is applied AFTER the Python-side expired/
        distilled/observation_type filter, so a store full of expired or already-distilled
        notes never truncates live/fresh ones — we iterate the cursor and stop once
        `limit` live rows are collected rather than pre-truncating in SQL.
        """
        sql = ("SELECT doc_id, text, metadata FROM chunks "
               "WHERE doc_id LIKE 'note-%' ORDER BY rowid DESC")
        results = []
        with self._connect() as db:
            for r in db.execute(sql):
                try:
                    meta = json.loads(r["metadata"])
                except Exception:
                    continue
                if not include_expired and meta.get("expired"):
                    continue
                if exclude_distilled and meta.get("distilled_at"):
                    stale = False
                    if meta.get("distilled_verdict") == "keep":
                        try:
                            stamped = datetime.fromisoformat(
                                meta["distilled_at"].replace("Z", "+00:00"))
                            stale = (datetime.now(timezone.utc) - stamped
                                     > timedelta(days=keep_review_days))
                        except (ValueError, TypeError, AttributeError):
                            stale = False  # malformed timestamp: fail safe, stay excluded
                    if not stale:
                        continue
                if observation_type is not None and meta.get("observation_type") != observation_type:
                    continue
                results.append({
                    "doc_id": r["doc_id"],
                    "text": r["text"],
                    "metadata": meta,
                })
                if len(results) == limit:
                    break
        return results
```

`store.py` already imports `datetime`, `timedelta`, and `timezone` at the top of the file
(`from datetime import date, datetime, timedelta, timezone`) — no new import needed.

- [ ] **Step 4: Run the `note_chunks` tests**

Run: `pytest tests/test_store_schema_p3.py -k "note_chunks_exclude_distilled" -v -p no:xdist`
Expected: all 6 PASS (the 2 updated pre-existing tests plus the 4 new ones).

- [ ] **Step 5: Update `memory_distil.py`**

In `mcpbrain/memory_distil.py`, replace the module docstring (currently lines 1-13) with:

```python
"""memory_distil: review, expire, and promote session memory notes.

Block contract:
  build_distil_requests(store, *, cap, keep_review_days) -> list[dict]
      Returns live memory notes ready for LLM review.

  drain_distil(store, inbox_obj) -> {"expired": N, "promotions_flagged": N}
      Applies verdicts (all three stamp distilled_at + distilled_verdict so
      build_distil_requests stops re-submitting an already-classified note):
        "keep"    — stamps distilled_at + distilled_verdict="keep"; time-boxed —
                    build_distil_requests re-offers it once distilled_at is more
                    than keep_review_days old, since "keep" is a deferral, not a
                    terminal decision
        "expire"  — patches chunk metadata expired=True, records memory_expired;
                    permanent
        "promote" — records a memory_promotion finding; keeps the note live;
                    permanent
      Unknown doc_id or unknown verdict: silently skipped.
"""
```

Replace `build_distil_requests` (currently lines 27-42) with:

```python
def build_distil_requests(store, *, cap: int = 30, keep_review_days: int = 30) -> list[dict]:
    """Return up to `cap` live memory notes as LLM-ready request dicts.

    Each dict contains:
        {doc_id, title, content, captured_at}
    content is the body after the first blank line, capped at 300 chars.
    keep_review_days controls how long a "keep"-verdicted note stays excluded
    before it resurfaces for reconsideration (see note_chunks).
    """
    chunks = store.note_chunks(observation_type="memory", exclude_distilled=True,
                               keep_review_days=keep_review_days, limit=cap)
    results = []
    for c in chunks:
        text = c["text"] or ""
        meta = c.get("metadata") or {}

        # Split on first double-newline to get body; fall back to full text.
        parts = text.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else text
        content = body[:300]

        results.append({
            "doc_id": c["doc_id"],
            "title": meta.get("title", ""),
            "content": content,
            "captured_at": meta.get("captured_at", ""),
        })
    return results
```

In `drain_distil`, change the three `patch_chunk_metadata` calls. The `keep` branch currently
reads:

```python
        if verdict == "keep":
            # Stamp distilled_at even on a no-op verdict: nothing about an
            # unchanged note will produce a different answer on the next
            # distil run, so re-asking Haiku about it forever is pure
            # recurring cost. patch_chunk_metadata's own existence guard
            # (returns False, harmlessly) covers a doc_id gone stale between
            # listing and draining, so no separate get_chunk check is needed
            # for this branch.
            store.patch_chunk_metadata(doc_id, distilled_at=now)
            continue
```

becomes:

```python
        if verdict == "keep":
            # Stamp distilled_at + distilled_verdict="keep" even on a no-op
            # verdict: nothing about an unchanged note will produce a
            # different answer on the next distil run, so re-asking Haiku
            # about it forever is pure recurring cost. This is a deferral,
            # not a decision — note_chunks re-includes it once distilled_at
            # is stale (see keep_review_days). patch_chunk_metadata's own
            # existence guard (returns False, harmlessly) covers a doc_id
            # gone stale between listing and draining, so no separate
            # get_chunk check is needed for this branch.
            store.patch_chunk_metadata(doc_id, distilled_at=now, distilled_verdict="keep")
            continue
```

The `expire` branch currently reads:

```python
        if verdict == "expire":
            ok = store.patch_chunk_metadata(doc_id, expired=True, distilled_at=now)
```

becomes:

```python
        if verdict == "expire":
            ok = store.patch_chunk_metadata(doc_id, expired=True, distilled_at=now,
                                            distilled_verdict="expire")
```

The `promote` branch currently reads:

```python
            ok = store.patch_chunk_metadata(doc_id, distilled_at=now)
            if ok:
                promoted_count += 1
```

becomes:

```python
            ok = store.patch_chunk_metadata(doc_id, distilled_at=now, distilled_verdict="promote")
            if ok:
                promoted_count += 1
```

- [ ] **Step 6: Run the `memory_distil` tests**

Run: `pytest tests/test_memory_distil.py -v -p no:xdist`
Expected: all PASS, including the pre-existing `test_drain_expires_and_promotes` (unmodified —
its assertions don't touch `distilled_verdict` and remain valid).

- [ ] **Step 7: Run the full impacted suites**

Run: `pytest tests/test_store_schema_p3.py tests/test_memory_distil.py tests/test_memory_index.py -q`
Expected: PASS. (`test_memory_index.py` covers `memory_index.py`'s `note_chunks` call, which
passes neither `exclude_distilled` nor `keep_review_days` and must be completely unaffected.)

- [ ] **Step 8: Lint**

Run: `ruff check mcpbrain/store.py mcpbrain/memory_distil.py tests/test_store_schema_p3.py tests/test_memory_distil.py`
Expected: `All checks passed!`

- [ ] **Step 9: Commit**

```bash
git add mcpbrain/store.py mcpbrain/memory_distil.py tests/test_store_schema_p3.py tests/test_memory_distil.py
git commit -m "fix(memory_distil): time-box the keep verdict, not just expire/promote

distilled_at's permanence is correct for expire (a genuine 'stale'
decision) and promote (became a real memory file), but keep is a
deferral, not a decision -- left permanent, nearly every note gets
looked at exactly once, at its freshest and least-informative moment,
and the live memory-note set grows unbounded.

Add distilled_verdict alongside distilled_at so note_chunks can tell
them apart: a keep-verdicted note re-surfaces once its stamp is older
than keep_review_days (default 30, matching this codebase's existing
staleness-threshold convention); expire/promote stay permanent."
```

---

## Done criteria

- `pytest tests/test_store_schema_p3.py tests/test_memory_distil.py tests/test_memory_index.py -q` passes.
- `ruff check mcpbrain/store.py mcpbrain/memory_distil.py tests/test_store_schema_p3.py tests/test_memory_distil.py` is clean.
- One commit on `main`, unpushed.
- Report to Josh so he can run the full suite, then decide separately about restarting the local daemon and about releasing.

## Deliberately not in this plan

- A `config.py` getter / fleet flag for `keep_review_days` (spec: out of scope — no one has
  asked for this to vary per install).
- Any change to `expire`/`promote`'s permanence.
- Any version bump or release step.
