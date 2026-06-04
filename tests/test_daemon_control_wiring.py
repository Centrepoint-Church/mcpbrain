"""Daemon loop mode starts and stops the control API + browser wizard.

The Phase 2 exit requires the daemon to serve a token-guarded loopback control
API, and `mcpbrain setup` reads the control_port file ControlServer.start()
writes. main()'s loop branch must therefore construct a ControlServer with the
daemon, start it, run the loop, and stop it on exit.

Fully offline: get_embedder and Store are faked so fastembed never loads, and
MCPBRAIN_HOME points at an empty tmp dir so _enrich_client_from_config and
_backup_from_config read no config (return None / (None, None)).
"""

import mcpbrain.daemon as daemon_module
import mcpbrain.embed as embed_module
import mcpbrain.store as store_module


class FakeEmbedder:
    dim = 384


class FakeStore:
    def __init__(self, *a, **kw):
        pass

    def init(self):
        pass


class FakeControlServer:
    """Records construction + start/stop ordering without binding a socket."""

    instances = []

    def __init__(self, daemon, home, store=None):
        self.daemon = daemon
        self.home = home
        self.store = store
        self.events = []
        self.port = 54321
        FakeControlServer.instances.append(self)

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")


def test_main_loop_starts_and_stops_control_server(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # Lazy imports inside main() resolve the attribute at call time, so patching
    # the module attribute before calling main() is sufficient.
    monkeypatch.setattr(embed_module, "get_embedder", lambda kind=None: FakeEmbedder())
    monkeypatch.setattr(store_module, "Store", FakeStore)

    FakeControlServer.instances = []
    monkeypatch.setattr(daemon_module.control_api, "ControlServer", FakeControlServer)

    captured = {}

    def fake_run(self):
        # Record run() against the same event log as start/stop so we can prove
        # the server was running BETWEEN start() and stop().
        captured["daemon"] = self
        FakeControlServer.instances[0].events.append("run")

    monkeypatch.setattr(daemon_module.Daemon, "run", fake_run)

    daemon_module.main([])

    # Exactly one ControlServer, constructed with the daemon instance.
    assert len(FakeControlServer.instances) == 1
    ctrl = FakeControlServer.instances[0]
    assert ctrl.daemon is captured["daemon"]
    assert isinstance(ctrl.daemon, daemon_module.Daemon)

    # The daemon's store must be wired through, or every dashboard API call
    # returns 503 ("dashboard not available") in production.
    assert isinstance(ctrl.store, FakeStore)

    # start() -> run() -> stop(), in that exact order.
    assert ctrl.events == ["start", "run", "stop"]
