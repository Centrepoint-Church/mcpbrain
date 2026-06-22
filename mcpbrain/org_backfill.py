"""Deterministic org backfill sweep (Q4).

Runs org_from_email over existing entities that have no org set but have an
email_addr that can be resolved via the domain taxonomy. No LLM — purely
deterministic.

Reports:
  - Entities updated (gained an org)
  - Unknown sender domains (not in the taxonomy; surfaced for review)
"""
from __future__ import annotations

import logging

log = logging.getLogger("mcpbrain.org_backfill")


def run_backfill(store, *, limit: int | None = None) -> dict:
    """Assign org to entities that have an email_addr but no org.

    Uses graph_write.org_from_email (domain taxonomy lookup, deterministic).
    Skips entities where the email domain resolves to '' (empty input) or
    'external' (unrecognised — surfaced in the unknown_domains report).

    Returns {
        "updated": int,             entities that gained an org
        "skipped_external": int,    entities whose domain is external/unknown
        "skipped_no_email": int,    entities with no email_addr (not queried)
        "unknown_domains": list[str]  unrecognised sender domains for review
    }
    """
    from mcpbrain.graph_write import org_from_email
    from mcpbrain import orgs as _orgs

    taxonomy = _orgs.taxonomy_from_config()
    candidates = store.entities_without_org(limit=limit)

    updated = 0
    skipped_external = 0
    unknown_domains: set[str] = set()

    for ent in candidates:
        email_addr = (ent.get("email_addr") or "").strip()
        if not email_addr:
            continue

        org = org_from_email(email_addr, taxonomy)

        if not org or org == "external":
            skipped_external += 1
            # Harvest the domain for the "missing from taxonomy" report.
            if "@" in email_addr:
                domain = email_addr.split("@", 1)[1].lower().strip()
                if domain:
                    unknown_domains.add(domain)
            continue

        # Found a recognised org — write it back.
        store.update_entity_org(ent["id"], org)
        updated += 1

    if unknown_domains:
        log.info(
            "org_backfill: %d unknown domains not in taxonomy (top 20): %s",
            len(unknown_domains),
            sorted(unknown_domains)[:20],
        )

    return {
        "updated": updated,
        "skipped_external": skipped_external,
        "unknown_domains": sorted(unknown_domains),
    }
