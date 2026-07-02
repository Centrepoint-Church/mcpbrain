import json

from mcpbrain.store import Store
from mcpbrain.review_apply import (
    apply_orphan_verdicts,
    apply_missing_org_verdicts,
    apply_ownerless_verdicts,
    apply_org_verdicts,
    apply_duplicate_verdicts,
)


def _seed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    s.upsert_entity("e1", "Junk Entity", "person", org="", seen="2026-05-30")
    s.upsert_entity("e2", "Real Person", "person", org="Acme", seen="2026-05-30")
    s.upsert_entity("e3", "Mystery Entity", "person", org="", seen="2026-05-30")
    return s


def _write_config(tmp_path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


ACME_CFG = {"orgs": [{"name": "Acme", "domains": ["acme.com"]}, {"name": "Personal"}]}


def test_suppress_verdict_suppresses_entity_and_resolves_finding(tmp_path):
    s = _seed(tmp_path)
    fid = s.record_finding("lint:orphan_entity", "e1", summary="orphan")
    finding = s.open_findings("lint:orphan_entity")[0]

    result = apply_orphan_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": "e1", "verdict": "suppress", "reason": "extraction noise"}],
        cap=50,
    )

    with s._connect() as db:
        row = db.execute("SELECT * FROM entity_suppressions WHERE entity_id='e1'").fetchone()
    assert row is not None
    assert row["reason"] == "extraction noise"
    assert s.open_findings("lint:orphan_entity") == []  # finding resolved
    assert result == {"suppressed": 1, "kept": 0, "skipped": 0, "capped": 0, "missing": 0}


def test_unsuppress_entity_makes_suppression_recoverable(tmp_path):
    s = _seed(tmp_path)
    finding_id = s.record_finding("lint:orphan_entity", "e1", summary="orphan")
    finding = s.open_findings("lint:orphan_entity")[0]

    apply_orphan_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": "e1", "verdict": "suppress"}], cap=50
    )
    assert s.unsuppress_entity("e1") is True

    with s._connect() as db:
        row = db.execute("SELECT * FROM entity_suppressions WHERE entity_id='e1'").fetchone()
    assert row is None
    # entities row was never touched by suppression, so it's still present.
    assert "e1" in {e["id"] for e in s.list_entities()}


def test_keep_verdict_resolves_finding_without_suppressing(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e2", summary="probably fine")
    finding = s.open_findings("lint:orphan_entity")[0]

    result = apply_orphan_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": "e2", "verdict": "keep"}], cap=50
    )

    with s._connect() as db:
        row = db.execute("SELECT * FROM entity_suppressions WHERE entity_id='e2'").fetchone()
    assert row is None
    assert s.open_findings("lint:orphan_entity") == []
    assert result == {"suppressed": 0, "kept": 1, "skipped": 0, "capped": 0, "missing": 0}


def test_unrecognised_verdict_treated_as_skip(tmp_path):
    s = _seed(tmp_path)
    s.record_finding("lint:orphan_entity", "e3", summary="unclear")
    finding = s.open_findings("lint:orphan_entity")[0]

    result = apply_orphan_verdicts(
        s, [{"finding_id": finding["id"], "ref_id": "e3", "verdict": "maybe???"}], cap=50
    )

    with s._connect() as db:
        row = db.execute("SELECT * FROM entity_suppressions WHERE entity_id='e3'").fetchone()
    assert row is None
    assert s.open_findings("lint:orphan_entity") == []  # still resolved, just no mutation
    assert result == {"suppressed": 0, "kept": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_cap_stops_applying_suppressions(tmp_path):
    s = _seed(tmp_path)
    fid1 = s.record_finding("lint:orphan_entity", "e1", summary="orphan 1")
    fid2 = s.record_finding("lint:orphan_entity", "e3", summary="orphan 2")
    findings = {f["ref_id"]: f["id"] for f in s.open_findings("lint:orphan_entity")}

    verdicts = [
        {"finding_id": findings["e1"], "ref_id": "e1", "verdict": "suppress"},
        {"finding_id": findings["e3"], "ref_id": "e3", "verdict": "suppress"},
    ]
    result = apply_orphan_verdicts(s, verdicts, cap=1)

    assert result["suppressed"] == 1
    assert result["capped"] == 1
    with s._connect() as db:
        rows = db.execute("SELECT entity_id FROM entity_suppressions").fetchall()
    assert len(rows) == 1
    # The capped finding is left open (not resolved) so it's picked up next run.
    assert len(s.open_findings("lint:orphan_entity")) == 1


def test_suppress_verdict_with_missing_entity_leaves_finding_open(tmp_path):
    """ref_id no longer names a real row in entities (e.g. merged/renamed away
    between when the finding was detected and when the verdict was applied).
    suppress_entity returns False; this must NOT be counted as a successful
    suppression, must NOT resolve the finding, and must NOT write a row to
    entity_suppressions. It's tallied under "missing" instead."""
    s = _seed(tmp_path)
    fid = s.record_finding("lint:orphan_entity", "e_ghost", summary="orphan")
    finding = s.open_findings("lint:orphan_entity")[0]

    result = apply_orphan_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": "e_ghost", "verdict": "suppress", "reason": "stale"}],
        cap=50,
    )

    assert result == {"suppressed": 0, "kept": 0, "skipped": 0, "capped": 0, "missing": 1}
    with s._connect() as db:
        row = db.execute("SELECT * FROM entity_suppressions WHERE entity_id='e_ghost'").fetchone()
    assert row is None
    # Finding left open — will be picked up again on a future pass.
    open_ids = {f["id"] for f in s.open_findings("lint:orphan_entity")}
    assert finding["id"] in open_ids


