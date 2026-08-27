# Enrichment Pipeline Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut enrichment input tokens ~87% (11.0M → ~1.4M on the current queue) by scoping per-unit context, and make it impossible for a document to enter the queue in a state that cannot be processed.

**Architecture:** Three workstreams. W3 adds a salience rule so junk Drive HTML is cold-marked (reversible, stays searchable). W2 makes over-long messages splittable at chunk seams with part-precise marking, and chunks captured notes. W1 replaces the shared 45KB `context.json` with a per-unit scoped context written into each unit file, which also un-collapses the packing budget. Order is W3 → W2 → W1: W1's budget assumes nothing unsplittable can enter the queue.

**Tech Stack:** Python 3, SQLite (`mcpbrain/store.py`), pytest, no external model API (Claude Code subagents only).

**Spec:** `docs/superpowers/specs/2026-08-27-enrichment-efficiency-design.md`

## Global Constraints

- **Nothing is truncated.** Splitting is lossless; concatenated parts must equal the original text. Cold-marking is the only "don't extract this" lever, and it is reversible (`store.set_enrich_state(ids, "")`).
- **Cold ≠ deleted.** Cold chunks stay `embedded=1`, in FTS, and in recall. Never couple cold-marking to retrieval.
- **`_unit_payload` stays pure file I/O.** No store access on the claim path.
- **Message metadata is system-owned.** `messages[]` and `part_doc_ids` are read from the unit file, never from the model's echo.
- **Sweeps are attended.** Dry-run default, `--yes` gate, daemon stopped, backup verified. Nothing in the daemon's cadences may call them.
- **Gold floor:** `recall@10 ≥ 0.780 / MRR ≥ 0.550` via `uv run python tests/eval/run_eval.py --gold --k 10`.
- **Constants:** `CONTEXT_CAP = 8_000`; `_CORE_CAP = 40`; `unit_pull_cap = 60_000`; `_PULL_SOFT_LIMIT = 50_000`; `SPOOL_CHAR_BUDGET = 24_000`.
- **Scoped test runs.** Run only the edited + directly impacted test files; Josh runs the full suite himself.

---

# W3 — Salience ceiling

### Task 1: Cold-mark Drive `text/html`

**Files:**
- Modify: `mcpbrain/prepare.py` (`_COLD_DRIVE_MIMES`, ~line 248)
- Test: `tests/test_prepare.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `prepare.should_enrich(chunk) -> bool` now returns `False` for a Drive chunk whose `metadata.mime_type` is `text/html`.

- [ ] **Step 1: Write the failing test**

```python
def test_should_enrich_cold_marks_drive_html():
    """Drive text/html is a saved web page, not a document. On the live store it
    was exactly two files (5.07MB) — a SHEIN shop page and a Bookabin payment
    page. Cold is reversible and keeps them searchable."""
    from mcpbrain.prepare import should_enrich
    chunk = {"text": "x" * 5000,
             "metadata": {"source_type": "gdrive", "file_id": "f1",
                          "mime_type": "text/html", "content_subtype": "prose"}}
    assert should_enrich(chunk) is False


def test_should_enrich_keeps_drive_pdf():
    """Guard against over-broadening: a long PDF is still extracted."""
    from mcpbrain.prepare import should_enrich
    chunk = {"text": "x" * 5000,
             "metadata": {"source_type": "gdrive", "file_id": "f2",
                          "mime_type": "application/pdf", "content_subtype": "prose"}}
    assert should_enrich(chunk) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prepare.py -k drive_html -v`
Expected: FAIL — `assert True is False`

- [ ] **Step 3: Add the mime**

In `mcpbrain/prepare.py`, add to `_COLD_DRIVE_MIMES`:

```python
    "text/html",
    # Drive text/html is an UPLOADED .html file — a saved web page, never an
    # authored document (a Google Doc has its own mime). On the live store this
    # was exactly two files totalling 5.07MB: a 4.95MB SHEIN shop page and a
    # Bookabin payment page. The shop page alone was 2,904 hot chunks and formed
    # a 5,075,515-byte work unit no drainer could hold, re-produced every spool
    # cycle. Cold is reversible: the chunks stay embedded, in FTS, and in recall.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_prepare.py -k "drive_html or drive_pdf" -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/prepare.py tests/test_prepare.py
git commit -m "fix(salience): cold-mark Drive text/html saved web pages"
```

---

### Task 2: `bin/resalience.py` — re-apply the salience gate to existing chunks

**Files:**
- Create: `bin/resalience.py`
- Test: `tests/test_resalience.py`

**Interfaces:**
- Consumes: `prepare.should_enrich(chunk) -> bool`; `store.set_enrich_state(doc_ids: list[str], state: str) -> None`.
- Produces: `resalience.scan(store) -> list[str]` (doc_ids that now fail the gate); `resalience.apply(store, doc_ids: list[str]) -> int`.

Generalised rather than hardcoded to HTML, so the next gate change needs no new script.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3
from bin import resalience


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.cold = []

    def iter_hot_chunks(self):
        return iter(self.rows)

    def set_enrich_state(self, doc_ids, state):
        assert state == "cold"
        self.cold.extend(doc_ids)


def test_scan_finds_chunks_that_now_fail_the_gate():
    rows = [
        {"doc_id": "a", "text": "x" * 5000,
         "metadata": {"source_type": "gdrive", "file_id": "f", "mime_type": "text/html"}},
        {"doc_id": "b", "text": "x" * 5000,
         "metadata": {"source_type": "gdrive", "file_id": "g", "mime_type": "application/pdf"}},
    ]
    assert resalience.scan(FakeStore(rows)) == ["a"]


def test_apply_cold_marks_and_returns_count():
    store = FakeStore([])
    assert resalience.apply(store, ["a", "b"]) == 2
    assert store.cold == ["a", "b"]


def test_apply_is_a_noop_on_empty():
    store = FakeStore([])
    assert resalience.apply(store, []) == 0
    assert store.cold == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resalience.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.resalience'`

- [ ] **Step 3: Add `store.iter_hot_chunks`**

In `mcpbrain/store.py`, beside `unenriched_chunks`:

```python
    def iter_hot_chunks(self):
        """Yield every non-cold chunk as {doc_id, text, metadata}, streaming.

        Feeds bin/resalience.py, which re-applies prepare.should_enrich to the
        existing corpus after a gate change. Streams rather than materialising:
        the live store is ~109k non-cold rows and this is an attended,
        daemon-stopped sweep, not a cycle pass.
        """
        with self._connect() as db:
            for r in db.execute(
                "SELECT doc_id, text, metadata FROM chunks "
                "WHERE COALESCE(enrich_state,'') != 'cold' ORDER BY rowid"
            ):
                try:
                    meta = json.loads(r["metadata"])
                except (TypeError, ValueError):
                    meta = {}
                yield {"doc_id": r["doc_id"], "text": r["text"], "metadata": meta}
```

- [ ] **Step 4: Write `bin/resalience.py`**

```python
#!/usr/bin/env python3
"""Re-apply prepare.should_enrich to the existing corpus after a gate change.

ATTENDED ONLY. Dry-run by default; --yes is required to write. Stop the daemon
first (single-writer invariant) and confirm a recent verified backup. Nothing in
the daemon's cadences calls this — same posture as bin/consolidate.py.

Cold-marking is REVERSIBLE and is not deletion: a cold chunk stays embedded, in
FTS, and in recall (recall_excludes_cold is off). Undo with
store.set_enrich_state(doc_ids, "").
"""
import argparse
import sys

from mcpbrain import config
from mcpbrain.prepare import should_enrich
from mcpbrain.store import Store

_BATCH = 500


def scan(store) -> list[str]:
    """doc_ids of non-cold chunks that no longer pass the salience gate."""
    return [c["doc_id"] for c in store.iter_hot_chunks() if not should_enrich(c)]


def apply(store, doc_ids: list[str]) -> int:
    """Cold-mark doc_ids in batches. Returns the number marked."""
    if not doc_ids:
        return 0
    for i in range(0, len(doc_ids), _BATCH):
        store.set_enrich_state(doc_ids[i:i + _BATCH], "cold")
    return len(doc_ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args(argv)

    store = Store(str(config.app_dir()))
    doc_ids = scan(store)
    print(f"[resalience] {len(doc_ids)} non-cold chunk(s) now fail the gate")
    if not doc_ids:
        return 0
    if not args.yes:
        print("[resalience] DRY RUN — re-run with --yes to cold-mark them")
        print(f"[resalience] first 10: {doc_ids[:10]}")
        return 0
    n = apply(store, doc_ids)
    print(f"[resalience] cold-marked {n} chunk(s)")
    print("[resalience] reverse with store.set_enrich_state(doc_ids, '')")
    print("[resalience] now run: uv run python tests/eval/run_eval.py --gold --k 10")
    print("[resalience] floor: recall@10 >= 0.780 / MRR >= 0.550")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_resalience.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add bin/resalience.py tests/test_resalience.py mcpbrain/store.py
git commit -m "feat(salience): bin/resalience.py re-applies should_enrich to the corpus"
```

---

# W2 — Oversize content

### Task 3: `reassemble_thread` carries chunk provenance

**Files:**
- Modify: `mcpbrain/thread_enrich.py` (`_join_with_gaps`, `reassemble_thread`)
- Test: `tests/test_thread_enrich.py`

**Interfaces:**
- Consumes: nothing.
- Produces: each message dict from `reassemble_thread` gains `chunk_doc_ids: list[str]` — the doc_ids of its chunks, in `chunk_index` order, parallel to the text pieces.

- [ ] **Step 1: Write the failing test**

