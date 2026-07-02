"""Small metrics helper for the brain-review cadence. See docs/superpowers/plans/
2026-07-02-session-4-brain-review-cadence.md."""


def review_metrics(store) -> dict:
    open_findings = store.open_findings(None)
    by_type: dict = {}
    for finding in open_findings:
        ftype = finding["finding_type"]
        by_type[ftype] = by_type.get(ftype, 0) + 1
    # No daemon-side bookkeeping yet for findings resolved in the most recent run.
    return {"open_findings": len(open_findings), "by_type": by_type, "resolved_last_run": 0}
