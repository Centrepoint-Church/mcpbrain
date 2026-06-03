"""Proactive detection pass — Phase 3 Task 4.

Ported from src/proactive.py:964-1014 (Nexus). Two detectors are carried across:

- detect_projects_without_next_action: active projects with no open linked action (GTD leak).
- detect_areas_overdue_for_review: areas whose review_cadence has elapsed.

Nexus note: the original filters action_type='next'. mcpbrain has no action_type
column on the unified actions table, so ANY open action linked to a project is
treated as satisfying "has a next action". This is a deliberate simplification;
if action_type is added later the WHERE clause can be tightened.

Not ported: the Nexus `josh_owner_clause`-based detectors (`detect_actions_due_soon`
etc.) are deliberately excluded here because `brain_actions` (Phase 1, Task 8.2)
already covers owner/deadline queries via the MCP tool.
"""

import datetime as dt

_CADENCE_DAYS = {"weekly": 7, "fortnightly": 14, "monthly": 30, "quarterly": 90}


def detect_projects_without_next_action(store) -> list[dict]:
    """Active projects with no open linked action (GTD leak).

    An open action linked via project_id satisfies the check. Actions whose
    status != 'open' (e.g. 'done', 'recorded') do not count.
    """
    with store._connect() as db:
        rows = db.execute("""
            SELECT p.id AS project_id, p.name, p.area_id
            FROM projects p
            WHERE p.archived_at IS NULL
              AND COALESCE(p.status, 'active') = 'active'
              AND NOT EXISTS (
                  SELECT 1 FROM actions a
                  WHERE a.project_id = p.id
                    AND a.status = 'open'
              )
            ORDER BY p.id
        """).fetchall()
    return [dict(r) for r in rows]


def detect_areas_overdue_for_review(store, *, today=None) -> list[dict]:
    """Areas whose review_cadence has elapsed since last_reviewed_at (or created_at).

    today is injected for deterministic testing; defaults to dt.date.today().
    Areas with an unknown or null cadence are skipped.
    Results are sorted by days_overdue descending (worst first).
    """
    if today is None:
        today = dt.date.today()
    with store._connect() as db:
        rows = db.execute("""
            SELECT id AS area_id, name, org_id, review_cadence,
                   COALESCE(last_reviewed_at, created_at) AS reference_date
            FROM areas
            WHERE active=1 AND archived_at IS NULL
        """).fetchall()
    out = []
    for r in rows:
        cad = _CADENCE_DAYS.get(r["review_cadence"])
        if not cad:
            continue
        try:
            ref = dt.date.fromisoformat(str(r["reference_date"])[:10])
        except Exception:
            continue
        elapsed = (today - ref).days
        if elapsed > cad:
            out.append({
                "area_id": r["area_id"],
                "name": r["name"],
                "org_id": r["org_id"],
                "review_cadence": r["review_cadence"],
                "days_overdue": elapsed - cad,
            })
    return sorted(out, key=lambda x: -x["days_overdue"])


def run(store, *, now: str | None = None) -> dict:
    """Run both proactive detectors and record findings.

    Upserts a proactive_findings row for every active gap; resolves rows whose
    underlying condition has cleared. Returns counts of live gaps found this
    pass.
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc).isoformat()
    today = dt.date.fromisoformat(now[:10])

    project_gaps = detect_projects_without_next_action(store)
    area_gaps = detect_areas_overdue_for_review(store, today=today)

    live_project_ids = []
    for g in project_gaps:
        ref_id = g["project_id"]
        store.record_finding(
            "project_no_next_action", ref_id,
            summary=f"Project '{g['name']}' has no open next action",
            severity="info", detected_at=now,
        )
        live_project_ids.append(ref_id)
    store.resolve_findings_not_in("project_no_next_action", live_project_ids, now)

    live_area_ids = []
    for g in area_gaps:
        ref_id = g["area_id"]
        store.record_finding(
            "area_overdue", ref_id,
            org=g.get("org_id", ""),
            summary=f"Area '{g['name']}' overdue by {g['days_overdue']} days ({g['review_cadence']})",
            severity="info", detected_at=now,
        )
        live_area_ids.append(ref_id)
    store.resolve_findings_not_in("area_overdue", live_area_ids, now)

    return {
        "project_no_next_action": len(project_gaps),
        "area_overdue": len(area_gaps),
    }
