"""Final-review fixes: corrections survive the ACTUAL automated writers.

Every test here drives the real writer (resolve, graph_write, profile_audit,
org_import, org_curate, graph_cleanup, review_apply) against a real Store --
not only the Store setters, which is the seam earlier tests exercised.
"""
from mcpbrain import graph_corrections as gc
from mcpbrain import orgs
from mcpbrain.store import Store

_TAX = orgs.OrgTaxonomy(names=("Northgate Trust", "Southbank Community Trust",
                               "The Lantern Co"),
                        aliases={"northgate trust inc": "Northgate Trust"})


def _store(tmp_path):
    s = Store(tmp_path / "f.sqlite3", dim=4)
    s.init()
    return s


def _ent(s, eid, name, type_="person", *, org="", email="", mentions=1, origin="local",
         aliases=""):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type,org,email_addr,mentions,origin,aliases) "
                   "VALUES(?,?,?,?,?,?,?,?)", (eid, name, type_, org, email, mentions, origin,
                                                aliases))


def _distinct(s, a, b):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_distinct_pairs(a,b) VALUES(?,?)", tuple(sorted((a, b))))


def _lock(s, eid, field):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_field_locks(entity_id,field) VALUES(?,?)", (eid, field))


def _ids(s):
    with s._connect() as db:
        return {r[0] for r in db.execute("SELECT id FROM entities")}


# --- C1: not_same is checked LIVE inside the resolve loops -------------------

def test_c1_deterministic_merge_respects_not_same_transitively(tmp_path):
    from mcpbrain.resolve import _deterministic_merges
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", mentions=1)
    _ent(s, "dana-b", "Dana Okafor", mentions=2)
    _ent(s, "dana-c", "Dana Okafor", mentions=9)   # survivor
    _distinct(s, "dana-a", "dana-b")
    _deterministic_merges(s)
    ids = _ids(s)
    assert "dana-c" in ids
    # A folded into C, so (A,B) became (B,C): B must NOT then merge into C.
    assert "dana-b" in ids
    assert s.is_distinct_pair("dana-b", "dana-c")


def test_c1_email_merge_respects_not_same_transitively(tmp_path):
    from mcpbrain.resolve import _email_equality_merges
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", email="dana@northgate.example", mentions=1)
    _ent(s, "dana-b", "D Okafor", email="dana@northgate.example", mentions=2)
    _ent(s, "dana-c", "Dana O", email="dana@northgate.example", mentions=9)
    _distinct(s, "dana-a", "dana-b")
    _email_equality_merges(s, home=tmp_path)
    ids = _ids(s)
    assert "dana-c" in ids and "dana-b" in ids
    assert s.is_distinct_pair("dana-b", "dana-c")


# --- C2: field locks hold against writers that do their own SQL -------------

def _locked_dana(s, *, field="org", org="Northgate Trust", ovf="2026-09-24", origin="local"):
    _ent(s, "dana-okafor", "Dana Okafor", org=org, email="dana@northgate.example",
         origin=origin)
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET org_valid_from=? WHERE id='dana-okafor'", (ovf,))
    _lock(s, "dana-okafor", field)


