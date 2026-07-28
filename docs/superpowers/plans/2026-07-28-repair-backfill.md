# Repair Backfill Implementation Plan (spec 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Tasks are grouped into WAVES — every task in a wave owns a disjoint set of files and runs in PARALLEL. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the corpus the corrected extractor can now read properly — purge the 68,193 content-free chunks, recover the rows clipped from 455 spreadsheets, and re-extract 9,351 legacy Drive files — attended, backup-gated and gold-gated, without deleting anything whose provenance the graph depends on.

**Architecture:** One new idea, and it does most of the work: chunks start carrying `chunker_version`, exactly as they already carry `enriched_version`. That makes "which chunks were written by an out-of-date chunker" a plain indexed query, which turns the repair into a **level-triggered reconciler** — the same pattern the daemon work adopted — with no new queue, no new state table and no cursor to corrupt. Bumping the version also invalidates every stale fleet ingest-cache artifact for free, because `pipeline_fingerprint` already folds `chunker_version` into the artifact filename and the import gate. Everything else is a thin attended CLI over library functions, following `bin/consolidate.py`.

**Tech Stack:** Python 3.11, SQLite (`sqlite-vec` + FTS5), Google Drive API, pytest + pytest-xdist, ruff.

---

## Live baseline (measured 2026-07-28, read-only, on the 11 GB store)

Everything below is measured, not carried over from the audit — the corpus grew from 179,690 to 196,396 chunks since 2026-07-27.

| Quantity | Value | Note |
|---|---|---|
| Total chunks | **196,396** | |
| Content-free (no alphanumeric char) | **68,193** (34.7%) | 67,210 of them share ONE `content_hash` |
| Redundant copies | **106,357** (54%) | 196,396 total vs 90,039 distinct hashes |
| …still redundant after the content-free purge | **~38,164** | genuine duplicate files — see decision 2 |
| Files clipped by the old 200-row cap | **455** files / 1,924 marker chunks | audit said 338; it grew |
| Legacy Drive files (no `extraction_method`) | **9,351** files / 63,229 chunks | |
| Chunks over the 2,000-char embedder window | **16,997** | |
| Cold-marked chunks | **109,694** | |
| Chunks carrying `chunk_total` | **0** | spec 2 only affects new writes; nothing has re-synced yet |
| Gmail chunks | **4,171** (2.1%) | Drive dominates the corpus ~47:1 |
| Zero-length / sub-5-char chunks | **22** / 53 | |
| **Content-free chunks referenced by `entity_relations.source_doc_id`** | **0** | |
| **Content-free chunks referenced by `email_entities.message_id`** | **0** | |

Those last two rows are the safety proof for the whole purge. `store.delete_chunks` explicitly does **not** touch graph rows ("invalidation is a separate, bitemporal step"), so deleting a chunk the graph cites would leave dangling provenance. Nothing in the purge set is cited. Task 2 re-asserts this at runtime rather than trusting this table.

---

## Global Constraints

- **Work on `main`, commit as you go.** Pushing source is fine (your normal workflow). **Releasing is not** — no `bin/release.py`, no dist wheel, no plugin sync, no version bumps in the five version files. That is a separate explicit decision.
- **Do not run the repair against the live store.** This plan builds and tests the machinery. Running it on the 11 GB store is an attended step Josh performs, with a backup, following Task 4's runbook section. A subagent must never invoke `bin/repair.py` without `--dry-run` against the real `MCPBRAIN_HOME`.
- **Never copy `brain.sqlite3`.** It is 11 GB and there is 46 GB free. A previous session filled the disk to zero copying it twice, froze the machine, and the emergency cleanup destroyed an unrelated app's data. Query it read-only in place (`file:...?mode=ro`) and never `cp` it. The one legitimate full copy is `bin/repair.py`'s own pre-flight backup, which is Josh's attended step and checks free space first.
- **Do not run the full suite.** Josh runs `pytest tests/` himself. Scope runs to the files named in each task's test step.
- **`uv run pytest …` / `uv run ruff check …`** — bare `pytest`/`python` are not on PATH. If the suite reports `ModuleNotFoundError: fastembed`, the venv lost the extra: `uv sync --extra daemon --extra dev`. Adding a dependency and running plain `uv sync` is what drops it.
- **Ruff must pass on `mcpbrain/`, `bin/` and every test file you touch.** `tests/` has pre-existing errors; do not fix them and do not add to them.
- **Do not touch the daemon scheduling work or spec 2's ingest path** beyond what a task explicitly names. Both are done, reviewed and pushed.

---

## Execution: three gates, four tasks

```
Gate 1   Task 1  Chunk provenance + version bump         (solo — blocks everything)
           │
Gate 2   ├─ Task 2  Purge + retrieval dedup   store.py, retrieval.py
         └─ Task 3  Re-ingest + cache orphans  sync/drive.py, ingest_cache.py
           │
Gate 3   └─ Task 4  The attended CLI           bin/repair.py, doctor.py     (solo)
```

Task 1 is solo because it stamps a new metadata field at four write sites and adds the selector every later task queries. Gate 2's two tasks are genuinely independent and file-disjoint. Gate 3 is solo and deliberately not split: the only other candidate work is ~20 lines of `doctor.py` reporting, and spawning a second agent for that costs more than it saves.

**Test-file ownership** (no two tasks in a wave write the same test file):

| Task | Owns |
|---|---|
| 1 | `test_chunk_metadata.py`, `test_chunking.py`, `test_org_contracts.py` |
| 2 | `test_store.py`, `test_retrieval.py`, `test_purge.py` (create) |
| 3 | `test_drive_sync.py`, `test_ingest_cache_lifecycle.py` |
| 4 | `test_repair.py` (create), `test_doctor.py` |

---

## Design decisions

### 1. `chunker_version` becomes a per-chunk stamp, and the bump does three jobs

`chunker_version` exists today only in the org pin, where `pipeline_fingerprint(embed_model, dim, chunker_version)` keys the shared-drive ingest-cache artifact filename and gates `ingest_cache.try_import` (`art.chunker_version != pin.chunker_version` → reject). **Nothing stamps it on a chunk**, so the store cannot answer "which of my chunks predate the current chunker" — which is precisely the question this repair needs.

Spec 2 changed chunking materially (the `chunk_text` empty/oversize fix, row-group tabular chunks, the `has_content` guard), and `chunker_version` was left unbumped. That is a live correctness gap, not just a missing convenience: a fleet member still importing a pre-spec-2 cache artifact gets old-shape chunks and has no way to know.

Bumping it to `2`, and stamping it at write time, does three things at once:

1. **Invalidates every stale fleet cache artifact, with no special-casing.** The fingerprint changes, so the artifact path changes and `try_import`'s own version check rejects the old one. Re-extraction happens locally and republishes under the new fingerprint. This is why the plan needs no cache-busting code.
2. **Becomes the repair selector.** `WHERE COALESCE(chunker_version,0) < 2` is the level-triggered condition — idempotent, resumable, restartable, no cursor. Directly mirrors `reflow_outdated_chunks`, which is the established pattern in this codebase for exactly this shape of problem.
3. **Makes every future chunking change self-describing.** The next change is a constant bump plus a CLI run.

### 2. Cross-document duplicates are deduped at RETRIEVAL, never deleted

38,164 redundant copies survive the content-free purge, and they are genuine duplicate *files* — the asset register exists three times in Drive (two identical names plus a `(1)` copy), each chunked independently. The tempting move is to delete two of the three.

Do not. Chunk `doc_id`s are positional (`gdrive-<file_id>-<i>`) and are cited as graph provenance (`entity_relations.source_doc_id`, `email_entities.message_id`, evidence strings). Deleting one file's chunks because another file happens to hold the same bytes makes **that file** unfindable by name, folder or `file_id`, and orphans every graph row that cited it — and `delete_chunks` deliberately does not clean those up. The user would lose the ability to answer "where does this document live" for a document that still exists in Drive.

The actual harms of duplication are two different things, and each has its own correct fix:

