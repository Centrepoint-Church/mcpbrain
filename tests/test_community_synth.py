"""community_synthesis: titles + summaries for communities lacking them."""
from mcpbrain import community_synth
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _seed_community(s, cid, member_names, title=""):
    with s._connect() as db:
        for i, name in enumerate(member_names):
            eid = f"e-{cid}-{i}"
            db.execute("INSERT OR IGNORE INTO entities(id, name, type) VALUES(?,?,?)",
                       (eid, name, "person"))
            db.execute("INSERT INTO entity_communities(entity_id, community_id, level) "
                       "VALUES(?,?,0)", (eid, cid))
        db.execute("INSERT INTO community_summaries(community_id, level, title, summary, "
                   "member_count) VALUES(?,0,?,'',?)", (cid, title, len(member_names)))


def test_requests_pick_untitled_communities(tmp_path):
    s = _store(tmp_path)
    _seed_community(s, 1, ["Ann A", "Bob B"], title="")
    _seed_community(s, 2, ["Cee C"], title="Named already")
    reqs = community_synth.build_community_requests(s, cap=10)
    ids = [r["community_id"] for r in reqs]
    assert 1 in ids and 2 not in ids
    assert "Ann A" in reqs[0]["members"]


def test_members_are_sampled_not_dumped(tmp_path):
    # A community can have thousands of members; dumping all of them blew up the
    # spool / brain_enrich_pull. Only a bounded SAMPLE is emitted, but member_count
    # stays the true total.
    s = _store(tmp_path)
    _seed_community(s, 1, [f"Person {i:04d}" for i in range(5000)], title="")
    reqs = community_synth.build_community_requests(s, cap=10, member_sample=40)
    assert reqs[0]["member_count"] == 5000          # true count preserved
    assert len(reqs[0]["members"]) == 40            # but only a sample shipped


def test_single_member_community_excluded(tmp_path):
    s = _store(tmp_path)
    _seed_community(s, 1, ["Solo Person"], title="")   # 1 member, untitled — excluded
    _seed_community(s, 2, ["Ann A", "Bob B"], title="")  # 2 members, untitled — included
    reqs = community_synth.build_community_requests(s, cap=10)
    ids = [r["community_id"] for r in reqs]
    assert 1 not in ids
    assert 2 in ids


def test_drain_writes_title_summary_change_log(tmp_path):
    s = _store(tmp_path)
    _seed_community(s, 1, ["Ann A"], title="")
    n = community_synth.drain_communities(s, {"community_synthesis": [
        {"community_id": 1, "title": "Ops cluster", "summary": "People who run ops."}]})
    assert n["communities_written"] == 1
    with s._connect() as db:
        row = db.execute("SELECT title, summary FROM community_summaries "
                         "WHERE community_id=1").fetchone()
    assert row["title"] == "Ops cluster"
    assert s.recent_changes(5)[0]["source"] == "community_synthesis"