# --- Task 2.2: apply_missing_org_verdicts ---------------------------------


def _seed_missing_org(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    s.upsert_entity("e1", "Vendor Co", "org", org="", seen="2026-05-30")
    s.upsert_entity("e2", "Some Contact", "person", org="", seen="2026-05-30")
    s.upsert_entity("e3", "Another Contact", "person", org="", seen="2026-05-30")
    s.upsert_entity("e4", "Fourth Contact", "person", org="", seen="2026-05-30")
    return s


def test_assign_with_valid_taxonomy_org_sets_org_and_resolves(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_missing_org(tmp_path)
    fid = s.record_finding("lint:missing_org", "e1", summary="no org")
    finding = s.open_findings("lint:missing_org")[0]

    result = apply_missing_org_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": "e1", "verdict": "assign", "org": "Acme"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        row = db.execute("SELECT org FROM entities WHERE id='e1'").fetchone()
    assert row["org"] == "Acme"
    assert s.open_findings("lint:missing_org") == []
    assert result == {"assigned": 1, "external": 0, "skipped": 0, "capped": 0, "missing": 0}


def test_assign_with_org_not_in_taxonomy_does_not_apply(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_missing_org(tmp_path)
    fid = s.record_finding("lint:missing_org", "e2", summary="no org")
    finding = s.open_findings("lint:missing_org")[0]

    result = apply_missing_org_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": "e2", "verdict": "assign", "org": "MadeUpOrg"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        row = db.execute("SELECT org FROM entities WHERE id='e2'").fetchone()
    assert row["org"] == ""  # not silently applied
    # Not counted as a success — an invalid-org "assign" must not masquerade as one.
    assert result == {"assigned": 0, "external": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_external_and_skip_resolve_without_org_change(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_missing_org(tmp_path)
    fid_a = s.record_finding("lint:missing_org", "e2", summary="no org")
    fid_b = s.record_finding("lint:missing_org", "e3", summary="no org")
    findings = {f["ref_id"]: f["id"] for f in s.open_findings("lint:missing_org")}

    result = apply_missing_org_verdicts(
        s,
        [
            {"finding_id": findings["e2"], "ref_id": "e2", "verdict": "external"},
            {"finding_id": findings["e3"], "ref_id": "e3", "verdict": "skip"},
        ],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        rows = db.execute("SELECT id, org FROM entities WHERE id IN ('e2','e3')").fetchall()
    assert {r["org"] for r in rows} == {""}
    assert s.open_findings("lint:missing_org") == []
    assert result == {"assigned": 0, "external": 1, "skipped": 1, "capped": 0, "missing": 0}


def test_unrecognised_verdict_treated_as_skip_missing_org(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_missing_org(tmp_path)
    fid = s.record_finding("lint:missing_org", "e2", summary="no org")
    finding = s.open_findings("lint:missing_org")[0]

    result = apply_missing_org_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": "e2", "verdict": "maybe???"}],
        cap=50,
        home=home,
    )

    assert s.open_findings("lint:missing_org") == []
    assert result == {"assigned": 0, "external": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_cap_stops_applying_assign_verdicts(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_missing_org(tmp_path)
    fid1 = s.record_finding("lint:missing_org", "e1", summary="no org")
    fid2 = s.record_finding("lint:missing_org", "e4", summary="no org")
    findings = {f["ref_id"]: f["id"] for f in s.open_findings("lint:missing_org")}

    verdicts = [
        {"finding_id": findings["e1"], "ref_id": "e1", "verdict": "assign", "org": "Acme"},
        {"finding_id": findings["e4"], "ref_id": "e4", "verdict": "assign", "org": "Personal"},
    ]
    result = apply_missing_org_verdicts(s, verdicts, cap=1, home=home)

    assert result["assigned"] == 1
    assert result["capped"] == 1
    with s._connect() as db:
        rows = db.execute("SELECT id, org FROM entities WHERE org!=''").fetchall()
    assert len(rows) == 1
    # The capped finding is left open (not resolved) so it's picked up next run.
    assert len(s.open_findings("lint:missing_org")) == 1


def test_assign_verdict_with_missing_entity_leaves_finding_open(tmp_path):
    """ref_id no longer names a real row in entities (e.g. merged/renamed away
    between when the finding was detected and when the verdict was applied).
    store.update_entity_org returns False; this must NOT be counted as a
    successful assignment, must NOT resolve the finding, and must NOT create
    or touch any entity row. It's tallied under "missing" instead."""
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_missing_org(tmp_path)
    fid = s.record_finding("lint:missing_org", "e_ghost", summary="no org")
    finding = s.open_findings("lint:missing_org")[0]

    result = apply_missing_org_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": "e_ghost", "verdict": "assign", "org": "Acme"}],
        cap=50,
        home=home,
    )

    assert result == {"assigned": 0, "external": 0, "skipped": 0, "capped": 0, "missing": 1}
    with s._connect() as db:
        row = db.execute("SELECT * FROM entities WHERE id='e_ghost'").fetchone()
    assert row is None
    # Finding left open — will be picked up again on a future pass.
    open_ids = {f["id"] for f in s.open_findings("lint:missing_org")}
    assert finding["id"] in open_ids


# --- Task 3.1: apply_ownerless_verdicts -----------------------------------


def _seed_ownerless(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    action_ids = {}
    with s._connect() as db:
        for key, text in (("a1", "Send the budget"), ("a2", "Follow up with vendor")):
            cur = db.execute(
                "INSERT INTO actions(text, owner, status, source) VALUES(?, '', 'open', 'email')",
                (text,))
            action_ids[key] = cur.lastrowid
    return s, action_ids


def test_owner_verdict_with_valid_action_sets_owner_and_resolves(tmp_path):
    s, action_ids = _seed_ownerless(tmp_path)
    fid = s.record_finding("lint:ownerless_action", str(action_ids["a1"]), summary="ownerless")
    finding = s.open_findings("lint:ownerless_action")[0]

    result = apply_ownerless_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": action_ids["a1"], "verdict": "owner",
          "owner": "Alice Admin", "owner_entity_id": "e1"}],
        cap=50,
    )

    with s._connect() as db:
        row = db.execute("SELECT owner, owner_entity_id FROM actions WHERE id=?", (action_ids["a1"],)).fetchone()
    assert row["owner"] == "Alice Admin"
    assert row["owner_entity_id"] == "e1"
    assert s.open_findings("lint:ownerless_action") == []
    assert result == {"owner_assigned": 1, "waiting_on": 0, "unowned": 0, "skipped": 0, "capped": 0, "missing": 0}


def test_owner_verdict_with_stale_action_leaves_finding_open(tmp_path):
    s, action_ids = _seed_ownerless(tmp_path)
    fid = s.record_finding("lint:ownerless_action", "9999", summary="ownerless")
    finding = s.open_findings("lint:ownerless_action")[0]

    result = apply_ownerless_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": 9999, "verdict": "owner", "owner": "Ghost Owner"}],
        cap=50,
    )

    assert result == {"owner_assigned": 0, "waiting_on": 0, "unowned": 0, "skipped": 0, "capped": 0, "missing": 1}
    open_ids = {f["id"] for f in s.open_findings("lint:ownerless_action")}
    assert finding["id"] in open_ids


def test_waiting_on_unowned_skip_resolve_without_owner_change(tmp_path):
    s, action_ids = _seed_ownerless(tmp_path)
    fid_a = s.record_finding("lint:ownerless_action", str(action_ids["a1"]), summary="ownerless")
    fid_b = s.record_finding("lint:ownerless_action", str(action_ids["a2"]), summary="ownerless")
    findings = {f["ref_id"]: f["id"] for f in s.open_findings("lint:ownerless_action")}

    result = apply_ownerless_verdicts(
        s,
        [
            {"finding_id": findings[str(action_ids["a1"])], "ref_id": action_ids["a1"], "verdict": "waiting_on"},
            {"finding_id": findings[str(action_ids["a2"])], "ref_id": action_ids["a2"], "verdict": "unowned"},
        ],
        cap=50,
    )

    with s._connect() as db:
        rows = db.execute("SELECT owner FROM actions WHERE id IN (?,?)", (action_ids["a1"], action_ids["a2"])).fetchall()
    assert {r["owner"] for r in rows} == {""}
    assert s.open_findings("lint:ownerless_action") == []
    assert result == {"owner_assigned": 0, "waiting_on": 1, "unowned": 1, "skipped": 0, "capped": 0, "missing": 0}


def test_unrecognised_verdict_treated_as_skip_ownerless(tmp_path):
    s, action_ids = _seed_ownerless(tmp_path)
    fid = s.record_finding("lint:ownerless_action", str(action_ids["a1"]), summary="ownerless")
    finding = s.open_findings("lint:ownerless_action")[0]

    result = apply_ownerless_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": action_ids["a1"], "verdict": "maybe???"}],
        cap=50,
    )

    with s._connect() as db:
        row = db.execute("SELECT owner FROM actions WHERE id=?", (action_ids["a1"],)).fetchone()
    assert row["owner"] == ""
    assert s.open_findings("lint:ownerless_action") == []
    assert result == {"owner_assigned": 0, "waiting_on": 0, "unowned": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_owner_verdict_missing_owner_field_treated_as_skip(tmp_path):
    s, action_ids = _seed_ownerless(tmp_path)
    fid = s.record_finding("lint:ownerless_action", str(action_ids["a1"]), summary="ownerless")
    finding = s.open_findings("lint:ownerless_action")[0]

    result = apply_ownerless_verdicts(
        s,
        [{"finding_id": finding["id"], "ref_id": action_ids["a1"], "verdict": "owner"}],
        cap=50,
    )

    with s._connect() as db:
        row = db.execute("SELECT owner FROM actions WHERE id=?", (action_ids["a1"],)).fetchone()
    assert row["owner"] == ""
    assert s.open_findings("lint:ownerless_action") == []
    assert result == {"owner_assigned": 0, "waiting_on": 0, "unowned": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_cap_stops_applying_owner_verdicts(tmp_path):
    s, action_ids = _seed_ownerless(tmp_path)
    fid1 = s.record_finding("lint:ownerless_action", str(action_ids["a1"]), summary="ownerless")
    fid2 = s.record_finding("lint:ownerless_action", str(action_ids["a2"]), summary="ownerless")
    findings = {f["ref_id"]: f["id"] for f in s.open_findings("lint:ownerless_action")}

    verdicts = [
        {"finding_id": findings[str(action_ids["a1"])], "ref_id": action_ids["a1"], "verdict": "owner", "owner": "Alice"},
        {"finding_id": findings[str(action_ids["a2"])], "ref_id": action_ids["a2"], "verdict": "owner", "owner": "Bob"},
    ]
    result = apply_ownerless_verdicts(s, verdicts, cap=1)

    assert result["owner_assigned"] == 1
    assert result["capped"] == 1
    with s._connect() as db:
        rows = db.execute("SELECT id FROM actions WHERE owner!=''").fetchall()
    assert len(rows) == 1
    # The capped finding is left open (not resolved) so it's picked up next run.
    assert len(s.open_findings("lint:ownerless_action")) == 1


# --- Task 3.2: apply_org_verdicts -----------------------------------------
#
# Three bundled finding kinds with different ref_id semantics:
#   lint:ambiguous_org  -> ref_id is a real entity id
#   lint:duplicate_org  -> ref_id is the variant ORG STRING itself
#   org_unrecognised    -> ref_id is the raw unrecognised org string


def _seed_org(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    # entity tagged 'external' whose domain actually maps to a configured org
    s.upsert_entity("e1", "External Contact", "person", org="external", seen="2026-05-30")
    s.upsert_entity("e2", "Second External", "person", org="external", seen="2026-05-30")
    return s


def test_ambiguous_org_canonicalize_updates_entity_org_and_resolves(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:ambiguous_org", "e1", summary="ambiguous org",
                      detail="{'should_be': 'Acme'}")
    finding = s.open_findings("lint:ambiguous_org")[0]

    result = apply_org_verdicts(
        s,
        [{"finding_id": finding["id"], "finding_type": "lint:ambiguous_org",
          "ref_id": "e1", "verdict": "canonicalize", "canonical_org": "Acme"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        row = db.execute("SELECT org FROM entities WHERE id='e1'").fetchone()
    assert row["org"] == "Acme"
    assert s.open_findings("lint:ambiguous_org") == []
    assert result == {"canonicalized": 1, "suggested": 0, "skipped": 0, "capped": 0, "missing": 0}


def test_ambiguous_org_canonicalize_with_invalid_org_treated_as_skip(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:ambiguous_org", "e1", summary="ambiguous org")
    finding = s.open_findings("lint:ambiguous_org")[0]

    result = apply_org_verdicts(
        s,
        [{"finding_id": finding["id"], "finding_type": "lint:ambiguous_org",
          "ref_id": "e1", "verdict": "canonicalize", "canonical_org": "MadeUpOrg"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        row = db.execute("SELECT org FROM entities WHERE id='e1'").fetchone()
    assert row["org"] == "external"  # not silently applied
    assert s.open_findings("lint:ambiguous_org") == []
    assert result == {"canonicalized": 0, "suggested": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_duplicate_org_canonicalize_rewrites_org_field(tmp_path):
    """canonicalize on lint:duplicate_org is a bulk text-field rewrite: every
    entity tagged org=<variant> gets relabeled org=<canonical>. No entity is
    merged or deleted."""
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    # Regular entities tagged with the variant org string — this is what
    # check_duplicate_orgs actually flags (the .org text field, not a
    # dedicated org-typed node).
    s.upsert_entity("p1", "Person One", "person", org="Acme Corp", seen="2026-05-30")
    s.upsert_entity("p2", "Person Two", "person", org="Acme Corp", seen="2026-05-30")
    s.record_finding("lint:duplicate_org", "Acme Corp", summary="likely duplicate",
                      detail="{'canonical_org': 'Acme', 'entity_count': 2, 'score': 90}")
    finding = s.open_findings("lint:duplicate_org")[0]

    result = apply_org_verdicts(
        s,
        [{"finding_id": finding["id"], "finding_type": "lint:duplicate_org",
          "ref_id": "Acme Corp", "verdict": "canonicalize", "canonical_org": "Acme"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        rows = db.execute(
            "SELECT id, org FROM entities WHERE id IN ('p1','p2')").fetchall()
        merge_log = db.execute("SELECT * FROM entity_merge_log").fetchall()
    assert len(rows) == 2
    assert all(r["org"] == "Acme" for r in rows)
    assert merge_log == []  # nothing was merged — only the org field changed
    assert s.open_findings("lint:duplicate_org") == []
    assert result == {"canonicalized": 1, "suggested": 0, "skipped": 0, "capped": 0, "missing": 0}


def test_duplicate_org_canonicalize_no_matching_entities_leaves_open(tmp_path):
    """A stale finding: no entity currently carries org=<variant> (e.g. it
    was already fixed or the finding predates other changes). rewrite_org_field
    returns 0 rows updated, so the finding is left open and tallied 'missing',
    with zero mutation to the entities table."""
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:duplicate_org", "Ghost Corp", summary="likely duplicate",
                      detail="{'canonical_org': 'Acme'}")
    finding = s.open_findings("lint:duplicate_org")[0]

    result = apply_org_verdicts(
        s,
        [{"finding_id": finding["id"], "finding_type": "lint:duplicate_org",
          "ref_id": "Ghost Corp", "verdict": "canonicalize", "canonical_org": "Acme"}],
        cap=50,
        home=home,
    )

    assert result == {"canonicalized": 0, "suggested": 0, "skipped": 0, "capped": 0, "missing": 1}
    with s._connect() as db:
        merge_log = db.execute("SELECT * FROM entity_merge_log").fetchall()
        orgs_seen = {r["org"] for r in db.execute("SELECT org FROM entities").fetchall()}
    assert merge_log == []  # nothing was merged
    assert orgs_seen == {"external"}  # no entities table changes
    open_ids = {f["id"] for f in s.open_findings("lint:duplicate_org")}
    assert finding["id"] in open_ids


def test_org_unrecognised_add_to_config_records_suggestion(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("org_unrecognised", "widgetco llc", summary="unconfigured org")
    finding = s.open_findings("org_unrecognised")[0]
    config_path = f"{home}/config.json"
    with open(config_path) as f:
        before = f.read()

    result = apply_org_verdicts(
        s,
        [{"finding_id": finding["id"], "finding_type": "org_unrecognised",
          "ref_id": "widgetco llc", "verdict": "add_to_config"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        row = db.execute("SELECT * FROM org_suggestions WHERE raw_org='widgetco llc'").fetchone()
    assert row is not None
    assert s.open_findings("org_unrecognised") == []
    with open(config_path) as f:
        after = f.read()
    assert after == before  # config.json is never auto-written
    assert result == {"canonicalized": 0, "suggested": 1, "skipped": 0, "capped": 0, "missing": 0}


def test_org_skip_resolves_without_mutation_for_each_kind(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:ambiguous_org", "e1", summary="ambiguous")
    s.record_finding("lint:duplicate_org", "Acme Corp", summary="dup")
    s.record_finding("org_unrecognised", "widgetco llc", summary="unrecognised")
    findings = {
        "lint:ambiguous_org": s.open_findings("lint:ambiguous_org")[0]["id"],
        "lint:duplicate_org": s.open_findings("lint:duplicate_org")[0]["id"],
        "org_unrecognised": s.open_findings("org_unrecognised")[0]["id"],
    }

    verdicts = [
        {"finding_id": findings["lint:ambiguous_org"], "finding_type": "lint:ambiguous_org",
         "ref_id": "e1", "verdict": "skip"},
        {"finding_id": findings["lint:duplicate_org"], "finding_type": "lint:duplicate_org",
         "ref_id": "Acme Corp", "verdict": "skip"},
        {"finding_id": findings["org_unrecognised"], "finding_type": "org_unrecognised",
         "ref_id": "widgetco llc", "verdict": "skip"},
    ]
    result = apply_org_verdicts(s, verdicts, cap=50, home=home)

    with s._connect() as db:
        row = db.execute("SELECT org FROM entities WHERE id='e1'").fetchone()
        merge_log = db.execute("SELECT * FROM entity_merge_log").fetchall()
        suggestions = db.execute("SELECT * FROM org_suggestions").fetchall()
    assert row["org"] == "external"
    assert merge_log == []
    assert suggestions == []
    assert s.open_findings("lint:ambiguous_org") == []
    assert s.open_findings("lint:duplicate_org") == []
    assert s.open_findings("org_unrecognised") == []
    assert result == {"canonicalized": 0, "suggested": 0, "skipped": 3, "capped": 0, "missing": 0}


def test_unrecognised_verdict_treated_as_skip_org(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:ambiguous_org", "e1", summary="ambiguous")
    finding = s.open_findings("lint:ambiguous_org")[0]

    result = apply_org_verdicts(
        s,
        [{"finding_id": finding["id"], "finding_type": "lint:ambiguous_org",
          "ref_id": "e1", "verdict": "maybe???"}],
        cap=50,
        home=home,
    )

    with s._connect() as db:
        row = db.execute("SELECT org FROM entities WHERE id='e1'").fetchone()
    assert row["org"] == "external"
    assert s.open_findings("lint:ambiguous_org") == []
    assert result == {"canonicalized": 0, "suggested": 0, "skipped": 1, "capped": 0, "missing": 0}


def test_cap_stops_applying_org_canonicalize_verdicts(tmp_path):
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:ambiguous_org", "e1", summary="ambiguous 1")
    s.record_finding("lint:ambiguous_org", "e2", summary="ambiguous 2")
    findings = {f["ref_id"]: f["id"] for f in s.open_findings("lint:ambiguous_org")}

    verdicts = [
        {"finding_id": findings["e1"], "finding_type": "lint:ambiguous_org",
         "ref_id": "e1", "verdict": "canonicalize", "canonical_org": "Acme"},
        {"finding_id": findings["e2"], "finding_type": "lint:ambiguous_org",
         "ref_id": "e2", "verdict": "canonicalize", "canonical_org": "Acme"},
    ]
    result = apply_org_verdicts(s, verdicts, cap=1, home=home)

    assert result["canonicalized"] == 1
    assert result["capped"] == 1
    with s._connect() as db:
        rows = db.execute("SELECT id FROM entities WHERE org='Acme' AND id IN ('e1','e2')").fetchall()
    assert len(rows) == 1
    # The capped finding is left open (not resolved) so it's picked up next run.
    assert len(s.open_findings("lint:ambiguous_org")) == 1


def test_cap_is_shared_across_canonicalize_and_add_to_config(tmp_path):
    """The cap is one shared mutation budget across all three org-hygiene
    kinds bundled into this applier (matches the single `capped` counter in
    the return shape and the single `cap=50` passed to the whole review_org
    block in drain.py's BLOCK_DRAINERS registration)."""
    home = _write_config(tmp_path, ACME_CFG)
    s = _seed_org(tmp_path)
    s.record_finding("lint:ambiguous_org", "e1", summary="ambiguous")
    s.record_finding("org_unrecognised", "widgetco llc", summary="unrecognised")
    findings = {
        "lint:ambiguous_org": s.open_findings("lint:ambiguous_org")[0]["id"],
        "org_unrecognised": s.open_findings("org_unrecognised")[0]["id"],
    }

    verdicts = [
        {"finding_id": findings["lint:ambiguous_org"], "finding_type": "lint:ambiguous_org",
         "ref_id": "e1", "verdict": "canonicalize", "canonical_org": "Acme"},
        {"finding_id": findings["org_unrecognised"], "finding_type": "org_unrecognised",
         "ref_id": "widgetco llc", "verdict": "add_to_config"},
    ]
    result = apply_org_verdicts(s, verdicts, cap=1, home=home)

    assert result["canonicalized"] == 1
    assert result["suggested"] == 0
    assert result["capped"] == 1
    with s._connect() as db:
        suggestions = db.execute("SELECT * FROM org_suggestions").fetchall()
    assert suggestions == []
    assert len(s.open_findings("org_unrecognised")) == 1


# --- apply_duplicate_verdicts (merge_review hardening) ----------------------
#
# This tier is the LLM-adjudicated entity-merge applier ported from drain.
# _apply_merge_answers, hardened with the type/role-address guards the other
# review-adjudication appliers above already have (see review_apply.py's
# apply_duplicate_verdicts docstring for the safety rationale).


def _pair_id(a_id, b_id):
    return "|".join(sorted((a_id, b_id)))


def _set_email(store, entity_id, email_addr):
    with store._connect() as db:
        db.execute("UPDATE entities SET email_addr=? WHERE id=?", (email_addr, entity_id))


def _seed_dupes(tmp_path):
    s = Store(str(tmp_path / "dupes.sqlite3"), dim=4)
    s.init()
    return s


def test_duplicate_verdict_merges_mergeable_type_with_normal_emails(tmp_path):
    s = _seed_dupes(tmp_path)
    s.upsert_entity("joel-chelliah", "Joel Chelliah", "person", "Acme", "2026-04-01")
    s.upsert_entity("joel-chelliah", "Joel Chelliah", "person", "Acme", "2026-04-02")  # mentions=2
    s.upsert_entity("j-chelliah", "J Chelliah", "person", "Acme", "2026-04-01")  # mentions=1
    _set_email(s, "joel-chelliah", "joel.chelliah@acme.com")
    _set_email(s, "j-chelliah", "j.chelliah@acme.com")

    ans = {"pair_id": _pair_id("joel-chelliah", "j-chelliah"),
           "same": True, "canonical": "Joel Chelliah"}
    result = apply_duplicate_verdicts(s, [ans], cap=50)

    assert result == {"merged": 1, "guarded": 0, "capped": 0, "skipped": 0}
    assert s.get_entity("j-chelliah") is None
    assert s.get_entity("joel-chelliah") is not None
    merges = s.list_entity_merges()
    assert len(merges) == 1
    assert merges[0]["winner_id"] == "joel-chelliah"
    assert merges[0]["loser_id"] == "j-chelliah"
    assert merges[0]["method"] == "llm"


def test_duplicate_verdict_on_non_mergeable_type_is_guarded(tmp_path):
    s = _seed_dupes(tmp_path)
    s.upsert_entity("doc-a", "Untitled document", "document", "", "2026-04-01")
    s.upsert_entity("doc-b", "Untitled document", "document", "", "2026-04-01")

    ans = {"pair_id": _pair_id("doc-a", "doc-b"), "same": True, "canonical": "Untitled document"}
    result = apply_duplicate_verdicts(s, [ans], cap=50)

    assert result == {"merged": 0, "guarded": 1, "capped": 0, "skipped": 0}
    assert s.get_entity("doc-a") is not None
    assert s.get_entity("doc-b") is not None
    assert s.list_entity_merges() == []


def test_duplicate_verdict_with_role_address_is_guarded(tmp_path):
    s = _seed_dupes(tmp_path)
    s.upsert_entity("staffer-a", "Alex Staffer", "person", "Acme", "2026-04-01")
    s.upsert_entity("staffer-b", "Sam Staffer", "person", "Acme", "2026-04-01")
    _set_email(s, "staffer-a", "office@centrepoint.church")
    _set_email(s, "staffer-b", "sam.staffer@acme.com")

    ans = {"pair_id": _pair_id("staffer-a", "staffer-b"), "same": True, "canonical": "Staffer"}
    result = apply_duplicate_verdicts(s, [ans], cap=50)

    assert result == {"merged": 0, "guarded": 1, "capped": 0, "skipped": 0}
    assert s.get_entity("staffer-a") is not None
    assert s.get_entity("staffer-b") is not None
    assert s.list_entity_merges() == []


def test_duplicate_verdict_cap_stops_applying_merges(tmp_path):
    s = _seed_dupes(tmp_path)
    s.upsert_entity("a1", "Alpha One", "person", "Acme", "2026-04-01")
    s.upsert_entity("a2", "Alpha Two", "person", "Acme", "2026-04-01")
    s.upsert_entity("b1", "Beta One", "person", "Acme", "2026-04-01")
    s.upsert_entity("b2", "Beta Two", "person", "Acme", "2026-04-01")

    answers = [
        {"pair_id": _pair_id("a1", "a2"), "same": True, "canonical": "Alpha"},
        {"pair_id": _pair_id("b1", "b2"), "same": True, "canonical": "Beta"},
    ]
    result = apply_duplicate_verdicts(s, answers, cap=1)

    assert result == {"merged": 1, "guarded": 0, "capped": 1, "skipped": 0}
    merges = s.list_entity_merges()
    assert len(merges) == 1
    # whichever pair merged, the other pair's two entities both still exist
    # untouched (capped means untouched, not consumed).
    merged_ids = {merges[0]["winner_id"], merges[0]["loser_id"]}
    other_pair = {"b1", "b2"} if merged_ids == {"a1", "a2"} else {"a1", "a2"}
    for eid in other_pair:
        assert s.get_entity(eid) is not None


def test_duplicate_verdict_preexisting_skip_reasons_still_handled(tmp_path):
    s = _seed_dupes(tmp_path)
    s.upsert_entity("only-one", "Only One", "person", "Acme", "2026-04-01")

    answers = [
        {"pair_id": "malformed", "same": True, "canonical": ""},  # bad pair_id
        {"pair_id": _pair_id("ghost-1", "ghost-2"), "same": True, "canonical": ""},  # missing entity
        {"pair_id": _pair_id("only-one", "only-one"), "same": "true", "canonical": ""},  # non-bool same
    ]
    result = apply_duplicate_verdicts(s, answers, cap=50)

    assert result == {"merged": 0, "guarded": 0, "capped": 0, "skipped": 3}
    assert s.get_entity("only-one") is not None
    assert s.list_entity_merges() == []
