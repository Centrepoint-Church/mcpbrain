"""Tests for mcpbrain/proactive.py — Phase 3 Task 4.

Sub-tasks 4.1 (detectors), 4.2 (run + findings sink).
"""

import datetime as dt
from mcpbrain.store import Store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_store(tmp_path, name="proactive.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _insert_project(store, project_id, name="Test", status="active", archived_at=None):
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO projects(id, name, status, archived_at) VALUES (?,?,?,?)",
            (project_id, name, status, archived_at),
        )


def _insert_action(store, project_id=None, area_id=None, status="open", owner="Sam"):
    with store._connect() as db:
        db.execute(
            "INSERT INTO actions(text, project_id, area_id, status, owner) VALUES (?,?,?,?,?)",
            ("test action", project_id or "", area_id or "", status, owner),
        )


def _insert_area(store, area_id, name="Test Area", review_cadence="weekly",
                 last_reviewed_at=None, active=1):
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO areas(id, org_id, name, review_cadence, last_reviewed_at, active) "
            "VALUES (?,?,?,?,?,?)",
            (area_id, "Acme", name, review_cadence, last_reviewed_at, active),
        )


# ---------------------------------------------------------------------------
# Sub-task 4.1: detector tests
# ---------------------------------------------------------------------------

def test_projects_without_next_action_flagged(tmp_path):
    """An active project with no open actions is flagged."""
    from mcpbrain.proactive import detect_projects_without_next_action
    store = _make_store(tmp_path)
    _insert_project(store, "p-1", name="College 2026")
    gaps = detect_projects_without_next_action(store)
    assert any(g["project_id"] == "p-1" for g in gaps)


def test_projects_without_next_action_cleared_when_action_exists(tmp_path):
    """An active project WITH an open action is NOT flagged."""
    from mcpbrain.proactive import detect_projects_without_next_action
    store = _make_store(tmp_path)
    _insert_project(store, "p-2", name="CAMS Review")
    _insert_action(store, project_id="p-2", status="open")
    gaps = detect_projects_without_next_action(store)
    assert all(g["project_id"] != "p-2" for g in gaps)


def test_projects_without_next_action_archived_skipped(tmp_path):
    """An archived project is not flagged, even if it has no open actions."""
    from mcpbrain.proactive import detect_projects_without_next_action
    store = _make_store(tmp_path)
    _insert_project(store, "p-archived", archived_at="2026-01-01")
    gaps = detect_projects_without_next_action(store)
    assert all(g["project_id"] != "p-archived" for g in gaps)


def test_projects_without_next_action_done_action_does_not_satisfy(tmp_path):
    """A project with only a DONE action (status='done') is still flagged."""
    from mcpbrain.proactive import detect_projects_without_next_action
    store = _make_store(tmp_path)
    _insert_project(store, "p-done-action", name="Stalled project")
    _insert_action(store, project_id="p-done-action", status="done")
    gaps = detect_projects_without_next_action(store)
    assert any(g["project_id"] == "p-done-action" for g in gaps)


def test_areas_overdue_weekly_flagged(tmp_path):
    """An active weekly area last reviewed 10 days ago is flagged with days_overdue=3."""
    from mcpbrain.proactive import detect_areas_overdue_for_review
    store = _make_store(tmp_path)
    today = dt.date(2026, 6, 3)
    last_reviewed = (today - dt.timedelta(days=10)).isoformat()
    _insert_area(store, "a-ops", review_cadence="weekly", last_reviewed_at=last_reviewed)
    gaps = detect_areas_overdue_for_review(store, today=today)
    assert len(gaps) == 1
    assert gaps[0]["area_id"] == "a-ops"
    assert gaps[0]["days_overdue"] == 3  # 10 days elapsed, cadence=7, overdue=3


def test_areas_overdue_reviewed_yesterday_not_flagged(tmp_path):
    """An area reviewed yesterday (within cadence) is not flagged."""
    from mcpbrain.proactive import detect_areas_overdue_for_review
    store = _make_store(tmp_path)
    today = dt.date(2026, 6, 3)
    last_reviewed = (today - dt.timedelta(days=1)).isoformat()
    _insert_area(store, "a-recent", review_cadence="weekly", last_reviewed_at=last_reviewed)
    gaps = detect_areas_overdue_for_review(store, today=today)
    assert all(g["area_id"] != "a-recent" for g in gaps)


def test_areas_overdue_unknown_cadence_skipped(tmp_path):
    """An area with an unknown/null cadence is not flagged."""
    from mcpbrain.proactive import detect_areas_overdue_for_review
    store = _make_store(tmp_path)
    today = dt.date(2026, 6, 3)
    _insert_area(store, "a-no-cadence", review_cadence=None, last_reviewed_at="2020-01-01")
    _insert_area(store, "a-unknown", review_cadence="daily", last_reviewed_at="2020-01-01")
    gaps = detect_areas_overdue_for_review(store, today=today)
    area_ids = {g["area_id"] for g in gaps}
    assert "a-no-cadence" not in area_ids
    assert "a-unknown" not in area_ids  # "daily" is not in _CADENCE_DAYS


