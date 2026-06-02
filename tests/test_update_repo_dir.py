"""_repo_dir() resolution order: MCPBRAIN_REPO env, then persisted config.

Offline and fast: no git, no network. Each case builds a tmp dir with a
pyproject.toml and checks _repo_dir() resolves to it.
"""

import mcpbrain.update as upd


def _make_repo(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    return tmp_path


def test_repo_dir_uses_env_when_valid(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("MCPBRAIN_REPO", str(repo))
    assert upd._repo_dir() == str(repo)


def test_repo_dir_ignores_env_without_pyproject(tmp_path, monkeypatch):
    # Env points at a dir with no pyproject.toml: fall through to config.
    bad = tmp_path / "bad"
    bad.mkdir()
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setenv("MCPBRAIN_REPO", str(bad))

    import mcpbrain.config as cfg
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    cfg.write_config(str(tmp_path), {"repo_dir": str(repo)})

    assert upd._repo_dir() == str(repo)


def test_repo_dir_uses_persisted_config_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MCPBRAIN_REPO", raising=False)
    repo = _make_repo(tmp_path / "repo")

    import mcpbrain.config as cfg
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    cfg.write_config(str(tmp_path), {"repo_dir": str(repo)})

    assert upd._repo_dir() == str(repo)


def test_repo_dir_ignores_stale_config_path(tmp_path, monkeypatch):
    # Persisted repo_dir no longer has a pyproject.toml: fall through to the
    # __file__ walk (which finds the real repo this test runs in), not raise.
    monkeypatch.delenv("MCPBRAIN_REPO", raising=False)
    gone = tmp_path / "gone"
    gone.mkdir()

    import mcpbrain.config as cfg
    monkeypatch.setattr(cfg, "app_dir", lambda: tmp_path)
    cfg.write_config(str(tmp_path), {"repo_dir": str(gone)})

    # The package source tree has a pyproject.toml above it, so the walk wins.
    assert upd._repo_dir().endswith("mcp-ops-brain")
