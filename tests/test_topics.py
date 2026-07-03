import json
from mcpbrain import topics

def test_lowercase_and_whitespace(tmp_path):
    assert topics.normalize_topic("  Worship  Team ", str(tmp_path)) == "worship team"

def test_strips_leading_qualifier(tmp_path):
    assert topics.normalize_topic("annual budget", str(tmp_path)) == "budget"
    assert topics.normalize_topic("the budget", str(tmp_path)) == "budget"

def test_singularizes(tmp_path):
    assert topics.normalize_topic("budgets", str(tmp_path)) == "budget"

def test_synonym_map(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"topic_synonyms": {"finances": "budget"}}))
    assert topics.normalize_topic("finances", str(tmp_path)) == "budget"

def test_synonym_applied_after_singularize(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps(
        {"topic_synonyms": {"finance": "budget"}}))
    # 'finances' -> singular 'finance' -> synonym 'budget'
    assert topics.normalize_topic("finances", str(tmp_path)) == "budget"

def test_distinct_concepts_not_merged(tmp_path):
    # No synonym entry: 'prayer' and 'prayer meeting' stay distinct.
    assert topics.normalize_topic("prayer", str(tmp_path)) == "prayer"
    assert topics.normalize_topic("prayer meeting", str(tmp_path)) == "prayer meeting"

def test_empty(tmp_path):
    assert topics.normalize_topic("   ", str(tmp_path)) == ""