- **Recall crowding** (three identical hits consuming a top-10 slot) → dedupe by `content_hash` in `hybrid_search`, keeping the best-ranked representative. Cheap, reversible, no data loss. Task 2.
- **Three copies of a file existing in Drive** → a Drive hygiene decision for a human, not something an ingest repair should silently enact.

This is not a deferral: deleting them is the wrong action, and the right action is taken.

### 3. The bulk re-ingest is an attended CLI, not a daemon cadence

Re-fetching 9,351 Drive files is a bounded one-off with real API cost and a genuine gold-regression risk. The daemon's job is steady state. `chunker_version` is bumped only when chunking changes — rare, and always alongside a release — so wiring this as a cadence would mean every future bump silently re-fetches the entire corpus unattended, on every install in the fleet. The selector is shared, so a future cadence remains a three-line change if that ever becomes wanted; it is deliberately not wired now.

### 4. Gmail is not re-fetched

Gmail is 4,171 chunks — 2.1% of the store, a 47:1 ratio against Drive. The chunking defects that touched it are 22 zero-length and 53 sub-5-char chunks, all of which the content-free/tiny sweep removes directly. Re-fetching a mailbox to re-chunk 75 bad rows is not a trade worth making, and `has_content` prevents new ones. Gmail chunks are therefore stamped with the new `chunker_version` **as they naturally re-sync**, and the CLI's re-ingest phase is Drive-only. Stated so nobody later reads the Drive-only scope as an oversight.

### 5. B5's remaining half: the cache-import path

Spec 2 closed the orphan-on-shrink defect for locally-extracted Drive files (`upsert_file_chunks`) but explicitly skipped the shared-drive cache-import path, to avoid racing `ingest_cache.try_import`'s own transaction. So a shared-drive file that shrank and is served from cache still leaves indices `n..m-1` searchable forever. Task 3 closes it **inside** `_import_artifact`'s existing transaction, which is where it always belonged — no race, because it is the same transaction that writes the replacement rows.

---

## File structure

**New files**

| File | Responsibility |
|---|---|
| `bin/repair.py` | The attended CLI. Thin: argument parsing, backup, phase dispatch, printed gold gate. All logic lives in the library, mirroring `bin/consolidate.py` (91 lines). |
| `tests/test_purge.py` | Purge selection, vec/FTS mirror clearing, provenance safety assertion. |
| `tests/test_repair.py` | CLI phases, dry-run default, refusal conditions. |

**Modified**

| File | Change |
|---|---|
| `mcpbrain/chunking.py` | `CHUNKER_VERSION = 2` constant. |
| `mcpbrain/sync/{normalise,drive,calendar,attachments}.py` | Stamp `chunker_version` on every chunk. |
| `mcpbrain/org_defaults.py`, `mcpbrain/config.py` | Org-pin `chunker_version` → `"2"`. |
| `mcpbrain/store.py` | `stale_chunker_file_ids`, `content_free_doc_ids`, `purge_doc_ids`, `count_content_free`. |
| `mcpbrain/retrieval.py` | `hybrid_search` dedupes by `content_hash`. |
| `mcpbrain/sync/drive.py` | `reingest_files`. |
| `mcpbrain/ingest_cache.py` | Orphan sweep inside `_import_artifact`. |
| `mcpbrain/doctor.py` | Repair-state lines. |

---

## Gate 1 — Task 1: Chunk provenance and the version bump

**Files:**
- Modify: `mcpbrain/chunking.py`, `mcpbrain/sync/normalise.py`, `mcpbrain/sync/drive.py`, `mcpbrain/sync/calendar.py`, `mcpbrain/sync/attachments.py`, `mcpbrain/org_defaults.py`, `mcpbrain/config.py`, `mcpbrain/store.py`
- Test: `tests/test_chunk_metadata.py`, `tests/test_chunking.py`, `tests/test_org_contracts.py`

**Interfaces produced:**
- `chunking.CHUNKER_VERSION: int` (= 2)
- Every `Chunk.metadata` carries `chunker_version: int`
- `store.stale_chunker_file_ids(version: int, limit: int) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_chunk_metadata.py`:

```python
def test_every_write_path_stamps_the_chunker_version():
    """The store cannot currently answer 'which of my chunks predate the current
    chunker' — chunker_version lives only in the org pin, where it keys the
    ingest-cache fingerprint. That question is the whole repair selector, so it
    has to be answerable from a chunk."""
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync.calendar import normalise_calendar
    from mcpbrain.sync.drive import normalise_drive
    from mcpbrain.sync.normalise import normalise_gmail

    drive = normalise_drive({"id": "f1", "name": "D.txt", "mimeType": "text/plain"},
                            "Some prose worth keeping.")
    gmail = normalise_gmail({"id": "m1", "threadId": "t1", "labelIds": ["INBOX"],
                             "payload": {"mimeType": "text/plain",
                                         "headers": [{"name": "Subject", "value": "s"}],
                                         "body": {"data": _b64("Body text here.")}}})
    cal = normalise_calendar({"id": "e1", "summary": "Standup",
                              "start": {"dateTime": "2026-06-02T09:00:00Z"}})

    for label, chunks in (("drive", drive), ("gmail", gmail), ("calendar", cal)):
        assert chunks, f"{label} fixture produced no chunks"
        for c in chunks:
            assert c.metadata["chunker_version"] == CHUNKER_VERSION, (
                f"{label} chunk is not stamped with the chunker version"
            )


def test_an_attachment_chunk_is_also_stamped():
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.sync import attachments

    raw = {"id": "m1", "threadId": "t1", "labelIds": [],
           "payload": {"headers": [{"name": "Subject", "value": "Invoice"}],
                       "parts": [{"filename": "n.txt", "mimeType": "text/plain",
                                  "body": {"attachmentId": "a1", "size": 20}}]}}
    part = attachments.iter_attachment_parts(raw["payload"])[0]

    chunks = attachments.normalise_attachment(raw, part, b"Attachment prose body.")

    assert chunks
    assert chunks[0].metadata["chunker_version"] == CHUNKER_VERSION


def test_the_chunker_version_is_ahead_of_the_pre_spec_2_chunker():
    """Spec 2 changed chunking materially — the chunk_text empty/oversize fix,
    row-group tabular chunks, the has_content guard — and left this unbumped. An
    unbumped version means a fleet member still importing a pre-spec-2 cache
    artifact gets old-shape chunks with no way to know."""
    from mcpbrain.chunking import CHUNKER_VERSION

    assert CHUNKER_VERSION >= 2
```

