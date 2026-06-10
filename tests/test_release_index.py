def test_render_simple_index_lists_wheels(tmp_path):
    import sys; sys.path.insert(0, "bin")
    import release
    wheels = ["mcpbrain-0.2.0-py3-none-any.whl", "mcpbrain-0.3.0-py3-none-any.whl"]
    html = release.render_package_index(wheels)
    assert all(w in html for w in wheels)
    assert "<a href=" in html and "mcpbrain" in html
