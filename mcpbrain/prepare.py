"""Prepare step: turn un-enriched email threads into content-addressed work units.

The daemon runs this. It takes Phase 1's thread groups, noise-filters them,
attaches standing context plus each thread's prior context and open actions,
caps the number of threads, splits over-long threads, and (when resolution is
due) appends a merge-review block. The result is written as immutable,
content-hashed work units (keyed by unit_id) atomically under
MCPBRAIN_HOME/enrich_queue/units/ for the extractor to pull.

Phase-1 contract this module codes against
-------------------------------------------
A "batch" (thread group) returned by group_unenriched_threads exposes:
  - .thread_id : str
  - .doc_ids   : list[str]  chunk doc_ids for the thread (passed to mark_enriched)
  - .chunks    : list       passed to reassemble_thread; each becomes a message

Phase-1 symbols are reached through the indirection seams below
(_group_unenriched_threads, _reassemble_thread, _build_known_people,
_org_domain_lines). Phase 1 has landed, so each seam now calls the real
mcpbrain.thread_enrich / mcpbrain.prompt / mcpbrain.graph_write module
imported at module top. The seams are kept as the unit-test monkeypatch
surface (tests/test_prepare.py patches them).

Note: _read_projects and _read_areas seams were removed in §9E.

Store methods used (provided by the store passed in): mark_enriched(doc_ids),
thread_context(thread_id), unified_actions(thread_id=, status=),
entities_for_resolution(). thread_context is still absent (Phase 3), so its
caller degrades to '' on AttributeError; the rest exist.
"""

import datetime
import json
import logging
import os
import re
import tempfile
from contextlib import nullcontext

from mcpbrain import config, prompt, thread_enrich
from mcpbrain.enrich_blocks import UNIT_BLOCKS as _UNIT_BLOCKS
from mcpbrain.resolve import _candidate_pairs
from mcpbrain.thread_enrich import _CHUNK_JOIN

log = logging.getLogger("mcpbrain.prepare")


# --- noise filter (ported verbatim from src/enrich_gmail.py:82-126) --------

NOISE_SENDERS = [
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "notifications@", "newsletter",
    "automated", "autorespond", "bounce@", "support@mailchimp",
    "msonlineservicesteam", "microsoftonline.com",
    # --- mcpbrain additions (NOT part of the verbatim Nexus port) ----------
    # High-confidence vendor blast tokens only. Both observed live as pure
    # marketing senders; neither appears in real org/ministry mail.
    "updates@",       # Ubiquiti mailchimp newsletter (updates@ui.com)
    "microsoftstore",  # Microsoft Store retail blast (Microsoftstore@microsoftstore.microsoft.com)
    "toolsonair.com",  # ToolsOnAir vendor blast (license/trade-show marketing, no relationship mail)
    "medium.com",      # Medium digest/newsletter platform, no genuine 1:1 correspondence
    # Deliberately NOT added: support@, info@, hello@ — too broad, hit real mail.
    # Deliberately NOT added: peakconsultancy.com.au — the sender (John Hardy)
    # has genuine correspondence history (meeting invites, assistance
    # requests); a domain-level block would also suppress future real mail
    # from him. His occasional pure-marketing blasts slip through uncaught,
    # same accepted tradeoff as Fivetran below.
]

NOISE_SUBJECT_PATTERNS = [
    r"^unsubscribe",
    r"your (order|receipt|invoice|statement|bill)",
    r"password reset",
    r"verify your email",
    r"confirm your (subscription|account|email)",
    r"^\[?automated\]?",
    r"delivery (failed|notification|status)",
    r"^ops-brain[:\s]",
    r"^(daily|morning) briefing",
    r"^out of office",
    r"^automatic reply",
    # --- mcpbrain additions (NOT part of the verbatim Nexus port) ----------
    # Marketing-distinctive openers/markers. Each is anchored or specific
    # enough that internal/ministry subjects won't match.
    r"^now available\b",          # retail launch blast ("Now available: ...")
    r"view (this email )?in (your )?browser",  # bulk-mail header leaking into subject
    r"\bshop (?:now|today)\b",    # retail CTA ("Shop now" / "Shop today") — adjacency required
    r"^\d+% off\b",               # discount blast leading subject ("50% off...") — anchored to avoid mid-subject false positives
    # Deliberately NOT added: ^new , generic single words — match legit mail.
]

_compiled_subject = [re.compile(p, re.IGNORECASE) for p in NOISE_SUBJECT_PATTERNS]

# --- bulk-mail body markers (mcpbrain addition, NOT in the Nexus port) -----
# Strong markers that essentially never appear in genuine 1:1 correspondence.
# Kept tight: substring markers are near-definitive bulk signals; a bare
# "unsubscribe" is only treated as a marker when an http URL is also present,
# since a real person can mention the word in passing.
_BULK_BODY_MARKERS = (
    "mailchi.mp",
    "list-unsubscribe",
    "view in browser",
    "view this email in your browser",
    # Internal tool debug/eval output (mcpbrain addition) — not bulk mail, but
    # the same "never appears in genuine correspondence" property holds, and
    # this content has leaked into the graph as bogus business "fyi" notes.
    "ops-brain eval harness",
    "evals passed",
)

_SUBJECT_LEADING_DECORATION = re.compile(r"^[^\w\[]+")


