from mcpbrain.daemon import _render_project_instructions

def test_instructions_mention_brain_graph():
    assert "brain_graph" in _render_project_instructions("Josh", ["Centrepoint"])

def test_instructions_mention_communities_mode():
    t = _render_project_instructions("Josh", ["Centrepoint"])
    assert "communities" in t.lower()

def test_instructions_include_examples():
    t = _render_project_instructions("Josh", [])
    assert "hops" in t or "connected" in t
    assert "community" in t.lower() or "circle" in t.lower()

def test_instructions_still_mention_existing_tools():
    t = _render_project_instructions("Alice", ["Acme"])
    for tool in ("brain_search", "brain_context", "brain_actions"):
        assert tool in t
