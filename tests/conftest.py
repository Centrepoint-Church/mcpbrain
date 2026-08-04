import pytest


@pytest.fixture
def protocol_session(tmp_path):
    """An async-context-manager FACTORY for a real ClientSession over a real
    stdio subprocess (`python -m mcpbrain.mcp_server`), with the child's
    stderr captured to a file the caller can print on failure.

    Shared here (not in test_mcp_protocol_surface.py) because Tasks 9, 10, 12
    need the exact same subprocess-over-stdio harness for their own protocol
    round-trips.

    This repo's suite has no pytest-asyncio (see test_mcp_server_stdio.py,
    which drives its own session with a plain `asyncio.run(...)` rather than
    `@pytest.mark.asyncio`), so this fixture is a regular (sync) fixture that
    hands back an `@asynccontextmanager` factory instead of a live session --
    callers open it inside their own `asyncio.run(...)`:

        async def _body():
            async with protocol_session() as (session, stderr_path):
                await session.list_tools()
        asyncio.run(_body())

    Also pre-initializes the store schema (`Store(...).init()`) before the
    subprocess ever spawns. In a real install the daemon always runs
    `store.init()` (creates the sqlite file + WAL + tables) long before the
    MCP server's first client connects; a fresh MCPBRAIN_HOME with no
    brain.sqlite3 at all makes the MCP server's *read-only* `Store(...,
    read_only=True)` fail outright ("unable to open database file") the
    moment any store-touching tool runs its first query -- a fixture gap
    (this suite never ran a daemon), not a genuine dispatch bug. Initializing
    here mirrors the real ordering instead of tainting Task 3's dispatch-layer
    coverage with an unrelated environment-setup failure.
    """
    import contextlib
    import os
    import sys
    from pathlib import Path

    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    home = tmp_path / "home"
    (home / "context").mkdir(parents=True)
    (home / "context" / "memory.md").write_text("# memory\n", encoding="utf-8")

    Store(home / "brain.sqlite3", dim=embedder_dim("bge-small"), read_only=False).init()

    stderr_path = tmp_path / "server-stderr.log"
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "MCPBRAIN_HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }

    @contextlib.asynccontextmanager
    async def _open(message_handler=None):
        """`message_handler` is an optional async callable receiving the server
        NOTIFICATIONS this session surfaces (ClientSession tees every parsed
        server notification to it). Needed by
        test_mcp_resource_notifications.py to observe
        resources/list_changed arriving; None keeps the SDK's default no-op, so
        every existing caller is unaffected. Passed through here rather than
        rebuilding this subprocess harness in one test module.
        """
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcpbrain.mcp_server"], env=env,
        )
        # mcp 2.x's ClientSession takes read_timeout_seconds as a float
        # (1.x took a datetime.timedelta).
        timeout = 15.0
        with open(stderr_path, "wb") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timeout,
                                         message_handler=message_handler) as session:
                    await session.initialize()
                    yield session, str(stderr_path)

    return _open


class _Progress:
    """One `notifications/progress` delivery, as (progress, total, message).

    `ClientSession.call_tool`'s `progress_callback` (mcp 2.x's `ProgressFnT`)
    is `async def(progress, total, message)` -- this just gives the collected
    values names so a test can write `p.total`/`p.message` instead of
    `p[1]`/`p[2]`.
    """
    def __init__(self, progress, total, message):
        self.progress = progress
        self.total = total
        self.message = message

    def __repr__(self):
        return f"_Progress(progress={self.progress!r}, total={self.total!r}, message={self.message!r})"