def _normalise_noise_subject(subject: str) -> str:
    """Strip leading emoji / punctuation so ^anchors still match decorated subjects."""
    return _SUBJECT_LEADING_DECORATION.sub("", subject).lstrip()


def _is_noise(sender: str, subject: str) -> bool:
    sender_lower = sender.lower()
    if any(n in sender_lower for n in NOISE_SENDERS):
        return True
    normalised = _normalise_noise_subject(subject)
    for pattern in _compiled_subject:
        if pattern.search(subject) or pattern.search(normalised):
            return True
    return False


def _is_bulk_body(text: str) -> bool:
    """True when a message body carries a strong bulk-mail marker.

    mcpbrain addition. These markers (mailchimp links, list-unsubscribe headers,
    "view in browser" links) are near-definitive bulk-mail signals that don't
    show up in genuine 1:1 mail. A bare "unsubscribe" is too weak on its own, so
    it only counts when an http URL sits in the same body.
    """
    if not text:
        return False
    lower = text.lower()
    if any(marker in lower for marker in _BULK_BODY_MARKERS):
        return True
    if "unsubscribe" in lower and "http" in lower:
        return True
    return False


def thread_is_noise(messages) -> bool:
    """A thread is noise when its lead message (earliest by date) is noise.

    A noise lead means the whole thread is automated, so the later human replies
    (if any) don't rescue it. The lead's body is also checked for bulk-mail
    markers (mcpbrain addition), which catches mailchimp/newsletter senders that
    the sender/subject rules miss.
    """
    if not messages:
        return False
    lead = min(messages, key=lambda m: m.get("date", ""))
    if _is_noise(lead.get("sender", ""), lead.get("subject", "")):
        return True
    return _is_bulk_body(lead.get("text", ""))


# --- trivial-thread short-circuit (Task 2.1) --------------------------------

# Total body chars across a thread's messages under this is a candidate for
# the deterministic extractive-summary path (no model call). Deliberately
# small: this is meant to catch one-line acks ("Thanks, sounds good."), not
# genuinely short-but-substantive threads.
_TRIVIAL_CHARS = 300

# A simple, cheap substring scan for action cues. Per the task brief this is
# intentionally NOT the heavier extractor action-heuristics — a false negative
# here (missing a real action cue) just means the thread falls through to the
# normal model path, which is the safe direction to err in.
# A thread is NOT trivial if any message hints at a question, request, OR a
# commitment/action — short messages routinely carry real actions ("I'll send it
# Monday"), and a false-trivial classification drops that action (the model never
# sees it). Err toward non-trivial: an over-match just costs one model call, while a
# missed commitment is silent data loss. Substring, case-insensitive.
_ACTION_CUES = (
    "?", "can you", "please",
    # commitments
    "i'll", "i will", "we'll", "we will", "i'm going", "we're going", "let me", "let's",
    # action verbs
    "send", "confirm", "schedule", "pay", "wire", "sign", "review", "follow up",
    "followup", "deadline", "due", "action", "next step", "to-do", "todo",
    # time anchors that usually accompany a commitment
    "tomorrow", "next week", "monday", "tuesday", "wednesday", "thursday", "friday",
    "by eod", "by end of",
)


def is_trivial_thread(messages) -> bool:
    """True when a thread is short enough and free of action cues to be safely
    summarised deterministically instead of sent through the model.

    True when the total character count across all messages' "text" is under
    _TRIVIAL_CHARS AND no message's text contains an action cue (a case-
    insensitive substring scan for "?", "can you", or "please"). An empty
    thread (no messages) is trivial by definition — zero chars, no cues.
    """
    total = 0
    for m in messages:
        text = m.get("text", "") or ""
        total += len(text)
        lower = text.lower()
        if any(cue in lower for cue in _ACTION_CUES):
            return False
    return total < _TRIVIAL_CHARS


# --- Phase 1 seams ---------------------------------------------------------

def _group_unenriched_threads(store, **kw):
    # Indirection kept as the unit-test seam; backed by the real mcpbrain.thread_enrich.
    return thread_enrich.group_unenriched_threads(store, **kw)


def _reassemble_thread(chunks):
    # Indirection kept as the unit-test seam; backed by the real mcpbrain.thread_enrich.
    return thread_enrich.reassemble_thread(chunks)


def _build_known_people(store, batch_thread_ids):
    # Indirection kept as the unit-test seam; backed by the real mcpbrain.prompt.
    return prompt.build_known_people(store, batch_thread_ids=batch_thread_ids)


# --- per-unit known_people scoping (Task 12) -------------------------------
#
# Every enrichment unit today gets the SAME shared known_people list
# (build_known_people's core + batch overlay), regardless of what that unit's
# content actually mentions. The functions below build the SELECTION
# machinery: given a pool of candidate people (prompt.build_candidate_people)
# indexed once per write_units call, pick only the people a given unit's text
# actually mentions, plus the standing core. Not yet wired into the write path
# (Task 14 does that) — these are pure functions, exercised directly by tests.

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


def _org_domain_lines():
    # Indirection kept as the unit-test seam; backed by the configured taxonomy.
    from mcpbrain import orgs
    return list(orgs.taxonomy_from_config().domain_lines)


def _valid_org_tags():
    # The org enum the extractor must choose from: configured org names plus
    # the reserved external/unknown tags. Fed into pending.json context so the
    # prompt prose (enrich_prompt.md) never hardcodes an install's orgs.
    from mcpbrain import orgs
    tax = orgs.taxonomy_from_config()
    return list(tax.names) + list(orgs.RESERVED_TAGS)


