"""Prepare step: turn un-enriched email threads into a pending.json spool.

The daemon runs this. It takes Phase 1's thread groups, noise-filters them,
attaches standing context plus each thread's prior context and open actions,
caps the number of threads, splits over-long threads, and (when resolution is
due) appends a merge-review block. The result is written atomically to
MCPBRAIN_HOME/enrich_queue/pending.json for the extractor to read.

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

from mcpbrain import config, prompt, thread_enrich
from mcpbrain.enrich_blocks import UNIT_BLOCKS as _UNIT_BLOCKS
from mcpbrain.resolve import _candidate_pairs

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
    # Deliberately NOT added: support@, info@, hello@ — too broad, hit real mail.
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

    if source == "gmail" or meta.get("thread_id"):
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
        # Drive: gate on the extractor's content_subtype tag, then mime + length.
        # content_subtype is set per-MIME at ingest (normalise_drive); a 'table'
        # chunk (spreadsheet/CSV) is tabular data, not prose worth entity
        # extraction — skip it source-agnostically so a tag set on any future
        # tabular source is honoured without re-listing mimes here.
        if str(meta.get("content_subtype") or "").lower() == "table":
            return False
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


def _apply_salience_gate(store, batches: list, *, require_drive_mention: bool = False) -> tuple[list, dict]:
    """Run should_enrich() over all chunks in each batch.

    Chunks that do not enrich are marked 'cold' in the store (reversible) and
    removed from the batch. Empty batches are discarded. Returns (kept_batches,
    summary) where summary has 'gated' (cold-marked) and 'kept' counts.

    When require_drive_mention is True, a Drive chunk that passes should_enrich is
    ADDITIONALLY required to be referenced in email (ops-brain's mention gate) —
    off by default because mcpbrain holds valuable un-emailed docs (minutes,
    profiles) that a blanket mention requirement would wrongly cold-gate.
    """
    gated = kept = 0
    result = []
    for batch in batches:
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

def _filter_noise(store, batches) -> list:
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
    """
    kept = []
    for batch in batches:
        messages = list(_reassemble_thread(batch.chunks))
        if thread_is_noise(messages):
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


def _apply_trivial_threads(store, batches, *, home=None) -> list:
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
    """
    _home = str(home) if home is not None else str(config.app_dir())
    if not config.enrich_trivial_thread_summary(_home):
        return batches
    from mcpbrain import graph_write

    kept = []
    for batch in batches:
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
        graph_write.apply(store, extraction, doc_ids=batch.doc_ids, home=home)
        store.mark_enriched(batch.doc_ids)
    return kept


# --- thread assembly -------------------------------------------------------

def _thread_block(store, batch) -> dict:
    """Reassemble one thread into the pending-thread shape: ordered messages with
    body text, plus prior context and open actions.

    prior_thread_context is '' until Phase 3 populates it; degrade to empty
    rather than fail. open_actions is [] when the thread has no open actions.

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


def _split_long_thread(block, char_budget: int) -> list:
    """Split a thread whose joined message bodies exceed char_budget into ordered
    sub-batches. Each sub-batch shares the thread_id, prior_thread_context,
    open_actions, and org_hint (all thread-level metadata, not per-message), and
    carries {"part": i, "of": k} so the drain can re-group them by thread_id
    before apply. Message order is preserved across the split.
    """
    messages = block["messages"]
    total = sum(len(m.get("text", "")) for m in messages)
    if total <= char_budget:
        return [block]
    if len(messages) <= 1:
        # A single message can't be split across messages; it ships as one
        # over-budget part. Log it so the breach of the size guard is visible
        # (the extractor session may need to truncate this one itself).
        log.warning("prepare: thread %s is a single message of %d chars, over "
                    "the %d budget; shipping unsplit",
                    block.get("thread_id"), total, char_budget)
        return [block]

    groups = []
    current = []
    current_chars = 0
    for m in messages:
        size = len(m.get("text", ""))
        if current and current_chars + size > char_budget:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(m)
        current_chars += size
    if current:
        groups.append(current)

    k = len(groups)
    parts = []
    for i, group in enumerate(groups, start=1):
        group_chars = sum(len(m.get("text", "")) for m in group)
        if group_chars > char_budget:
            # A single message larger than the budget lands alone in its group.
            log.warning("prepare: thread %s part %d/%d is %d chars, over the %d "
                        "budget (a single oversized message)",
                        block.get("thread_id"), i, k, group_chars, char_budget)
        parts.append({
            "thread_id": block["thread_id"],
            "prior_thread_context": block["prior_thread_context"],
            "open_actions": block["open_actions"],
            "org_hint": block.get("org_hint", ""),
            "part": i,
            "of": k,
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

def _community_summaries_for_people(store, known_people: list) -> list[dict]:
    """Deduplicated community summaries for the communities the known-people
    entities belong to. Degrades to [] on any error."""
    if not known_people:
        return []
    try:
        entity_ids = [p["id"] for p in known_people if p.get("id")]
        if not entity_ids:
            return []
        memberships = store.communities_for(entity_ids)
        cids = {m["community_id"] for m in memberships}
        if not cids:
            return []
        return [s for s in store.list_communities() if s["community_id"] in cids]
    except Exception as exc:  # noqa: BLE001
        log.warning("_community_summaries_for_people failed: %s", exc)
        return []


def _build_context(store, thread_ids) -> dict:
    home = str(config.app_dir())
    known_people = _build_known_people(store, batch_thread_ids=thread_ids)
    return {
        "owner_name": config.owner_full_name(home) or config.owner_name(home),
        "known_people": known_people,
        "org_domain_map": _org_domain_lines(),
        "valid_orgs": _valid_org_tags(),
        "community_summaries": _community_summaries_for_people(store, known_people),
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
# enrich_queue/units/, plus a shared enrich_queue/context.json. The enrich session
# consumes them one subagent per unit (see mcp_server.brain_enrich_units / _pull /
# _push). This replaces the single churning pending.json + read-time manifest.

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
    """Turn a prepared batch dict (threads + optional blocks + context) into
    immutable, pre-sized work-unit files under enrich_queue/units/, plus a shared
    enrich_queue/context.json the pull attaches. Each unit is sized so its pull
    (work + rules + context) fits the cap. Unit ids are content hashes, so
    re-running on the same un-enriched work is idempotent. Honors a window cap
    (backpressure): when the queue already holds >= window undrained units, the
    cycle produces no new ones. Returns a summary."""
    if pull_cap is None:
        pull_cap = config.unit_pull_cap(home)
    from pathlib import Path
    queue = (config.app_dir() if home is None else Path(home)) / "enrich_queue"
    units_dir = queue / "units"
    units_dir.mkdir(parents=True, exist_ok=True)
    # Shared standing context, refreshed each cycle. It is reference data
    # (known_people, valid_orgs, …), not work, so refreshing it under in-flight
    # units is harmless — a unit's WORK never changes.
    context = data.get("context") or {}
    _atomic_write(queue / "context.json", json.dumps(context, ensure_ascii=False))
    existing = list(units_dir.glob("*.json"))
    if len(existing) >= window:
        return {"units_written": 0, "units_pending": len(existing),
                "skipped": "window_full"}
    ctx_len = len(json.dumps(context, ensure_ascii=False))
    budget = max(2000, pull_cap - _UNIT_RULES_RESERVE - ctx_len - 1500)
    written = 0
    for chunk in _pack_by_size(data.get("threads") or [], budget,
                               lambda t: len(json.dumps(t)) + 1):
        tids = sorted(str(t.get("thread_id")) for t in chunk)
        uid = _unit_id("thread", ",".join(tids))
        _atomic_write(units_dir / f"{uid}.json",
                      json.dumps({"unit_id": uid, "kind": "thread",
                                  "threads": chunk}, ensure_ascii=False))
        written += 1
    for k in _UNIT_BLOCKS:
        for chunk in _pack_by_size(data.get(k) or [], budget,
                                   lambda it: len(json.dumps(it)) + 1):
            sig = k + ":" + json.dumps(chunk, sort_keys=True, ensure_ascii=False)
            uid = _unit_id("block", sig)
            _atomic_write(units_dir / f"{uid}.json",
                          json.dumps({"unit_id": uid, "kind": "block",
                                      "block": k, "items": chunk}, ensure_ascii=False))
            written += 1
    return {"units_written": written, "units_pending": len(existing) + written}


def prepare_units(store, *, thread_cap: int, char_budget: int,
                  resolution_due: bool, now=None,
                  synthesis_requests: list | None = None,
                  extra_blocks: dict | None = None, home=None,
                  window: int = 600) -> dict:
    """Build the current batch (un-enriched threads + due blocks) and write it as
    work units. The work-queue replacement for prepare(): no single pending.json —
    a bounded queue of immutable units the enrich session consumes. Unlike prepare()
    it still produces block units when there are no threads."""
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
            require_drive_mention=config.salience_require_drive_mention(_home))
    non_noise = _filter_noise(store, batches)
    # Trivial threads are deterministically extracted and marked enriched here
    # (no model call) before thread_cap is applied. group_unenriched_threads already
    # caps the pool at thread_cap, so within THIS cycle excluding trivial threads
    # doesn't add more model calls. The benefit is cross-cycle: resolving trivial
    # threads clears them from the backlog faster, making more distinct non-trivial
    # threads visible to group_unenriched_threads in the NEXT cycle.
    non_trivial = _apply_trivial_threads(store, non_noise, home=home)
    kept = non_trivial[:thread_cap]
    data = build_pending(store, kept, char_budget=char_budget, now=now,
                         resolution_due=resolution_due,
                         synthesis_requests=synthesis_requests,
                         extra_blocks=extra_blocks)
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
                  extra_blocks: dict | None = None) -> dict:
    """Assemble the pending.json dict for already-grouped, noise-filtered batches.

    Pure assembly: builds thread blocks (splitting over-long threads), context,
    and the optional merge-review block, then returns the dict. Does NOT write
    any file and does NOT mark the store. `batch_id` defaults to a timestamped
    id when not supplied. Callers that need many concurrent batches pass their
    own unique batch_id.
    """
    threads = []
    for batch in batches:
        block = _thread_block(store, batch)
        threads.extend(_split_long_thread(block, char_budget))

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


