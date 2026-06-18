import argparse
import sys
from mcpbrain.daemon import main as _daemon_main
from mcpbrain.auth import main as _auth_main

def _mcp_main():
    from mcpbrain.mcp_server import main as m; m()
def _setup_main(argv):    from mcpbrain.setup import main as m; m(argv)
def _connect_main(argv):  from mcpbrain.setup import connect_main as m; m(argv)
def _update_main(argv):   from mcpbrain.update import main as m; m(argv)
def _tray_main(argv):     from mcpbrain.tray import main as m; m(argv)
def _monitor_main():      from mcpbrain.monitor import main as m; m()
def _home_main():
    from mcpbrain.config import app_dir
    print(str(app_dir()))

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="mcpbrain")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("daemon","mcp-server","auth","setup","connect","update","tray","home",
                 "records-prune","records-health",
                 "session-start","session-end",
                 "monitor","restore","fleet-report","doctor"):
        sub.add_parser(name, add_help=(name == "mcp-server"))
    ns, rest = p.parse_known_args(argv)
    def _records_cadence_main(argv):
        from mcpbrain.records_cadences import main as m
        return m(argv)
    return {
        "daemon": lambda: _daemon_main(rest), "mcp-server": _mcp_main,
        "auth": lambda: _auth_main(rest), "setup": lambda: _setup_main(rest),
        "connect": lambda: _connect_main(rest),
        "update": lambda: _update_main(rest),
        "tray": lambda: _tray_main(rest),
        "home": _home_main,
        "records-prune": lambda: _records_cadence_main(["records-prune", *rest]),
        "records-health": lambda: _records_cadence_main(["records-health", *rest]),
        "session-start": lambda: __import__("mcpbrain.session_hooks", fromlist=["session_start_main"]).session_start_main(rest),
        "session-end": lambda: __import__("mcpbrain.session_hooks", fromlist=["session_end_main"]).session_end_main(rest),
        "monitor": _monitor_main,
        "restore": lambda: __import__("mcpbrain.restore", fromlist=["run_restore_main"]).run_restore_main(rest),
        "fleet-report": lambda: __import__(
            "mcpbrain.fleet_cli", fromlist=["main"]).main(rest),
        "doctor": lambda: __import__("mcpbrain.doctor", fromlist=["run_doctor_main"]).run_doctor_main(rest),
    }[ns.cmd]()