```python
def test_reassemble_thread_carries_chunk_doc_ids_in_order():
    """A message body IS a join of chunks. Splitting it back at those seams is
    what lets a part be marked against exactly the chunks it covered."""
    from mcpbrain.thread_enrich import reassemble_thread
    chunks = [
        {"doc_id": "gdrive-f-1", "text": "second",
         "metadata": {"file_id": "f", "chunk_index": 1, "chunk_total": 2}},
        {"doc_id": "gdrive-f-0", "text": "first",
         "metadata": {"file_id": "f", "chunk_index": 0, "chunk_total": 2}},
    ]
    msgs = reassemble_thread(chunks)
    assert len(msgs) == 1
    assert msgs[0]["chunk_doc_ids"] == ["gdrive-f-0", "gdrive-f-1"]
    assert msgs[0]["text"] == "first\n\nsecond"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_thread_enrich.py -k chunk_doc_ids -v`
Expected: FAIL — `KeyError: 'chunk_doc_ids'`

- [ ] **Step 3: Emit the doc_ids**

In `mcpbrain/thread_enrich.py`, inside `reassemble_thread`'s per-group loop, after `text = _join_with_gaps(parts)`:

```python
        # Chunk-level provenance, ordered exactly as _join_with_gaps consumed
        # the pieces. prepare._split_long_thread splits an over-long message at
        # these seams and carries the covered ids as part_doc_ids, so drain can
        # mark exactly the chunks a part covered instead of the whole document.
        chunk_doc_ids = [p["doc_id"] for p in parts]
```

and add `"chunk_doc_ids": chunk_doc_ids,` to the appended message dict.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_thread_enrich.py -v`
Expected: PASS (all existing tests still pass — this is additive)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/thread_enrich.py tests/test_thread_enrich.py
git commit -m "feat(enrich): reassemble_thread carries per-message chunk_doc_ids"
```

---

### Task 4: Split over-long messages at chunk seams

**Files:**
- Modify: `mcpbrain/prepare.py` (`_split_long_thread`, ~line 608)
- Test: `tests/test_prepare.py`

**Interfaces:**
- Consumes: `message["chunk_doc_ids"]` from Task 3.
- Produces: each part dict from `_split_long_thread` gains `part_doc_ids: list[str]`. Parts still carry `part`/`of`/`thread_id`/`messages` as before.

- [ ] **Step 1: Write the failing test**

```python
def test_split_long_thread_splits_within_a_single_message():
    """A Drive doc is ONE message, so the old between-messages split could not
    touch it — it logged a warning and shipped a 5MB unit no drainer could hold.
    Splitting at chunk seams is lossless: the parts concatenate to the original."""
    from mcpbrain.prepare import _split_long_thread
    pieces = [f"chunk{i} " + "x" * 90 for i in range(10)]
    block = {
        "thread_id": "f", "prior_thread_context": "", "open_actions": [],
        "org_hint": "",
        "messages": [{"message_id": "f", "sender": "", "date": "", "labels": "",
                      "subject": "doc.pdf", "text": "\n\n".join(pieces),
                      "chunk_doc_ids": [f"gdrive-f-{i}" for i in range(10)]}],
    }
    parts = _split_long_thread(block, 300)
    assert len(parts) > 1
    assert [p["part"] for p in parts] == list(range(1, len(parts) + 1))
    assert all(p["of"] == len(parts) for p in parts)
    # Lossless: every chunk appears exactly once, in order.
    covered = [d for p in parts for d in p["part_doc_ids"]]
    assert covered == [f"gdrive-f-{i}" for i in range(10)]
    # And the text survives intact.
    rejoined = "\n\n".join(m["text"] for p in parts for m in p["messages"])
    assert rejoined == "\n\n".join(pieces)


def test_split_long_thread_short_message_is_untouched():
    from mcpbrain.prepare import _split_long_thread
    block = {"thread_id": "t", "prior_thread_context": "", "open_actions": [],
             "org_hint": "",
             "messages": [{"message_id": "m1", "text": "short",
                           "chunk_doc_ids": ["gmail-m1-0"]}]}
    assert _split_long_thread(block, 24000) == [block]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prepare.py -k split_long_thread -v`
Expected: FAIL — `assert 1 > 1` (the single message ships unsplit)

- [ ] **Step 3: Implement seam-splitting**

In `mcpbrain/prepare.py`, add above `_split_long_thread`:

```python
def _split_message_at_seams(msg: dict, char_budget: int) -> list[dict]:
    """Split ONE over-long message into pieces at its chunk boundaries.

    A message body is a join of chunks (thread_enrich.reassemble_thread), so it
    splits back losslessly at those same seams — no truncation, and each piece
    knows exactly which chunks it covers (`chunk_doc_ids`), which is what lets
    drain mark part-precisely instead of marking a whole Drive document off the
    first part.

    chunking.chunk_text bounds chunks at ~1800 chars, so any budget >= that is
    reachable. A message with no chunk_doc_ids (a pre-Task-3 unit, or a store
    row written before notes were chunked) cannot be split and is returned
    whole — the caller's existing over-budget warning still fires, and the
    claim-time attempt cap bounds the retry loop.
    """
    ids = msg.get("chunk_doc_ids") or []
    if len(ids) <= 1:
        return [msg]
    pieces = msg.get("text", "").split(_CHUNK_JOIN)
    if len(pieces) != len(ids):
        # A gap marker was inserted (a partially-enriched/cold document), so the
        # text pieces no longer align 1:1 with the ids. Splitting here would
        # mis-attribute chunks, so ship whole rather than mark the wrong rows.
        return [msg]
    out, cur_txt, cur_ids = [], [], []
    for piece, did in zip(pieces, ids):
        projected = sum(len(t) for t in cur_txt) + len(cur_txt) * 2 + len(piece)
        if cur_txt and projected > char_budget:
            out.append({**msg, "text": _CHUNK_JOIN.join(cur_txt),
                        "chunk_doc_ids": cur_ids})
            cur_txt, cur_ids = [], []
        cur_txt.append(piece)
        cur_ids.append(did)
    if cur_txt:
        out.append({**msg, "text": _CHUNK_JOIN.join(cur_txt),
                    "chunk_doc_ids": cur_ids})
    return out
```

Add near the imports in `prepare.py`:

```python
from mcpbrain.thread_enrich import _CHUNK_JOIN
```

Then in `_split_long_thread`, replace the single-message early return:

```python
    if len(messages) <= 1:
```

with seam-splitting, and stamp `part_doc_ids` on every emitted part. Replace the whole body after the `total <= char_budget` check with:

```python
    # Expand any over-long message into seam-split pieces FIRST, so a
    # single-message thread (every Drive document, every captured note) is
    # splittable at all. This is the fix for the 5,075,515-byte unit.
    expanded = []
    for m in messages:
        if len(m.get("text", "")) > char_budget:
            pieces = _split_message_at_seams(m, char_budget)
            if len(pieces) == 1:
                log.warning("prepare: thread %s has an unsplittable message of "
                            "%d chars, over the %d budget; shipping whole",
                            block.get("thread_id"), len(m.get("text", "")),
                            char_budget)
            expanded.extend(pieces)
        else:
            expanded.append(m)

    groups, current, current_chars = [], [], 0
    for m in expanded:
        size = len(m.get("text", ""))
        if current and current_chars + size > char_budget:
            groups.append(current)
            current, current_chars = [], 0
        current.append(m)
        current_chars += size
    if current:
        groups.append(current)

    if len(groups) <= 1:
        return [block]

    k = len(groups)
    parts = []
    for i, group in enumerate(groups, start=1):
        parts.append({
            "thread_id": block["thread_id"],
            "prior_thread_context": block["prior_thread_context"],
            "open_actions": block["open_actions"],
            "org_hint": block.get("org_hint", ""),
            "part": i,
            "of": k,
            # Exactly the chunks this part's text covers. drain prefers this
            # over doc_ids_for_messages, which for a Drive doc resolves the
            # file_id to EVERY chunk of the document.
            "part_doc_ids": [d for m in group for d in (m.get("chunk_doc_ids") or [])],
            "messages": group,
        })
    return parts
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_prepare.py -k "split_long_thread or split_message" -v`
Expected: PASS

- [ ] **Step 5: Run the full prepare + thread_enrich suites for regressions**

Run: `uv run pytest tests/test_prepare.py tests/test_thread_enrich.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/prepare.py tests/test_prepare.py
git commit -m "fix(enrich): split over-long messages at chunk seams, losslessly"
```

---

### Task 5: Part-precise marking in drain

**Files:**
- Modify: `mcpbrain/drain.py` (`_regroup_parts` ~line 246; the doc_ids resolve ~line 485)
- Test: `tests/test_drain.py`

**Interfaces:**
- Consumes: `part_doc_ids` on a part (Task 4), read from the unit file.
- Produces: `drain` marks only a part's own chunks. `_regroup_parts` unions `part_doc_ids` when merging.

Without this, part 1 of a Drive document marks the whole file enriched and parts 2..N are wasted.

- [ ] **Step 1: Write the failing test**

```python
def test_regroup_parts_unions_part_doc_ids():
    from mcpbrain.drain import _regroup_parts
    out = _regroup_parts([
        {"thread_id": "f", "part": 1, "of": 2, "part_doc_ids": ["a", "b"],
         "messages": [{"message_id": "f"}]},
        {"thread_id": "f", "part": 2, "of": 2, "part_doc_ids": ["c"],
         "messages": [{"message_id": "f"}]},
    ])
    assert len(out) == 1
    assert out[0]["part_doc_ids"] == ["a", "b", "c"]
    assert "part" not in out[0]


def test_drain_marks_only_the_parts_chunks(tmp_path, monkeypatch):
    """A Drive file_id resolves to EVERY chunk of the document. A part must mark
    only the chunks it covered, or part 1 consumes the whole file."""
    from mcpbrain import drain as drain_mod

    class FakeStore:
        def doc_ids_for_messages(self, mids):
            raise AssertionError("part_doc_ids must win over the file-wide resolve")
        def drop_cold(self, ids):
            return ids

    ext = {"thread_id": "f", "part_doc_ids": ["a", "b"],
           "messages": [{"message_id": "f"}]}
    resolved = drain_mod._resolve_doc_ids(FakeStore(), ext, {})
    assert resolved == ["a", "b"]


def test_resolve_doc_ids_falls_back_when_no_part_ids():
    from mcpbrain import drain as drain_mod

    class FakeStore:
        def doc_ids_for_messages(self, mids):
            return ["a", "b", "c", "d"]
        def drop_cold(self, ids):
            return ids

    ext = {"thread_id": "f", "messages": [{"message_id": "f"}]}
    assert drain_mod._resolve_doc_ids(FakeStore(), ext, {}) == ["a", "b", "c", "d"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drain.py -k "part_doc_ids or resolve_doc_ids" -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.drain' has no attribute '_resolve_doc_ids'`