# --- salience gate (Q1) ---------------------------------------------------

# Drive mime types that are tabular/raw data — skip prose-extraction.
# These files have no meaningful entity/relation content but inflate the graph.
_COLD_DRIVE_MIMES = frozenset({
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "text/csv",
    "application/csv",
    "text/tab-separated-values",
    "application/vnd.ms-excel",
    "text/html",
    # Drive text/html is an UPLOADED .html file — a saved web page, never an
    # authored document (a Google Doc has its own mime). On the live store this
    # was exactly two files totalling 5.07MB: a 4.95MB SHEIN shop page and a
    # Bookabin payment page. The shop page alone was 2,904 hot chunks and formed
    # a 5,075,515-byte work unit no drainer could hold, re-produced every spool
    # cycle. Cold is reversible: the chunks stay embedded, in FTS, and in recall.
})

# Gmail category labels that indicate bulk / non-correspondence mail and are safe
# to skip from graph-extraction. CATEGORY_UPDATES is deliberately NOT here: Gmail
# files plenty of legitimate transactional/human threads under Updates, so skipping
# it wholesale loses real signal. Deprioritising (not skipping) Updates belongs to
# the importance-scoring work (B3), not this binary gate.
_PROMOTIONAL_LABELS = frozenset({
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_FORUMS",
})

# Minimum text length (chars) for a Drive document to be worth extraction.
# Docs below this are likely near-empty stubs or auto-generated covers.
_MIN_DRIVE_TEXT = 200


def should_enrich(chunk: dict) -> bool:
    """Return True when a chunk is worth LLM graph-extraction.

    Source-aware gate:
    - Any source: skip a chunk tagged content_subtype 'table' (I1).
    - Email: skip CATEGORY_PROMOTIONS/UPDATES Gmail labels (in addition to the
      existing _filter_noise sender/subject checks). The label check works on the
      already-retrieved label metadata stored in the chunk.
    - Drive: skip tabular mime types (spreadsheets, CSV) and docs with very short
      text (< _MIN_DRIVE_TEXT chars). Real prose documents pass through.

    Skipped chunks are NOT lost — they stay embedded/searchable (embedded=1).
    The caller marks them 'cold' via store.set_enrich_state; they never enter
    the extraction queue while cold.

    Returns True for any unrecognised source (fail-open, no false negatives).
    """
    meta = chunk.get("metadata") or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}

    # The chunk metadata field is `source_type` (not `source`); fall back to the
    # legacy key and to structural hints (thread_id ⇒ email, file_id/mime ⇒ Drive).
    source = str(meta.get("source_type") or meta.get("source") or "").lower()

    # Source-AGNOSTIC, and it has to physically sit above the per-source
    # branches to be so (I1): this check used to live inside the Drive branch
    # while claiming source-independence in its own comment. Email ATTACHMENT
    # chunks are source_type 'gmail', so an emailed budget workbook took the
    # Gmail branch, never reached this check, and sent every one of its row-group
    # chunks to the extractor uncapped. A 'table' chunk is tabular data, not
    # prose worth entity extraction, whoever produced it; any future tabular
    # source is honoured without re-listing mimes anywhere.
    if str(meta.get("content_subtype") or "").lower() == "table":
        return False

    if source == "gmail" or meta.get("thread_id"):
        # Header-based bulk signal (List-Id / List-Unsubscribe / Precedence),
        # stamped at ingest by normalise_gmail. Checked BEFORE the Gmail
        # CATEGORY_* labels because it is strictly stronger: Gmail's categoriser
        # misses plenty of list mail that carries these headers, and this is the
        # signal that used to DROP the message outright at ingest (A4). Now it
        # cold-marks instead — embedded, searchable, never graph-extracted,
        # and reversible.
        if meta.get("bulk"):
            return False
        # Email: check Gmail category labels.
        labels_raw = meta.get("labels") or ""
        if isinstance(labels_raw, list):
            labels = {str(lb).upper() for lb in labels_raw}
        else:
            labels = {lb.strip().upper() for lb in str(labels_raw).split(",")}
        if labels & _PROMOTIONAL_LABELS:
            return False  # bulk/promotional email
        return True

    if source in ("gdrive", "drive") or meta.get("file_id") or meta.get("mime_type"):
        # Drive: content_subtype is checked above (source-agnostically); what is
        # left here is the Drive-specific mime + length gate.
        mime = str(meta.get("mime_type") or "").lower()
        if mime in _COLD_DRIVE_MIMES:
            return False
        text_len = len(chunk.get("text") or "")
        if text_len < _MIN_DRIVE_TEXT:
            return False
        return True

    # Unknown source: pass through (fail-open).
    return True


def _is_drive_chunk(meta: dict) -> bool:
    src = str(meta.get("source_type") or meta.get("source") or "").lower()
    return src in ("gdrive", "drive") or bool(meta.get("file_id"))


def _drive_mentioned_in_email(store, meta: dict) -> bool:
    """True if this Drive doc's file_id or file_name appears in any email.

    The ops-brain salience rule: a Drive doc is worth graph-extraction when it is
    referenced in correspondence (a shared link / named attachment). Used only as
    a stricter gate when `salience_require_drive_mention` is enabled.
    """
    return store.email_mentions(meta.get("file_id") or "", meta.get("file_name") or "")


