import argparse
import sys
from mcpbrain.daemon import main as _daemon_main
from mcpbrain.auth import main as _auth_main

def _mcp_main():
    from mcpbrain.mcp_server import main as m; m()
def _setup_main(argv):    from mcpbrain.setup import main as m; m(argv)
def _update_main(argv):   from mcpbrain.update import main as m; m(argv)
def _register_main(argv): from mcpbrain.wizard.register import main as m; m(argv)
def _tray_main(argv):     from mcpbrain.tray import main as m; m(argv)

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="mcpbrain")
    sub = p.add_subparsers(dest="cmd", required=True)
    # add_help only for mcp-server; every other subcommand delegates --help to its
    # own module parser (parse_known_args forwards --help into `rest`).
    for name in ("daemon","mcp-server","auth","setup","update","register","tray",
                 "enrich-backfill","records-prune","records-health",
                 "records-gardener","meeting-packs","session-start","session-end"):
        sub.add_parser(name, add_help=(name == "mcp-server"))
    ns, rest = p.parse_known_args(argv)
    def _records_cadence_main(argv):
        from mcpbrain.records_cadences import main as m
        return m(argv)
    return {
        "daemon": lambda: _daemon_main(rest), "mcp-server": _mcp_main,
        "auth": lambda: _auth_main(rest), "setup": lambda: _setup_main(rest),
        "update": lambda: _update_main(rest), "register": lambda: _register_main(rest),
        "tray": lambda: _tray_main(rest),
        "enrich-backfill": lambda: __import__("mcpbrain.enrich_backfill", fromlist=["main"]).main(rest),
        "records-prune": lambda: _records_cadence_main(["records-prune", *rest]),
        "records-health": lambda: _records_cadence_main(["records-health", *rest]),
        "records-gardener": lambda: __import__("mcpbrain.cowork", fromlist=["gardener_main"]).gardener_main(rest),
        "meeting-packs": lambda: __import__("mcpbrain.cowork", fromlist=["meeting_packs_main"]).meeting_packs_main(rest),
        "session-start": lambda: __import__("mcpbrain.session_hooks", fromlist=["session_start_main"]).session_start_main(rest),
        "session-end": lambda: __import__("mcpbrain.session_hooks", fromlist=["session_end_main"]).session_end_main(rest),
    }[ns.cmd]()
