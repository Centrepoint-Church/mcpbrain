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


# Test files that construct a bare/real Daemon and can therefore reach
# _spawn_replacement() / _exit_for_restart() (the watchdog's self-restart
# path). subprocess.Popen is only neutralised for tests collected from one of
# these — see _no_real_exit below for why a suite-wide Popen patch is wrong.
_DAEMON_SPAWN_REACHABLE_FILES = {
    "test_daemon_watchdog.py",
    "test_run_loop_wiring.py",
    "test_maintenance_scheduler.py",
    "test_daemon_thread_safety.py",
}


@pytest.fixture(autouse=True)
def _no_real_exit(monkeypatch, request):
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
    unwinds in reverse order). os._exit has no other legitimate caller anywhere
    in the suite, so this half of the patch stays global.

    subprocess.Popen is a DIFFERENT story and is deliberately FILE-SCOPED, not
    global. _spawn_replacement's body calls subprocess.Popen(...) before its
    os._exit(1), so with only os._exit neutralised, a future test calling the
    real _spawn_replacement() without also stubbing Popen would spawn a real
    detached `python -m mcpbrain.daemon` subprocess before hitting the
    neutralised os._exit — a lingering real background process, worse than the
    original footgun. A first attempt patched subprocess.Popen globally in this
    same autouse fixture, but subprocess.Popen is ONE shared module object for
    the whole process (import subprocess doesn't give each importer its own
    copy) — that broke ~70 unrelated tests across test_records_repo.py,
    test_backup_records.py, test_phase2_gardener.py, etc. that shell out via
    mcpbrain/records.py's _git() helper for real git operations. So the Popen
    patch is applied ONLY when the currently-running test file is one that
    actually constructs a Daemon and could reach _spawn_replacement/
    _exit_for_restart (_DAEMON_SPAWN_REACHABLE_FILES, above) — every other test
    file's subprocess.Popen is left completely untouched. The two watchdog
    tests already `import subprocess` and monkeypatch.setattr(subprocess,
    "Popen", ...) themselves — same last-write-wins mechanism as os._exit — so
    their own mock keeps winning for their test body with no change needed on
    their side."""
    from mcpbrain import daemon as _d

    def _boom_exit(code=0):
        raise AssertionError(f"os._exit({code!r}) called in a test")

    monkeypatch.setattr(_d.os, "_exit", _boom_exit)

    if request.node.path.name in _DAEMON_SPAWN_REACHABLE_FILES:
        import subprocess

        def _boom_popen(*args, **kwargs):
            raise AssertionError("subprocess.Popen called in a test")

        monkeypatch.setattr(subprocess, "Popen", _boom_popen)
