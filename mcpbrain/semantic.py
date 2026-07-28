"""Semantic layer for the enrichment graph (Phase 1, Task 5).

Builds a single synthesised vector document per enriched thread: org + subject
+ summary + a People line + Action lines + a Topics line. The doc is embedded
into the mcpbrain search index so `brain_search` returns enriched summaries
alongside raw chunks.

Ported from the Nexus `build_vector_doc` (src/enrich_gmail.py:1605-1672), the
TEXT ASSEMBLY only. The Nexus Qdrant payload/metadata (message_id, sender,
date, has_actions, people, labels, enriched_at) is dropped — mcpbrain keys the
chunk on its own doc_id (`enriched-{thread_id}`) and only carries the five
fields the contextual-prefix / filter paths actually read.
"""

from mcpbrain.graph_write import (
    SYSTEM_LABELS,
    _is_owner,
    canonical_org,
    owner_identity_from_config,
)


# The BGE window is 512 tokens ≈ 2,000 characters, and embed.contextual_prefix
# (default ON) eats into the same budget — hence the headroom. Anything past it
# is silently truncated by the model and its tail is unsearchable (B3).
#
# This is the ONE population chunk_text cannot bound, because the semantic doc
# is written whole to keep its `enriched-<thread_id>` doc_id (mark_enriched,
# doc_ids_for_messages and the stale-reextract sweep all key on it). Splitting
# it is therefore off the table — but it does not need splitting, because the
# doc is SYNTHESISED here, line by line, so its length is ours to choose.
SEMANTIC_MAX_CHARS = 1800


# A truncated fragment shorter than this is not worth keeping in place of the
# elision marker — at that point there is no room left for anything readable.
_MIN_TRUNCATED_LINE_CHARS = 40


def _fit(lines: list[str], budget: int) -> list[str]:
    """Keep whole lines while they fit; TRUNCATE the one that overflows, then stop.

    Callers pass lines in DESCENDING value order — subject, From/Date, summary,
    then People, Actions, Topics, Labels — because what gets dropped under
    pressure must be the least query-relevant content. Dropping the summary to
    keep a Labels line would be worse than not bounding at all.

    I2: this used to drop the offending line WHOLESALE, so an over-budget summary
    — the doc's only searchable prose — vanished entirely and the chunk was left
    as bare headers. That is precisely the failure the paragraph above warns
    against, caused by the bounding itself. The overflowing line is now cut to
    the remaining budget with a trailing '…'; later lines are still elided.
    """
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            room = budget - used - 1        # -1 for the joining newline
            if room >= _MIN_TRUNCATED_LINE_CHARS and line.strip():
                # Its own trailing '…' is the elision marker — for this line AND
                # for the lines after it, which by definition have no room left.
                out.append(line[:room - 1] + "…")
            else:
                out.append("…")
            break
        out.append(line)
        used += len(line) + 1
    return out


def build_semantic_doc(extraction: dict, thread: dict, owner=None, taxonomy=None,
                       *, date_iso: str = "", message_id: str = "") -> tuple[str, dict]:
    """Assemble the synthesised vector doc for one enriched thread.

    `extraction` is the thread's extraction JSON (org, summary, content_type,
    entities, actions, topics). `thread` is the thread lead message envelope
    (subject, sender, date, labels) — apply() passes the lead message it already
    derived. Returns (text, metadata).

    The text mirrors the Nexus shape: an org-prefixed Email line, From/Date,
    Type, a blank line then the summary, a People line (persons other than the
    install owner), an Actions block, then Topics and Labels lines.

    owner: optional OwnerIdentity; None resolves from config.
    taxonomy: optional OrgTaxonomy; None resolves from config.
    date_iso: optional ISO-normalised date for the lead message, if the caller
    already has one.
    message_id: the lead message's id, for message-level provenance (C3).
    """
    if owner is None:
        owner = owner_identity_from_config()
    org = canonical_org(extraction.get("org", "unknown") or "unknown", taxonomy)
    actions_list = extraction.get("actions", []) or []
    topics_list = extraction.get("topics", []) or []
    entities_list = extraction.get("entities", []) or []
    summary = extraction.get("summary", "") or ""
    content_type = extraction.get("content_type", "") or ""

    subject = thread.get("subject", "") or ""
    sender = thread.get("sender", "") or ""
    date = thread.get("date", "") or ""

    raw_labels = thread.get("labels", "") or ""
    custom_labels = [
        lbl.strip() for lbl in raw_labels.split(",")
        if lbl.strip() and lbl.strip() not in SYSTEM_LABELS
    ]

    org_prefix = f"[{org}]" if org and org != "unknown" else ""
    email_line = f"{org_prefix} Email: {subject}".strip()
    lines = [email_line, f"From: {sender}", f"Date: {date}"]
    if content_type:
        lines.append(f"Type: {content_type}")
    if summary:
        lines += ["", summary]

    people_names = [
        e.get("name", "") for e in entities_list
        if e.get("type") == "person"
        and e.get("name")
        and not _is_owner(e.get("name", ""), owner)
    ]
    if people_names:
        lines += ["", f"People: {', '.join(people_names)}"]

    if actions_list:
        lines += ["", "Actions:"]
        for a in actions_list:
            line = f"- {a.get('description', '')}"
            action_owner_name = a.get("owner_name") or ""
            if action_owner_name:
                line += f" (owner: {action_owner_name})"
            due = a.get("due_date") or ""
            if due:
                line += f" (due: {due})"
            lines.append(line)

    if topics_list:
        lines += ["", f"Topics: {', '.join(topics_list)}"]
    if custom_labels:
        lines.append(f"Labels: {', '.join(custom_labels)}")

    text = "\n".join(_fit(lines, SEMANTIC_MAX_CHARS))

    thread_id = extraction.get("thread_id", "") or ""
    metadata = {
        # C4: a calendar-sourced enrichment carries a cal-* thread id and was
        # nonetheless labelled gmail_enriched_v2 — observed live on
        # cal-e734d9f93c894a5a81e3230300748014. No consumer reads these values
        # today beyond tests (grep: semantic.py is the only writer, and nothing
        # in importance.py or retrieval.py branches on them), so correcting the
        # label is safe.
        "source_type": ("calendar_enriched_v2" if thread_id.startswith("cal-")
                        else "gmail_enriched_v2"),
        "thread_id": thread_id,
        "subject": subject[:200],
        "org": org,
        "content_type": content_type,
        # C2: without a date, importance.recency_decay returns its neutral 0.5
        # fallback for all 21,162 of these. `date` is the lead's RFC2822 header,
        # which importance._parse_age_days already handles.
        "date": date[:80],
        # C3: thread-level provenance without message-level provenance means a
        # fact can be traced to a thread but not to the message it came from.
        "message_id": message_id[:200],
    }
    if date_iso:
        metadata["date_iso"] = date_iso[:40]
    return text, metadata