def _budget_spent(budget, where: str, done: int, total: int) -> bool:
    """True once `budget` is exhausted, logging which prepare loop stopped where.

    Shared by the three per-batch write loops below and mirrors the check in
    build_pending. Stopping early is always safe HERE because none of these
    loops has committed anything for the batches it has not reached yet: those
    batches are still un-enriched, so _group_unenriched_threads picks them up
    again next cycle. The unprocessed tail must therefore be DROPPED from this
    cycle's result, never passed through — a batch that skipped the salience
    gate / noise filter / trivial-thread triage would otherwise reach the
    extractor ungated, which is the one thing an early return could get wrong.
    """
    if budget is None or not budget.expired():
        return False
    log.info("%s: budget spent after %d/%d batches; remainder re-queues next cycle",
             where, done, total)
    return True


def _apply_salience_gate(store, batches: list, *, require_drive_mention: bool = False,
                         bulk_section=None, budget=None) -> tuple[list, dict]:
    """Run should_enrich() over all chunks in each batch.

    Chunks that do not enrich are marked 'cold' in the store (reversible) and
    removed from the batch. Empty batches are discarded. Returns (kept_batches,
    summary) where summary has 'gated' (cold-marked) and 'kept' counts.

    When require_drive_mention is True, a Drive chunk that passes should_enrich is
    ADDITIONALLY required to be referenced in email (ops-brain's mention gate) —
    off by default because mcpbrain holds valuable un-emailed docs (minutes,
    profiles) that a blanket mention requirement would wrongly cold-gate.

    `store.set_enrich_state` writes `chunks.enrich_state` — the same table the
    four gated maintenance passes mutate — so (Task 2 duty-cycle/race-safety
    fix) each batch's write is bracketed in `bulk_section` (a zero-arg
    context-manager factory, default `contextlib.nullcontext`), same as
    build_pending's per-thread loop, and `budget` bounds the loop (see
    _budget_spent).
    """
    if bulk_section is None:
        bulk_section = nullcontext
    gated = kept = 0
    result = []
    for done, batch in enumerate(batches):
        if _budget_spent(budget, "salience gate", done, len(batches)):
            break
        cold_ids = []
        kept_chunks = []
        for chunk in batch.chunks:
            meta = chunk.get("metadata") or {}
            if isinstance(meta, str):
                import json as _json
                try:
                    meta = _json.loads(meta)
                except Exception:
                    meta = {}
            keep = should_enrich(chunk)
            if keep and require_drive_mention and _is_drive_chunk(meta):
                keep = _drive_mentioned_in_email(store, meta)
            if keep:
                kept_chunks.append(chunk)
                kept += 1
            else:
                cold_ids.append(chunk["doc_id"])
                gated += 1
        if cold_ids:
            with bulk_section():
                store.set_enrich_state(cold_ids, "cold")
        if kept_chunks:
            import copy
            new_batch = copy.copy(batch)
            new_batch.doc_ids = [c["doc_id"] for c in kept_chunks]
            new_batch.chunks = kept_chunks
            result.append(new_batch)
    if gated or kept:
        log.info("salience gate: gated=%d cold, kept=%d for extraction", gated, kept)
    return result, {"gated": gated, "kept": kept}


# --- noise marking ---------------------------------------------------------

def _filter_noise(store, batches, *, bulk_section=None, budget=None) -> list:
    """Return the non-noise batches; mark each noise batch enriched so it never
    re-queues. This is the only place prepare writes the store. Prepare runs in
    the daemon (single writer), so the single-writer invariant holds. Marking
    happens before pending.json is written, so a noise thread never reaches the
    extractor.

    thread_is_noise reads sender/subject/date off MESSAGE dicts, so each batch's
    raw chunks must first go through _reassemble_thread (the same seam _thread_block
    uses). Raw chunks carry that data inside chunk metadata, not at top level, so
    running the filter on batch.chunks directly would see empty fields and never
    detect noise. Kept batches get reassembled again in _thread_block; that double
    call is fine because prepare runs on a small capped batch per cycle.

    `store.mark_enriched` writes `chunks.enriched` — the same column the
    stale_reextract/reflow_outdated_chunks gated passes reset — so (Task 2
    duty-cycle/race-safety fix) each write is bracketed in `bulk_section`
    (default `contextlib.nullcontext`), and `budget` bounds the loop (see
    _budget_spent).
    """
    if bulk_section is None:
        bulk_section = nullcontext
    kept = []
    for done, batch in enumerate(batches):
        if _budget_spent(budget, "noise filter", done, len(batches)):
            break
        messages = list(_reassemble_thread(batch.chunks))
        if thread_is_noise(messages):
            with bulk_section():
                store.mark_enriched(batch.doc_ids)
        else:
            kept.append(batch)
    return kept


_SENTENCE_SPLIT = re.compile(r"[.!?]")


def _extractive_summary(lead: dict) -> str:
    """Deterministic summary for a trivial thread: the lead message's subject
    plus the first sentence of its body (split on . / ! / ?, first non-empty
    segment, whitespace-trimmed). Falls back gracefully when either piece is
    missing so this never raises on a sparse fixture."""
    subject = (lead.get("subject") or "").strip()
    text = lead.get("text") or ""
    first_sentence = ""
    for segment in _SENTENCE_SPLIT.split(text):
        segment = segment.strip()
        if segment:
            first_sentence = segment
            break
    if subject and first_sentence:
        return f"{subject}: {first_sentence}"
    return subject or first_sentence


