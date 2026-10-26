# Oversized chunks, the held reflow owner, and the ingestion-integrity parked minors

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Doctor reads 0 oversized-and-truncated chunks and 0 held reflow owners on every install, and the six minors parked by the 2026-10-22 ingestion-integrity work are fixed.

**Architecture:** Fix each producer so new chunks fit the 512-token embedder window, heal existing rows once per install (reflow re-render where the source is reachable, multi-window embedding for everything else), and close the parked minors in the modules that own them.

**Tech Stack:** Python 3.12, SQLite (STRICT tables), fastembed bge-small, pytest.

**Spec:** this document's "Findings" section (owner request 2026-10-26: "fix the 193 oversized chunks and the 1 stuck file and the six small parked review items"); parked items as recorded in `docs/superpowers/specs/2026-10-22-ingestion-integrity-design.md` and the 2026-10-23 owner-fix re-review (M-N1..M-N3, N5, N7, mentions).

## Findings (live store, read-only, 2026-10-26)

`doctor` counts chunks with `length(text) > 2000` (`index.EMBED_WINDOW_CHARS`). 193:

| Group | Chunks | Cause |
|---|---|---|
| Gmail, 1 message (an Uber receipt) | 79 | `<style>` CSS text kept by `normalise.strip_html`. `beautifulsoup4` is NOT a dependency, so every install uses the regex fallback, which strips tags but keeps the CONTENT of `<style>`/`<script>`/`<head>`/comments. 523 messages (1,531 chunks) carry CSS text; this one is oversize. Its reflow outcome is `source_gone` (message no longer fetchable). |
| Drive spreadsheets (`content_subtype='table'`), 7 files | 98 | 91 row chunks up to 2,321 chars: 40 fields with long header names stay over budget even at the 5-char cell floor in `tabular._fit_row_sentence`. 7 summary chunks up to 5,400 chars: `_summary_text`'s `Columns:` list is unbounded (64-68 columns). |
| `enriched-*` digests | 15 | LLM-written, 2,013-3,601 chars. Derived (cold) but embedded. |
| `note-core-identity-seed` | 1 | 2,836 chars, deliberately one row at a fixed doc_id (CLAUDE.md: never split it). |

Held reflow owner: one shared-drive Google Doc (`application/vnd.google-apps.document`). Its text was replaced with images; `modifiedTime` changed, so the reflow routes it to the ordinary handler, `fetch_content` returns an EMPTY `Content` (export succeeded, no text), and both `handle_drive_item` and `_cache_first_extract_one` return without writing. The 8 stale v1 chunks of text that no longer exists stay; the selector re-matches them; the hold repeats daily forever.