- [ ] **Step 3: Union in `_regroup_parts`**

In `mcpbrain/drain.py`, inside `_regroup_parts`, after `merged["messages"] = messages`:

```python
        # Union the parts' chunk provenance so the merged extraction marks every
        # chunk its combined text covered. Only parts that landed in the SAME
        # inbox file reach here; parts split across units drain separately and
        # each marks its own chunks, which is correct by construction.
        part_ids = [d for p in ordered for d in (p.get("part_doc_ids") or [])]
        if part_ids:
            merged["part_doc_ids"] = part_ids
```

And downgrade the `of`-mismatch warning — parts split across units are independently applicable by design, not evidence of a dropped part. Replace the `if of and len(ordered) != of:` warning body with:

```python
        if of and len(ordered) != of:
            independent = all(p.get("part_doc_ids") for p in ordered)
            (log.info if independent else log.warning)(
                "drain: thread %s received %d parts but declared of=%d; %s",
                tid, len(ordered), of,
                "parts are independently applicable (part_doc_ids present)"
                if independent else "applying incomplete thread")
```

- [ ] **Step 4: Extract `_resolve_doc_ids`**

Add to `mcpbrain/drain.py`, above `drain()`:

```python
def _resolve_doc_ids(store, extraction: dict, unit_messages_by_thread: dict) -> list[str]:
    """The chunks this extraction covers, cold-filtered, ready for apply/mark.

    Precedence:
      1. `part_doc_ids` — exactly the chunks a seam-split part covered. System-
         owned (prepare writes it into the unit; drain reads it from there, never
         from the model's echo). Required for correctness on Drive documents,
         where doc_ids_for_messages resolves a file_id to EVERY chunk of the file
         — so without this, part 1 marks the whole document and parts 2..N are
         wasted.
      2. the model's message ids.
      3. the unit's canonical message ids, when the model echoed bad ones.

    drop_cold applies to every branch: a file-wide resolve returns cold chunks the
    extraction never covered (the 0.7.103 fix).
    """
    part_ids = extraction.get("part_doc_ids")
    if part_ids:
        return store.drop_cold(list(part_ids))
    msg_ids = [m.get("message_id") for m in extraction.get("messages", [])
               if m.get("message_id")]
    doc_ids = store.doc_ids_for_messages(msg_ids) if msg_ids else []
    if not doc_ids:
        _u = unit_messages_by_thread.get(extraction.get("thread_id")) or []
        _umids = [m.get("message_id") for m in _u if m.get("message_id")]
        doc_ids = store.doc_ids_for_messages(_umids) if _umids else []
    return store.drop_cold(doc_ids) if doc_ids else []
```

Then in `drain()`, replace the inline resolve block (the `msg_ids = …` through `doc_ids = store.drop_cold(doc_ids)` sequence) with:

```python
                doc_ids = _resolve_doc_ids(store, extraction, unit_messages_by_thread)
                if not doc_ids:
```

leaving the existing "matched no chunk" give-up branch that follows unchanged.

- [ ] **Step 5: Inject `part_doc_ids` from the unit, like `messages`**

In `drain()`, where the unit file is read into `unit_messages_by_thread`, also capture part ids. Change that loop to:

```python
                    for t in (unit_data.get("threads") or []):
                        tid = t.get("thread_id")
                        msgs = t.get("messages")
                        if tid and isinstance(msgs, list) and msgs:
                            unit_messages_by_thread[tid] = msgs
                        if tid and t.get("part_doc_ids"):
                            # System-owned, exactly like messages[]: accumulate
                            # across this unit's parts of the same thread.
                            unit_part_ids_by_thread.setdefault(tid, []).extend(
                                t["part_doc_ids"])
```

Declare `unit_part_ids_by_thread: dict = {}` beside `unit_messages_by_thread`, and inject before validation, beside the existing `messages` injection:

```python
                if not extraction.get("part_doc_ids") and unit_part_ids_by_thread.get(extraction.get("thread_id")):
                    extraction["part_doc_ids"] = unit_part_ids_by_thread[extraction["thread_id"]]
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_drain.py tests/test_drain_giveup.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/drain.py tests/test_drain.py
git commit -m "fix(drain): mark only the chunks a seam-split part covered"
```

---

### Task 6: Claim-time attempt bump

**Files:**
- Modify: `mcpbrain/tools.py` (`brain_enrich_claim`, ~line 1282)
- Test: `tests/test_mcp_enrich_meeting_tools.py`

**Interfaces:**
- Consumes: `store.bump_enrich_attempts(doc_ids) -> int`.
- Produces: no signature change; a claimed unit's chunks accrue an attempt.

`_give_up_or_bump` currently fires only *on push*, so a unit too large to process never increments and never gives up. Task 4 should make that unreachable; this makes it non-fatal if Task 4 is ever wrong.

- [ ] **Step 1: Write the failing test**

```python
def test_bump_unit_attempts_bumps_every_part_doc_id(tmp_path, monkeypatch):
    """A unit no drainer can process never reaches push, so the push-side
    give-up never fires and it re-queues forever. Bumping on CLAIM bounds it."""
    from mcpbrain import tools
    seen = []

    class FakeStore:
        def __init__(self, home):
            pass

        def bump_enrich_attempts(self, ids):
            seen.extend(ids)
            return 1

    monkeypatch.setattr("mcpbrain.store.Store", FakeStore)
    tools._bump_unit_attempts(str(tmp_path), {
        "unit_id": "u-1", "kind": "thread",
        "threads": [{"thread_id": "t1", "part_doc_ids": ["a", "b"]},
                    {"thread_id": "t2", "part_doc_ids": ["c"]}]})
    assert seen == ["a", "b", "c"]


def test_bump_unit_attempts_is_a_noop_without_part_ids(tmp_path, monkeypatch):
    from mcpbrain import tools

    class Boom:
        def __init__(self, home):
            raise AssertionError("must not touch the store with no ids")

    monkeypatch.setattr("mcpbrain.store.Store", Boom)
    tools._bump_unit_attempts(str(tmp_path), {"unit_id": "u-1", "threads": []})


def test_bump_unit_attempts_never_raises(tmp_path, monkeypatch):
    """It runs on the claim hot path and must never fail a claim."""
    from mcpbrain import tools

    class Boom:
        def __init__(self, home):
            raise RuntimeError("store down")

    monkeypatch.setattr("mcpbrain.store.Store", Boom)
    tools._bump_unit_attempts(str(tmp_path), {
        "threads": [{"part_doc_ids": ["a"]}]})   # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_enrich_meeting_tools.py -k bump_unit_attempts -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.tools' has no attribute '_bump_unit_attempts'`

- [ ] **Step 3: Implement the bump**

Add to `mcpbrain/tools.py`, beside `_unit_payload`:

```python
def _bump_unit_attempts(home, d: dict) -> None:
    """Bump the extraction-attempt counter for a claimed unit's chunks.

    drain._give_up_or_bump only fires on PUSH, so a unit no drainer can process
    (too large to hold, malformed) never increments and re-queues forever — the
    5,075,515-byte unit's failure mode. Bumping at CLAIM time bounds it: after
    _EMPTY_ATTEMPT_CAP claims the chunks are consumed by the push-side give-up.

    Best-effort and store-optional: this runs on the MCP claim path, which must
    stay cheap and must never fail a claim.
    """
    try:
        ids = [i for t in (d.get("threads") or [])
               for i in (t.get("part_doc_ids") or [])]
        if not ids:
            return
        from mcpbrain.store import Store
        Store(str(home)).bump_enrich_attempts(ids)
    except Exception:  # noqa: BLE001 — bookkeeping must never break a claim
        log.debug("claim: attempt bump failed", exc_info=True)
```

Call it in `brain_enrich_claim` immediately before `return _unit_payload(home, d, uid, with_rules)`:

```python
            _bump_unit_attempts(home, d)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_enrich_meeting_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/tools.py tests/test_mcp_enrich_meeting_tools.py
git commit -m "fix(enrich): bump attempt counter on claim so unprocessable units give up"
```

---

### Task 7: Chunk captured notes

**Files:**
- Modify: `mcpbrain/drain.py` (`drain_captures`, the `kind == "ingest"` branch, ~line 681)
- Test: `tests/test_drain_captures.py`

**Interfaces:**
- Consumes: `chunking.chunk_text(text) -> list[str]`.
- Produces: a note is stored as `note-<hash>` when it fits one chunk, else `note-<hash>-<i>`. Every piece carries `note_id`, `chunk_index`, `chunk_total`.

Follows `consolidation.py`'s established precedent exactly, so 2,109 of 3,299 existing notes need no migration.

- [ ] **Step 1: Write the failing test**