def _apply_trivial_threads(store, batches, *, home=None, bulk_section=None,
                           budget=None) -> list:
    """Route trivial threads (see is_trivial_thread) straight to a deterministic
    extractive-summary write via graph_write.apply(), skipping the model unit
    path entirely; return the remaining (non-trivial) batches unchanged for the
    normal build_pending/write_units flow.

    Mirrors _filter_noise's shape and write-ownership: this is a second place
    prepare writes the store, again safe because prepare runs single-writer in
    the daemon. Each batch's thread is reassembled via _thread_block (this
    duplicates one _thread_block call for batches that turn out non-trivial —
    the same accepted tradeoff _filter_noise's docstring calls out for kept
    batches being reassembled again downstream).

    `graph_write.apply` + `store.mark_enriched` both write `chunks` (and the
    graph), so (Task 2 duty-cycle/race-safety fix) each batch's write is
    bracketed in `bulk_section` (default `contextlib.nullcontext`), and
    `budget` bounds the loop (see _budget_spent).
    """
    if bulk_section is None:
        bulk_section = nullcontext
    _home = str(home) if home is not None else str(config.app_dir())
    if not config.enrich_trivial_thread_summary(_home):
        return batches
    from mcpbrain import graph_write

    kept = []
    for done, batch in enumerate(batches):
        if _budget_spent(budget, "trivial threads", done, len(batches)):
            break
        block = _thread_block(store, batch)
        messages = block["messages"]
        if not is_trivial_thread(messages):
            kept.append(batch)
            continue
        lead = min(messages, key=lambda m: m.get("date", "") or "") if messages else {}
        extraction = {
            "thread_id": block["thread_id"],
            "org": block["org_hint"] or "unknown",
            "content_type": "fyi",  # valid per chunking._VALID_CONTENT_TYPES
            "summary": _extractive_summary(lead),
            "messages": messages,
            "entities": [],
            "relations": [],
            "actions": [],
            "topics": [],
        }
        with bulk_section():
            graph_write.apply(store, extraction, doc_ids=batch.doc_ids, home=home)
            store.mark_enriched(batch.doc_ids)
    return kept


# --- thread assembly -------------------------------------------------------

