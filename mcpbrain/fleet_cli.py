"""CLI entry for `mcpbrain fleet-report` — beacon write + report aggregation."""
from __future__ import annotations

import argparse

from mcpbrain import config, fleet


def _build_drive_service():
    from mcpbrain import auth
    creds = auth.load_credentials()
    # build_service's default timeout_s dropped from DEFAULT_HTTP_TIMEOUT_S
    # (600s) to DEFAULT_READ_TIMEOUT_S (60s) so the daemon's per-cycle reads
    # can't stall the whole cycle for ten minutes — this call site silently
    # inherited that drop. The beacon/report payloads this module uploads are
    # small (per-install JSON, an aggregated status.html), so 60s would almost
    # certainly be fine either way; use the larger timeout anyway because this
    # is a one-shot CLI/cadence call (not the daemon's contended cycle path
    # the shorter default protects) aggregating potentially many beacons
    # sequentially across a large fleet, and a generous timeout only ever
    # prevents a premature cut-off (see auth.DEFAULT_HTTP_TIMEOUT_S's docstring).
    return auth.build_service("drive", "v3", creds, timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mcpbrain fleet-report")
    ap.add_argument("--beacon", action="store_true",
                    help="write this install's health beacon (used by the hourly cadence)")
    args = ap.parse_args(argv)

    home = str(config.app_dir())
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        # --beacon runs on an hourly timer. If fleet was never configured (or was
        # cleared), that is a benign no-op, not an error — exit 0 so an orphaned
        # cadence doesn't spam the launchd error log every hour. The manual report
        # path (no --beacon) still tells the human why there's nothing to show.
        if args.beacon:
            return
        print("fleet.folder_id not set — run mcpbrain setup to configure.")
        raise SystemExit(1)

    svc = _build_drive_service()
    if args.beacon:
        fleet.write_beacon(home, svc)
        return
    fleet.write_report(home, svc)
    print(f"Fleet report written. View status.html in the fleet folder: "
          f"https://drive.google.com/drive/folders/{folder_id}")
