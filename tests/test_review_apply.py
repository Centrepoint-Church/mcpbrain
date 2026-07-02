import json

from mcpbrain.store import Store
from mcpbrain.review_apply import apply_orphan_verdicts, apply_missing_org_verdicts


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
    assert result == {"assigned": 1, "external": 0, "skipped": 0, "capped": 0}


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
    assert result == {"assigned": 0, "external": 0, "skipped": 1, "capped": 0}


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
    assert result == {"assigned": 0, "external": 1, "skipped": 1, "capped": 0}


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
    assert result == {"assigned": 0, "external": 0, "skipped": 1, "capped": 0}


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
