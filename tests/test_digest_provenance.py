"""Enriched-digest provenance can be repaired WITHOUT re-running the model.

C2 (no date -> importance.recency_decay returns its neutral 0.5 for the
highest-value chunks in the store) and C4 (calendar-derived digests labelled
gmail_enriched_v2) were both fixed in build_semantic_doc, which only runs on
re-enrichment — so neither reached the 22,357 digests already stored.

Bumping ENRICH_LOGIC_VERSION to force re-enrichment was the wrong instrument:
measured on the live store, `date` is recoverable from the digest's OWN text for
15,683 of 22,357 (the text carries a `Date:` line because build_semantic_doc
writes one), and `source_type` is 100% derivable from `thread_id`. Meanwhile
19,934 of 21,029 email digests have had their source chunks pruned by retention,
so re-enrichment cannot recover a date for the remainder either — the Haiku spend
buys nothing precisely where the deterministic route falls short.
"""
from mcpbrain.digest_provenance import derive_patch


def _digest(text: str, meta: dict) -> dict:
    return {"doc_id": f"enriched-{meta.get('thread_id', 'x')}", "text": text,
            "metadata": meta}


def test_date_is_recovered_from_the_digest_text():
    d = _digest("[ACC] Email: Hall B booking\n"
                "From: sam@example.com\n"
                "Date: Tue, 02 Jun 2026 16:30:01 +0800\n"
                "Type: request\n\nConfirmed for Sunday.",
                {"source_type": "gmail_enriched_v2", "thread_id": "t-1"})

    patch = derive_patch(d)

    assert patch["date"] == "Tue, 02 Jun 2026 16:30:01 +0800"


def test_the_date_parse_cannot_swallow_the_following_line():
    """A `\\s*` after 'Date:' matches newlines, so an EMPTY Date line would take
    the next line as its value — measured live, that produced
    'Type: notification' as a date for every calendar digest. Horizontal
    whitespace only."""
    d = _digest("[ACC] Email: ACC State Leaders Gathering\n"
                "From: \n"
                "Date: \n"
                "Type: notification",
                {"source_type": "gmail_enriched_v2", "thread_id": "cal-abc"})

    patch = derive_patch(d)

    assert "date" not in patch, f"parsed a bogus date: {patch.get('date')!r}"


def test_a_calendar_digest_is_relabelled():
    """C4, observed live on enriched-cal-e734d9f93c894a5a81e3230300748014."""
    d = _digest("Date: 2026-05-10", {"source_type": "gmail_enriched_v2",
                                     "thread_id": "cal-e734d9f9"})

    assert derive_patch(d)["source_type"] == "calendar_enriched_v2"


def test_an_email_digest_keeps_its_label():
    d = _digest("Date: 2026-05-10", {"source_type": "gmail_enriched_v2",
                                     "thread_id": "t-1234"})

    assert "source_type" not in derive_patch(d), (
        "an unchanged value must not be written — a no-op patch is a wasted write"
    )


def test_a_digest_that_is_already_correct_yields_no_patch():
    """The phase must converge: a second run has to find nothing to do."""
    d = _digest("Date: Tue, 02 Jun 2026 16:30:01 +0800",
                {"source_type": "gmail_enriched_v2", "thread_id": "t-1",
                 "date": "Tue, 02 Jun 2026 16:30:01 +0800"})

    assert derive_patch(d) == {}


def test_an_existing_date_is_never_overwritten():
    """A date written by build_semantic_doc at enrichment time is authoritative;
    the text is only a fallback for digests that predate the fix."""
    d = _digest("Date: 1999-01-01", {"source_type": "gmail_enriched_v2",
                                     "thread_id": "t-1", "date": "2026-06-02"})

    assert "date" not in derive_patch(d)


def test_an_iso_date_also_populates_date_iso():
    """importance._parse_age_days reads date first, but date_iso is the field the
    C2 fix adds when the caller has an ISO value — populate it when the recovered
    date already IS ISO-8601, so both readers agree."""
    d = _digest("Date: 2026-06-23T06:00:00+08:00",
                {"source_type": "gmail_enriched_v2", "thread_id": "cal-x"})

    patch = derive_patch(d)

    assert patch["date"] == "2026-06-23T06:00:00+08:00"
    assert patch["date_iso"] == "2026-06-23T06:00:00+08:00"


def test_an_rfc2822_date_does_not_populate_date_iso():
    d = _digest("Date: Tue, 02 Jun 2026 16:30:01 +0800",
                {"source_type": "gmail_enriched_v2", "thread_id": "t-1"})

    patch = derive_patch(d)

    assert "date_iso" not in patch, "an RFC2822 header is not an ISO value"


def test_the_recovered_date_actually_unblocks_recency_ranking():
    """The whole point of C2. A digest with no date scores the neutral 0.5
    fallback; with one it scores by age. If this does not move, the patch is
    cosmetic."""
    from mcpbrain.importance import recency_decay

    meta = {"source_type": "gmail_enriched_v2", "thread_id": "t-1"}
    assert recency_decay(meta) == 0.5

    d = _digest("Date: Tue, 02 Jun 2026 16:30:01 +0800", dict(meta))
    meta.update(derive_patch(d))

    assert recency_decay(meta) != 0.5, "the recovered date is not reaching the ranker"


def test_a_digest_with_no_recoverable_date_is_left_alone():
    """6,530 email digests had no Date line at enrichment time. Nothing to
    invent — and re-enrichment cannot recover it either, since their source
    chunks are pruned."""
    d = _digest("[ACC] Email: Something\n\nA summary with no header block.",
                {"source_type": "gmail_enriched_v2", "thread_id": "t-1"})

    assert derive_patch(d) == {}


def test_message_id_is_not_added():
    """C3 is deliberately NOT backfilled, and no longer written at all: nothing
    reads it, a thread-level digest carrying one message's id is false precision,
    and it collided with the lead's raw chunk in thread_enrich._chunk_key badly
    enough to need a defensive special-case. Real per-fact provenance lives in
    entity_relations.source_doc_id / email_entities."""
    d = _digest("Date: 2026-06-02", {"source_type": "gmail_enriched_v2",
                                     "thread_id": "t-1"})

    assert "message_id" not in derive_patch(d)
