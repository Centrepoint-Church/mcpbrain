"""CLI entry for `mcpbrain fleet-report` — beacon write + report aggregation."""
from __future__ import annotations

import argparse

from mcpbrain import config, fleet


def _build_drive_service():
    from mcpbrain import auth
    creds = auth.load_credentials()
    return auth.build_service("drive", "v3", creds)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="mcpbrain fleet-report")
    ap.add_argument("--beacon", action="store_true",
                    help="write this install's health beacon (used by the hourly cadence)")
    args = ap.parse_args(argv)

    home = str(config.app_dir())
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        print("fleet.folder_id not set — run mcpbrain setup to configure.")
        raise SystemExit(1)

    svc = _build_drive_service()
    if args.beacon:
        fleet.write_beacon(home, svc)
        return
    fleet.write_report(home, svc)
    print(f"Fleet report written. View status.html in the fleet folder: "
          f"https://drive.google.com/drive/folders/{folder_id}")
