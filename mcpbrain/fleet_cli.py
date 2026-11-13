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
    # inherited that drop. Checked (Task 6, per the brief's own conditional:
    # "pass DEFAULT_HTTP_TIMEOUT_S if this moves large cache blobs"): it does
    # NOT — write_beacon/write_report upload/download only small per-install
    # JSON beacons and one aggregated status.html. Left on the new 60s
    # default deliberately, matching the brief's literal conditional.
    return auth.build_service("drive", "v3", creds)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mcpbrain fleet-report")
    ap.add_argument("--beacon", action="store_true",
                    help="write this install's health beacon (used by the hourly cadence)")
    args = ap.parse_args(argv)

    from mcpbrain.fleet_storage import fleet_folder_id
    home = str(config.app_dir())
    folder_id = fleet_folder_id(home)       # config, else this build's tenant
    if not folder_id:
        # --beacon runs on an hourly timer. If fleet was never configured (or was
        # cleared), that is a benign no-op, not an error — exit 0 so an orphaned
        # cadence doesn't spam the launchd error log every hour. The manual report
        # path (no --beacon) still tells the human why there's nothing to show.
        if args.beacon:
            return
        print("no fleet folder: set fleet.folder_id, or use a build whose tenant "
              "profile names one.")
        raise SystemExit(1)

    svc = _build_drive_service()
    if args.beacon:
        fleet.write_beacon(home, svc)
        return
    fleet.write_report(home, svc)
    print(f"Fleet report written. View status.html in the fleet folder: "
          f"https://drive.google.com/drive/folders/{folder_id}")