Parked minors:
- **M-N1** a permanently failing embedder factory makes `Daemon._run_owner_action_repair` warn and retry every tick (60 s) with no backoff.
- **M-N2** `action_owner.undo_owner_repair` is inexact: it restores `prior_owner` (which may hold the clean-up's older value, COALESCE keeps the first) and reopens rows with `resolved_by=''`/`resolved_at=''` rather than their real prior values.
- **M-N3** the daemon's embedder skip (loading/failed) also skips `canonicalise`, which needs no model.
- **N5** `/api/recall` writes its exposure rows BEFORE it answers, so during a long bulk write the prompt hook times out.
- **N7** salience scored before M6 (owner boost follows source_class) is never recalculated: derived chunks that the old rule boosted keep +1.5.
- **mentions** `sources_backfill` recounts email_count but leaves `entities.mentions` as the inflated per-extraction bump count.

## Global Constraints

- Never commit real people's names, name-derived entity ids, document titles or email addresses from the live store; tests use the neutral cast (Dana Okafor, dana.okafor@example.org, Northgate Trust). `tests/test_no_tenant_literals.py` must pass.
- Scope test runs to the files you touch and their direct tests (`uv run pytest tests/<file> -q`); the owner runs the full suite.
- `uv run ruff check mcpbrain tests` clean for touched files.
- No version bump, no release, no push of a release. Commit on `main` with a conventional message ending in the session's attribution lines.
- Do NOT bump `CHUNKER_VERSION`, `SPLIT_VERSION` or any `EXTRACTION_VERSIONS` entry: a bump re-chunks every owner of that class on every install. Heal is targeted (Task 5).
- Every automated `UPDATE entities SET` must go through the field-lock guard (existing grep test).
- Clean-up steps log every changed value to `cleanup_log` (table, row key, column, old, new) in the same transaction, so `--undo STEP` reverses them.
- `index.EMBED_WINDOW_CHARS = 2000` measures the PREFIXED passage (contextual prefix + text).

## Review Focus

1. A sheet with 64 columns and headers over 60 chars each: no rendered chunk (summary or rows) exceeds `max_chars`, and no cell value is lost that fit before (Task 2).
2. An HTML-only marketing email with `<style>`, MSO conditional comments (`<!--[if mso]>…<![endif]-->`) and `<head><title>`: the body keeps every visible sentence and contains no CSS (Task 1).
3. A Drive PDF whose OCR comes back empty must KEEP its chunks (extractor limitation); only a Google-native file that changed and exports empty loses them (Task 4).
4. Multi-window embedding of a chunk whose prefix alone is near the window: still terminates, every piece fits, vector is unit-length (Task 3).
5. An install already `done` with the clean-up runs ONLY the new steps once; a fresh install runs all steps once; neither reruns (Task 8).

---

### Task 1: strip_html drops non-visible HTML content

**Files:**
- Modify: `mcpbrain/sync/normalise.py` (`strip_html`, regex fallback)
- Test: `tests/test_normalise_html.py` (create, or extend the existing strip_html tests if a file already covers it — grep `strip_html` in tests/)

- [ ] **Step 1: failing tests** — with bs4 forced unavailable (monkeypatch `builtins.__import__` or `sys.modules['bs4'] = None`):
  - `test_regex_fallback_drops_style_and_script`: `'<html><head><title>T</title><style>body{margin:0}@font-face{font-family:x}</style></head><body><p>Hi Dana,</p><script>var a=1;</script><p>Your trip cost $12.</p></body></html>'` → result contains `"Hi Dana,"` and `"Your trip cost $12."`, and none of `"margin"`, `"font-family"`, `"var a"`, `"T"` as a line.
  - `test_regex_fallback_drops_comments_and_mso_conditionals`: `'<p>A</p><!--[if mso]><style>td{x:1}</style><![endif]--><!-- tracking --><p>B</p>'` → lines exactly `["A", "B"]`.
  - `test_regex_fallback_keeps_inline_sentence` (existing I5 behaviour still holds): `'<p>Hi <b>Sam</b>, can you confirm <a href="x">the booking</a>?</p>'` → `"Hi Sam, can you confirm the booking?"`.
  - Same three inputs through the bs4 path when bs4 is importable (`pytest.importorskip("bs4")`) give the same visible text (comments must be removed there too).
- [ ] **Step 2: implement** — before the tag regex, remove (case-insensitive, DOTALL, non-greedy) whole `<!--…-->` comments (this covers MSO conditionals) and the full element CONTENT of `script`, `style`, `head`, `title`, `noscript`, `template`, `svg`. In the bs4 branch also remove `Comment` nodes. Keep the module's single-pass, no-new-dependency style.
- [ ] **Step 3:** run the tests + `tests/test_gmail*` and `tests/test_normalise*`; commit `fix(gmail): strip_html drops style/script/head/comment content`.

### Task 2: tabular rendering never exceeds the chunk budget

**Files:**
- Modify: `mcpbrain/sync/tabular.py` (`_summary_text`, `render_chunks`, `_fit_row_sentence`)
- Test: `tests/test_tabular.py` (extend)

**Interfaces:** `render_chunks(tables, *, file_name, max_chars) -> list[tuple[str, dict]]` unchanged. Output for a table where nothing overflows today MUST be byte-identical (existing tests pin this; add one that renders a normal 5-column sheet and compares to a frozen expected string if none exists).

- [ ] **Step 1: failing tests**
  - `test_wide_sheet_summary_fits`: 68 headers of ~70 chars, 3 rows → every emitted text `len <= max_chars` (use `CHUNK_CHARS`); the union of the summary chunks' `Columns:` entries equals all non-blank headers in order; continuation summary chunks have `table_role == "summary"` and a `summary_part` (1-based) in their metadata; the first summary chunk still carries `Rows:`/`Totals:`.
  - `test_wide_row_splits_instead_of_overflowing`: 40 headers of ~60 chars, one row of 40 short values → every chunk `<= max_chars`; every `Header: value` pair appears exactly once across the row chunks; the continuation chunks' titles read `### Sheet: <sheet> — row N (cont.)`-style (exact text your choice, must include the row number) and carry `row_start == row_end == N`.
  - `test_narrow_sheet_output_unchanged`: a 5-column, 30-row sheet renders exactly as before (freeze the expected list from the current implementation BEFORE changing code).
- [ ] **Step 2: implement**
  - Summary: if the joined summary exceeds `max_chars`, split the `Columns:` list across additional summary chunks (`### Sheet summary: <sheet> (<file>) — columns cont.` + `Columns: …`), each `<= max_chars`; header lines first chunk only. `_summary_text` takes `max_chars` (callers updated).
  - Rows: `_fit_row_sentence` keeps its cell-cap halving but, when the sentence still exceeds the budget at the 5-char floor, the row is instead rendered at the NORMAL cell cap and split at `"; "` field boundaries into pieces each `<= row budget`, emitted as their own chunk(s) (flush the current group first). Never truncate a cell to make a row fit when splitting would keep it whole; the 300-char `_MAX_CELL_CHARS` cap and the 40-field valve stay as they are.
- [ ] **Step 3:** run `tests/test_tabular.py tests/test_drive*.py tests/test_attachments*.py -q`; commit `fix(tables): split wide summaries and rows so no table chunk exceeds the window`.

### Task 3: multi-window passage embedding

**Files:**
- Modify: `mcpbrain/embed.py` (new function), `mcpbrain/index.py` (`index_pending`), `mcpbrain/store.py` (the single-doc embed at ~3660-3673), `mcpbrain/sync/reflow_handler.py` (`ReflowContext._embed`)
- Test: `tests/test_embed_windows.py` (create)

**Interfaces:**
- Produces: `embed.embed_chunk_passages(embedder, items: list[tuple[str, dict]], *, use_prefix: bool, window: int = index.EMBED_WINDOW_CHARS) -> list[list[float]]` — one unit vector per item. Avoid a circular import (define the window constant where both can import it, or pass it).

- [ ] **Step 1: failing tests** (fake embedder: deterministic vector per string, records every passage it is asked to embed)
  - item whose prefixed text fits → embedder sees exactly `prefix + text` once; returned vector equals the embedder's vector (behaviour unchanged for normal chunks, including batching: N short items → ONE `embed_passages` call).
  - 5,000-char text → every passage sent is `<= window`, each begins with the same prefix, the pieces (prefix stripped) concatenate back to the original text (`chunking.split_lossless`), result is the L2-normalised mean of the piece vectors.
  - prefix longer than `window - 200`: pieces still get at least 200 chars of text each (prefix truncated, never an infinite loop or empty piece).
  - `use_prefix=False`: no prefix anywhere.
  - `index_pending` on a store with one 5,000-char chunk logs no "exceed the … window" warning and writes a vector.
- [ ] **Step 2: implement** — collect all passages for the batch, call `embed_passages` once, regroup per item, mean + normalise for multi-piece items. Replace the oversize warning in `index_pending` with a DEBUG count of multi-window chunks. Use the function in the three call sites (index, store single-doc embed, reflow `_embed`).
- [ ] **Step 3:** run `tests/test_embed_windows.py tests/test_index*.py tests/test_reflow*.py -q`; commit `feat(embed): embed long chunks across windows instead of truncating`.

### Task 4: a changed Google-native Drive file that exports empty loses its stale chunks

**Files:**
- Modify: `mcpbrain/sync/drive.py` (`handle_drive_item`, `_cache_first_extract_one`, a shared helper)
- Test: `tests/test_drive_empty_native.py` (create; reuse fakes from existing drive handler tests)

**Interfaces:** Produces `drive.drop_emptied_native(store, fmeta, content, *, drive_id: str | None) -> bool` (name your choice): True when it removed chunks.

- [ ] **Step 1: failing tests**
  - My Drive: stored chunks for file F at `modified="2026-01-01T00:00:00Z"`; file meta now `mimeType` google doc, `modifiedTime` later; `fetch_content` returns a non-partial `Content` with empty text, no tables → after `handle_drive_item` F has no chunks, and `invalidate_sources_for_docs` ran with `reason="source_gone"` (assert an open action on F's doc id is `auto_archived`).
  - Same on the shared-drive path (`handle_shared_drive_item`, no cache hit) — only chunks whose `drive_id` equals this drive are removed; nothing published.
  - KEEP cases (chunks untouched): (a) `application/pdf` empty extraction; (b) native doc, empty, but `modifiedTime` equal to the stored `modified`; (c) `content.partial` true; (d) `fetch_content` returned None (unsupported).
  - Reflow integration: a `reflow:drive` row for such a file → `ReflowContext.handle` routes to the ordinary path → owner has no chunks → `store.reflow_live_counts` no longer counts it held.
- [ ] **Step 2: implement** — Google-native = mimeType starting `application/vnd.google-apps.`. Reuse the remove-event code path (invalidate + `delete_chunks`, shared-drive scoping by `drive_id`) — extract it into the helper rather than duplicating it. Log one INFO line per file.
- [ ] **Step 3:** run the new test + `tests/test_drive*.py tests/test_reflow*.py -q`; commit `fix(drive): a changed native file that exports empty drops its stale chunks`.

### Task 5: one-shot heal of existing oversized and CSS-polluted chunks; honest doctor line

**Files:**
- Modify: `mcpbrain/store.py` (selector + reset helpers), `mcpbrain/daemon.py` (`_run_reflow_seed` / `_reflow_seed_once`), `mcpbrain/doctor.py` (Chunk window line), `mcpbrain/probes.py` or `/api/status` builder if the reflow record is surfaced there
- Test: `tests/test_oversize_heal.py` (create), doctor test for the line

**Interfaces:**
- Consumes: Task 3 (windowed embedding), Task 1/2 (re-render now fits).
- Produces: meta key `oversize_heal` = JSON `{"state": "done", "at": iso, "gmail": n, "drive": n, "reembed": n}`; `Store.oversize_heal_targets() -> dict[str, list[str]]` (`{"reflow:gmail": [message_id…], "reflow:drive": [file_id…]}`), `Store.reset_embedding_for_oversize(window: int) -> int`.

- [ ] **Step 1: failing tests**
  - targets: a gmail chunk whose text contains `font-family:` and `{` → its message_id under `reflow:gmail`; a gdrive table chunk over 2,000 chars → its file_id under `reflow:drive`; a gdrive PROSE chunk over 2,000 → not a drive target; owners already queued or stamped `reflow_skipped` (any reason) are excluded.
  - heal runs ONCE, only when the reflow seed's gates pass (kill switch on, not halted, backup < 24 h): first gated tick → nothing and no marker; first open tick → enqueues targets (epoch `modified_at`, like the seed), resets `embedded=0` for every chunk whose PREFIXED passage exceeds the window (target owners included: a reflow that ends `source_gone` keeps its old chunks, and re-embedding ~200 chunks twice is cheap), writes the marker; second tick → no-op.
  - doctor: marker absent and oversize > 0 → `⚠️ Chunk window   N chunk(s) longer than one window — re-embed pending`; marker done → `✅ Chunk window   N chunk(s) longer than one window, embedded across windows`; 0 → `✅ … 0`.
- [ ] **Step 2: implement** — CSS predicate (SQL, gmail `source_type`): `text LIKE '%{%' AND (text LIKE '%font-family%' OR text LIKE '%@media%' OR text LIKE '%mso-%' OR text LIKE '%-webkit-%')`. Run the heal inside the seed pass after its gates, under the same bulk section the seed uses. One write transaction per part.
- [ ] **Step 3:** run the new tests + `tests/test_doctor*.py tests/test_daemon_reflow*.py -q`; commit `feat(reflow): one-shot heal of oversized and CSS-polluted chunks`.

### Task 6: owner repair — backoff, canonicalise without a model, exact undo

**Files:**
- Modify: `mcpbrain/daemon.py` (`_run_owner_action_repair`, `_run_action_hygiene` bookkeeping), `mcpbrain/action_owner.py`, `mcpbrain/ingestion_cleanup.py` (undo of step `owner_repair`, CLI accepts it)
- Test: `tests/test_action_owner.py`, `tests/test_daemon_action_hygiene*.py` (extend existing; grep for them)

- [ ] **Step 1: failing tests**
  - M-N3: embedder still loading → canonicalise still runs (rows rewritten), the same-thread comparison is skipped, watermark not advanced past uncompared threads; summary carries `owner_repair: embedder_loading` and the cadence stays due.
  - M-N1: a factory that raises on every call → first failure warns; subsequent ticks within the backoff do NOT call the factory; backoff doubles from 60 s to a 3,600 s ceiling; a success resets it; at most one WARNING per backoff step.
  - M-N2: `repair_owner_actions` logs every changed value to `cleanup_log` with step `owner_repair` (owner, owner_entity_id, prior_owner, prior_owner_entity_id for canonicalised rows; status, resolved_by, resolved_at for archived rows) with old and new values. `undo_owner_repair` restores exactly the logged old values (NULL stays NULL, the clean-up's earlier prior_owner is preserved), only on rows still holding the logged new value (drifted rows skipped and kept), then deletes those log rows. `mcpbrain clean-ingestion --undo owner_repair --yes` works (same refusal-while-daemon-runs rules). Rows repaired BEFORE this change (no log rows) are still undone the old way, and the docstring says so.
- [ ] **Step 2: implement** — reuse `ingestion_cleanup`'s log/undo machinery (its `_log` / `undo`) rather than a second log format; `owner_repair` is undo-able but is NOT one of `STEPS` (the clean-up never runs it).
- [ ] **Step 3:** run the touched tests; commit `fix(actions): owner repair backs off a failing model, canonicalises without one, undoes exactly`.

### Task 7: recall answers before it logs exposures (N5)

**Files:**
- Modify: `mcpbrain/control_api.py` (`/api/recall`)
- Test: `tests/test_control_api*.py` (extend)

- [ ] **Step 1: failing test** — a store whose `record_recall_feedback_batch` blocks on an Event: a client `POST /api/recall` receives its full JSON response while the write is still blocked; releasing the event then records the exposures.
- [ ] **Step 2: implement** — send the response, then log on the same handler thread (the socket write is unbuffered and Content-Length is set) or hand off to a single background worker; errors stay swallowed. No new thread per request if you choose a worker.
- [ ] **Step 3:** run `tests/test_control_api*.py tests/test_feedback*.py -q`; commit `fix(recall): answer before writing exposure rows`.

### Task 8: clean-up follow-up steps (N7 salience, mentions) and running added steps on done installs

**Files:**
- Modify: `mcpbrain/ingestion_cleanup.py` (two new steps, STEPS, `run_migration`), `mcpbrain/graph_write.py` (`_link` mentions), `mcpbrain/store.py` (removal mentions decrement ~3239-3245), `mcpbrain/importance.py` (expose the pre-M6 owner rule for the step, or reimplement it locally in the step)
- Test: `tests/test_ingestion_cleanup.py`, `tests/test_graph_write*.py` (extend)

- [ ] **Step 1: failing tests**
  - `recount_mentions` (after `sources_backfill` in STEPS): for every LOCAL entity, `mentions := count of non-derived email_entities rows` (the email_count basis); changed values logged; `--undo recount_mentions` restores them.
  - writer: `graph_write.apply` bumps `mentions` with `email_count` on every NEW primary-source link (all entity types, not only meetings); a derived source or a re-applied thread bumps nothing; store removal decrements `mentions` for all types, never below 0.
  - `derived_salience`: a derived-class chunk (session / brief / consolidated / digest; drafts were already excluded before M6) that the PRE-M6 owner rule would have boosted (explicit owner flag, owner sender, or SENT label; not draft) gets `salience := max(1.0, salience - 1.5)`, logged; evidence chunks, chunks with salience 0.0, and `note-core-identity-seed` are untouched; undo restores.
  - added steps on a done install: marker `{"state":"done","steps":[old 13]}` + backup < 24 h → `run_migration` runs ONLY the two new steps, marker `steps` now includes them, counts merged, `done`; a second call is a no-op. Backup gate closed → `done` marker unchanged (with a `pending_steps` field), retried next call. A fresh install runs all 15 once. `failed` stays failed.
- [ ] **Step 2: implement**; update the module docstring's step list.
- [ ] **Step 3:** run touched tests; commit `fix(cleanup): recount mentions, unboost historical derived salience; done installs run added steps`.

### Task 9: docs

**Files:** `CLAUDE.md` (Ingestion integrity section: parked items closed; a short "Oversized chunks and the held owner (2026-10-26)" paragraph with the findings table numbers, the heal and the doctor line), `docs/RELEASE-RUNBOOK.md` §9 (15 steps; owner_repair undo), `docs/superpowers/specs/2026-10-22-ingestion-integrity-design.md` (amendment notes for N5, N7, mentions, owner repair undo). No real names or titles.

- [ ] Commit `docs: oversized chunks, held owner, parked minors closed`.