def _thread_block(store, batch) -> dict:
    """Reassemble one thread into the pending-thread shape: ordered messages with
    body text, plus prior context and open actions.

    prior_thread_context prefers store.thread_context (the periodic cross-message
    synthesis pass, threads with email_count>=5); when that's empty (not yet
    synthesized, or the thread is too short to ever qualify) it falls back to
    store.thread_summary_digest, a join of each message's own already-durable
    one-line summary. Degrades to '' if both are unavailable, rather than fail.
    open_actions is [] when the thread has no open actions.

    org_hint is a deterministic org guess derived from the lead message's
    (earliest by date, same tie-break as graph_write.apply()) sender email
    domain, resolved against the configured org taxonomy. It's computed
    unconditionally — cheap (one header parse + one dict lookup) and harmless
    to ship even when the consuming kill-switch (config.enrich_org_default_enabled,
    checked in graph_write.apply()) is off. Degrades to '' when the thread has
    no messages or the lead has no parseable sender email; never raises.
    """
    messages = list(_reassemble_thread(batch.chunks))
    try:
        prior = store.thread_context(batch.thread_id) or ""
    except AttributeError:  # Phase 1 seam: method absent until Phase 1 lands; real errors must surface.
        prior = ""
    if not prior:
        # thread_context is only ever populated by the periodic cross-message
        # synthesis pass (threads with email_count>=5); a shorter thread falls
        # back to a digest of each message's own already-durable one-line
        # summary rather than shipping genuinely empty prior context.
        try:
            prior = store.thread_summary_digest(batch.thread_id) or ""
        except AttributeError:  # Defensive: guard retained for fake stores in tests that omit it.
            prior = ""
    try:
        actions = store.unified_actions(thread_id=batch.thread_id, status="open") or []
    except AttributeError:  # Defensive: unified_actions exists post-Phase-1; guard retained for fake stores in tests that omit it.
        actions = []
    org_hint = ""
    if messages:
        from mcpbrain import graph_write, orgs
        lead = min(messages, key=lambda m: m.get("date", "") or "")
        email = graph_write._extract_email_addr(lead.get("sender", "") or "")
        if email:
            org_hint = graph_write.org_from_email(email, orgs.taxonomy_from_config())
    return {
        "thread_id": batch.thread_id,
        "prior_thread_context": prior,
        "open_actions": actions,
        "messages": messages,
        "org_hint": org_hint,
    }


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

    The pieces are READ from the message (`chunk_pieces`, stamped by
    reassemble_thread from the real chunk rows), never re-derived by splitting
    `text` on _CHUNK_JOIN. chunk_text PACKS several paragraphs into one chunk
    whenever they fit the budget together, so a chunk's own text routinely
    contains internal "\\n\\n" — re-splitting the joined body then produced MORE
    pieces than there were chunk_doc_ids (60 paragraphs / 8 chunks on a real
    document), the length guard fired, and the message shipped whole. One
    paragraph per chunk is the exception, not the rule, so that derivation
    defeated seam splitting for ordinary documents.
    """
    ids = msg.get("chunk_doc_ids") or []
    if len(ids) <= 1:
        return [msg]
    pieces = msg.get("chunk_pieces")
    if pieces is None or msg.get("chunk_has_gap") or len(pieces) != len(ids):
        # No pieces (a unit written before they were carried); or a gap marker
        # was inserted (a partially-enriched/cold document), so _CHUNK_JOIN.join
        # of the pieces does NOT reproduce the text and a part's body could not
        # be reconstructed losslessly; or — defensively — the two lists have
        # drifted out of step. Splitting here would mis-attribute chunks, so ship
        # whole rather than mark the wrong rows.
        return [msg]
    def _piece(txts: list[str], dids: list[str]) -> dict:
        # chunk_pieces/chunk_has_gap are re-derived for the emitted piece so a
        # part never carries its parent's full piece list.
        return {**msg, "text": _CHUNK_JOIN.join(txts), "chunk_doc_ids": dids,
                "chunk_pieces": txts, "chunk_has_gap": False}

    out, cur_txt, cur_ids = [], [], []
    for piece, did in zip(pieces, ids):
        projected = (sum(len(t) for t in cur_txt)
                     + len(cur_txt) * len(_CHUNK_JOIN) + len(piece))
        if cur_txt and projected > char_budget:
            out.append(_piece(cur_txt, cur_ids))
            cur_txt, cur_ids = [], []
        cur_txt.append(piece)
        cur_ids.append(did)
    if cur_txt:
        out.append(_piece(cur_txt, cur_ids))
    return out


def _split_long_thread(block, char_budget: int) -> list:
    """Split a thread whose joined message bodies exceed char_budget into ordered
    sub-batches. Each sub-batch shares the thread_id, prior_thread_context,
    open_actions, and org_hint (all thread-level metadata, not per-message), and
    carries {"part": i, "of": k} so the drain can re-group them by thread_id
    before apply. Message order is preserved across the split.

    Over-long individual messages are first expanded at their chunk seams
    (_split_message_at_seams) so a single-message thread — every Drive
    document, every captured note — is splittable at all, not just threads
    with multiple messages.
    """
    messages = block["messages"]
    total = sum(len(m.get("text", "")) for m in messages)
    if total <= char_budget:
        return [block]

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


# --- merge-review block ----------------------------------------------------

def _merge_pair(a: dict, b: dict) -> dict:
    """Shape one candidate pair. pair_id is the two ids sorted and joined by '|',
    so the same two entities yield the same id regardless of argument order.
    """
    pair_id = "|".join(sorted((a["id"], b["id"])))
    return {
        "pair_id": pair_id,
        "a": {"id": a["id"], "name": a["name"], "type": a["type"]},
        "b": {"id": b["id"], "name": b["name"], "type": b["type"]},
    }


# Max candidate pairs folded into one spool batch. The fuzzy finder can emit
# hundreds of thousands of pairs on a large brain; without a cap the merge_review
# block alone made pending.json >100MB (far too big to load into context). Kept
# small so it leaves room for threads under the MCP pull's char budget. Capping is
# safe: the remaining pairs surface on later cycles as adjudicated ones leave the
# candidate pool.
_MERGE_REVIEW_CAP = 50


def _merge_review_block(store, *, cap: int = _MERGE_REVIEW_CAP) -> list:
    """Candidate pairs for LLM adjudication, folded into the spool. Reuses the
    existing fuzzy candidate finder, capped to `cap` pairs per batch. The
    deterministic resolve tier still runs every cycle elsewhere; this block only
    covers the LLM-adjudication tier.
    """
    pairs = _candidate_pairs(store.entities_for_resolution())
    return [_merge_pair(a, b) for a, b in pairs[:cap]]


# --- context assembly ------------------------------------------------------

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


# --- atomic write ----------------------------------------------------------

def _atomic_write(target, text: str) -> None:
    """Atomic write (temp file + os.replace), creating the parent dir. No stray
    temp on failure."""
    from pathlib import Path
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent),
                               prefix="." + target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- work-queue producer ---------------------------------------------------
# The daemon produces a bounded queue of immutable, pre-sized WORK UNITS under
# enrich_queue/units/, each carrying its OWN scoped `context` (Task 14: no more
# shared enrich_queue/context.json). The enrich session consumes them one
# subagent per unit (see mcp_server.brain_enrich_units / _pull / _push). This
# replaces the single churning pending.json + read-time manifest.

# A unit is sized so its pull (unit work + rules + context) fits the cap.
# _UNIT_RULES_RESERVE is the room left for the rules block the pull attaches.
# The cap itself is read from config.unit_pull_cap() AT CALL TIME (not import) so
# a config change takes effect on the next write_units() call, no daemon restart
# needed. write_units accepts pull_cap= so callers can still override explicitly.
_UNIT_RULES_RESERVE = 11_000


def _unit_id(kind: str, signature: str) -> str:
    """Content-addressed unit id, so re-producing the same un-enriched work writes
    the same file (idempotent dedupe — no double-queueing)."""
    import hashlib
    return "u-" + hashlib.sha1(f"{kind}:{signature}".encode("utf-8")).hexdigest()[:12]


def _pack_by_size(items, budget, sizer):
    """Greedily pack items into chunks whose serialized size stays under budget,
    always keeping at least one item per chunk."""
    cur, size = [], 0
    for it in items:
        s = sizer(it)
        if cur and size + s > budget:
            yield cur
            cur, size = [], 0
        cur.append(it)
        size += s
    if cur:
        yield cur


def write_units(data: dict, *, home=None, pull_cap=None,
                window: int = 600) -> dict:
    """Turn a prepared batch dict (threads + optional blocks + standing context +
    people pool/core) into immutable, pre-sized work-unit files under
    enrich_queue/units/. Each unit carries its OWN `context`: the standing block
    (owner_name/org_domain_map/valid_orgs) plus a known_people list SCOPED to
    that unit's own text (Task 12's _scoped_known_people) — there is no more
    shared enrich_queue/context.json. Each unit is sized so its pull (work +
    rules + context) fits the cap. Unit ids are content hashes, so re-running on
    the same un-enriched work is idempotent. Honors a window cap (backpressure):
    when the queue already holds >= window undrained units, the cycle produces
    no new ones. Returns a summary."""
    if pull_cap is None:
        pull_cap = config.unit_pull_cap(home)
    from pathlib import Path
    queue = (config.app_dir() if home is None else Path(home)) / "enrich_queue"
    units_dir = queue / "units"
    units_dir.mkdir(parents=True, exist_ok=True)
    existing = list(units_dir.glob("*.json"))
    if len(existing) >= window:
        return {"units_written": 0, "units_pending": len(existing),
                "skipped": "window_full"}
    standing = data.get("context") or {}
    pool = data.get("people_pool") or []
    core = data.get("people_core") or []
    # The token index is built ONCE per write_units call (not per unit): an
    # O(people) scan per unit would be ~5,000 names x ~130 units of wasted work
    # every cycle (see _build_people_index's own docstring).
    index = _build_people_index(pool)
    # CONTEXT_CAP replaces the old context-length term: context no longer grows
    # with the batch (it's per-unit and capped), so the budget is deterministic
    # and independent of corpus size.
    budget = max(2000, pull_cap - _UNIT_RULES_RESERVE - CONTEXT_CAP - 1500)
    written = 0
    for chunk in _pack_by_size(data.get("threads") or [], budget,
                               lambda t: len(json.dumps(t)) + 1):
        tids = sorted(str(t.get("thread_id")) for t in chunk)
        uid = _unit_id("thread", ",".join(tids))
        body = {"unit_id": uid, "kind": "thread", "threads": chunk}
        body["context"] = {**standing,
                           "known_people": _scoped_known_people(
                               core, index, json.dumps(chunk, ensure_ascii=False))}
        _atomic_write(units_dir / f"{uid}.json",
                      json.dumps(body, ensure_ascii=False))
        written += 1
    for k in _UNIT_BLOCKS:
        for chunk in _pack_by_size(data.get(k) or [], budget,
                                   lambda it: len(json.dumps(it)) + 1):
            sig = k + ":" + json.dumps(chunk, sort_keys=True, ensure_ascii=False)
            uid = _unit_id("block", sig)
            body = {"unit_id": uid, "kind": "block", "block": k, "items": chunk}
            body["context"] = {**standing,
                               "known_people": _scoped_known_people(
                                   core, index, json.dumps(chunk, ensure_ascii=False))}
            _atomic_write(units_dir / f"{uid}.json",
                          json.dumps(body, ensure_ascii=False))
            written += 1
    return {"units_written": written, "units_pending": len(existing) + written}


def prepare_units(store, *, thread_cap: int, char_budget: int,
                  resolution_due: bool, now=None,
                  synthesis_requests: list | None = None,
                  extra_blocks: dict | None = None, home=None,
                  window: int = 600, budget=None, bulk_section=None) -> dict:
    """Build the current batch (un-enriched threads + due blocks) and write it as
    work units. The work-queue replacement for prepare(): no single pending.json —
    a bounded queue of immutable units the enrich session consumes. Unlike prepare()
    it still produces block units when there are no threads.

    `budget` (a `Budget`, or None for unbounded) is threaded into EVERY
    per-batch loop this function drives -- the three writing helpers
    (`_apply_salience_gate`/`_filter_noise`/`_apply_trivial_threads`) as well as
    build_pending's thread-assembly loop -- so a large kept-thread set can't
    hold the daemon's cycle indefinitely. run_one() never passed a budget here
    at all, so this step ran unbounded for the whole of prepare_units while the
    cycle held _bulk_lock (live: 8m39s in one cycle, zero heartbeat advance).
    Budgeting only build_pending was NOT enough: the three helpers above run
    FIRST and are the expensive ones under contention -- each does per-batch
    store I/O inside its own `bulk_section`, so with a waiter present each batch
    additionally pays `BULK_LOCK_YIELD_S` (0.25s) on section exit. At a few
    hundred batches that is minutes of unbudgeted work before build_pending's
    check is ever reached, i.e. exactly the stall the budget exists to bound.
    Stopping early is safe at every one of these points: nothing is committed
    for the batches not yet reached, they are still un-enriched, and
    _group_unenriched_threads picks them up again next cycle.

    `bulk_section` (a zero-arg context-manager factory, default
    `contextlib.nullcontext`) is threaded into `_apply_salience_gate`/
    `_filter_noise`/`_apply_trivial_threads` — the three helpers below that
    actually write `chunks` (`set_enrich_state`/`mark_enriched`/
    `graph_write.apply`), each bracketing its own per-batch write. This closes
    a real race the previous plan revision introduced: those three calls run
    OUTSIDE run_cycle's bulk_section entirely (prepare_units itself isn't
    wrapped there — see run_cycle), so without their OWN sectioning here they
    could race the four gated maintenance passes' chunk-column writes with no
    lock at all. `build_pending`'s own per-thread loop does no writes
    (`_thread_block` only reads `thread_context`/`unified_actions`), so it
    only needs the `budget` check above, not `bulk_section`.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    batches = _group_unenriched_threads(store, thread_cap=thread_cap)
    # Q1 salience gate: run before noise filter so cold-marked chunks are excluded
    # from the thread_cap count. Gate is behind a config flag (default OFF).
    salience_summary = {}
    _home = str(config.app_dir())
    if config.salience_gate_enabled(_home):
        batches, salience_summary = _apply_salience_gate(
            store, batches,
            require_drive_mention=config.salience_require_drive_mention(_home),
            bulk_section=bulk_section, budget=budget)
    non_noise = _filter_noise(store, batches, bulk_section=bulk_section, budget=budget)
    # Trivial threads are deterministically extracted and marked enriched here
    # (no model call) before thread_cap is applied. group_unenriched_threads already
    # caps the pool at thread_cap, so within THIS cycle excluding trivial threads
    # doesn't add more model calls. The benefit is cross-cycle: resolving trivial
    # threads clears them from the backlog faster, making more distinct non-trivial
    # threads visible to group_unenriched_threads in the NEXT cycle.
    non_trivial = _apply_trivial_threads(store, non_noise, home=home,
                                         bulk_section=bulk_section, budget=budget)
    kept = non_trivial[:thread_cap]
    data = build_pending(store, kept, char_budget=char_budget, now=now,
                         resolution_due=resolution_due,
                         synthesis_requests=synthesis_requests,
                         extra_blocks=extra_blocks, budget=budget)
    # The per-unit known_people pool (Task 12/14): built once per cycle here,
    # then indexed once per write_units() call and scoped per unit there. A
    # store error degrades to an empty pool/core (core-only context per unit,
    # matching write_units' own failure posture) rather than failing the whole
    # cycle — this is reference data, not work.
    try:
        data["people_core"] = prompt.build_known_people(store, batch_thread_ids=[])
    except Exception as exc:  # noqa: BLE001
        log.warning("prepare_units: build_known_people failed: %s", exc)
        data["people_core"] = []
    try:
        data["people_pool"] = prompt.build_candidate_people(store)
    except Exception as exc:  # noqa: BLE001
        log.warning("prepare_units: build_candidate_people failed: %s", exc)
        data["people_pool"] = []
    summary = write_units(data, home=home, window=window)
    summary["threads"] = len(data.get("threads") or [])
    summary["batch_id"] = data.get("batch_id")
    if salience_summary:
        summary["salience_gate"] = salience_summary
    return summary