(`_b64` already exists in this file's helpers from spec 2; if not, copy it from `tests/test_normalise.py`.)

Add to `tests/test_org_contracts.py`:

```python
def test_the_org_pin_chunker_version_matches_the_code():
    """The pin's chunker_version keys pipeline_fingerprint, which keys the
    ingest-cache artifact filename AND gates try_import. If the pin lags the
    code, installs keep importing artifacts built by the old chunker — the bump
    IS the cache invalidation, so a drift here silently defeats it."""
    from mcpbrain import org_defaults
    from mcpbrain.chunking import CHUNKER_VERSION

    assert org_defaults.ORG_PIN_CHUNKER_VERSION == str(CHUNKER_VERSION)


def test_bumping_the_chunker_version_changes_the_artifact_fingerprint():
    from mcpbrain.org_contracts import pipeline_fingerprint

    assert (pipeline_fingerprint("bge-small", 384, "1")
            != pipeline_fingerprint("bge-small", 384, "2"))
```

Add to `tests/test_chunk_metadata.py` (the selector):

```python
def test_stale_chunker_file_ids_selects_only_out_of_date_drive_files(tmp_path):
    """The level-triggered selector. No queue, no cursor: re-running walks
    forward because each repaired file stops matching. Same shape as
    reflow_outdated_chunks, which is the established pattern here."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-old-0", "legacy text", "h1",
                       {"source_type": "gdrive", "file_id": "old"})          # no version
    store.upsert_chunk("gdrive-mid-0", "half text", "h2",
                       {"source_type": "gdrive", "file_id": "mid",
                        "chunker_version": 1})
    store.upsert_chunk("gdrive-new-0", "fresh text", "h3",
                       {"source_type": "gdrive", "file_id": "new",
                        "chunker_version": 2})

    assert sorted(store.stale_chunker_file_ids(2, limit=10)) == ["mid", "old"]


def test_stale_chunker_file_ids_respects_its_limit_and_is_gmail_free(tmp_path):
    """Drive-only by design (decision 4): Gmail is 2% of the store and its
    chunking defects are 75 rows the purge removes directly, so re-fetching a
    mailbox is not a trade worth making."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    for i in range(5):
        store.upsert_chunk(f"gdrive-f{i}-0", f"text {i}", f"h{i}",
                           {"source_type": "gdrive", "file_id": f"f{i}"})
    store.upsert_chunk("gmail-m1-body-0", "mail text", "hm",
                       {"source_type": "gmail", "message_id": "m1"})

    got = store.stale_chunker_file_ids(2, limit=3)

    assert len(got) == 3
    assert all(g.startswith("f") for g in got), f"non-Drive id leaked in: {got}"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_chunk_metadata.py tests/test_org_contracts.py tests/test_chunking.py -q -p no:randomly`
Expected: `ImportError: cannot import name 'CHUNKER_VERSION'`, `AttributeError: ORG_PIN_CHUNKER_VERSION`, `AttributeError: stale_chunker_file_ids`.

- [ ] **Step 3: Add the constant**

In `mcpbrain/chunking.py`, near the top:

```python
# Version of the chunking pipeline that produced a chunk. Stamped into every
# chunk's metadata at write time and folded into the org pin's
# pipeline_fingerprint (which keys the shared-drive ingest-cache artifact
# filename AND gates ingest_cache.try_import).
#
# 1 -> 2 (spec 2, 2026-07-28): chunk_text no longer emits empty or oversize
#   chunks; tabular sources are chunked by header-repeating row group instead of
#   character-split; content-free text is never written as a chunk.
#
# Bumping this is what makes a chunking change VISIBLE: it invalidates every
# stale fleet cache artifact automatically, and `WHERE COALESCE(chunker_version,
# 0) < CHUNKER_VERSION` becomes the level-triggered selector bin/repair.py walks.
# Bump it whenever chunk boundaries or chunk admission change.
CHUNKER_VERSION = 2
```

- [ ] **Step 4: Stamp it at all four write sites**

Each site already builds a metadata dict per chunk. Add `"chunker_version": CHUNKER_VERSION` to the base metadata in:

- `sync/normalise.py` → `normalise_gmail`'s `base_metadata`
- `sync/drive.py` → `normalise_drive`'s `base_meta`
- `sync/calendar.py` → `normalise_calendar`'s `meta`
- `sync/attachments.py` → `normalise_attachment`'s `base`

importing `CHUNKER_VERSION` from `mcpbrain.chunking` alongside the existing `chunk_text` / `content_hash` / `has_content` imports.

**Do not** add it in `tabular.py`'s metadata extras — those merge on top of `base_meta`, and duplicating it in two places is how the two would eventually disagree.

- [ ] **Step 5: Bump the org pin**

In `mcpbrain/org_defaults.py`, add (or update, if a chunker-version default already lives there under another name — read the module first and follow its naming):

```python
# Must equal str(chunking.CHUNKER_VERSION) — pinned by
# test_the_org_pin_chunker_version_matches_the_code. The pin feeds
# pipeline_fingerprint, so a drift here means installs keep importing
# ingest-cache artifacts built by a superseded chunker.
ORG_PIN_CHUNKER_VERSION = "2"
```

and wire it wherever the pin's other fields (`embed_model`, `dim`, `enrich_logic_floor`) are defaulted, so `config.py:1015`'s `chunker_version=raw.get("chunker_version", "")` picks up the default rather than an empty string. Read that call site and match how the neighbouring fields do it.

**This changes the ingest-cache artifact path for every install.** That is the intended effect (decision 1) and needs no other code: the first sync after the bump misses the cache, extracts locally under the new chunker, and republishes. Note it in the commit message so it is not mistaken for a regression when cache-hit rates drop to zero for one round.

- [ ] **Step 6: Add the selector**

In `mcpbrain/store.py`, beside `reflow_outdated_chunks` (whose shape this deliberately mirrors):

```python
    def stale_chunker_file_ids(self, version: int, limit: int) -> list[str]:
        """Drive `file_id`s with at least one chunk written by an older chunker.

        The level-triggered selector for bin/repair.py's re-ingest phase: no
        queue, no cursor, no new state. Re-running walks forward because a
        repaired file stops matching, and an interrupted run simply resumes —
        the same property that made reflow_outdated_chunks the right shape for
        change-driven re-extraction.

        Distinct file_ids (not doc_ids) because re-ingest operates per FILE: one
        Drive fetch replaces all of that file's chunks at once.

        Drive-only. Gmail is 2% of the corpus and its chunking defects are ~75
        rows the purge deletes outright, so re-fetching a mailbox to re-chunk
        them is not a trade worth making; Gmail chunks pick up the new version as
        they naturally re-sync.

        Ordered by MIN(rowid) so the oldest, least-recently-touched files repair
        first and progress is monotonic across runs.
        """
        with self._connect() as db:
            rows = db.execute(
                "SELECT json_extract(metadata,'$.file_id') AS fid, MIN(rowid) AS r "
                "FROM chunks "
                "WHERE json_extract(metadata,'$.source_type')='gdrive' "
                "  AND json_extract(metadata,'$.file_id') IS NOT NULL "
                "  AND COALESCE(json_extract(metadata,'$.chunker_version'),0) < ? "
                "GROUP BY fid ORDER BY r LIMIT ?",
                (int(version), int(limit)),
            ).fetchall()
        return [r["fid"] for r in rows]
```

**Index check:** `idx_chunks_fileid` (an expression index on `metadata.$.file_id`, added in 0.7.105) exists, but this query filters on `chunker_version`, which has none — so it plans as a `SCAN chunks` over ~196k rows. Measure it before deciding: run `EXPLAIN QUERY PLAN` and time the real call against a copy-free read-only connection to the live store. If it exceeds ~2 s, add a matching expression index in `init()` following the exact pattern of the four added in 0.7.105, and say in the commit message what it measured before and after. Do **not** add the index speculatively.

- [ ] **Step 7: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_chunk_metadata.py tests/test_org_contracts.py tests/test_chunking.py tests/test_normalise.py tests/test_drive_extraction.py tests/test_calendar_sync.py tests/test_attachments.py tests/test_ingest_cache_lifecycle.py -q -p no:randomly`
Expected: PASS. Then remove the stamp from ONE write site, confirm that site's test fails, restore.

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/chunking.py mcpbrain/sync/ mcpbrain/org_defaults.py mcpbrain/config.py \
        mcpbrain/store.py tests/test_chunk_metadata.py tests/test_org_contracts.py
git commit -m "feat(chunking): stamp chunker_version on every chunk; bump the pin to 2

Spec 2 changed chunking materially and left chunker_version unbumped, so the
store could not answer 'which of my chunks predate the current chunker' — the
question the whole repair selects on — and fleet members kept importing
pre-spec-2 ingest-cache artifacts with no way to know.

The bump does three jobs: it invalidates every stale cache artifact for free
(pipeline_fingerprint already folds chunker_version into the artifact path and
the import gate), it makes 'COALESCE(chunker_version,0) < CHUNKER_VERSION' the
level-triggered repair selector, and it makes future chunking changes
self-describing.

Expect cache-hit rates to drop to zero for one sync round per install — that is
the invalidation working, not a regression."
```

---

## Gate 2 — Task 2: Purge and retrieval dedup

Runs in parallel with Task 3.

**Files:**
- Modify: `mcpbrain/store.py`, `mcpbrain/retrieval.py`
- Test: `tests/test_purge.py` (create), `tests/test_store.py`, `tests/test_retrieval.py`

**Interfaces:**
- Consumes: nothing from Task 1 (deliberately — this task is about content, not versions, so the two waves stay independent).
- Produces: `store.content_free_doc_ids(limit)`, `store.count_content_free()`, `store.purge_doc_ids(doc_ids)`, and `hybrid_search`'s content-hash dedup.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_purge.py`:

```python
"""68,193 of 196,396 live chunks (34.7%) contain no alphanumeric character at
all — ~2,000-char strings of '| | | | |' from empty spreadsheet cells, every one
embedded, none matchable by any query. 67,210 of them share a single
content_hash, which alone accounts for 63% of the store's 106,357 redundant
copies.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_content_free_selection_finds_pipes_and_spares_real_content(tmp_path):
    store = _store(tmp_path)
    store.upsert_chunk("d-pipes", "|  |  |  |\n|  |  |  |", "h1", {})
    store.upsert_chunk("d-sep", "| --- | --- |", "h2", {})
    store.upsert_chunk("d-real", "| Rent | 500 |", "h3", {})
    store.upsert_chunk("d-cjk", "| 会議 |", "h4", {})

    doomed = set(store.content_free_doc_ids(limit=100))

    assert doomed == {"d-pipes", "d-sep"}
    assert store.count_content_free() == 2


def test_purge_clears_the_vector_and_fts_mirrors(tmp_path):
    """The findings register is explicit that a purge 'must clear the matching
    vector and FTS rows'. A chunk row deleted while its vec_chunks row survives
    leaves a dangling embedding the kNN arm can still return."""
    store = _store(tmp_path)
    store.upsert_chunk("d1", "|  |  |", "h1", {})
    store.write_embedding("d1", [0.1, 0.2, 0.3, 0.4])

    assert store.purge_doc_ids(["d1"]) == 1

    assert store.get_chunk("d1") is None
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM fts_chunks WHERE doc_id='d1'").fetchone()[0] == 0


def test_purge_refuses_a_doc_id_the_graph_cites(tmp_path):
    """store.delete_chunks deliberately does NOT touch graph rows ('invalidation
    is a separate, bitemporal step'), so deleting a cited chunk leaves dangling
    provenance. Measured on the live store: ZERO of the 68,193 content-free
    chunks are cited. This asserts that at runtime rather than trusting the
    measurement — if a future chunk shape ever gets cited, the purge must stop,
    not silently orphan the graph."""
    store = _store(tmp_path)
    store.upsert_chunk("d-cited", "|  |  |", "h1", {})
    with store._connect(write=True) as db:
        db.execute("INSERT INTO entity_relations"
                   "(source_id,target_id,kind,source_doc_id,valid_from) "
                   "VALUES(1,2,'mentioned_with','d-cited','2026-01-01')")

    import pytest
    with pytest.raises(ValueError, match="cited"):
        store.purge_doc_ids(["d-cited"])

    assert store.get_chunk("d-cited") is not None, "nothing may be deleted on refusal"


def test_purge_is_all_or_nothing(tmp_path):
    """A partial purge would leave the caller unable to say what happened."""
    store = _store(tmp_path)
    store.upsert_chunk("d-ok", "|  |", "h1", {})
    store.upsert_chunk("d-cited", "|  |", "h2", {})
    with store._connect(write=True) as db:
        db.execute("INSERT INTO entity_relations"
                   "(source_id,target_id,kind,source_doc_id,valid_from) "
                   "VALUES(1,2,'mentioned_with','d-cited','2026-01-01')")

    import pytest
    with pytest.raises(ValueError):
        store.purge_doc_ids(["d-ok", "d-cited"])

    assert store.get_chunk("d-ok") is not None
```

Add to `tests/test_retrieval.py`:

```python
def test_hybrid_search_returns_one_hit_per_distinct_content(tmp_path, monkeypatch):
    """38,164 redundant copies survive the content-free purge: genuine duplicate
    FILES (the asset register exists three times in Drive). Deleting two of the
    three is the wrong fix — doc_ids are positional and cited as graph
    provenance, so it would make that file unfindable and orphan its rows. The
    real harm is recall crowding, and this is where crowding is fixed."""
    from mcpbrain.retrieval import hybrid_search
    from mcpbrain.store import Store

    class _Emb:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    emb = _Emb()
    body = "The fixed asset register for the 2023 financial year."
    for n, fid in enumerate(("a", "b", "c")):
        store.upsert_chunk(f"gdrive-{fid}-0", body, "same-hash",
                           {"source_type": "gdrive", "file_id": fid,
                            "file_name": f"Asset Register{'' if n == 0 else f' ({n})'}.xlsx"})
        store.embed_doc(f"gdrive-{fid}-0", emb, home=str(tmp_path))
    store.upsert_chunk("gdrive-z-0", "Unrelated minutes of the board meeting.",
                       "other-hash", {"source_type": "gdrive", "file_id": "z"})
    store.embed_doc("gdrive-z-0", emb, home=str(tmp_path))

    hits = hybrid_search(store, emb, "asset register", limit=10)

    hashes = [h["content_hash"] for h in hits if h.get("content_hash")]
    assert len(hashes) == len(set(hashes)), (
        f"duplicate content crowded the result set: {hashes}"
    )
    assert sum(1 for h in hits if h["doc_id"].startswith("gdrive-") and
               h["doc_id"] != "gdrive-z-0") == 1


def test_dedup_keeps_the_best_ranked_representative(tmp_path, monkeypatch):
    """Which copy survives matters: dropping the top-ranked one would lower the
    result's quality while claiming to improve it."""
    from mcpbrain import retrieval

    hits = [{"doc_id": "d1", "content_hash": "h", "score": 0.9},
            {"doc_id": "d2", "content_hash": "h", "score": 0.5},
            {"doc_id": "d3", "content_hash": "other", "score": 0.7}]

    out = retrieval._dedupe_by_content(hits)

    assert [h["doc_id"] for h in out] == ["d1", "d3"]


def test_dedup_passes_through_hits_with_no_content_hash(tmp_path):
    """Not every producer sets it; a missing hash must never collapse rows."""
    from mcpbrain import retrieval

    hits = [{"doc_id": "d1", "score": 0.9}, {"doc_id": "d2", "score": 0.5}]

    assert len(retrieval._dedupe_by_content(hits)) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_purge.py tests/test_retrieval.py -q -p no:randomly`
Expected: `AttributeError: content_free_doc_ids`, `AttributeError: _dedupe_by_content`, and the crowding assertion failing with three identical hashes.

- [ ] **Step 3: Add the purge primitives**

In `mcpbrain/store.py`:

```python
    # Chunk text carrying no alphanumeric character at all. 68,193 of 196,396
    # live chunks (34.7%) match: ~2,000-char strings of '| | | | |' from empty
    # spreadsheet cells, all embedded, none matchable. GLOB (not LIKE) because
    # LIKE has no character-class syntax in SQLite. Full scan by necessity —
    # this runs from an attended CLI, not the recall path.
    _CONTENT_FREE = "text NOT GLOB '*[A-Za-z0-9]*'"

    def count_content_free(self) -> int:
        """How many chunks carry no alphanumeric character."""
        with self._connect() as db:
            return db.execute(
                f"SELECT COUNT(*) FROM chunks WHERE {self._CONTENT_FREE}"
            ).fetchone()[0]

    def content_free_doc_ids(self, limit: int) -> list[str]:
        """Up to `limit` content-free chunk doc_ids, oldest first."""
        with self._connect() as db:
            return [r["doc_id"] for r in db.execute(
                f"SELECT doc_id FROM chunks WHERE {self._CONTENT_FREE} "
                "ORDER BY rowid LIMIT ?", (int(limit),))]

    def cited_doc_ids(self, doc_ids) -> list[str]:
        """Which of `doc_ids` the graph cites as provenance.

        `delete_chunks` deliberately does not touch graph rows — invalidation is
        a separate, bitemporal step — so deleting a cited chunk leaves dangling
        provenance that nothing cleans up. This is the check that makes a purge
        safe rather than merely measured-safe.
        """
        doc_ids = list(doc_ids)
        if not doc_ids:
            return []
        marks = ",".join("?" * len(doc_ids))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT source_doc_id AS d FROM entity_relations "
                f"WHERE source_doc_id IN ({marks}) "
                f"UNION SELECT message_id AS d FROM email_entities "
                f"WHERE message_id IN ({marks})",
                (*doc_ids, *doc_ids)).fetchall()
        return [r["d"] for r in rows]

    def purge_doc_ids(self, doc_ids) -> int:
        """Delete these chunks and their vec/FTS mirrors. Returns rows deleted.

        Raises ValueError, deleting NOTHING, if the graph cites any of them —
        all-or-nothing so the caller can always say what happened. Delegates the
        actual delete to `delete_chunks`, which already clears both mirrors.
        """
        doc_ids = list(doc_ids)
        if not doc_ids:
            return 0
        cited = self.cited_doc_ids(doc_ids)
        if cited:
            raise ValueError(
                f"refusing to purge {len(cited)} doc_id(s) cited as graph "
                f"provenance (e.g. {cited[:3]}); graph invalidation is a "
                "separate bitemporal step and this would orphan those rows")
        return self.delete_chunks(doc_ids)
```

- [ ] **Step 4: Add the retrieval dedup**

In `mcpbrain/retrieval.py`:

```python
def _dedupe_by_content(hits: list[dict]) -> list[dict]:
    """Keep one hit per distinct `content_hash`, best-ranked first.

    54% of the live store is redundant copies. The content-free purge removes
    68,193 of the 106,357, but ~38,164 remain and are genuine duplicate FILES —
    the fixed asset register exists three times in Drive (two identical names
    plus a '(1)' copy), each chunked independently. Three identical hits
    consuming three of ten slots is a real recall loss.

    Deleting the duplicate chunks instead would be wrong: doc_ids are positional
    (gdrive-<file_id>-<i>) and cited as graph provenance, so removing one file's
    copy makes THAT file unfindable by name, folder or file_id and orphans its
    rows. Crowding is a ranking problem, so it is fixed in the ranker —
    reversibly, with nothing lost.

    Hits are assumed already ordered best-first; a hit with no content_hash
    passes through untouched (not every producer sets it, and collapsing on a
    missing key would silently drop rows).
    """
    seen: set[str] = set()
    out: list[dict] = []
    for hit in hits:
        h = hit.get("content_hash")
        if h:
            if h in seen:
                continue
            seen.add(h)
        out.append(hit)
    return out
```

Then call it in `hybrid_search` **after** ranking and **before** the `limit` truncation, so dedup frees slots for genuinely different content rather than shortening the result list. Read the tail of `hybrid_search` and place it accordingly; if the function currently truncates before returning, the dedup goes immediately before that slice, and the candidate pool feeding it must not already be capped at `limit` — check, and widen the pool if it is (this is the same mistake the 0.7.103 expansion fix and the 0.7.110 open-actions fix both had to undo: truncating an ordered set before filtering it).

Ensure `content_hash` is present on hit dicts. If `hybrid_search`'s rows do not select it, add it to the SELECT — and say so in the commit message, because a missing key would make the dedup a silent no-op that still passes a unit test on synthetic dicts.

- [ ] **Step 5: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_purge.py tests/test_store.py tests/test_retrieval.py tests/test_brain_search_score.py tests/test_semantic.py tests/test_memory_tier.py -q -p no:randomly`
Expected: PASS.

Then: comment out the `_dedupe_by_content` call in `hybrid_search`, confirm `test_hybrid_search_returns_one_hit_per_distinct_content` fails (not just the unit test on synthetic dicts — the integration one), restore.

- [ ] **Step 6: Gold check**

Run: `uv run python tests/eval/run_eval.py --gold --k 10`
Expected: **at or above 0.700 / 0.510** (today's measured baseline — note CLAUDE.md's 0.750/0.556 is stale; the drift predates this work and is unexplained). Dedup can only *raise* recall by freeing slots, so a drop here means the dedup is discarding the wrong representative or the pool is capped before dedup. Record the number in the commit message either way.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/store.py mcpbrain/retrieval.py tests/test_purge.py \
        tests/test_store.py tests/test_retrieval.py
git commit -m "feat(store): safe content-free purge; dedupe recall by content hash

68,193 of 196,396 live chunks (34.7%) carry no alphanumeric character — empty
spreadsheet cells, all embedded, none matchable — and 67,210 share one hash.
purge_doc_ids clears the vec and FTS mirrors and REFUSES, deleting nothing, if
the graph cites any doc_id (delete_chunks does not touch graph rows, so that
would orphan provenance; measured today: zero of the 68,193 are cited, and this
asserts it rather than trusting the measurement).

The ~38,164 duplicates that survive are genuine duplicate FILES, and deleting
them would make those files unfindable and orphan their rows. Crowding is a
ranking problem, so hybrid_search now keeps one hit per distinct content_hash."
```

---

## Gate 2 — Task 3: Targeted re-ingest and the cache-import orphan sweep

Runs in parallel with Task 2.

**Files:**
- Modify: `mcpbrain/sync/drive.py`, `mcpbrain/ingest_cache.py`
- Test: `tests/test_drive_sync.py`, `tests/test_ingest_cache_lifecycle.py`

**Interfaces:**
- Consumes: `drive.fetch_content`, `drive.normalise_drive`, `drive.upsert_file_chunks`, `drive.folder_path` (all spec 2).
- Produces: `drive.reingest_files(service, store, file_ids, *, bulk_section=None, report=None) -> dict`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_drive_sync.py`:

```python
def test_reingest_files_replaces_a_files_chunks_from_a_fresh_fetch(tmp_path, monkeypatch):
    """There is no targeted re-ingest path: backfill_drive filters on
    modifiedTime, and the delta sync only sees CHANGED files — so a file whose
    content is fine but whose CHUNKING is out of date can never be revisited.
    455 clipped spreadsheets and 9,351 legacy files need exactly that."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    # Pre-existing chunks from the old chunker: more of them, and stale text.
    for i in range(4):
        store.upsert_chunk(f"gdrive-f1-{i}", f"|  |  | old {i} |", f"h{i}",
                           {"source_type": "gdrive", "file_id": "f1",
                            "chunk_index": i})

    class _Service:
        def files(self):
            return self

        def get(self, fileId, fields=None, supportsAllDrives=None):
            self._fid = fileId
            return self

        def get_media(self, fileId, supportsAllDrives=None):
            return self

        def execute(self):
            return {"id": "f1", "name": "Notes.txt", "mimeType": "text/plain",
                    "modifiedTime": "2026-07-01T00:00:00Z", "parents": []}

    monkeypatch.setattr(drive, "_fetch_text",
                        lambda service, meta: "Recovered prose content.")

    summary = drive.reingest_files(_Service(), store, ["f1"])

    assert summary["files"] == 1
    remaining = sorted(store.doc_ids_for_file("f1"))
    assert remaining == ["gdrive-f1-0"], f"stale chunks survived: {remaining}"
    assert "Recovered" in store.get_chunk("gdrive-f1-0")["text"]


def test_reingest_files_skips_a_file_that_no_longer_exists(tmp_path):
    """A file deleted from Drive since it was chunked must not abort the run or
    delete its chunks — that is the removal path's job, not the repair's."""
    from googleapiclient.errors import HttpError

    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-gone-0", "text", "h", {"source_type": "gdrive",
                                                      "file_id": "gone"})

    class _Resp:
        status = 404

    class _Service:
        def files(self):
            return self

        def get(self, **kw):
            return self

        def execute(self):
            raise HttpError(_Resp(), b"not found")

    summary = drive.reingest_files(_Service(), store, ["gone"])

    assert summary["missing"] == 1
    assert summary["files"] == 0
    assert store.get_chunk("gdrive-gone-0") is not None


def test_reingest_files_is_bounded_and_reports_per_file_failures(tmp_path, monkeypatch):
    """One unreadable file in 9,351 must not end the run."""
    from mcpbrain.store import Store
    from mcpbrain.sync import drive

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()

    class _Service:
        def files(self):
            return self

        def get(self, fileId=None, **kw):
            self._fid = fileId
            return self

        def execute(self):
            return {"id": self._fid, "name": f"{self._fid}.txt",
                    "mimeType": "text/plain", "parents": []}

    def _boom(service, meta):
        if meta["id"] == "bad":
            raise RuntimeError("extraction exploded")
        return "fine content"

    monkeypatch.setattr(drive, "_fetch_text", _boom)

    summary = drive.reingest_files(_Service(), store, ["ok1", "bad", "ok2"])

    assert summary["files"] == 2
    assert summary["failed"] == 1
```

Add to `tests/test_ingest_cache_lifecycle.py`:

```python
def test_a_cache_import_deletes_chunks_the_artifact_no_longer_has(tmp_path):
    """B5's remaining half. Spec 2 closed orphan-on-shrink for locally-extracted
    files (upsert_file_chunks) but deliberately skipped the cache-import path to
    avoid racing try_import's own transaction. So a shared-drive file that shrank
    and is served from cache still leaves indices n..m-1 searchable forever, and
    re-fed to expansion as current content.

    The sweep belongs INSIDE _import_artifact's transaction — the same one that
    writes the replacement rows — so there is no race to avoid."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    for i in range(4):
        store.upsert_chunk(f"gdrive-f1-{i}", f"old para {i}", f"h{i}",
                           {"source_type": "gdrive", "file_id": "f1",
                            "chunk_index": i})

    # Build a 1-chunk artifact for the same file and import it. Use this module's
    # existing artifact-construction helper rather than hand-rolling a second one.
    _import_one_chunk_artifact(store, file_id="f1", text="only para now")

    assert sorted(store.doc_ids_for_file("f1")) == ["gdrive-f1-0"], (
        "the cache import left the shrunk file's tail chunks behind"
    )


def test_a_cache_import_of_the_same_size_deletes_nothing(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "para", "h0",
                       {"source_type": "gdrive", "file_id": "f1", "chunk_index": 0})

    _import_one_chunk_artifact(store, file_id="f1", text="para updated")

    assert sorted(store.doc_ids_for_file("f1")) == ["gdrive-f1-0"]
```

**Read this test file first** and reuse whatever artifact-building helper it already has for `publish`/`try_import` round-trips (spec 2's work added several). Write `_import_one_chunk_artifact` as a thin wrapper over that, not a second implementation.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_drive_sync.py tests/test_ingest_cache_lifecycle.py -q -p no:randomly`
Expected: `AttributeError: reingest_files`, and the cache-import test showing four surviving chunks.

- [ ] **Step 3: Add `reingest_files`**

In `mcpbrain/sync/drive.py`, beside `backfill_drive`:

```python
def reingest_files(service, store, file_ids, *, bulk_section=None,
                   report: dict | None = None) -> dict:
    """Re-fetch and re-chunk specific Drive files by id.

    The mechanism the repair needs and the sync layer lacked: `sync_drive` only
    sees files the Changes API reports as MODIFIED, and `backfill_drive` filters
    on modifiedTime — so a file whose bytes are unchanged but whose CHUNKING is
    out of date (455 spreadsheets clipped at row 200, 9,351 files extracted by
    the pre-per-type extractor) could never be revisited by either.

    Per file: files().get for fresh metadata -> fetch_content -> normalise_drive
    -> upsert_file_chunks, which replaces the file's chunks and deletes the ones
    it no longer has (B5). Touches NO cursor, so it cannot disturb delta sync and
    is safe to interrupt.

    Deliberately bypasses the ingest cache. A cache hit would hand back the
    artifact for this content hash, which is what we are trying to replace — and
    after the chunker_version bump the fingerprint no longer matches anyway, so
    there is nothing to hit. Extraction is local and republishing happens through
    the normal sync path.

    Isolation is per FILE: a 404 (deleted since it was chunked) counts as
    `missing` and its chunks are LEFT ALONE — removal is the delta sync's job,
    not the repair's — and any other failure counts as `failed` and moves on.
    One unreadable file in 9,351 must not end the run.

    Returns {"files": n_reingested, "missing": n, "failed": n, "orphans": n}.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    fields = "id,name,mimeType,modifiedTime,owners,parents"
    folder_cache: dict = {}
    summary = {"files": 0, "missing": 0, "failed": 0, "orphans": 0}
    for fid in file_ids:
        try:
            fmeta = service.files().get(
                fileId=fid, fields=fields, supportsAllDrives=True).execute()
        except HttpError as exc:
            resp = getattr(exc, "resp", None)
            if resp is not None and resp.status == 404:
                log.info("reingest: %s no longer exists in Drive; leaving its "
                         "chunks for the delta sync's removal path", fid)
                summary["missing"] += 1
                continue
            raise
        try:
            content = fetch_content(service, fmeta, store=store, report=report)
            if content is None or (not content.text and not content.tables):
                log.info("reingest: %s yielded no content", fid)
                summary["failed"] += 1
                continue
            chunks = normalise_drive(
                fmeta, content.text, tables=content.tables,
                folder=folder_path(service, fmeta, folder_cache))
            if not chunks:
                summary["failed"] += 1
                continue
            with bulk_section():
                summary["orphans"] += upsert_file_chunks(
                    store, chunks, file_id=fid, partial=content.partial)
            summary["files"] += 1
        except Exception as exc:  # noqa: BLE001 — one file must not end the run
            log.warning("reingest: %s failed: %s", fid, exc)
            summary["failed"] += 1
    return summary
```

Check `fetch_content`'s real signature before writing this — spec 2 gave it a `report` parameter for aggregated skip rows; pass it through if present, drop the argument if not.

- [ ] **Step 4: Close B5 on the cache-import path**

In `mcpbrain/ingest_cache.py`'s `_import_artifact`, after the replacement rows are written and inside the **same** transaction, delete the file's chunks that the artifact does not contain:

```python
    # B5, cache-import half. Spec 2 closed orphan-on-shrink for locally
    # extracted files (drive.upsert_file_chunks) but skipped this path to avoid
    # racing this function's own transaction. Doing it HERE removes the race
    # entirely: it is the same transaction that writes the replacement rows.
    #
    # Without it, a shared-drive file that shrank and is served from cache keeps
    # indices n..m-1 searchable indefinitely, and expansion re-feeds them as
    # current content.
    written = {row["doc_id"] for row in rows}
    stale = [d for d in store.doc_ids_for_file(art.file_id) if d not in written]
    if stale:
        log.info("ingest_cache: %s shrank; deleting %d orphaned chunk(s)",
                 art.file_id, len(stale))
        store.delete_chunks(stale)
```

**Read `_import_artifact` first.** It builds a `rows` list then writes it; place this after the write and confirm whether it holds its own `_connect(write=True)` block or delegates. If it delegates to a store method that opens its own transaction, put the sweep in that store method instead so the atomicity claim in the comment is true. Do not write a comment asserting atomicity you have not verified.

- [ ] **Step 5: Run the tests and verify they discriminate**

Run: `uv run pytest tests/test_drive_sync.py tests/test_drive_shared.py tests/test_drive_changes.py tests/test_ingest_cache_lifecycle.py tests/test_fleet_storage_drive.py -q -p no:randomly`
Expected: PASS. Then remove the orphan sweep, confirm the cache-import test fails, restore.

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/sync/drive.py mcpbrain/ingest_cache.py tests/test_drive_sync.py \
        tests/test_ingest_cache_lifecycle.py
git commit -m "feat(drive): re-ingest specific files by id; close B5 on the cache path

sync_drive only sees files the Changes API reports modified and backfill_drive
filters on modifiedTime, so a file whose bytes are unchanged but whose chunking
is out of date — 455 spreadsheets clipped at row 200, 9,351 files from the
pre-per-type extractor — could never be revisited. reingest_files is that path:
per-file isolated, cursor-free, safe to interrupt.

Also closes the half of B5 spec 2 left open. The orphan sweep now runs inside
_import_artifact's own transaction, so a shared-drive file that shrank and is
served from cache no longer keeps its tail chunks searchable forever."
```

---

## Gate 3 — Task 4: The attended CLI

Solo. Depends on all three earlier tasks.

**Files:**
- Create: `bin/repair.py`
- Modify: `mcpbrain/doctor.py`
- Test: `tests/test_repair.py` (create), `tests/test_doctor.py`

**Interfaces:**
- Consumes: `store.{count_content_free, content_free_doc_ids, purge_doc_ids, stale_chunker_file_ids}` (Tasks 1, 2), `drive.reingest_files` (Task 3), `chunking.CHUNKER_VERSION` (Task 1), `backup.snapshot` (existing).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repair.py`:

```python
"""The repair CLI is attended and destructive; its guardrails are the tests.

Precedent: bin/consolidate.py — 91 lines, backup first, gold gate printed, all
logic in the library.
"""
import subprocess
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin" / "repair.py"


def _run(*args, home):
    return subprocess.run([sys.executable, str(_BIN), *args],
                          capture_output=True, text=True,
                          env={"MCPBRAIN_HOME": str(home), "PATH": ""})


def test_dry_run_is_the_default(tmp_path):
    """Nothing destructive may happen without an explicit --apply. The one
    guardrail that matters most: this operates on an 11 GB irreplaceable store."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "|  |  |", "h1", {})

    out = _run("purge-empty", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "dry run" in out.stdout.lower()
    assert Store(tmp_path / "brain.sqlite3", dim=4).get_chunk("d1") is not None


def test_apply_refuses_without_enough_free_disk(tmp_path, monkeypatch):
    """The backup is a full copy of an 11 GB file. A previous session filled this
    machine's disk to zero copying that database and the emergency cleanup
    destroyed an unrelated application's data. Refuse rather than risk it."""
    import bin.repair as repair

    monkeypatch.setattr(repair, "_free_bytes", lambda path: 1024)

    ok, why = repair.preflight(tmp_path / "brain.sqlite3", db_bytes=11 * 1024**3)

    assert ok is False
    assert "disk" in why.lower()


def test_purge_reports_what_it_would_do_without_doing_it(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    for i in range(3):
        store.upsert_chunk(f"d{i}", "|  |  |", f"h{i}", {})
    store.upsert_chunk("keep", "real content", "hk", {})

    out = _run("purge-empty", home=tmp_path)

    assert "3" in out.stdout
    assert Store(tmp_path / "brain.sqlite3", dim=4).count_content_free() == 3


def test_reingest_phase_reports_the_stale_file_count(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "legacy", "h1",
                       {"source_type": "gdrive", "file_id": "f1"})

    out = _run("reingest-stale", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "1" in out.stdout


def test_an_unknown_phase_exits_nonzero(tmp_path):
    out = _run("delete-everything", home=tmp_path)

    assert out.returncode != 0
```

Add to `tests/test_doctor.py`:

```python
def test_doctor_reports_repair_state(tmp_path, monkeypatch):
    """The repair's progress has to be visible without running the CLI, or
    'is it done?' becomes a guess."""
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "|  |  |", "h1", {})
    store.upsert_chunk("gdrive-f1-0", "legacy text", "h2",
                       {"source_type": "gdrive", "file_id": "f1"})

    assert store.count_content_free() == 1
    assert store.stale_chunker_file_ids(2, limit=10) == ["f1"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_repair.py tests/test_doctor.py -q -p no:randomly`
Expected: `FileNotFoundError` / `ModuleNotFoundError: bin.repair`.

- [ ] **Step 3: Write `bin/repair.py`**

Keep it thin — argument parsing, pre-flight, phase dispatch, printed gate. Mirror `bin/consolidate.py`'s shape (read it first; it is 91 lines).

```python
"""Attended, backup-gated corpus repair (curator-run). Spec 3.

Phases (each independently runnable, each idempotent):

  purge-empty     delete chunks carrying no alphanumeric character
                  (68,193 of 196,396 live chunks; 34.7%)
  reingest-stale  re-fetch and re-chunk Drive files whose chunks predate
                  chunking.CHUNKER_VERSION (455 clipped spreadsheets +
                  9,351 legacy files)
  status          report remaining work; changes nothing

DRY RUN IS THE DEFAULT. Pass --apply to write. --apply takes a full WAL-safe
backup first and refuses if free disk is under twice the database size: the
store is ~11 GB, and a previous session filled this machine's disk to zero
copying it, froze the machine, and the emergency cleanup destroyed an unrelated
application's data.

After --apply, run the gold gate and restore the printed backup if it regresses.
Every phase is resumable — the selectors are level-triggered, so an interrupted
run is simply re-run.
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config                     # noqa: E402
from mcpbrain.backup import snapshot            # noqa: E402
from mcpbrain.chunking import CHUNKER_VERSION   # noqa: E402
from mcpbrain.store import Store                # noqa: E402

_PURGE_BATCH = 5000


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def preflight(db_path: Path, *, db_bytes: int | None = None) -> tuple[bool, str]:
    """Refuse to --apply unless a backup can safely fit.

    Twice the database size: one for the backup, one for headroom (SQLite needs
    room for its WAL and for the snapshot's own temporary files).
    """
    if db_bytes is None:
        db_bytes = db_path.stat().st_size if db_path.exists() else 0
    need = db_bytes * 2
    free = _free_bytes(db_path.parent)
    if free < need:
        return False, (f"insufficient disk: need ~{need // 1024**3} GB "
                       f"(2x the {db_bytes // 1024**3} GB store), "
                       f"{free // 1024**3} GB free")
    return True, ""


def _backup(db_path: Path) -> Path:
    # WAL-safe: the store runs journal_mode=WAL, so a plain file copy can MISS
    # committed transactions. backup.snapshot uses the SQLite backup API.
    dest = db_path.with_suffix(db_path.suffix + f".bak-{int(time.time())}")
    return snapshot(db_path, dest)


def phase_status(store, apply: bool) -> None:
    empty = store.count_content_free()
    total = store.count_chunks()
    stale = len(store.stale_chunker_file_ids(CHUNKER_VERSION, limit=100_000))
    print(f"content-free chunks       : {empty} of {total} "
          f"({100 * empty / total:.1f}%)" if total else f"content-free: {empty}")
    print(f"Drive files to re-chunk   : {stale} (chunker_version < {CHUNKER_VERSION})")


def phase_purge_empty(store, apply: bool) -> None:
    total = store.count_content_free()
    print(f"[purge-empty] {total} content-free chunk(s) match")
    if not apply:
        print("[purge-empty] dry run — nothing deleted; pass --apply to write")
        return
    done = 0
    while True:
        batch = store.content_free_doc_ids(limit=_PURGE_BATCH)
        if not batch:
            break
        # purge_doc_ids raises, deleting nothing, if the graph cites any id.
        done += store.purge_doc_ids(batch)
        print(f"[purge-empty] {done}/{total}")
    print(f"[purge-empty] deleted {done}")


def phase_reingest_stale(store, apply: bool, *, limit: int) -> None:
    ids = store.stale_chunker_file_ids(CHUNKER_VERSION, limit=limit)
    print(f"[reingest-stale] {len(ids)} Drive file(s) selected (limit {limit})")
    if not apply:
        print("[reingest-stale] dry run — nothing fetched; pass --apply to write")
        return
    from mcpbrain.auth import build_google_services
    from mcpbrain.sync.drive import reingest_files
    services = build_google_services()
    drive = services.get("drive_service")
    if drive is None:
        # build_google_services omits a service whose scope the token lacks,
        # rather than failing the whole build — so this is a missing scope, not
        # a crash, and it must be said plainly.
        print("[reingest-stale] no drive_service (token lacks the Drive scope); "
              "re-authenticate with `mcpbrain setup`", file=sys.stderr)
        return
    print(f"[reingest-stale] {reingest_files(drive, store, ids)}")


_PHASES = {"status": phase_status, "purge-empty": phase_purge_empty,
           "reingest-stale": phase_reingest_stale}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=sorted(_PHASES))
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--limit", type=int, default=500,
                    help="max Drive files per reingest-stale run")
    args = ap.parse_args(argv)

    home = config.app_dir()
    db_path = Path(home) / "brain.sqlite3"
    if not db_path.exists():
        print(f"no store at {db_path}", file=sys.stderr)
        return 2

    if args.apply:
        ok, why = preflight(db_path)
        if not ok:
            print(f"[repair] refusing to apply: {why}", file=sys.stderr)
            return 3
        backup = _backup(db_path)
        print(f"[repair] backup written: {backup}")

    # Dim comes from the embedder, exactly as bin/consolidate.py:51 does it —
    # there is no config.embed_dim; the org pin's `dim` is a fleet-baseline
    # field, not this install's live dimension.
    from mcpbrain.embed import get_embedder
    store = Store(db_path, dim=get_embedder("bge-small").dim)
    fn = _PHASES[args.phase]
    if args.phase == "reingest-stale":
        fn(store, args.apply, limit=args.limit)
    else:
        fn(store, args.apply)

    if args.apply:
        print("\n[repair] Run the gold gate now (PRODUCTION path):\n"
              "  uv run python tests/eval/run_eval.py --gold --k 10\n"
              "  Baseline 2026-07-28: recall@10 0.700 / MRR 0.510.\n"
              f"  If it regresses, restore:  cp {backup} {db_path}\n"
              "  Once it passes and you are satisfied, delete the backup — it is\n"
              "  a full copy of the store and this machine has limited headroom.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Both external helpers above are already resolved against the real modules (`store.count_chunks` exists; `config.embed_dim` does **not** — the dim comes from `get_embedder`, as `bin/consolidate.py:51` does it; the Drive client comes from `auth.build_google_services()["drive_service"]`, which omits a service whose scope the token lacks rather than raising). `backup.snapshot` exists and is the WAL-safe path. No guessing required.

- [ ] **Step 4: Add the doctor lines**

In `mcpbrain/doctor.py`, alongside the existing store checks (match the surrounding line style — read a neighbouring check first):

```python
    empty = store.count_content_free()
    stale = len(store.stale_chunker_file_ids(CHUNKER_VERSION, limit=100_000))
    lines.append(f"{'✅' if not empty else '⚠️'} content-free chunks: {empty}")
    lines.append(f"{'✅' if not stale else '⚠️'} Drive files awaiting re-chunk: {stale}")
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_repair.py tests/test_doctor.py tests/test_purge.py tests/test_store.py -q -p no:randomly && uv run ruff check mcpbrain/ bin/ tests/test_repair.py`
Expected: PASS, ruff clean.

- [ ] **Step 6: Dry-run against the live store, read-only**

This is the one place the plan touches real data, and it must stay read-only:

```bash
uv run python bin/repair.py status
uv run python bin/repair.py purge-empty          # dry run: no --apply
uv run python bin/repair.py reingest-stale       # dry run: no --apply
```

Expected, against the 2026-07-28 baseline: `content-free chunks: ~68,193`, `Drive files to re-chunk: ~9,400` (the 9,351 legacy files plus the 455 clipped spreadsheets, minus overlap). If either number is wildly different, **stop and report** — it means a selector is wrong, and finding that out now costs nothing while finding it out after `--apply` costs the store.

Do **not** pass `--apply`. That is Josh's attended step.

- [ ] **Step 7: Commit**

```bash
git add bin/repair.py mcpbrain/doctor.py tests/test_repair.py tests/test_doctor.py
git commit -m "feat(repair): attended, backup-gated corpus repair CLI

Phases: purge-empty (68,193 content-free chunks), reingest-stale (Drive files
whose chunks predate CHUNKER_VERSION — 455 clipped spreadsheets + 9,351 legacy
files), status. Dry run is the default; --apply takes a WAL-safe backup first and
refuses if free disk is under twice the store size.

Every selector is level-triggered, so each phase is idempotent and an
interrupted run is simply re-run. Follows bin/consolidate.py: thin CLI, logic in
the library, gold gate printed with the restore command."
```

---

## The attended run (Josh's step, not a subagent's)

Once all four tasks are merged and reviewed:

1. **Check headroom.** `df -h /` — the backup is a full copy of an ~11 GB store. Needs ~22 GB free.
2. **`uv run python bin/repair.py status`** — confirm the numbers match the baseline table above.
3. **`uv run python bin/repair.py purge-empty --apply`** — note the printed backup path. Expect ~68,193 deletions and a substantial file-size drop after a `VACUUM` (which the CLI deliberately does not run: `VACUUM` needs free space equal to the whole database and must be a separate, deliberate step).
4. **Gold gate.** `uv run python tests/eval/run_eval.py --gold --k 10`. Baseline 0.700 / 0.510. A purge of content-free chunks should not move it at all; anything lower means restore.
5. **`uv run python bin/repair.py reingest-stale --apply --limit 500`**, repeatedly. 9,400 files at 500 per run is ~19 runs; each is independent and resumable. Re-run the gold gate every few hundred files — this phase *changes what is in the index*, so it is the one that can genuinely regress recall.
6. **Delete the backup** once satisfied.
7. **Stop and reassess if gold drops below 0.700 / 0.510 at any checkpoint.** The 455 recovered spreadsheets add thousands of row-group chunks, and design decision 6 of spec 2 flagged exactly this risk: semantically-similar ledger rows crowding recall. Retrieval dedup (Task 2) helps, but if crowding shows up, the answer is a ranking change, not abandoning the recovered content.

---

## Findings index

| Finding | Issue | Where |
|---|---|---|
| B1 (repair half) | 455 files clipped at row 200 | Task 3 mechanism, Task 4 phase |
| C7 | 9,351 legacy files below current extractor fidelity | Task 1 selector, Task 3 mechanism, Task 4 phase |
| D | 106,357 redundant copies (54%) | Task 2 — 68,193 purged, ~38,164 deduped at retrieval |
| B5 (remaining half) | Cache-import path leaves orphans on shrink | Task 3 |
| — (raised 2026-07-28) | `chunker_version` unbumped after spec 2 | Task 1 |
| B3 (residual) | 16,997 chunks over the embedder window | Shrinks as re-chunking proceeds; Task 4's `status` and `doctor` report it. No separate action — `chunk_text` and the row-group chunker both bound their output, so re-chunking is the fix. |

---

## Notes for the implementer

- **Gates are barriers; tasks within a gate are not.** Launch both Gate 2 tasks at once. They own disjoint files by construction. If a subagent needs a file its wave-partner owns, it must stop and report.
- **The tests are the specification.** Where a step gives both, the test is authoritative.
- **Revert-and-confirm on every new test**, and report it. Spec 2's review found a fix that was *"never reachable in production, its test passing only on a hardcoded literal"* — that is the failure mode this habit exists to catch.
- **Two places this plan says "read the real code first"** — `_import_artifact`'s transaction structure, and `hybrid_search`'s tail (specifically: whether its candidate pool is already capped at `limit` before dedup runs, which would make dedup shorten results instead of freeing slots — the same mistake the 0.7.103 expansion fix and the 0.7.110 open-actions fix each had to undo). Those are not hedges; writing them from the plan alone would produce a plausible-looking bug. Everything else, including every helper name in `bin/repair.py`, is already resolved against the real modules.
- **Never `cp` the database.** See the global constraints. Read-only URI connections only.
- **Do not run `--apply`.** Task 4 Step 6 is read-only by design; the attended run is Josh's.
- **Gold baseline is 0.700 / 0.510** as measured 2026-07-28. CLAUDE.md's 0.750 / 0.556 is stale — the drift predates this work and is still unexplained. Do not treat 0.700 as a regression to chase.
- **Do not push a release.** Source pushes are fine; `bin/release.py`, the dist wheel, the plugin sync and the five version files are not part of this plan.
