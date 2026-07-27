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


@pytest.fixture(autouse=True)
def _no_real_exit(monkeypatch):
    """os._exit(1) in the watchdog bypasses pytest entirely — a test that ever
    reaches it kills the worker with no traceback. Today that path is unreachable
    only by accident (frozen clocks, stubbed _stalled_phase); nothing structural
    prevents it. Neutralise it for every test.

    Patched at the os._exit BOUNDARY, not by nuking Daemon._exit_for_restart /
    Daemon._spawn_replacement at the class level: both real methods bottom out
    in os._exit(1) (daemon.py:2528, :2547) and that is the only call this fixture
    needs to make unreachable. A class-level patch on the methods themselves
    would win over the instance-level monkeypatches that
    test_daemon_watchdog.py's test_spawn_replacement_detaches_the_successor_on_windows
    / test_spawn_replacement_passes_no_creationflags_on_posix rely on — those
    two deliberately exercise the REAL _spawn_replacement() body (asserting on
    subprocess.Popen's creationflags/close_fds) and already mock only
    subprocess.Popen and os._exit directly. Patching os._exit here instead
    keeps both true: nothing in the suite can reach the real process-killing
    exit, and those two tests' own os._exit override still wins for the
    duration of their test body (monkeypatch is last-write-wins, teardown
    unwinds in reverse order)."""
    from mcpbrain import daemon as _d

    def _boom(code=0):
        raise AssertionError(f"os._exit({code!r}) called in a test")

    monkeypatch.setattr(_d.os, "_exit", _boom)