# --- entry point -----------------------------------------------------------

def attach_extra_blocks(pending: dict, extra_blocks: dict | None) -> dict:
    """Merge optional block requests into pending.json. Empty/None blocks are
    omitted so the contract stays minimal."""
    if not extra_blocks:
        return pending
    out = dict(pending)
    for key, requests in extra_blocks.items():
        if requests:
            out[key] = requests
    return out


def build_pending(store, batches, *, char_budget: int, now,
                  batch_id: str | None = None, resolution_due: bool = False,
                  synthesis_requests: list | None = None,
                  extra_blocks: dict | None = None, budget=None) -> dict:
    """Assemble the pending.json dict for already-grouped, noise-filtered batches.

    Pure assembly: builds thread blocks (splitting over-long threads), context,
    and the optional merge-review block, then returns the dict. Does NOT write
    any file and does NOT mark the store. `batch_id` defaults to a timestamped
    id when not supplied. Callers that need many concurrent batches pass their
    own unique batch_id.

    `budget` (a `Budget`, or None for unbounded) is checked once per batch in
    the thread-assembly loop below, so a large `batches` list can't hold the
    daemon's cycle indefinitely -- each batch's _thread_block call does real
    store I/O (thread_context, unified_actions) that adds up across hundreds
    of threads. Stopping early is safe: any batch not yet assembled this cycle
    is simply picked up again next cycle (still un-enriched).
    """
    threads = []
    for batch in batches:
        if budget is not None and budget.expired():
            log.info("prepare_units: budget spent after %d threads", len(threads))
            break
        block = _thread_block(store, batch)
        for part in _split_long_thread(block, char_budget):
            # chunk_pieces is reassembly-only: a message's `text` IS their join,
            # so carrying it into the unit file would roughly DOUBLE every unit's
            # payload for data nothing downstream reads. The seam split happens
            # here; drain only ever needs the resulting part_doc_ids.
            # chunk_doc_ids is deliberately kept — part_doc_ids derives from it,
            # and it is the per-message provenance drain falls back on.
            for m in part.get("messages", []):
                m.pop("chunk_pieces", None)
                m.pop("chunk_has_gap", None)
            threads.append(part)

    context = _build_context(store, [b.thread_id for b in batches])
    merge_review = _merge_review_block(store) if resolution_due else []

    if batch_id is None:
        batch_id = f"batch-{now:%Y%m%d-%H%M%S}"
    data = {
        "batch_id": batch_id,
        "prepared_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "context": context,
        "threads": threads,
        "merge_review": merge_review,
    }
    if synthesis_requests:
        from mcpbrain.synthesise_threads import attach_synthesis_block
        data = attach_synthesis_block(data, synthesis_requests)
    data = attach_extra_blocks(data, extra_blocks)
    return data


