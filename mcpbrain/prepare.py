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


# --- thread assembly -------------------------------------------------------

def _thread_block(store, batch) -> dict:
    """Reassemble one thread into the pending-thread shape: ordered messages with
    body text, plus prior context and open actions.

    prior_thread_context is '' until Phase 3 populates it; degrade to empty
    rather than fail. open_actions is [] when the thread has no open actions.
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
    return {
        "thread_id": batch.thread_id,
        "prior_thread_context": prior,
        "open_actions": actions,
        "messages": messages,
    }


def _split_long_thread(block, char_budget: int) -> list:
    """Split a thread whose joined message bodies exceed char_budget into ordered
    sub-batches. Each sub-batch shares the thread_id, prior_thread_context, and
    open_actions, and carries {"part": i, "of": k} so the drain can re-group them
    by thread_id before apply. Message order is preserved across the split.
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


def _merge_review_block(store) -> list:
    """Candidate pairs for LLM adjudication, folded into the spool. Reuses the
    existing fuzzy candidate finder. The deterministic resolve tier still runs
    every cycle elsewhere; this block only covers the LLM-adjudication tier.
    """
    pairs = _candidate_pairs(store.entities_for_resolution())
    return [_merge_pair(a, b) for a, b in pairs]


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

def _write_pending(data: dict) -> None:
    """Write pending.json atomically (temp file + os.replace), mirroring the
    pattern in config.write_config. No stray temp on failure.
    """
    queue_dir = config.app_dir() / "enrich_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    target = queue_dir / "pending.json"
    fd, tmp = tempfile.mkstemp(dir=str(queue_dir), prefix=".pending.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def prepare(store, *, thread_cap: int, char_budget: int,
            resolution_due: bool, now=None,
            synthesis_requests: list | None = None,
            extra_blocks: dict | None = None) -> dict:
    """Build pending.json from un-enriched threads and return a summary.

    group_unenriched_threads already caps the thread COUNT; thread_cap is a
    belt-and-braces ceiling applied here too. The long-thread guard splits any
    thread whose joined bodies exceed char_budget. When there are zero non-noise
    threads, no file is written and a zero summary is returned.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    batches = _group_unenriched_threads(store, thread_cap=thread_cap)
    kept = _filter_noise(store, batches)
    kept = kept[:thread_cap]

    if not kept:
        return {"batch_id": None, "threads": 0, "merge_pairs": 0}

    data = build_pending(store, kept, char_budget=char_budget, now=now,
                         resolution_due=resolution_due,
                         synthesis_requests=synthesis_requests,
                         extra_blocks=extra_blocks)
    _write_pending(data)
    return {"batch_id": data["batch_id"], "threads": len(data["threads"]),
            "merge_pairs": len(data["merge_review"])}
