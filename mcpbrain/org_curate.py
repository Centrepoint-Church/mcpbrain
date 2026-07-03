"""Subsystem B3 — the curator.

A standard install with config.role='org_curator'. It curates claims, it does
not extract. Pipeline (daily cadence): ingest contribution JSONL from the fleet
into staging, deterministically merge (reusing resolve.py, role-address
guarded), count corroboration (distinct source_ref / contributor), adjudicate
what determinism can't settle on STRUCTURAL evidence only (verdict 'pending'
when it can't decide), and publish a versioned snapshot (manifest written LAST).
Reversible + capped, per the 0.7.84 brain-review hardening.

This module currently implements only the first pipeline step: ingest.
"""
from __future__ import annotations

import json
import logging

from mcpbrain.org_contracts import ContributionRecord

log = logging.getLogger(__name__)


def _ingest(store, fleet_storage) -> dict:
    """Read every contrib/**/*.jsonl batch into org_contrib_staging.

    Idempotent via the UNIQUE(contributor_email, source_ref, claim) constraint
    on org_contrib_staging: re-ingesting the same batch is a no-op. Malformed
    lines are logged and skipped rather than aborting the whole batch.

    Returns {"batches": n, "ingested": rows_new}.
    """
    batches = 0
    ingested = 0
    for path in fleet_storage.list_paths("contrib/"):
        if not path.endswith(".jsonl"):
            continue
        blob = fleet_storage.get_bytes(path)
        if not blob:
            continue
        batches += 1
        with store._connect() as db:
            for line in blob.decode("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = ContributionRecord.from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("curate: skipping malformed contrib line in %s: %s", path, exc)
                    continue
                cur = db.execute(
                    "INSERT OR IGNORE INTO org_contrib_staging"
                    "(contributor_email, source_ref, claim, confidence, valid_from, "
                    " valid_to, source_kind, batch_file) VALUES(?,?,?,?,?,?,?,?)",
                    (rec.contributor_email, rec.source_ref,
                     json.dumps(rec.claim, sort_keys=True), rec.confidence,
                     rec.valid_from, rec.valid_to, rec.source_kind, path))
                ingested += cur.rowcount
    return {"batches": batches, "ingested": ingested}
