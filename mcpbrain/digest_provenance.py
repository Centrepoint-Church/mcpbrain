"""Deterministic provenance repair for enriched-digest chunks (C2, C4).

`semantic.build_semantic_doc` gained `date` / `date_iso` / correct `source_type`
in spec 2, but it only runs when a thread is RE-enriched — so none of it reached
the 22,357 digests already in the store. Measured there: 0 of 22,324 carried a
date, meaning `importance.recency_decay` returned its neutral 0.5 fallback for
the LLM-digested summaries, the highest-value chunks in the corpus and the only
significant population the recency axis could not rank.

Forcing re-enrichment (bumping ENRICH_LOGIC_VERSION) was the wrong instrument:

  * `date` is recoverable from the digest's OWN text for 15,683 of 22,357,
    because build_semantic_doc has always written a "Date: ..." line into it;
  * `source_type` is 100% derivable from `thread_id`;
  * and 19,934 of 21,029 email digests have had their source chunks pruned by
    the retention job, so re-enrichment could not recover a date for the
    remainder EITHER — the model spend buys nothing exactly where this module
    falls short.

`message_id` (C3) is deliberately not backfilled and no longer written: nothing
reads it, attributing a thread-level summary to one message is false precision,
and it collided with the lead message's raw chunk in `thread_enrich._chunk_key`.
Per-fact provenance already lives in `entity_relations.source_doc_id` and
`email_entities`.

Applied with `store.patch_chunk_metadata`, which merges metadata without
touching `content_hash` or `embedded` — so nothing re-embeds and no chunk
re-queues.
"""

import re

# Horizontal whitespace only. `Date:\s*(\S.*)` would let `\s*` cross the newline
# on an EMPTY Date line and capture the FOLLOWING line — measured live, that
# yielded "Type: notification" as the date for every calendar digest.
_RE_DATE = re.compile(r"^Date:[^\S\n]*(\S.*)$", re.MULTILINE)

# ISO-8601-ish: YYYY-MM-DD, optionally with a time. Only these populate
# `date_iso`; an RFC2822 header ("Tue, 02 Jun 2026 ...") is not an ISO value,
# though importance._parse_age_days parses both from `date`.
_RE_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


def digest_source_type(thread_id: str) -> str:
    """The label a digest for `thread_id` should carry.

    C4: calendar-derived enrichments were labelled `gmail_enriched_v2` — observed
    live on `enriched-cal-e734d9f93c894a5a81e3230300748014`. The `cal-` prefix is
    the identity namespace every calendar row already uses (see
    thread_enrich._chunk_key), so this needs no lookup.
    """
    return ("calendar_enriched_v2" if (thread_id or "").startswith("cal-")
            else "gmail_enriched_v2")


def date_from_text(text: str) -> str:
    """The digest's own `Date:` line, or "" when it is absent or empty."""
    m = _RE_DATE.search(text or "")
    return m.group(1).strip() if m else ""


def derive_patch(chunk: dict, *, source_date: str = "") -> dict:
    """Metadata to merge into one digest chunk. Empty dict when already correct.

    `source_date` is an optional fallback for the ~2,000 digests whose source
    chunks survive retention — the caller looks it up; this stays pure so it is
    testable without a store.

    Never overwrites an existing value: a `date` written by build_semantic_doc at
    enrichment time is authoritative, and the text is only a fallback for digests
    that predate that fix. An unchanged `source_type` is omitted rather than
    rewritten, so a second run over a repaired store finds nothing to do — the
    phase converges.
    """
    meta = chunk.get("metadata") or {}
    patch: dict = {}

    want_type = digest_source_type(meta.get("thread_id", ""))
    if meta.get("source_type") != want_type:
        patch["source_type"] = want_type

    if not meta.get("date"):
        date = date_from_text(chunk.get("text", "")) or source_date
        if date:
            patch["date"] = date
            if not meta.get("date_iso") and _RE_ISO.match(date):
                patch["date_iso"] = date

    return patch