```python
import json


def _capture(tmp_path, title, content):
    """Drop one ingest envelope into the capture inbox and drain it."""
    from mcpbrain.drain import drain_captures
    from mcpbrain.store import Store
    inbox = tmp_path / "capture_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "c1.json").write_text(json.dumps({
        "kind": "ingest", "title": title, "content": content,
        "observation_type": "memory", "tags": "", "org": "",
        "captured_at": "2026-08-27T00:00:00Z"}))
    store = Store(str(tmp_path))
    drain_captures(store, home=str(tmp_path))
    return store


def test_short_note_stays_one_chunk_with_bare_doc_id(tmp_path):
    """The common case — 2,109 of 3,299 live notes — must not change shape."""
    store = _capture(tmp_path, "T", "a short body")
    rows = store.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert "-" not in rows[0]["doc_id"].removeprefix("note-")
    assert rows[0]["metadata"]["note_id"] == rows[0]["doc_id"]
    assert rows[0]["metadata"]["chunk_total"] == 1


def test_long_note_is_chunked_with_suffixed_doc_ids(tmp_path):
    """Notes bypassed chunk_text entirely, so only the first ~2,000 chars of a
    133,791-char note were ever embedded. 1,192 live notes / 21.1MB are affected."""
    body = "\n\n".join(f"para {i} " + "y" * 400 for i in range(30))
    store = _capture(tmp_path, "T", body)
    with store._connect() as db:
        ids = [r[0] for r in db.execute(
            "SELECT doc_id FROM chunks WHERE doc_id LIKE 'note-%' ORDER BY doc_id")]
    assert len(ids) > 1
    assert all(i.rsplit("-", 1)[1].isdigit() for i in ids)
    base = ids[0].rsplit("-", 1)[0]
    # Lossless: note_chunks reassembles the original body verbatim.
    rows = store.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == base
    assert rows[0]["text"] == f"T\n\n{body}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_drain_captures.py -k note -v`
Expected: FAIL — one row is written with the whole body

- [ ] **Step 3: Chunk on the ingest path**

In `mcpbrain/drain.py`, replace the `kind == "ingest"` body:

```python
            if kind == "ingest":
                text = f"{env['title'].strip()}\n\n{env['content'].strip()}"
                chash = content_hash(text)
                base_doc_id = f"note-{chash[:32]}"
                base_meta = {"source": "note", "title": env["title"],
                             "observation_type": env.get("observation_type", "note"),
                             "tags": env.get("tags", ""),
                             "org": env.get("org", ""),
                             "captured_at": env.get("captured_at", ""),
                             "note_id": base_doc_id}
                try:
                    # Notes used to bypass chunk_text entirely: one row per note,
                    # up to 133,791 chars, of which only the first ~2,000 were
                    # ever embedded (the BGE window). Same shape as
                    # consolidation.write_consolidated_note: a note that fits one
                    # chunk keeps the BARE id, so the common case needs no
                    # migration. chash stays the FULL-note hash on every piece, so
                    # re-capturing identical content is still a no-op.
                    pieces = chunk_text(text)
                    changed = False
                    for i, piece in enumerate(pieces):
                        doc_id = (base_doc_id if len(pieces) == 1
                                  else f"{base_doc_id}-{i}")
                        meta = {**base_meta, "chunk_index": i,
                                "chunk_total": len(pieces)}
                        if store.upsert_chunk(doc_id, piece, chash, meta):
                            changed = True
                    if changed:
                        store.record_change("capture_ingest", ref_id=base_doc_id,
                                            summary=f"Saved note '{env['title'][:60]}'")
                        applied += 1
                except Exception as exc:
                    log.error("capture: ingest failed for %s: %s", path.name, exc)
                    file_ok = False
```

Add `chunk_text` to the existing `mcpbrain.chunking` import in `drain.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_drain_captures.py tests/test_capture_writer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/drain.py tests/test_drain_captures.py
git commit -m "fix(capture): chunk notes so past ~2,000 chars is embedded"
```

---

### Task 8: `note_chunks` groups by `note_id`

**Files:**
- Modify: `mcpbrain/store.py` (`note_chunks`, ~line 2559)
- Test: `tests/test_measure_store.py` or a new `tests/test_note_chunks.py`

**Interfaces:**
- Consumes: `note_id`/`chunk_index` metadata from Task 7.
- Produces: `note_chunks()` returns ONE row per note — `doc_id` = base id, `text` = reassembled in `chunk_index` order. Unchanged for legacy single-chunk notes.

Without this, `memory_index` and `memory_distil` see fragments of one note as separate notes.

- [ ] **Step 1: Write the failing test**

```python
def test_note_chunks_reassembles_a_multi_chunk_note(tmp_path):
    from mcpbrain.store import Store
    s = Store(str(tmp_path))
    base = "note-abc"
    for i, piece in enumerate(["first", "second", "third"]):
        s.upsert_chunk(f"{base}-{i}", piece, "h",
                       {"source": "note", "observation_type": "memory",
                        "title": "T", "note_id": base,
                        "chunk_index": i, "chunk_total": 3})
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == base
    assert rows[0]["text"] == "first\n\nsecond\n\nthird"


def test_note_chunks_legacy_single_chunk_note_unchanged(tmp_path):
    from mcpbrain.store import Store
    s = Store(str(tmp_path))
    s.upsert_chunk("note-xyz", "body", "h",
                   {"source": "note", "observation_type": "memory", "title": "T"})
    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "note-xyz"
    assert rows[0]["text"] == "body"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_note_chunks.py -v`
Expected: FAIL — `assert 3 == 1`

- [ ] **Step 3: Group and reassemble**

In `mcpbrain/store.py`, at the end of `note_chunks`, before returning, fold the collected rows:

```python
        # Group multi-chunk notes back into ONE row per note. A note is the unit
        # memory_index (120-char hook) and memory_distil (doc_id-keyed verdicts)
        # operate on; returning fragments would make one note look like N notes.
        # Legacy rows carry no note_id and group to themselves, unchanged.
        grouped: dict[str, dict] = {}
        for r in results:
            nid = r["metadata"].get("note_id") or r["doc_id"]
            g = grouped.get(nid)
            if g is None:
                grouped[nid] = {"doc_id": nid, "metadata": r["metadata"],
                                "_parts": [(r["metadata"].get("chunk_index", 0), r["text"])]}
            else:
                g["_parts"].append((r["metadata"].get("chunk_index", 0), r["text"]))
        out = []
        for g in grouped.values():
            parts = sorted(g["_parts"], key=lambda p: p[0])
            out.append({"doc_id": g["doc_id"], "metadata": g["metadata"],
                        "text": "\n\n".join(t for _, t in parts)})
        return out
```

Note the `limit` must be applied to GROUPED notes, not raw rows — move the existing live-row counter to count distinct `note_id`s so a chunked note does not consume N of the budget.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_note_chunks.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py tests/test_note_chunks.py
git commit -m "fix(store): note_chunks returns one row per note, not per chunk"
```

---

### Task 9: `patch_note_metadata` for distil verdicts

**Files:**
- Modify: `mcpbrain/store.py` (beside `patch_chunk_metadata`), `mcpbrain/memory_distil.py` (lines 95, 105, 136)
- Test: `tests/test_note_chunks.py`

**Interfaces:**
- Consumes: `note_id` metadata.
- Produces: `store.patch_note_metadata(note_id: str, **patch) -> bool` — patches every sibling chunk; returns True if any row was updated.

`drain_distil` patches a base id that no longer exists as a row, so verdicts would silently no-op and notes would be re-distilled forever.

- [ ] **Step 1: Write the failing test**

```python
def test_patch_note_metadata_stamps_every_sibling(tmp_path):
    from mcpbrain.store import Store
    s = Store(str(tmp_path))
    base = "note-abc"
    for i in range(3):
        s.upsert_chunk(f"{base}-{i}", f"p{i}", "h",
                       {"source": "note", "observation_type": "memory",
                        "note_id": base, "chunk_index": i, "chunk_total": 3})
    assert s.patch_note_metadata(base, distilled_at="2026-08-27", distilled_verdict="keep") is True
    rows = s.note_chunks(observation_type="memory", include_expired=True)
    assert rows[0]["metadata"]["distilled_verdict"] == "keep"


def test_patch_note_metadata_falls_back_to_the_bare_doc_id(tmp_path):
    from mcpbrain.store import Store
    s = Store(str(tmp_path))
    s.upsert_chunk("note-xyz", "body", "h",
                   {"source": "note", "observation_type": "memory"})
    assert s.patch_note_metadata("note-xyz", expired=True) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_note_chunks.py -k patch_note -v`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'patch_note_metadata'`

- [ ] **Step 3: Implement**

In `mcpbrain/store.py`, after `patch_chunk_metadata`:

```python
    def patch_note_metadata(self, note_id: str, **patch) -> bool:
        """Merge kwargs into EVERY chunk of a note. Returns True if any updated.

        A chunked note has no row at its bare `note-<hash>` id — only
        `note-<hash>-<i>` siblings — so memory_distil's patch_chunk_metadata call
        would silently no-op and the note would be re-offered for distillation
        forever. Falls back to the bare doc_id for legacy single-chunk notes.

        These fields (expired / distilled_at / distilled_verdict) are not read by
        _fts_text or contextual_prefix, so this does not add to the known
        patch_chunk_metadata FTS-mirror drift.
        """
        ids = []
        if note_id:
            with self._connect() as db:
                ids = [r["doc_id"] for r in db.execute(
                    "SELECT doc_id FROM chunks WHERE "
                    + _meta_extract("metadata", "note_id") + " = ? ORDER BY doc_id",
                    (note_id,)).fetchall()]
        if not ids:
            ids = [note_id]          # legacy single-chunk note
        return any(self.patch_chunk_metadata(d, **patch) for d in ids)
```

Use the codebase's `_meta_extract` single-source-of-truth helper for the JSON path, per `CLAUDE.md` — never hand-write `json_extract`.

