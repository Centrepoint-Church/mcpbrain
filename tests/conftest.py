import pytest


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
