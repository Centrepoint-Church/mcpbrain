from mcpbrain import records


def test_ensure_skips_git_calls_when_cached(tmp_path, monkeypatch):
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")  # first: real init
    calls = {"n": 0}
    monkeypatch.setattr(records, "_git", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")  # cached: no git calls
    assert calls["n"] == 0
