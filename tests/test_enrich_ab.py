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