def test_areas_overdue_fortnightly(tmp_path):
    """A fortnightly area last reviewed 20 days ago is overdue by 6 days."""
    from mcpbrain.proactive import detect_areas_overdue_for_review
    store = _make_store(tmp_path)
    today = dt.date(2026, 6, 3)
    last_reviewed = (today - dt.timedelta(days=20)).isoformat()
    _insert_area(store, "a-fort", review_cadence="fortnightly", last_reviewed_at=last_reviewed)
    gaps = detect_areas_overdue_for_review(store, today=today)
    matching = [g for g in gaps if g["area_id"] == "a-fort"]
    assert len(matching) == 1
    assert matching[0]["days_overdue"] == 6  # 20 - 14 = 6


def test_areas_overdue_sorted_by_days_overdue_desc(tmp_path):
    """Results are sorted by days_overdue descending (worst first)."""
    from mcpbrain.proactive import detect_areas_overdue_for_review
    store = _make_store(tmp_path)
    today = dt.date(2026, 6, 3)
    # Area A: 30 days since review, weekly cadence -> overdue by 23 days
    _insert_area(store, "a-worse", name="Worse", review_cadence="weekly",
                 last_reviewed_at=(today - dt.timedelta(days=30)).isoformat())
    # Area B: 10 days since review, weekly cadence -> overdue by 3 days
    _insert_area(store, "a-less", name="Less", review_cadence="weekly",
                 last_reviewed_at=(today - dt.timedelta(days=10)).isoformat())
    gaps = detect_areas_overdue_for_review(store, today=today)
    assert gaps[0]["days_overdue"] >= gaps[-1]["days_overdue"]
    assert gaps[0]["area_id"] == "a-worse"


# ---------------------------------------------------------------------------
# Sub-task 4.2: run() + findings sink
# ---------------------------------------------------------------------------

def test_proactive_run_records_findings(tmp_path):
    """run(store, now=now) writes proactive_findings for both detector types."""
    from mcpbrain.proactive import run
    store = _make_store(tmp_path)
    now = "2026-06-03T10:00:00+00:00"

    # Project gap
    _insert_project(store, "p-gap", name="Gapped Project")
    # Area gap: last reviewed 10 days ago with weekly cadence
    last_reviewed = (dt.date(2026, 6, 3) - dt.timedelta(days=10)).isoformat()
    _insert_area(store, "a-gap", name="Gapped Area", review_cadence="weekly",
                 last_reviewed_at=last_reviewed)

    result = run(store, now=now)

    assert result["project_no_next_action"] == 1
    assert result["area_overdue"] == 1

    project_findings = store.open_findings("project_no_next_action")
    assert len(project_findings) == 1
    pf = project_findings[0]
    assert pf["ref_id"] == "p-gap"
    assert "Gapped Project" in pf["summary"]
    assert pf["resolved_at"] is None

    area_findings = store.open_findings("area_overdue")
    assert len(area_findings) == 1
    af = area_findings[0]
    assert af["ref_id"] == "a-gap"
    assert "Gapped Area" in af["summary"]
    assert af["resolved_at"] is None


def test_proactive_run_resolves_closed_gaps(tmp_path):
    """A previously-flagged project that now has an open action gets resolved_at set."""
    from mcpbrain.proactive import run
    store = _make_store(tmp_path)

    # First run: project has no open action -> flagged
    _insert_project(store, "p-resolves", name="Will Resolve")
    run(store, now="2026-06-01T10:00:00+00:00")

    findings_before = store.open_findings("project_no_next_action")
    assert any(f["ref_id"] == "p-resolves" for f in findings_before)

    # Add an open action to the project (fixes the gap)
    _insert_action(store, project_id="p-resolves", status="open")

    # Second run: project now has open action -> finding should be resolved
    run(store, now="2026-06-02T10:00:00+00:00")

    findings_after = store.open_findings("project_no_next_action")
    assert all(f["ref_id"] != "p-resolves" for f in findings_after)

    # Verify the finding row is resolved (not deleted)
    with store._connect() as db:
        row = db.execute(
            "SELECT resolved_at FROM proactive_findings "
            "WHERE finding_type='project_no_next_action' AND ref_id='p-resolves'"
        ).fetchone()
    assert row is not None
    assert row["resolved_at"] is not None


def test_proactive_run_re_opens_resolved_finding(tmp_path):
    """If a gap reappears after being resolved, the finding resurfaces (resolved_at cleared)."""
    from mcpbrain.proactive import run
    store = _make_store(tmp_path)

    # Run 1: project flagged
    _insert_project(store, "p-reopen", name="Reopen Me")
    run(store, now="2026-06-01T10:00:00+00:00")

    # Run 2: add action -> resolves
    _insert_action(store, project_id="p-reopen", status="open")
    run(store, now="2026-06-02T10:00:00+00:00")
    assert not store.open_findings("project_no_next_action")

    # Close the action -> gap reappears
    with store._connect() as db:
        db.execute("UPDATE actions SET status='done' WHERE project_id='p-reopen'")

    # Run 3: finding resurfaces
    run(store, now="2026-06-03T10:00:00+00:00")
    findings = store.open_findings("project_no_next_action")
    assert any(f["ref_id"] == "p-reopen" for f in findings)


def test_proactive_run_no_gaps_returns_zero_counts(tmp_path):
    """A store with no gaps returns zero counts and no open findings."""
    from mcpbrain.proactive import run
    store = _make_store(tmp_path)
    result = run(store, now="2026-06-03T10:00:00+00:00")
    assert result == {"project_no_next_action": 0, "area_overdue": 0}
    assert store.open_findings() == []