def test_c2_upsert_entity_newer_dated_org_leaves_locked_org(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    _locked_dana(s)
    got = gw.upsert_entity(s, name="Dana Okafor", entity_type="person",
                           org="Southbank Community Trust", valid_from="2026-12-01",
                           taxonomy=_TAX)
    assert got == "dana-okafor"
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"


def test_c2_upsert_entity_email_branch_set_org_recency_leaves_locked_org(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    _locked_dana(s)
    # A different display name routes through the email-dedup branch, i.e.
    # _set_org_recency.
    got = gw.upsert_entity(s, name="D Okafor", entity_type="person",
                           email_addr="dana@northgate.example",
                           org="Southbank Community Trust", valid_from="2026-12-01",
                           taxonomy=_TAX)
    assert got == "dana-okafor"
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"


def test_c2_set_org_recency_direct_leaves_locked_org(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    _locked_dana(s)
    with s._connect(write=True) as db:
        gw._set_org_recency(db, "dana-okafor", "The Lantern Co", "2026-12-01")
        gw._set_org_recency(db, "dana-okafor", "The Lantern Co", "")
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"


def test_c2_upsert_entity_locked_email_not_filled(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor")   # blank email, locked blank is impossible,
    _lock(s, "dana-okafor", "email")        # but a lock row must still be honoured
    gw.upsert_entity(s, name="Dana Okafor", entity_type="person",
                     email_addr="other@southbank.example", taxonomy=_TAX)
    assert (s.get_entity("dana-okafor")["email_addr"] or "") == ""


def test_c2_profile_audit_leaves_locked_org_and_counts_only_real(tmp_path):
    from mcpbrain import profile_audit
    s = _store(tmp_path)
    _locked_dana(s)
    _ent(s, "marcus-reyes", "Marcus Reyes", org="Northgate Trust")
    out = profile_audit.drain_audit(s, {"profile_audit": [
        {"entity_id": "dana-okafor",
         "corrections": [{"field": "org", "new_value": "The Lantern Co"}]},
        {"entity_id": "marcus-reyes",
         "corrections": [{"field": "org", "new_value": "The Lantern Co"}]},
    ]})
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"
    assert s.get_entity("marcus-reyes")["org"] == "The Lantern Co"
    assert out["corrections_applied"] == 1
    with s._connect() as db:
        refs = [r[0] for r in db.execute(
            "SELECT ref_id FROM change_log WHERE change_type='org_corrected'")]
    assert refs == ["marcus-reyes"]


def test_c2_org_import_preserves_locked_columns(tmp_path):
    import gzip
    import hashlib
    import json
    from mcpbrain import org_import
    from mcpbrain.org_contracts import SnapshotManifest
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor", org="Northgate Trust",
         email="dana@northgate.example", origin="org")
    _lock(s, "dana-okafor", "org")
    _lock(s, "dana-okafor", "name")
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    ent = {"kind": "entity", "id": "dana-okafor", "name": "Dana A. Okafor", "type": "person",
           "org": "The Lantern Co", "email_addr": "dana@lantern.example", "aliases": ""}
    gz = gzip.compress((json.dumps(ent, sort_keys=True) + "\n").encode())
    man = SnapshotManifest(version=1, created_at="t", entity_count=1, relation_count=0,
                           tombstone_count=0, snapshot_sha256=hashlib.sha256(gz).hexdigest())
    fs.put_bytes("org-graph/snapshot.jsonl.gz", gz)
    fs.put_bytes("org-graph/tombstones.jsonl", b"")
    fs.put_bytes("org-graph/manifest.json", json.dumps(man.to_dict(), sort_keys=True).encode())
    assert org_import.import_snapshot(s, fs)["status"] == "imported"
    e = s.get_entity("dana-okafor")
    assert e["name"] == "Dana Okafor" and e["org"] == "Northgate Trust"
    assert e["email_addr"] == "dana@lantern.example"      # not locked: import wins


def test_c2_org_curate_skeleton_leaves_locked_fields(tmp_path):
    from mcpbrain import org_curate
    s = _store(tmp_path)
    _locked_dana(s)
    _lock(s, "dana-okafor", "email")
    org_curate._apply_org_skeleton(s, "dana-okafor", {"org": "The Lantern Co",
                                                     "email_addr": "d@lantern.example"})
    e = s.get_entity("dana-okafor")
    assert e["org"] == "Northgate Trust" and e["email_addr"] == "dana@northgate.example"


def test_c2_org_curate_skeleton_writes_unlocked_field_beside_locked(tmp_path):
    from mcpbrain import org_curate
    s = _store(tmp_path)
    _locked_dana(s)                                        # org locked, email not
    org_curate._apply_org_skeleton(s, "dana-okafor", {"org": "The Lantern Co",
                                                     "email_addr": "d@lantern.example"})
    e = s.get_entity("dana-okafor")
    assert e["org"] == "Northgate Trust" and e["email_addr"] == "d@lantern.example"


def test_c2_graph_cleanup_fold_leaves_locked_org(tmp_path):
    from mcpbrain.maintenance import graph_cleanup
    s = _store(tmp_path)
    _locked_dana(s, org="northgate trust inc")
    _ent(s, "marcus-reyes", "Marcus Reyes", org="northgate trust inc")
    graph_cleanup.cleanup_graph(s, taxonomy=_TAX)
    assert s.get_entity("dana-okafor")["org"] == "northgate trust inc"


def test_c2_rewrite_org_field_mixed_locked_and_unlocked(tmp_path):
    s = _store(tmp_path)
    _locked_dana(s)
    _ent(s, "marcus-reyes", "Marcus Reyes", org="Northgate Trust")
    assert s.rewrite_org_field("Northgate Trust", "The Lantern Co") == 1
    assert s.get_entity("dana-okafor")["org"] == "Northgate Trust"
    assert s.get_entity("marcus-reyes")["org"] == "The Lantern Co"


def test_c2_store_upsert_entity_on_conflict_respects_locks(tmp_path):
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "", org="")
    _lock(s, "dana-okafor", "name")
    _lock(s, "dana-okafor", "org")
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", org="Northgate Trust")
    e = s.get_entity("dana-okafor")
    assert e["name"] == "" and e["org"] == ""


def test_c2_field_lock_index_exists(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE type='index' "
                          "AND name='idx_efl_field'").fetchone()


def test_c2_every_entity_field_write_honours_locks_or_says_why():
    """Grep guard: every `UPDATE entities SET` (and every `INSERT INTO entities
    ... DO UPDATE SET`) in mcpbrain/ that writes org, name or email_addr, or
    builds its SET clause dynamically, must reference entity_field_locks in the
    statement or carry a `# lock-exempt: <reason>` marker. A new writer that
    does its own SQL would otherwise bypass a user correction silently."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "mcpbrain"
    guarded = re.compile(r"(?<![\w.])(org|name|email_addr)\s*=|\{")
    offenders = []
    for path in root.rglob("*.py"):
        lines = path.read_text().splitlines()
        for i, line in enumerate(lines):
            if "UPDATE entities SET" in line:
                window = "\n".join(lines[i:i + 6])
                set_part = window.split("UPDATE entities SET", 1)[1]
            elif "DO UPDATE SET" in line and "INTO entities" in "\n".join(lines[max(0, i - 4):i + 1]):
                window = "\n".join(lines[i:i + 16])
                set_part = window.split("DO UPDATE SET", 1)[1]
            else:
                continue
            set_part = set_part.split("WHERE", 1)[0]
            if not guarded.search(set_part):
                continue
            if "entity_field_locks" in window or "# lock-exempt:" in line:
                continue
            offenders.append(f"{path.relative_to(root.parent)}:{i + 1}: {line.strip()}")
    assert not offenders, "field writes that ignore user locks:\n" + "\n".join(offenders)


# --- I1: set_field undo restores only its own column, compare-and-swap ------

def _stated(**kw):
    return {"basis": "user_stated", "reason": "the user said so", **kw}


def _dana(s):
    _ent(s, "dana-okafor", "Dana Okafor", org="Northgate Trust",
         email="dana@northgate.example")


def test_i1_undo_org_keeps_a_later_ui_rename_and_aliases(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="set_field", entity_id="dana-okafor", field="org",
                               value="The Lantern Co"))["correction_id"]
    s.rename_entity("dana-okafor", "Dana A. Okafor", user=True)   # graph-UI edit, no ledger row
    assert gc.submit(s, {"op": "undo", "correction_id": cid})["status"] == "reverted"
    e = s.get_entity("dana-okafor")
    assert e["org"] == "Northgate Trust"
    assert e["name"] == "Dana A. Okafor" and "Dana Okafor" in e["aliases"].split("|")


def test_i1_undo_org_refused_when_org_changed_since(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="set_field", entity_id="dana-okafor", field="org",
                               value="The Lantern Co"))["correction_id"]
    s.update_entity_org("dana-okafor", "Southbank Community Trust", user=True)
    out = gc.submit(s, {"op": "undo", "correction_id": cid})
    assert out["status"] == "refused"
    assert f"org changed since correction {cid}" in out["error"]
    assert "Southbank Community Trust" in out["error"]
    assert s.get_entity("dana-okafor")["org"] == "Southbank Community Trust"


def test_i1_undo_name_removes_only_the_alias_it_added(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="set_field", entity_id="dana-okafor", field="name",
                               value="Dana A. Okafor"))["correction_id"]
    with s._connect(write=True) as db:   # extraction adds an alias since
        db.execute("UPDATE entities SET aliases=aliases||'|D. Okafor' WHERE id='dana-okafor'")
    assert gc.submit(s, {"op": "undo", "correction_id": cid})["status"] == "reverted"
    e = s.get_entity("dana-okafor")
    assert e["name"] == "Dana Okafor"
    assert e["aliases"].split("|") == ["D. Okafor"]


def test_i1_undo_email_keeps_later_org_valid_from(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="set_field", entity_id="dana-okafor", field="email",
                               value="dana@lantern.example"))["correction_id"]
    s.update_entity_org("dana-okafor", "The Lantern Co", "2026-10-01")
    assert gc.submit(s, {"op": "undo", "correction_id": cid})["status"] == "reverted"
    e = s.get_entity("dana-okafor")
    assert e["email_addr"] == "dana@northgate.example"
    assert e["org"] == "The Lantern Co" and e["org_valid_from"] == "2026-10-01"


def test_i1_undo_role_refused_when_manual_row_no_longer_current(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="set_field", entity_id="dana-okafor", field="role",
                               value="Operations Lead"))["correction_id"]
    with s._connect(write=True) as db:
        db.execute("UPDATE entity_observations SET valid_to='2026-09-25' "
                   "WHERE entity_id='dana-okafor' AND attribute='role'")
        db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source, "
                   "valid_from) VALUES('dana-okafor','role','Finance Lead','manual','2026-09-25')")
    out = gc.submit(s, {"op": "undo", "correction_id": cid})
    assert out["status"] == "refused" and f"role changed since correction {cid}" in out["error"]


def test_i1_undo_hide_refused_when_suppression_replaced(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="hide", entity_id="dana-okafor"))["correction_id"]
    s.suppress_entity("dana-okafor", reason="junk")          # review applier since
    out = gc.submit(s, {"op": "undo", "correction_id": cid})
    assert out["status"] == "refused" and f"hide changed since correction {cid}" in out["error"]
    with s._connect() as db:
        assert db.execute("SELECT reason FROM entity_suppressions WHERE entity_id='dana-okafor'"
                          ).fetchone()[0] == "junk"


# --- I2: unmerge restores a winner column only if the merge's value stands --

def _merge_pair(s):
    _ent(s, "dana-okafor", "Dana Okafor", org="", mentions=9, aliases="Dana O")
    _ent(s, "dana-okafor-2", "Dana Okafor", org="Northgate Trust", mentions=1,
         email="dana@northgate.example")
    out = gc.submit(s, _stated(op="merge", entity_id="dana-okafor", other_id="dana-okafor-2"))
    assert out["status"] == "applied", out
    return out["correction_id"]


def test_i2_unmerge_keeps_automated_changes_since_the_merge(tmp_path):
    s = _store(tmp_path)
    cid = _merge_pair(s)
    w = s.get_entity("dana-okafor")
    assert w["org"] == "Northgate Trust" and w["email_addr"] == "dana@northgate.example"
    # Automated writers since the merge: notes, org, a new alias.
    s.set_entity_notes("dana-okafor", "added by synthesis")
    s.update_entity_org("dana-okafor", "The Lantern Co", "2026-10-01")
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET aliases=aliases||'|D. Okafor' WHERE id='dana-okafor'")
    assert gc.submit(s, {"op": "undo", "correction_id": cid})["status"] == "reverted"
    w = s.get_entity("dana-okafor")
    assert w["notes"] == "added by synthesis"          # changed since: kept
    assert w["org"] == "The Lantern Co"                # changed since: kept
    assert w["email_addr"] == ""                       # unchanged since: restored
    assert sorted(w["aliases"].split("|")) == ["D. Okafor", "Dana O"]
    assert s.get_entity("dana-okafor-2") is not None


def test_i2_unmerge_restores_untouched_winner_exactly(tmp_path):
    s = _store(tmp_path)
    cid = _merge_pair(s)
    assert gc.submit(s, {"op": "undo", "correction_id": cid})["status"] == "reverted"
    w = s.get_entity("dana-okafor")
    assert (w["name"], w["org"], w["aliases"], w["email_addr"]) == ("Dana Okafor", "", "Dana O", "")


# --- I3: a merge correction is not silently reversed ------------------------

def test_i3_upsert_of_merged_away_name_resolves_to_winner(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor", mentions=9, aliases="Dana O")
    _ent(s, "dee-okafor", "Dee Okafor", mentions=1)
    out = gc.submit(s, _stated(op="merge", entity_id="dana-okafor", other_id="dee-okafor"))
    assert out["status"] == "applied"
    assert len(s.get_entity("dana-okafor")["aliases"].split("|")) >= 2
    got = gw.upsert_entity(s, name="Dee Okafor", entity_type="person", taxonomy=_TAX)
    assert got == "dana-okafor"
    assert "dee-okafor" not in _ids(s)
    assert gc.submit(s, {"op": "undo", "correction_id": out["correction_id"]})["status"] == "reverted"


def test_i3_append_alias_writes_pipe_and_reads_legacy_comma(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor", aliases="Dana O")
    with s._connect(write=True) as db:
        gw._append_alias(db, "dana-okafor", "D. Okafor")
    assert s.get_entity("dana-okafor")["aliases"] == "Dana O|D. Okafor"
    _ent(s, "marcus-reyes", "Marcus Reyes", aliases="Marc R, M. Reyes")   # legacy comma value
    assert gw.upsert_entity(s, name="M. Reyes", entity_type="person",
                            taxonomy=_TAX) == "marcus-reyes"


def test_i3_new_entity_aliases_are_pipe_joined(tmp_path):
    from mcpbrain import graph_write as gw
    s = _store(tmp_path)
    eid = gw.upsert_entity(s, name="Priya Anand", entity_type="person",
                           aliases="P. Anand, Priya A", taxonomy=_TAX)
    assert s.get_entity(eid)["aliases"] == "P. Anand|Priya A"


def test_i3_merge_refused_for_org_origin_entity(tmp_path):
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor", mentions=9, origin="org")
    _ent(s, "dee-okafor", "Dee Okafor", mentions=1)
    out = gc.submit(s, _stated(op="merge", entity_id="dana-okafor", other_id="dee-okafor"))
    assert out["status"] == "refused" and "organisation-shared" in out["error"]
    assert {"dana-okafor", "dee-okafor"} <= _ids(s)


# --- I4: automated merges skip entities carrying user corrections -----------

def _user_hide(s, eid):
    s.suppress_entity(eid, reason="user")


def test_i4_has_user_corrections(tmp_path):
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor")
    _ent(s, "marcus-reyes", "Marcus Reyes")
    _ent(s, "priya-anand", "Priya Anand")
    assert not s.has_user_corrections("dana-okafor")
    _lock(s, "dana-okafor", "org")
    _user_hide(s, "marcus-reyes")
    s.suppress_entity("priya-anand", reason="junk")          # automated hide: not a correction
    assert s.has_user_corrections("dana-okafor")
    assert s.has_user_corrections("marcus-reyes")
    assert not s.has_user_corrections("priya-anand")


def test_i4_resolve_deterministic_skips_locked_and_hidden(tmp_path):
    from mcpbrain.resolve import _deterministic_merges
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", mentions=1)
    _ent(s, "dana-c", "Dana Okafor", mentions=9)
    _lock(s, "dana-a", "org")
    _ent(s, "marcus-a", "Marcus Reyes", mentions=1)
    _ent(s, "marcus-c", "Marcus Reyes", mentions=9)
    _user_hide(s, "marcus-c")
    assert _deterministic_merges(s) == 0
    assert {"dana-a", "dana-c", "marcus-a", "marcus-c"} <= _ids(s)


def test_i4_resolve_email_skips_locked(tmp_path):
    from mcpbrain.resolve import _email_equality_merges
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", email="dana@northgate.example", mentions=1)
    _ent(s, "dana-c", "Dee Okafor", email="dana@northgate.example", mentions=9)
    _lock(s, "dana-a", "name")
    assert _email_equality_merges(s, home=tmp_path) == 0
    assert {"dana-a", "dana-c"} <= _ids(s)


def test_i4_review_apply_guards_user_corrected_pair(tmp_path):
    from mcpbrain import review_apply
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", mentions=1)
    _ent(s, "dana-c", "Dana Okafor", mentions=9)
    _user_hide(s, "dana-a")
    out = review_apply.apply_duplicate_verdicts(
        s, [{"pair_id": "dana-a|dana-c", "same": True}], cap=10)
    assert out["merged"] == 0 and out["guarded"] == 1
    with s._connect() as db:
        assert db.execute("SELECT reason FROM entity_suppressions WHERE entity_id='dana-a'"
                          ).fetchone()[0] == "user"


def test_i4_org_curate_guards_user_corrected_pair(tmp_path):
    from mcpbrain import org_curate
    s = _store(tmp_path)
    _ent(s, "dana-a", "Dana Okafor", mentions=1)
    _ent(s, "dana-c", "Dana Okafor", mentions=9)
    _lock(s, "dana-c", "email")
    out = org_curate._apply_merge_verdicts(
        s, [{"pair_id": "dana-a|dana-c", "verdict": "merge"}], cap=10)
    assert out["merged"] == 0 and out["guarded"] == 1
    assert {"dana-a", "dana-c"} <= _ids(s)


def test_i4_org_import_slug_drift_skips_user_corrected_and_distinct(tmp_path):
    import gzip
    import hashlib
    import json
    from mcpbrain import org_import
    from mcpbrain.org_contracts import SnapshotManifest
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    _ent(s, "dana-local", "Dana Okafor", email="dana@northgate.example")
    _lock(s, "dana-local", "org")
    _ent(s, "marcus-local", "Marcus Reyes", email="marcus@northgate.example")
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    ents = [{"kind": "entity", "id": "dana-org", "name": "Dana Okafor", "type": "person",
             "org": "", "email_addr": "dana@northgate.example", "aliases": ""},
            {"kind": "entity", "id": "marcus-org", "name": "Marcus Reyes", "type": "person",
             "org": "", "email_addr": "marcus@northgate.example", "aliases": ""}]
    gz = gzip.compress(("\n".join(json.dumps(e, sort_keys=True) for e in ents) + "\n").encode())
    man = SnapshotManifest(version=1, created_at="t", entity_count=2, relation_count=0,
                           tombstone_count=0, snapshot_sha256=hashlib.sha256(gz).hexdigest())
    fs.put_bytes("org-graph/snapshot.jsonl.gz", gz)
    fs.put_bytes("org-graph/tombstones.jsonl", b"")
    fs.put_bytes("org-graph/manifest.json", json.dumps(man.to_dict(), sort_keys=True).encode())
    # marcus-org is stubbed by the import before reconcile; mark the pair
    # distinct up front by pre-creating the org stub.
    _ent(s, "marcus-org", "Marcus Reyes", origin="org")
    _distinct(s, "marcus-local", "marcus-org")
    assert org_import.import_snapshot(s, fs)["status"] == "imported"
    assert {"dana-local", "marcus-local"} <= _ids(s)


# --- I5: reason is required for every op except undo ------------------------

def test_i5_blank_or_missing_reason_is_refused(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    base = {"op": "hide", "entity_id": "dana-okafor"}
    for basis in ("user_stated", "inferred"):
        for reason in (None, "", "   "):
            args = {**base, "basis": basis}
            if reason is not None:
                args["reason"] = reason
            out = gc.submit(s, args)
            assert out["status"] == "refused" and "reason" in out["error"], out
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM graph_corrections").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM entity_suppressions").fetchone()[0] == 0


def test_i5_undo_needs_no_reason(tmp_path):
    s = _store(tmp_path)
    _dana(s)
    cid = gc.submit(s, _stated(op="hide", entity_id="dana-okafor"))["correction_id"]
    assert gc.submit(s, {"op": "undo", "correction_id": cid})["status"] == "reverted"


def test_i5_tool_description_says_reason_is_required():
    from mcpbrain import tools  # noqa: F401  (registers the tools)
    from mcpbrain.tool_registry import spec
    assert "non-empty reason" in spec("brain_graph_correct").description


# --- M2 / M3: entity resources see corrections ------------------------------

def test_m2_hide_and_merge_invalidate_top_entities_cache(tmp_path):
    from mcpbrain import entity_resource
    entity_resource._cache.clear()
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor", mentions=9)
    _ent(s, "dee-okafor", "Dee Okafor", mentions=1)
    _ent(s, "marcus-reyes", "Marcus Reyes")
    assert {e["id"] for e in entity_resource.top_entities(s)} == {
        "dana-okafor", "dee-okafor", "marcus-reyes"}
    hid = gc.submit(s, _stated(op="hide", entity_id="marcus-reyes"))["correction_id"]
    assert "marcus-reyes" not in {e["id"] for e in entity_resource.top_entities(s)}
    gc.submit(s, _stated(op="merge", entity_id="dana-okafor", other_id="dee-okafor"))
    assert "dee-okafor" not in {e["id"] for e in entity_resource.top_entities(s)}
    gc.submit(s, {"op": "undo", "correction_id": hid})
    assert "marcus-reyes" in {e["id"] for e in entity_resource.top_entities(s)}
    entity_resource._cache.clear()


def test_m3_render_markdown_shows_the_ranked_role(tmp_path):
    from mcpbrain import entity_resource
    s = _store(tmp_path)
    _ent(s, "dana-okafor", "Dana Okafor", org="Northgate Trust")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source, "
                   "valid_from) VALUES('dana-okafor','role','Operations Lead','manual','2026-01-01')")
        db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source, "
                   "valid_from) VALUES('dana-okafor','role','Events Coordinator',"
                   "'llm_extraction','2026-09-01')")
    md = entity_resource.render_markdown(s, "dana-okafor")["markdown"]
    assert "Operations Lead" in md.splitlines()[2]
    assert "Events Coordinator" not in md.splitlines()[2]
