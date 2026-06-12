"""Tests for mcpbrain/lint_graph.py — Phase 3 Task 2 + Task A5.

Sub-tasks covered:
  2.1  individual checks (check_missing_org, check_orphan_entities,
       check_ambiguous_org, check_ownerless_actions, check_duplicate_orgs)
  2.2  build_report counts + run() findings sink
  A5   deleted three redundant checks (check_possible_duplicates,
       check_community_singletons, check_threads_without_summary)
"""

import json

from mcpbrain.store import Store
from mcpbrain.lint_graph import (
    check_missing_org,
    check_orphan_entities,
    check_ambiguous_org,
    check_ownerless_actions,
    check_duplicate_orgs,
    build_report,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store(tmp_path, name="lint.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _add_entity(store, eid, name, etype="person", org="", email_addr="", email_count=0):
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO entities"
            "(id, name, type, org, email_addr, email_count) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (eid, name, etype, org, email_addr, email_count),
        )


def _add_relation(store, a, b):
    store.add_relation(a, "knows", b, "doc-test")


def _add_action(store, action_id, text, owner="", source="email", thread_id=""):
    with store._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO actions"
            "(id, text, owner, source, thread_id) VALUES (?, ?, ?, ?, ?)",
            (action_id, text, owner, source, thread_id),
        )


def _add_email_context(store, message_id, thread_id="", subject="", date_iso=""):
    store.upsert_email_context(
        message_id,
        subject=subject,
        thread_id=thread_id,
        date_iso=date_iso,
    )


# ---------------------------------------------------------------------------
# Sub-task 2.1 — check_missing_org
# ---------------------------------------------------------------------------

def test_check_missing_org_flags(tmp_path):
    """Entity with email_count>0, type!='topic', empty org -> flagged."""
    s = _store(tmp_path)
    _add_entity(s, "e1", "Alice Smith", org="", email_count=3)
    _add_entity(s, "e2", "Bob Jones", org="Acme", email_count=3)
    _add_entity(s, "e3", "some-topic", etype="topic", org="", email_count=5)

    with s._connect() as db:
        flagged = check_missing_org(db)

    ids = [r["id"] for r in flagged]
    assert "e1" in ids, "e1 (no org, person, 3 emails) should be flagged"
    assert "e2" not in ids, "e2 (has org) should not be flagged"
    assert "e3" not in ids, "e3 (topic type) should not be flagged"


# ---------------------------------------------------------------------------
# Sub-task 2.1 — check_orphan_entities
# ---------------------------------------------------------------------------

def test_check_orphan_entities_flags(tmp_path):
    """Entity with email_count=0 and no relations -> flagged; with relation -> not."""
    s = _store(tmp_path)
    _add_entity(s, "orphan", "Orphan", org="Acme", email_count=0)
    _add_entity(s, "connected", "Connected", org="Acme", email_count=0)
    _add_relation(s, "orphan", "connected")  # now connected has a relation

    with s._connect() as db:
        flagged = check_orphan_entities(db)

    ids = [r["id"] for r in flagged]
    # orphan has a relation too (entity_a side), so neither should be flagged
    assert "orphan" not in ids, "orphan has a relation (entity_a), not truly orphaned"
    assert "connected" not in ids, "connected has a relation (entity_b)"


def test_check_orphan_entities_truly_orphaned(tmp_path):
    """Entity with email_count=0 and genuinely no relations is flagged."""
    s = _store(tmp_path)
    _add_entity(s, "truly-orphan", "Truly Orphan", org="Acme", email_count=0)

    with s._connect() as db:
        flagged = check_orphan_entities(db)

    ids = [r["id"] for r in flagged]
    assert "truly-orphan" in ids, "truly-orphan (no emails, no relations) should be flagged"


# ---------------------------------------------------------------------------
# Sub-task 2.1 — check_ambiguous_org
# ---------------------------------------------------------------------------

