import pytest


@pytest.fixture(scope="session", autouse=True)
def _stable_model_cache():
    """Pin the embedding-model cache to one stable directory for the whole test
    session.

    _model_cache_dir() is FASTEMBED_CACHE_PATH or app_dir()/models — and app_dir()
    follows MCPBRAIN_HOME. Many tests set MCPBRAIN_HOME to a fresh tmp dir, so
    without this each would land on an empty cache and RE-DOWNLOAD the bge model.
    Pinning FASTEMBED_CACHE_PATH once (to the platform-default models dir, resolved
    before any test moves MCPBRAIN_HOME) means the model is fetched at most once per
    machine and shared across every test. setdefault respects an explicit value
    (dev shell / CI) and the per-test overrides in test_embed.py (which monkeypatch
    FASTEMBED_CACHE_PATH themselves)."""
    import os
    from mcpbrain import config
    os.environ.setdefault("FASTEMBED_CACHE_PATH", str(config.app_dir() / "models"))
    yield


@pytest.fixture(autouse=True)
def _clear_orgs_cache():
    """Clear the lru_cache on taxonomy_from_config before each test.

    Prevents cache pollution across test modules — one module's configured
    taxonomy must not bleed into another module's empty-config test.
    """
    from mcpbrain import orgs
    orgs.taxonomy_from_config.cache_clear()
    yield
    orgs.taxonomy_from_config.cache_clear()
