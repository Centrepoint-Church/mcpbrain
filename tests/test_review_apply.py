from mcpbrain.store import Store
from mcpbrain.review_apply import apply_orphan_verdicts


def _seed(tmp_path):
    s = Store(str(tmp_path / "b.sqlite3"), dim=4)
    s.init()
    s.upsert_entity("e1", "Junk Entity", "person", org="", seen="2026-05-30")
    s.upsert_entity("e2", "Real Person", "person", org="Acme", seen="2026-05-30")
    s.upsert_entity("e3", "Mystery Entity", "person", org="", seen="2026-05-30")
    return s


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
