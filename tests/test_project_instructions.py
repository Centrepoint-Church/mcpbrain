from mcpbrain.config import render_project_instructions


def _cfg(name="Josh", orgs=("Centrepoint",), role="", full=""):
    return {
        "owner_name": name,
        "owner_full_name": full,
        "owner_role": role,
        "orgs": [{"name": o} for o in orgs],
    }


def test_instructions_mention_brain_graph():
    assert "brain_graph" in render_project_instructions(_cfg())


def test_instructions_mention_communities_mode():
    t = render_project_instructions(_cfg())
    assert "communities" in t.lower()


def test_instructions_include_examples():
    t = render_project_instructions(_cfg(orgs=()))
    assert "hops" in t or "connected" in t
    assert "community" in t.lower() or "circle" in t.lower()


def test_instructions_still_mention_existing_tools():
    t = render_project_instructions(_cfg("Alice", ["Acme"]))
    for tool in ("brain_search", "brain_context", "brain_actions"):
        assert tool in t


def test_instructions_use_full_name_role_and_orgs():
    # The standing instructions should be framed in the owner's own details,
    # preferring their full name and folding in role + orgs.
    t = render_project_instructions(
        _cfg(name="Josh", full="Joshua Kemp", role="Operations Manager", orgs=["Centrepoint Church"])
    )
    assert "Joshua Kemp" in t
    assert "Operations Manager" in t
    assert "Centrepoint Church" in t


def test_instructions_degrade_without_details():
    # No name/role/orgs saved yet -> falls back to a neutral "you" without crashing.
    t = render_project_instructions({})
    assert "you're you's assistant," in t.lower()
    assert "brain_search" in t