@pytest.fixture
def protocol_session_with_progress(tmp_path):
    """Variant of `protocol_session` (see its docstring for the shared stdio-
    subprocess rationale) for Task 12's progress-notification tests.

    Two things `protocol_session` doesn't provide, both needed here:

    1. A way to observe `notifications/progress`: `session.call_tool` is
       wrapped so every call passes a `progress_callback` (mcp 2.x mints a
       fresh `progressToken` automatically whenever one is supplied -- see
       `JSONRPCDispatcher.send_raw_request`), and each delivery is collected
       into a plain list of `_Progress` as it arrives.
    2. A way to SEED the store/config before the subprocess spawns: an empty
       store makes a 3-hop `brain_graph` traversal terminate after hop 1 (the
       BFS frontier goes empty with nothing to expand), and a missing
       `email_id` makes `brain_draft_context` fail at its first stage before
       voice_rules/samples/critique ever run -- neither exercises genuine
       per-hop / per-stage progress. So the returned factory takes an
       optional `seed(store, home)` callable, invoked against the same
       writable `Store` the fixture initializes, before `stdio_client` spawns
       the child (which then opens its own handle onto the same sqlite file
       and sees whatever `seed` committed).

    `CLAUDE_BIN` is pointed at a path that cannot exist, so if a seed enables
    `draft_critic`, the critique stage's own subprocess call fails fast and
    deterministically (caught internally by draft_critic, never raises)
    instead of shelling out to a real `claude` CLI from inside a unit test.

    Usage:
        async def _body():
            async with protocol_session_with_progress(seed=my_seed) as (session, progress):
                await session.call_tool("brain_graph", {...})
                assert progress  # list of _Progress
        asyncio.run(_body())
    """
    import contextlib
    import os
    import sys
    from pathlib import Path

    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    home = tmp_path / "home"
    (home / "context").mkdir(parents=True)
    (home / "context" / "memory.md").write_text("# memory\n", encoding="utf-8")

    store = Store(home / "brain.sqlite3", dim=embedder_dim("bge-small"), read_only=False)
    store.init()

    stderr_path = tmp_path / "server-stderr-progress.log"
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "MCPBRAIN_HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "CLAUDE_BIN": str(tmp_path / "no-such-claude-binary"),
    }

    @contextlib.asynccontextmanager
    async def _open(seed=None):
        if seed is not None:
            seed(store, home)

        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "mcpbrain.mcp_server"], env=env,
        )
        progress: list[_Progress] = []

        async def _collect(value, total, message) -> None:
            progress.append(_Progress(value, total, message))

        with open(stderr_path, "wb") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=15.0) as session:
                    await session.initialize()

                    # Every call through this session gets progress_callback wired
                    # in, rather than making each test pass it at every call site.
                    _real_call_tool = session.call_tool

                    async def call_tool(name, arguments=None, **kwargs):
                        kwargs.setdefault("progress_callback", _collect)
                        return await _real_call_tool(name, arguments, **kwargs)

                    session.call_tool = call_tool
                    yield session, progress

    return _open


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    """A temp app-dir + stores + control client, matching build_server()'s
    keyword arguments (store, draft_store, client, home).

    Mirrors the temp-MCPBRAIN_HOME + Store pattern in
    test_mcp_server_stdio.py's stdio integration test (see that file's
    _run_session/test_stdio_spawn_initialize_and_brain_search), lifted into a
    shared fixture here because test_mcp_build_server.py and the later
    protocol-coverage / surface-upgrade tests (Tasks 3, 8-11) all need the
    same store+client wiring rather than each reinventing it. Composes with
    this file's autouse fixtures (_stable_model_cache pins FASTEMBED_CACHE_PATH
    before MCPBRAIN_HOME moves; _isolate_daemon_tempdir/_no_real_exit don't
    touch MCPBRAIN_HOME at all), so setting it here doesn't fight them.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    from mcpbrain.control_client import ControlClient
    from mcpbrain.embed import embedder_dim
    from mcpbrain.store import Store

    dim = embedder_dim("bge-small")
    path = config.store_path()
    store = Store(path, dim=dim, read_only=False)  # created here so a second open succeeds
    return {
        "store": store,
        "draft_store": Store(path, dim=dim, read_only=False),
        "client": ControlClient(),
        "home": str(tmp_path),
    }


async def list_tools_via_handler(server):
    """Invoke the registered tools/list handler directly and return the tool
    list, without an event loop-driven transport or a real MCP session.

    On mcp 2.x, handlers are looked up by method string
    (`server.get_request_handler("tools/list")` -> a HandlerEntry carrying
    `.params_type` and `.handler`) and invoked as `handler(ctx, params)`
    returning a full `types.ListToolsResult`. build_server()'s handlers ignore
    `ctx` entirely, so None is a faithful stand-in for the per-request
    ServerRequestContext the runner would build. Originally lived inline in
    test_mcp_build_server.py; moved here (Task 8) because
    test_mcp_tool_annotations.py needs the exact same accessor and cross-
    importing test-module helpers between test files is worse than a shared
    conftest.
    """
    from mcp import types

    entry = server.get_request_handler("tools/list")
    result = await entry.handler(None, types.PaginatedRequestParams())
    return result.tools


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


@pytest.fixture(autouse=True)
def _isolate_daemon_tempdir(tmp_path, monkeypatch):
    """Keep the snapshot-orphan sweep away from the real OS temp dir.

    Daemon.run() and _backup_under_bulk_lock() call
    backup.sweep_orphan_snapshots(tempfile.gettempdir(), max_age_s=...), which
    rmtree's every `mcpbrain-snap-*` directory older than the cutoff. Several
    tests drive a real Daemon.run(), and gettempdir() is /var/folders/... —
    shared with the user's live daemon. A planted canary there was deleted by
    running the suite, and a live backup's work dir is exactly that shape, so a
    developer running pytest could destroy an in-flight snapshot of an 11.9GB
    store.

    Note this redirects `tempfile.gettempdir` process-wide for the duration of
    each test — `daemon.tempfile` is the stdlib module itself, so there is no
    daemon-only binding to patch. That is broader than strictly needed but is
    the safer default: no test has a legitimate reason to write into the shared
    OS temp dir. Tests that pass an explicit parent — notably
    tests/test_snapshot_orphans.py — are unaffected and still exercise the real
    sweep against their own directory.
    """
    from mcpbrain import daemon as _d
    sweep_root = tmp_path / "ostmp"
    sweep_root.mkdir(exist_ok=True)
    monkeypatch.setattr(_d.tempfile, "gettempdir", lambda: str(sweep_root))
    yield