Then in `mcpbrain/memory_distil.py`, change all three call sites from
`store.patch_chunk_metadata(doc_id, …)` to `store.patch_note_metadata(doc_id, …)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_note_chunks.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/store.py mcpbrain/memory_distil.py tests/test_note_chunks.py
git commit -m "fix(memory): distil verdicts stamp every chunk of a note"
```

---

### Task 10: Note re-chunk sweep

**Files:**
- Create: `bin/rechunk_notes.py`
- Test: `tests/test_rechunk_notes.py`

**Interfaces:**
- Consumes: `chunk_text`, `store.note_chunks`, `store.upsert_chunk`, `store.delete_chunks`.
- Produces: `rechunk_notes.plan(store) -> list[dict]`; `rechunk_notes.apply(store, plan) -> int`.

1,192 notes / 21.1MB (96% of note text; 670 of them >24KB holding 16.5MB). 2,109 notes are untouched. **Gold-gated.**

- [ ] **Step 1: Write the failing test**

```python
def test_plan_selects_only_oversize_single_chunk_notes(tmp_path):
    from mcpbrain.store import Store
    from bin import rechunk_notes
    s = Store(str(tmp_path))
    s.upsert_chunk("note-short", "tiny", "h1", {"source": "note", "title": "a"})
    s.upsert_chunk("note-long", "\n\n".join("z" * 500 for _ in range(20)), "h2",
                   {"source": "note", "title": "b"})
    plan = rechunk_notes.plan(s)
    assert [p["note_id"] for p in plan] == ["note-long"]


def test_apply_is_lossless(tmp_path):
    from mcpbrain.store import Store
    from bin import rechunk_notes
    s = Store(str(tmp_path))
    body = "\n\n".join(f"para{i} " + "z" * 500 for i in range(20))
    s.upsert_chunk("note-long", body, "h2", {"source": "note", "title": "b"})
    rechunk_notes.apply(s, rechunk_notes.plan(s))
    rows = s.note_chunks()
    assert len(rows) == 1
    assert rows[0]["text"] == body          # round-trips exactly
    assert rows[0]["doc_id"] == "note-long"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rechunk_notes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.rechunk_notes'`

- [ ] **Step 3: Write the sweep**

```python
#!/usr/bin/env python3
"""Re-chunk oversize captured notes so their whole body is embedded.

ATTENDED ONLY. Dry-run by default; --yes required. Stop the daemon first and
confirm a recent verified backup — this rewrites chunk rows.

Notes bypassed chunk_text (drain_captures wrote one row per note), so only the
first ~2,000 chars of each was ever embedded. On the live store that is 1,192
notes holding 21.1MB — 96% of all note text. 2,109 notes already fit one chunk
and are untouched.

GOLD-GATED: run tests/eval/run_eval.py --gold --k 10 before and after.
Floor: recall@10 >= 0.780 / MRR >= 0.550. It should IMPROVE — 21.1MB currently
has no vector past each note's first ~2,000 chars.
"""
import argparse
import sys

from mcpbrain import config
from mcpbrain.chunking import chunk_text, content_hash
from mcpbrain.store import Store


def plan(store) -> list[dict]:
    """Notes whose body needs more than one chunk and is not already split."""
    out = []
    for row in store.note_chunks(include_expired=True, limit=10 ** 9):
        meta = row["metadata"]
        if meta.get("chunk_total", 1) > 1:
            continue                       # already chunked
        pieces = chunk_text(row["text"])
        if len(pieces) > 1:
            out.append({"note_id": row["doc_id"], "text": row["text"],
                        "metadata": meta, "pieces": pieces})
    return out


def apply(store, items: list[dict]) -> int:
    """Rewrite each planned note as suffixed chunks. Returns notes rewritten."""
    n = 0
    for it in items:
        base, pieces = it["note_id"], it["pieces"]
        chash = content_hash(it["text"])
        base_meta = {**it["metadata"], "note_id": base}
        for i, piece in enumerate(pieces):
            store.upsert_chunk(f"{base}-{i}", piece, chash,
                               {**base_meta, "chunk_index": i,
                                "chunk_total": len(pieces)})
        store.delete_chunks([base])        # the old whole-body row
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    store = Store(str(config.app_dir()))
    items = plan(store)
    chars = sum(len(i["text"]) for i in items)
    print(f"[rechunk] {len(items)} note(s), {chars:,} chars need re-chunking")
    if not items or not args.yes:
        if items:
            print("[rechunk] DRY RUN — re-run with --yes")
        return 0
    print(f"[rechunk] rewrote {apply(store, items)} note(s)")
    print("[rechunk] now run: uv run python tests/eval/run_eval.py --gold --k 10")
    print("[rechunk] floor: recall@10 >= 0.780 / MRR >= 0.550")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

If `store.delete_chunks` does not exist, add it beside `upsert_chunk` as a thin
`DELETE FROM chunks WHERE doc_id IN (…)` that also clears the FTS mirror, matching
the existing retention-sweep delete path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rechunk_notes.py tests/test_note_chunks.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add bin/rechunk_notes.py tests/test_rechunk_notes.py mcpbrain/store.py
git commit -m "feat(memory): bin/rechunk_notes.py re-chunks oversize notes"
```

---

# W1 — Payload economics

### Task 11: Shared name-matching helper

**Files:**
- Modify: `mcpbrain/chunking.py`, `mcpbrain/drain.py` (`_name_grounded`)
- Test: `tests/test_chunking.py`

**Interfaces:**
- Produces: `chunking.name_tokens(name: str) -> list[str]`; `chunking.name_in_text(name: str, haystack_lower: str) -> bool`.

`chunking.py` is the stdlib-only home for shared helpers (`text_norm` pulls in `inflect`). `drain._name_grounded` delegates so the two directions cannot drift.

- [ ] **Step 1: Write the failing test**

```python
def test_name_tokens_keeps_distinctive_tokens_only():
    from mcpbrain.chunking import name_tokens
    assert name_tokens("Joel Chelliah") == ["joel", "chelliah"]
    assert name_tokens("A B") == []          # nothing >= 4 chars


def test_name_in_text_matches_full_name_and_tokens():
    from mcpbrain.chunking import name_in_text
    assert name_in_text("Joel Chelliah", "spoke to joel chelliah today")
    assert name_in_text("Joel Chelliah", "ps joel will confirm")
    assert not name_in_text("Joel Chelliah", "nothing relevant here")
    assert not name_in_text("", "anything")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_chunking.py -k name_ -v`
Expected: FAIL — `ImportError: cannot import name 'name_tokens'`

- [ ] **Step 3: Implement, and delegate from drain**

In `mcpbrain/chunking.py`:

```python
_NAME_TOKEN_MIN = 4


def name_tokens(name: str) -> list[str]:
    """Distinctive (>= 4 char) lowercase alphanumeric tokens of a name.

    Shared by drain._name_grounded ("is this extracted name present in the
    source?") and prepare's context scoping ("which known people does this unit
    mention?"). Same heuristic run in opposite directions — one owner so they
    cannot drift.
    """
    return [t for t in re.split(r"[^a-z0-9]+", (name or "").strip().lower())
            if len(t) >= _NAME_TOKEN_MIN]


def name_in_text(name: str, haystack_lower: str) -> bool:
    """True when the full name, or any distinctive token of it, is in the text."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in haystack_lower:
        return True
    return any(t in haystack_lower for t in name_tokens(n))
```

In `mcpbrain/drain.py`, replace `_name_grounded`'s body with:

```python
def _name_grounded(name: str, source_lower: str) -> bool:
    """True if an extracted name is plausibly grounded in the source text.

    Delegates to chunking.name_in_text, which prepare's context scoping also
    uses in the opposite direction.
    """
    from mcpbrain.chunking import name_in_text
    return name_in_text(name, source_lower)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_chunking.py tests/test_drain_grounding.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/chunking.py mcpbrain/drain.py tests/test_chunking.py
git commit -m "refactor(enrich): one owner for name-in-text matching"
```

---

### Task 12: Per-unit `known_people` scoping

**Files:**
- Modify: `mcpbrain/prompt.py` (add `build_candidate_people`), `mcpbrain/prepare.py`
- Test: `tests/test_prepare.py`, `tests/test_prompt.py`

**Interfaces:**
- Consumes: `chunking.name_tokens`, `chunking.name_in_text`.
- Produces:
  - `prompt.build_candidate_people(store) -> list[dict]` — every confirmed person with `{"id","name","org","role","aliases"}`.
  - `prepare._parse_aliases(raw) -> list[str]`
  - `prepare._build_people_index(people) -> dict[str, list[dict]]`
  - `prepare._scoped_known_people(core, index, unit_text, *, cap=CONTEXT_CAP) -> list[dict]`
  - `prepare.CONTEXT_CAP = 8_000`

- [ ] **Step 1: Write the failing test**

```python
def test_parse_aliases_splits_json_list_and_pipes():
    """entities.aliases stores pipe-delimited strings INSIDE JSON list elements."""
    from mcpbrain.prepare import _parse_aliases
    assert _parse_aliases('["Pete|Peter", "Ps Pete"]') == ["Pete", "Peter", "Ps Pete"]
    assert _parse_aliases("") == []
    assert _parse_aliases(None) == []


def test_scoped_known_people_keeps_core_and_mentioned_only():
    from mcpbrain.prepare import _build_people_index, _scoped_known_people
    core = [{"id": "c1", "name": "Core Person", "org": "Acme", "role": "CEO"}]
    pool = [
        {"id": "p1", "name": "Taryn Hamilton", "org": "Acme", "role": "Pastor",
         "aliases": []},
        {"id": "p2", "name": "Nobody Mentioned", "org": "Acme", "role": "X",
         "aliases": []},
    ]
    out = _scoped_known_people(core, _build_people_index(pool),
                               "please ask taryn hamilton about hall b")
    ids = [p["id"] for p in out]
    assert "c1" in ids and "p1" in ids and "p2" not in ids


def test_scoped_known_people_matches_on_alias():
    from mcpbrain.prepare import _build_people_index, _scoped_known_people
    pool = [{"id": "p1", "name": "Peter Hammer", "org": "Acme", "role": "X",
             "aliases": ["Pete|Peter"]}]
    out = _scoped_known_people([], _build_people_index(pool), "pete is away")
    assert [p["id"] for p in out] == ["p1"]


def test_scoped_known_people_respects_the_cap_and_keeps_core_first():
    from mcpbrain.prepare import _build_people_index, _scoped_known_people
    core = [{"id": f"c{i}", "name": f"Core{i} Person", "org": "Acme", "role": "R"}
            for i in range(40)]
    out = _scoped_known_people(core, _build_people_index([]), "", cap=500)
    import json
    assert len(json.dumps(out)) <= 500
    assert out and out[0]["id"] == "c0"      # core ranks first, never trimmed away
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prepare.py -k "aliases or scoped_known" -v`
Expected: FAIL — `ImportError: cannot import name '_parse_aliases'`

