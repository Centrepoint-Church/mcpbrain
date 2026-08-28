def test_score_flags_lost_org_assignments():
    """The gate: B must not LOSE an org/role that A got right. A pure count
    match would hide a systematic misattribution, which is exactly why
    enrich_eval.graph_metrics is insufficient here."""
    from bin.enrich_ab import score_pair
    a = {"entities": [{"name": "Taryn Hamilton", "org": "Acme", "role": "Pastor"}]}
    b = {"entities": [{"name": "Taryn Hamilton", "org": "", "role": "Pastor"}]}
    r = score_pair(a, b)
    assert r["org_lost"] == ["Taryn Hamilton"]
    assert r["entities_lost"] == []


def test_score_pair_flags_dropped_entities():
    from bin.enrich_ab import score_pair
    a = {"entities": [{"name": "X", "org": "Acme"}, {"name": "Y", "org": "Acme"}]}
    b = {"entities": [{"name": "X", "org": "Acme"}]}
    assert score_pair(a, b)["entities_lost"] == ["Y"]


def test_score_pair_clean_when_identical():
    from bin.enrich_ab import score_pair
    a = {"entities": [{"name": "X", "org": "Acme", "role": "R"}]}
    assert score_pair(a, a) == {"entities_lost": [], "entities_gained": [],
                                "org_lost": [], "role_lost": []}


def test_prep_constructs_a_real_store_without_crashing(tmp_path, monkeypatch):
    """Same class of bug as bin/resalience.py and _bump_unit_attempts (Store(...)
    missing dim / wrong path) — prep()'s own test coverage never exercises this,
    since score_pair is a pure function. Verify prep() runs end-to-end against a
    real (throwaway) store and writes paired a/b files."""
    import json
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    from bin import enrich_ab

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(config.store_path(), dim=get_embedder("bge-small").dim)
    store.init()

    units_dir = tmp_path / "units"
    units_dir.mkdir()
    (units_dir / "u-1.json").write_text(json.dumps(
        {"unit_id": "u-1", "kind": "thread",
         "threads": [{"thread_id": "t1", "messages": [{"message_id": "t1", "text": "hi"}]}]}))

    full_context = tmp_path / "full-context.json"
    full_context.write_text(json.dumps({"known_people": []}))

    out_dir = tmp_path / "ab"
    count = enrich_ab.prep(str(units_dir), str(out_dir), 5, str(full_context))
    assert count == 1
    assert (out_dir / "a" / "u-1.json").exists()
    assert (out_dir / "b" / "u-1.json").exists()
