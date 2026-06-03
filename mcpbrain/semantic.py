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

from mcpbrain.graph_write import SYSTEM_LABELS, canonical_org


def build_semantic_doc(extraction: dict, thread: dict) -> tuple[str, dict]:
    """Assemble the synthesised vector doc for one enriched thread.

    `extraction` is the thread's extraction JSON (org, summary, content_type,
    entities, actions, topics). `thread` is the thread lead message envelope
    (subject, sender, date, labels) — apply() passes the lead message it already
    derived. Returns (text, metadata).

    The text mirrors the Nexus shape: an org-prefixed Email line, From/Date,
    Type, a blank line then the summary, a People line (non-Josh persons), an
    Actions block, then Topics and Labels lines.
    """
    org = canonical_org(extraction.get("org", "unknown") or "unknown")
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
        and "josh" not in e.get("name", "").lower()
    ]
    if people_names:
        lines += ["", f"People: {', '.join(people_names)}"]

    if actions_list:
        lines += ["", "Actions:"]
        for a in actions_list:
            line = f"- {a.get('description', '')}"
            owner = a.get("owner_name") or ""
            if owner:
                line += f" (owner: {owner})"
            due = a.get("due_date") or ""
            if due:
                line += f" (due: {due})"
            lines.append(line)

    if topics_list:
        lines += ["", f"Topics: {', '.join(topics_list)}"]
    if custom_labels:
        lines.append(f"Labels: {', '.join(custom_labels)}")

    text = "\n".join(lines)

    metadata = {
        "source_type": "gmail_enriched_v2",
        "thread_id": extraction.get("thread_id", "") or "",
        "subject": subject[:200],
        "org": org,
        "content_type": content_type,
    }
    return text, metadata