- [ ] **Step 3: Add the candidate query**

In `mcpbrain/prompt.py`:

```python
def build_candidate_people(store, *, owner=None) -> list[dict]:
    """Every person with a confirmed org, for per-unit context scoping.

    This is a POOL, not a payload: prepare indexes it once per write_units call
    and selects only the people a given unit actually mentions. It is
    deliberately wider than build_known_people's batch overlay — a body mention
    of someone outside this batch now resolves, which the old shared-context
    shape could not do.
    """
    if owner is None:
        owner = owner_identity_from_config()
    owner_like = f"%{owner.name.lower()}%" if owner.name else "\x00"
    with store._connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.id, e.name, e.org, e.aliases,
                   (SELECT eo.value FROM entity_observations eo
                    WHERE eo.entity_id = e.id AND eo.attribute = 'role'
                      AND eo.valid_to IS NULL AND eo.invalidated_at IS NULL
                      AND length(eo.value) BETWEEN 3 AND 70
                    ORDER BY {_ROLE_SOURCE_CASE} DESC, eo.confidence DESC
                    LIMIT 1) AS best_role
            FROM entities e
            WHERE e.type = 'person'
              AND e.org NOT IN ('', 'unknown')
              AND lower(e.name) NOT LIKE ?
            """,
            (owner_like,),
        ).fetchall()
    out = []
    for r in rows:
        if _is_install_owner(r["id"], r["name"], owner):
            continue
        out.append({"id": r["id"], "name": r["name"], "org": r["org"] or "",
                    "role": _clean_role(r["best_role"]), "aliases": r["aliases"]})
    return out
```

- [ ] **Step 4: Add the scoping helpers to `prepare.py`**

```python
# Max serialized bytes of a unit's known_people block. p95 of the measured
# distribution over 860 real units (p50 5,618 / p90 7,643 / p95 8,312 / max
# 14,679), so it trims ~7% of units — and it trims the WEAKEST-ranked matches
# rather than dropping known_people wholesale, which is what the old 50KB
# soft-limit fallback did (inverting quality: the largest, most substantive
# units got the least context). Also makes the packing budget deterministic.
CONTEXT_CAP = 8_000


def _parse_aliases(raw) -> list[str]:
    """Flatten entities.aliases into alias strings.

    The column is a JSON list whose ELEMENTS may themselves be pipe-delimited
    ('Pete|Peter', 'Taryn Hansen|Taryn'), so both levels must be split. Coverage
    is 2.9% today (175 of 5,992 people, and zero of the 405 that were in the old
    shared context), so this earns nothing yet — it grows on its own through
    merge_entities' loser-alias carry. It must NOT be treated as justifying a
    smaller core.
    """
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        val = raw
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list):
        return []
    return [piece.strip() for item in val
            for piece in str(item).split("|") if piece.strip()]


def _build_people_index(people: list[dict]) -> dict:
    """token -> [person], built ONCE per write_units call.

    An O(people) substring scan per unit would be ~5,000 names x ~130 units of
    work every cycle. Inverting to a token index makes selection O(unit tokens)
    instead: tokenize the unit once, look each token up.
    """
    from mcpbrain.chunking import name_tokens
    idx: dict = {}
    for p in people:
        toks = set(name_tokens(p.get("name") or ""))
        for a in _parse_aliases(p.get("aliases")):
            toks.update(name_tokens(a))
        for t in toks:
            idx.setdefault(t, []).append(p)
    return idx


def _scoped_known_people(core: list[dict], index: dict, unit_text: str,
                         *, cap: int = CONTEXT_CAP) -> list[dict]:
    """The known people this unit actually mentions, plus the standing core.

    Ranked core -> exact full name -> name token -> alias token, then trimmed to
    `cap` bytes from the weakest end. Core is never trimmed away: it is what
    carries the nickname case ("Bob" for "Robert Smith") that a lexical scan
    structurally cannot.
    """
    from mcpbrain.chunking import name_in_text, name_tokens
    hay = (unit_text or "").lower()
    ranked: list[tuple[int, dict]] = [(0, p) for p in core]
    seen = {p["id"] for p in core}
    for tok in set(re.split(r"[^a-z0-9]+", hay)):
        for p in index.get(tok, ()):
            if p["id"] in seen:
                continue
            name = (p.get("name") or "").strip().lower()
            if name and name in hay:
                rank = 1
            elif any(t in hay for t in name_tokens(name)):
                rank = 2
            elif any(name_in_text(a, hay) for a in _parse_aliases(p.get("aliases"))):
                rank = 3
            else:
                continue
            seen.add(p["id"])
            ranked.append((rank, p))
    ranked.sort(key=lambda r: r[0])
    out: list[dict] = []
    for _, p in ranked:
        entry = {"id": p["id"], "name": p["name"], "org": p.get("org", ""),
                 "role": p.get("role")}
        trial = out + [entry]
        if out and len(json.dumps(trial)) > cap:
            break
        out = trial
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_prepare.py -k "aliases or scoped_known" tests/test_prompt.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/prepare.py mcpbrain/prompt.py tests/test_prepare.py tests/test_prompt.py
git commit -m "feat(enrich): per-unit known_people scoping (lexical + alias + core)"
```

---

### Task 13: Rules by kind and a derived reserve

**Files:**
- Modify: `mcpbrain/tools.py` (`_enrich_rules`, `_unit_payload`)
- Test: `tests/test_enrich_prompt_doc.py`

**Interfaces:**
- Produces: `tools._enrich_rules_for(kind: str, block: str | None = None) -> str`; `tools.enrich_rules_reserve() -> int`.

A `thread` unit needs ~12.4KB of the 24,554-char block; the other ~12.1KB is block-kind protocol it never uses. 850 of 868 units are threads.

- [ ] **Step 1: Write the failing test**

```python
def test_thread_rules_exclude_block_protocols():
    from mcpbrain.tools import _enrich_rules_for
    r = _enrich_rules_for("thread")
    assert "## The extraction envelope" in r
    assert "## Entity and relation discipline" in r
    assert "## Thread-mode rules" in r
    assert "## Memory-distil rules" not in r
    assert "## Org-hygiene review rules" not in r


def test_block_rules_carry_only_that_block():
    from mcpbrain.tools import _enrich_rules_for
    r = _enrich_rules_for("block", "memory_distil")
    assert "## Memory-distil rules" in r
    assert "## Thread-mode rules" not in r
    assert "## The extraction envelope" in r      # common preamble


def test_every_unit_block_has_a_rules_section():
    """Guards the same drift class tests/test_enrich_blocks.py already pins."""
    from mcpbrain.enrich_blocks import UNIT_BLOCKS
    from mcpbrain.tools import _enrich_rules_for
    for b in UNIT_BLOCKS:
        assert len(_enrich_rules_for("block", b)) > 0, b


def test_reserve_covers_every_kind():
    from mcpbrain.enrich_blocks import UNIT_BLOCKS
    from mcpbrain.tools import _enrich_rules_for, enrich_rules_reserve
    reserve = enrich_rules_reserve()
    assert len(_enrich_rules_for("thread")) <= reserve
    for b in UNIT_BLOCKS:
        assert len(_enrich_rules_for("block", b)) <= reserve
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich_prompt_doc.py -k "rules_for or reserve" -v`
Expected: FAIL — `ImportError: cannot import name '_enrich_rules_for'`

- [ ] **Step 3: Implement section selection**

In `mcpbrain/tools.py`, after `_enrich_rules`:

```python
# Section titles in enrich_prompt.md, keyed to what each unit kind needs. The
# prompt is NOT rewritten — it is split on its existing "## " headings.
_RULES_COMMON = ("The extraction envelope", "Using the standing context")
_RULES_THREAD = ("Entity and relation discipline", "Drive document mode",
                 "Thread-mode rules")
_RULES_BLOCK = {
    "merge_review": "Merge-review rules",
    "org_merge_review": "Org merge-review rules (curator only)",
    "review_orphan": "Orphan-entity review rules",
    "review_missing_org": "Missing-org review rules",
    "review_ownerless": "Ownerless-action review rules",
    "review_org": "Org-hygiene review rules",
    "synthesis": "Thread-synthesis rules",
    "profile_synthesis": "Profile-synthesis rules",
    "community_synthesis": "Community-synthesis rules",
    "memory_distil": "Memory-distil rules",
    "profile_audit": "Profile-audit rules",
}


def _rules_sections() -> dict:
    """Split the canonical rules on their '## ' headings. Cached."""
    global _RULES_SECTIONS_CACHE
    try:
        return _RULES_SECTIONS_CACHE
    except NameError:
        pass
    out = {}
    for chunk in re.split(r"\n(?=## )", _enrich_rules()):
        line = chunk.split("\n", 1)[0]
        if line.startswith("## "):
            out[line[3:].strip()] = chunk.rstrip()
    _RULES_SECTIONS_CACHE = out
    return out


def _enrich_rules_for(kind: str, block: str | None = None) -> str:
    """Only the rule sections this unit kind needs.

    A thread unit does not need the 11 block protocols (~12.1KB of 24,554), and
    850 of 868 live units are threads. The drainer's system-prompt copy stays
    WHOLE — it handles every kind and its prefix is cached across the pool — so
    bin/sync_agents.py is unaffected.
    """
    sections = _rules_sections()
    wanted = list(_RULES_COMMON)
    if kind == "block":
        title = _RULES_BLOCK.get(block or "")
        if title:
            wanted.append(title)
    else:
        wanted.extend(_RULES_THREAD)
    return "\n\n".join(sections[t] for t in wanted if t in sections)


def enrich_rules_reserve() -> int:
    """Max serialized rules length across every unit kind.

    prepare's packing budget subtracts this. It was a stale 11_000 literal while
    the real block is 24,554 chars, which is why EVERY live unit exceeded
    unit_pull_cap on the with_rules=True path. Derived, never hardcoded.
    """
    from mcpbrain.enrich_blocks import UNIT_BLOCKS
    return max([len(_enrich_rules_for("thread"))]
               + [len(_enrich_rules_for("block", b)) for b in UNIT_BLOCKS])
```