def test_check_ambiguous_org_flags(tmp_path, monkeypatch):
    """Entity with org='external', email_addr contains example.org -> flagged."""
    (tmp_path / "config.json").write_text(json.dumps({"orgs": [
        {"name": "Acme", "domains": ["example.org"]},
    ]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    _add_entity(s, "amb1", "Jane Doe", org="external",
                email_addr="jane@example.org", email_count=3)
    _add_entity(s, "amb2", "John External", org="external",
                email_addr="john@gmail.com", email_count=3)
    _add_entity(s, "amb3", "Already Tagged", org="Acme",
                email_addr="tagged@example.org", email_count=3)

    with s._connect() as db:
        flagged = check_ambiguous_org(db)

    ids = [r["id"] for r in flagged]
    assert "amb1" in ids, "amb1 (external + acme domain) should be flagged"
    assert "amb2" not in ids, "amb2 (external but unknown domain) should not be flagged"
    assert "amb3" not in ids, "amb3 (already correct org) should not be flagged"

    # Check should_be is set correctly
    amb1_row = next(r for r in flagged if r["id"] == "amb1")
    assert amb1_row["should_be"] == "Acme"




# ---------------------------------------------------------------------------
# Sub-task 2.1 — check_ownerless_actions
# ---------------------------------------------------------------------------

def test_check_ownerless_actions_flags(tmp_path):
    """actions row with owner='', source='email' -> flagged."""
    s = _store(tmp_path)
    _add_action(s, 1, "Review budget", owner="", source="email")
    _add_action(s, 2, "Send report", owner="Sam", source="email")
    _add_action(s, 3, "Manual note", owner="", source="manual")  # not email source

    with s._connect() as db:
        flagged = check_ownerless_actions(db)

    ids = [r["id"] for r in flagged]
    assert 1 in ids, "action 1 (no owner, email source) should be flagged"
    assert 2 not in ids, "action 2 (has owner) should not be flagged"
    assert 3 not in ids, "action 3 (manual source) should not be flagged"


def test_check_ownerless_actions_joins_email_context(tmp_path):
    """When thread_id is set, subject and date_iso are joined from email_context."""
    s = _store(tmp_path)
    _add_email_context(s, "msg1", thread_id="thread-abc",
                       subject="Budget Review", date_iso="2026-05-01")
    _add_action(s, 10, "Review the budget", owner="", source="email",
                thread_id="thread-abc")

    with s._connect() as db:
        flagged = check_ownerless_actions(db)

    assert len(flagged) >= 1
    row = next(r for r in flagged if r["id"] == 10)
    assert row["subject"] == "Budget Review"
    assert row["date_iso"] == "2026-05-01"


# ---------------------------------------------------------------------------
# Sub-task 2.1 — check_duplicate_orgs
# ---------------------------------------------------------------------------

def test_check_duplicate_orgs_flags(tmp_path, monkeypatch):
    """Entity with org='Acme Corp WA' -> flagged as variant (score >= 60)."""
    (tmp_path / "config.json").write_text(json.dumps({"orgs": [
        {"name": "Acme", "domains": ["example.org"]},
    ]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = _store(tmp_path)
    _add_entity(s, "e1", "Alice", org="Acme Corp WA", email_count=1)
    _add_entity(s, "e2", "Bob", org="Acme", email_count=1)  # canonical

    with s._connect() as db:
        flagged = check_duplicate_orgs(db)

    variants = [r["variant_org"] for r in flagged]
    assert "Acme Corp WA" in variants, (
        "Acme Corp WA should be flagged as a Acme variant"
    )
    assert "Acme" not in variants, "Canonical orgs should never be flagged"

    row = next(r for r in flagged if r["variant_org"] == "Acme Corp WA")
    assert row["score"] >= 60
    assert row["canonical_org"] == "Acme"


def test_check_duplicate_orgs_canonical_not_flagged(tmp_path):
    """Canonical org values are never flagged."""
    s = _store(tmp_path)
    for org in ("Acme", "ACC", "Courageous Church", "external"):
        _add_entity(s, f"e-{org.lower().replace(' ', '-')}", "Person", org=org, email_count=1)

    with s._connect() as db:
        flagged = check_duplicate_orgs(db)

    variants = [r["variant_org"] for r in flagged]
    for org in ("Acme", "ACC", "Courageous Church", "external"):
        assert org not in variants, f"{org} should not be flagged as a variant"


# ---------------------------------------------------------------------------
# Sub-task 2.2 — build_report counts findings
# ---------------------------------------------------------------------------

def test_build_report_counts_findings(tmp_path):
    """Store with two planted issues: report names both sections with non-OK counts,
    header reports total findings count."""
    s = _store(tmp_path)
    # Plant a missing-org entity
    _add_entity(s, "no-org", "No Org Person", org="", email_count=5)
    # Plant an ownerless action
    _add_action(s, 99, "Unowned task", owner="", source="email")

    with s._connect() as db:
        report = build_report(db)

    assert "total findings" in report or "No findings" in report
    # At least the missing-org section should show issues
    assert "Missing org tag" in report
    # The no-org entity should be flagged
    assert "1 issues" in report or "issues" in report


def test_build_report_ok_when_clean(tmp_path):
    """Empty store: report says 'No findings'."""
    s = _store(tmp_path)

    with s._connect() as db:
        report = build_report(db)

    assert "No findings" in report


# ---------------------------------------------------------------------------
# Sub-task 2.2 — run() records proactive findings
# ---------------------------------------------------------------------------

def test_lint_records_findings(tmp_path):
    """run(store, now) writes one proactive_findings row per entity-level finding,
    with finding_type='lint:<check_name>' and severity='info'."""
    s = _store(tmp_path)
    # Plant a missing-org entity
    _add_entity(s, "no-org-2", "No Org Person 2", org="", email_count=5)
    # Plant an ownerless action
    _add_action(s, 88, "Another unowned task", owner="", source="email")

    now = "2026-06-03T00:00:00Z"
    result = run(s, now=now, log_dir=tmp_path / "logs")

    assert result["findings"] >= 1
    assert "report_path" in result

    open_findings = s.open_findings()
    types = {f["finding_type"] for f in open_findings}
    assert any(t.startswith("lint:") for t in types), (
        f"Expected at least one lint: finding, got types: {types}"
    )

    for f in open_findings:
        if f["finding_type"].startswith("lint:"):
            assert f["severity"] == "info"
            assert f["ref_id"] != ""


def test_lint_resolves_stale_findings(tmp_path):
    """run() resolves prior lint findings no longer present."""
    s = _store(tmp_path)
    # First run: plant entity with missing org
    _add_entity(s, "fix-me", "Fix Me", org="", email_count=5)

    now1 = "2026-06-03T00:00:00Z"
    run(s, now=now1, log_dir=tmp_path / "logs")

    # Verify finding was recorded
    open1 = s.open_findings(finding_type="lint:missing_org")
    assert any(f["ref_id"] == "fix-me" for f in open1), "fix-me should have a lint finding"

    # Fix the entity (give it an org)
    with s._connect() as db:
        db.execute("UPDATE entities SET org='Acme' WHERE id='fix-me'")

    # Second run: fix-me is no longer flagged — finding should be resolved
    now2 = "2026-06-03T01:00:00Z"
    run(s, now=now2, log_dir=tmp_path / "logs")

    open2 = s.open_findings(finding_type="lint:missing_org")
    assert not any(f["ref_id"] == "fix-me" for f in open2), (
        "fix-me finding should be resolved after entity got an org"
    )


def test_lint_report_written_to_log_dir(tmp_path):
    """run() writes lint_YYYY-MM-DD.md to log_dir."""
    s = _store(tmp_path)
    log_dir = tmp_path / "custom_logs"
    now = "2026-06-03T12:00:00Z"

    result = run(s, now=now, log_dir=log_dir)

    assert log_dir.exists()
    report_path = log_dir / "lint_2026-06-03.md"
    assert report_path.exists(), f"Expected {report_path} to exist"
    assert result["report_path"] == str(report_path)
    content = report_path.read_text()
    assert "Knowledge Graph Lint" in content


# ---------------------------------------------------------------------------
# Task A5: Verify deleted checks are not importable
# ---------------------------------------------------------------------------

def test_deleted_lint_checks_not_importable():
    """Verify that the three deleted lint checks are no longer present."""
    from mcpbrain import lint_graph
    assert not hasattr(lint_graph, "check_possible_duplicates")
    assert not hasattr(lint_graph, "check_community_singletons")
    assert not hasattr(lint_graph, "check_threads_without_summary")