Then in `_unit_payload`, replace `out["rules"] = _enrich_rules()` with:

```python
        out["rules"] = _enrich_rules_for(d.get("kind"), d.get("block"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_enrich_prompt_doc.py tests/test_enrich_blocks.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/tools.py tests/test_enrich_prompt_doc.py
git commit -m "perf(enrich): serve only the rule sections a unit kind needs"
```

---

### Task 14: Per-unit context replaces `context.json`

**Files:**
- Modify: `mcpbrain/prepare.py` (`write_units`, `_build_context`, `build_pending`, `prepare_units`), `mcpbrain/tools.py` (`_unit_payload`)
- Test: `tests/test_prepare.py`, `tests/test_prepare_community_context.py`

**Interfaces:**
- Consumes: Task 12's `_build_people_index` / `_scoped_known_people`; Task 13's `enrich_rules_reserve`.
- Produces: unit files gain a `context` key; `context.json` is no longer written; `_unit_payload` reads `d["context"]`.

`community_summaries` is **deleted**: 6,255 bytes per unit with no consumer anywhere.

- [ ] **Step 1: Write the failing test**

```python
def test_write_units_writes_context_into_each_unit(tmp_path):
    from mcpbrain.prepare import write_units
    data = {"threads": [{"thread_id": "t1",
                         "messages": [{"message_id": "m1", "text": "hi taryn"}]}],
            "context": {"owner_name": "Josh", "valid_orgs": ["Acme"],
                        "org_domain_map": [], "known_people": []}}
    write_units(data, home=str(tmp_path))
    import json, glob
    (f,) = glob.glob(str(tmp_path / "enrich_queue" / "units" / "*.json"))
    unit = json.loads(open(f).read())
    assert unit["context"]["owner_name"] == "Josh"
    assert not (tmp_path / "enrich_queue" / "context.json").exists()


def test_unit_payload_reads_the_units_own_context(tmp_path):
    from mcpbrain.tools import _unit_payload
    d = {"kind": "thread", "threads": [],
         "context": {"owner_name": "Josh", "known_people": [{"id": "a"}]}}
    out = _unit_payload(str(tmp_path), d, "u-1", False)
    assert out["context"]["known_people"] == [{"id": "a"}]


def test_context_carries_no_community_summaries(tmp_path):
    """Dead payload: 6,255 bytes/unit that nothing reads — not enrich_prompt.md,
    not the enrich-batch agent, not routines/enrich.md."""
    from mcpbrain.prepare import _build_context
    assert "community_summaries" not in _build_context(None, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prepare.py -k "unit_context or community_summaries" -v`
Expected: FAIL — unit has no `context` key

- [ ] **Step 3: Slim `_build_context` and delete the dead block**

In `mcpbrain/prepare.py`, replace `_build_context` with the standing-only shape and delete `_community_summaries_for_people` entirely:

```python
def _build_context(store, thread_ids) -> dict:
    """The STANDING reference block, shared by every unit and tiny (~150 bytes).

    known_people is no longer here: it is scoped per unit in write_units, because
    the batch-wide list had grown to 405 people / 39,017 bytes and was being
    re-sent with every one of 860 units — 88.7% of everything reaching the model.

    community_summaries is gone entirely: it had no consumer.
    """
    home = str(config.app_dir())
    return {
        "owner_name": config.owner_full_name(home) or config.owner_name(home),
        "org_domain_map": _org_domain_lines(),
        "valid_orgs": _valid_org_tags(),
    }
```

Delete `tests/test_prepare_community_context.py`, or reduce it to the
`test_context_carries_no_community_summaries` guard above.

- [ ] **Step 4: Scope context per unit in `write_units`**

Replace `write_units`' context handling. Remove the `context.json` write and the
`ctx_len` term, and compute per-unit:

```python
    standing = data.get("context") or {}
    pool = data.get("people_pool") or []
    core = data.get("people_core") or []
    index = _build_people_index(pool)
    budget = max(2000, pull_cap - _UNIT_RULES_RESERVE - CONTEXT_CAP - 1500)
```

and for each packed chunk, before writing the unit:

```python
        body = {"unit_id": uid, "kind": "thread", "threads": chunk}
        body["context"] = {**standing,
                           "known_people": _scoped_known_people(
                               core, index, json.dumps(chunk, ensure_ascii=False))}
        _atomic_write(units_dir / f"{uid}.json",
                      json.dumps(body, ensure_ascii=False))
```

Do the same for the block-unit loop, passing `json.dumps(chunk)` as the unit text.

In `prepare_units`, populate the pool once per cycle and pass it through:

```python
    data["people_core"] = prompt.build_known_people(store, batch_thread_ids=[])
    data["people_pool"] = prompt.build_candidate_people(store)
```

- [ ] **Step 5: Serve the unit's own context**

In `mcpbrain/tools.py`, replace `_unit_payload`'s `context.json` read:

```python
    # The unit carries its own scoped context (prepare.write_units). This keeps
    # _unit_payload pure file I/O — no store access on the claim hot path — and
    # is why context.json no longer exists. A pre-migration unit has no context
    # key and degrades to {}; the queue is rebuilt, so that is transitional only.
    ctx = d.get("context") or {}
```

and delete the now-unreachable `_PULL_SOFT_LIMIT` fallback that stripped
`known_people`, replacing it with a relevance-preserving trim:

```python
    if len(_json.dumps(out)) > _PULL_SOFT_LIMIT:
        # Trim from the WEAKEST end (the list is already relevance-ranked by
        # prepare._scoped_known_people) rather than dropping known_people
        # wholesale, which starved exactly the largest units.
        kp = list(ctx.get("known_people") or [])
        while kp and len(_json.dumps(out)) > _PULL_SOFT_LIMIT:
            kp.pop()
            out["context"] = {**ctx, "known_people": kp}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_prepare.py tests/test_enrich_prompt_doc.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/prepare.py mcpbrain/tools.py tests/
git commit -m "perf(enrich): per-unit scoped context replaces the shared 45KB context.json"
```

---

### Task 15: Wire the derived reserve and pin the cap invariant

**Files:**
- Modify: `mcpbrain/prepare.py` (`_UNIT_RULES_RESERVE`)
- Test: `tests/test_prepare.py`

**Interfaces:**
- Consumes: `tools.enrich_rules_reserve()`.
- Produces: `_UNIT_RULES_RESERVE` is derived; the packing budget is deterministic.

- [ ] **Step 1: Write the failing test**

```python
def test_packing_budget_is_deterministic_and_large():
    """The old budget was max(2000, 60000 - 11000 - 45508 - 1500) = 2000 — the
    floor — because context.json had grown to 45KB. That is what made units
    one-thread-each and 7x underfilled."""
    from mcpbrain.prepare import CONTEXT_CAP, _UNIT_RULES_RESERVE
    budget = max(2000, 60_000 - _UNIT_RULES_RESERVE - CONTEXT_CAP - 1500)
    assert budget > 30_000


def test_reserve_is_not_the_stale_literal():
    from mcpbrain.prepare import _UNIT_RULES_RESERVE
    assert _UNIT_RULES_RESERVE != 11_000
    assert _UNIT_RULES_RESERVE >= 12_000


def test_no_unit_exceeds_pull_cap_with_rules(tmp_path):
    """The invariant ALL 868 live units violate today: 45,511 (context)
    + 24,554 (rules) = 70,065 > 60,000 before any work is added."""
    import glob, json
    from mcpbrain.prepare import write_units
    from mcpbrain.tools import _unit_payload
    data = {"threads": [{"thread_id": f"t{i}",
                         "messages": [{"message_id": f"m{i}", "text": "x" * 3000}]}
                        for i in range(20)],
            "context": {"owner_name": "Josh", "valid_orgs": [], "org_domain_map": []},
            "people_core": [], "people_pool": []}
    write_units(data, home=str(tmp_path))
    for f in glob.glob(str(tmp_path / "enrich_queue" / "units" / "*.json")):
        d = json.load(open(f))
        payload = _unit_payload(str(tmp_path), d, d["unit_id"], True)
        assert len(json.dumps(payload)) <= 60_000, f
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prepare.py -k "budget or reserve or pull_cap" -v`
Expected: FAIL — `assert 11000 != 11000`

- [ ] **Step 3: Derive the reserve**

In `mcpbrain/prepare.py`, replace the literal:

```python
def _rules_reserve() -> int:
    """Room the pull's rules block needs, derived from the real rules.

    Was a hardcoded 11_000 while _enrich_rules() had grown to 24,554 chars, so
    the budget under-reserved by 13,554 and no unit could meet unit_pull_cap.
    Derived from tools.enrich_rules_reserve(), which takes the MAX across kinds
    and is applied uniformly — one budget, kind-agnostic packing, never able to
    under-reserve.
    """
    from mcpbrain.tools import enrich_rules_reserve
    return enrich_rules_reserve()


_UNIT_RULES_RESERVE = _rules_reserve()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_prepare.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/prepare.py tests/test_prepare.py
git commit -m "fix(enrich): derive the rules reserve so units can meet pull_cap"
```

---

### Task 16: A/B extraction-quality harness

**Files:**
- Create: `bin/enrich_ab.py`
- Test: `tests/test_enrich_ab.py`

**Interfaces:**
- Produces: `enrich_ab.prep(units_dir, out_dir, n) -> int`; `enrich_ab.score(a_dir, b_dir) -> dict`.

There is **no Anthropic API key in mcpbrain** and 0.7.106 removed the only
subprocess-`claude` path, so the harness is two deterministic halves with a
Claude Code session supplying the model calls in between.

- [ ] **Step 1: Write the failing test**

```python
def test_score_flags_lost_org_assignments():
    """The gate: B must not LOSE an org/role that A got right. A pure count
    match would hide a systematic misattribution, which is exactly why
    enrich_eval.graph_metrics is insufficient here."""
    from bin.enrich_ab import score_pair
    a = {"entities": [{"name": "Taryn Hamilton", "org": "Acme", "role": "Pastor"}]}
    b = {"entities": [{"name": "Taryn Hamilton", "org": "", "role": "Pastor"}]}
    r = score_pair(a, b)
    assert r["org_lost"] == ["Taryn Hamilton"]
    assert r["entities_lost"] == []


def test_score_pair_flags_dropped_entities():
    from bin.enrich_ab import score_pair
    a = {"entities": [{"name": "X", "org": "Acme"}, {"name": "Y", "org": "Acme"}]}
    b = {"entities": [{"name": "X", "org": "Acme"}]}
    assert score_pair(a, b)["entities_lost"] == ["Y"]


def test_score_pair_clean_when_identical():
    from bin.enrich_ab import score_pair
    a = {"entities": [{"name": "X", "org": "Acme", "role": "R"}]}
    assert score_pair(a, a) == {"entities_lost": [], "entities_gained": [],
                                "org_lost": [], "role_lost": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_enrich_ab.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.enrich_ab'`

- [ ] **Step 3: Write the harness**

```python
#!/usr/bin/env python3
"""A/B extraction quality: full shared context (A) vs per-unit scoped context (B).

mcpbrain has NO model API key (0.7.106 removed the only subprocess-claude path),
so this is two deterministic halves with a Claude Code session in between:

  1. prep  — emit paired payloads for N real units into ab/a/ and ab/b/
  2. (a Claude Code session drains BOTH sets through enrich-batch subagents)
  3. score — diff the two extraction sets

GATE: B must not lose org/role assignments A got right. Disagreements are the
artifact — eyeball them; do not auto-pass on counts.
"""
import argparse
import json
import sys
from pathlib import Path


def score_pair(a: dict, b: dict) -> dict:
    """Diff one A/B extraction pair."""
    def by_name(e):
        return {x.get("name"): x for x in (e.get("entities") or []) if x.get("name")}
    ea, eb = by_name(a), by_name(b)
    return {
        "entities_lost": sorted(set(ea) - set(eb)),
        "entities_gained": sorted(set(eb) - set(ea)),
        "org_lost": sorted(n for n in set(ea) & set(eb)
                           if ea[n].get("org") and not eb[n].get("org")),
        "role_lost": sorted(n for n in set(ea) & set(eb)
                            if ea[n].get("role") and not eb[n].get("role")),
    }


def score(a_dir: str, b_dir: str) -> dict:
    """Aggregate score_pair across every matching unit result."""
    totals = {"units": 0, "entities_lost": [], "org_lost": [], "role_lost": []}
    for pa in sorted(Path(a_dir).glob("*.json")):
        pb = Path(b_dir) / pa.name
        if not pb.exists():
            continue
        r = score_pair(json.loads(pa.read_text()), json.loads(pb.read_text()))
        totals["units"] += 1
        for k in ("entities_lost", "org_lost", "role_lost"):
            totals[k].extend(f"{pa.stem}:{n}" for n in r[k])
    return totals


def prep(units_dir: str, out_dir: str, n: int, full_context_path: str) -> int:
    """Emit N paired payloads: a/ = full context, b/ = the unit's scoped context."""
    from mcpbrain import config, prepare, prompt
    from mcpbrain.store import Store
    store = Store(str(config.app_dir()))
    core = prompt.build_known_people(store, batch_thread_ids=[])
    pool = prompt.build_candidate_people(store)
    index = prepare._build_people_index(pool)
    # The A side is the PRE-CHANGE 405-person list, which no longer exists once
    # Task 14 deletes context.json — so it is loaded from the snapshot taken in
    # Task 17 Step 1, never rebuilt.
    full = json.loads(Path(full_context_path).read_text())["known_people"]
    a_dir, b_dir = Path(out_dir) / "a", Path(out_dir) / "b"
    a_dir.mkdir(parents=True, exist_ok=True)
    b_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for f in sorted(Path(units_dir).glob("*.json"))[:n]:
        d = json.loads(f.read_text())
        text = json.dumps(d.get("threads") or d.get("items") or [])
        (a_dir / f.name).write_text(json.dumps({**d, "context": {"known_people": full}}))
        (b_dir / f.name).write_text(json.dumps(
            {**d, "context": {"known_people":
                              prepare._scoped_known_people(core, index, text)}}))
        count += 1
    return count


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep")
    p.add_argument("--units", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("-n", type=int, default=30)
    p.add_argument("--full-context", required=True,
                   help="snapshot of the pre-change context.json (Task 17 Step 1)")
    s = sub.add_parser("score")
    s.add_argument("--a", required=True)
    s.add_argument("--b", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "prep":
        print(f"[ab] wrote {prep(args.units, args.out, args.n, args.full_context)} pair(s)")
    else:
        print(json.dumps(score(args.a, args.b), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`--full-context` is the snapshot of the live `enrich_queue/context.json` taken in
Task 17 Step 1, **before Task 14 deletes it**. The A side must be the real
pre-change 405-person list, not a rebuild.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_enrich_ab.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add bin/enrich_ab.py tests/test_enrich_ab.py
git commit -m "test(enrich): A/B harness for scoped-context extraction quality"
```

---

### Task 17: Validation and queue rebuild

**Files:**
- Modify: `CLAUDE.md` (current-state section)

No code. This is the attended run-through, in order.

- [ ] **Step 1: Snapshot the A-side context before it is deleted**

```bash
cp "$HOME/Library/Application Support/mcpbrain/enrich_queue/context.json" \
   /tmp/ab-context-full.json
```

- [ ] **Step 2: Run the A/B and read the disagreements**

```bash
uv run python bin/enrich_ab.py prep \
  --units "$HOME/Library/Application Support/mcpbrain/enrich_queue/units" \
  --out /tmp/ab -n 30 --full-context /tmp/ab-context-full.json
# drain /tmp/ab/a and /tmp/ab/b through enrich-batch subagents, then:
uv run python bin/enrich_ab.py score --a /tmp/ab/a --b /tmp/ab/b
```

Gate: `org_lost` and `role_lost` must be empty, or every entry explained on
inspection. Do not proceed on a count match alone.

- [ ] **Step 3: Stop the daemon and take a verified backup**

```bash
launchctl bootout gui/$(id -u)/com.mcpbrain 2>/dev/null || true
uv run python -c "from mcpbrain import backup; backup.run_backup()"
```

- [ ] **Step 4: Gold baseline**

```bash
uv run python tests/eval/run_eval.py --gold --k 10
```

Record recall@10 / MRR. Floor is 0.780 / 0.550.

- [ ] **Step 5: Run both sweeps, dry-run first**

```bash
uv run python bin/resalience.py
uv run python bin/resalience.py --yes
uv run python bin/rechunk_notes.py
uv run python bin/rechunk_notes.py --yes
```

- [ ] **Step 6: Gold after**

```bash
uv run python tests/eval/run_eval.py --gold --k 10
```

Must clear 0.780 / 0.550. The note sweep should *improve* it — 21.1MB currently
has no vector past each note's first ~2,000 chars. If it regresses, restore the
backup; do not proceed.

- [ ] **Step 7: Rebuild the queue**

```bash
cd "$HOME/Library/Application Support/mcpbrain/enrich_queue"
rm -rf units claims context.json
```

Units are content-addressed and regenerate from `enriched=0` chunks, so nothing
is lost and no migration code is needed.

- [ ] **Step 8: Restart the daemon and confirm the new shape**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.mcpbrain.plist
```

Then after one spool cycle, confirm: units are far fewer, each carries its own
`context`, no `context.json` exists, and no unit exceeds 60,000 bytes.

- [ ] **Step 9: Update `CLAUDE.md` and commit**

Record the measured before/after, the two sweeps as run (with row counts), the
gold numbers, and that `bin/resalience.py` and `bin/rechunk_notes.py` are
attended-only and never called by a cadence.

```bash
git add CLAUDE.md
git commit -m "docs: record the enrichment efficiency work and its live validation"
```

---

## Notes for the executor

- **Do not run the full suite** — Josh runs `pytest tests/` himself. Scope runs to the edited and directly impacted files.
- **Do not push or release.** Shipping is an all-users action and needs an explicit instruction.
- `bin/resalience.py` and `bin/rechunk_notes.py` must never be wired into a daemon cadence.
- If a task reveals that an interface named here does not exist (e.g. `store.delete_chunks`), add it in that task rather than working around it, and say so in the commit.
