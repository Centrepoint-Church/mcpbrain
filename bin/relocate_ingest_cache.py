"""One-shot cleanup: remove the legacy in-drive `.mcpbrain-cache/` folders left in
each Shared Drive root by the pre-centralization ingest cache.

As of the centralization change, the ingest cache lives under the fleet folder
(<fleet folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/), so the old
per-team-drive `.mcpbrain-cache/` folders are dead clutter. This removes them.

RUN ONCE, AND ONLY AFTER every install in the fleet has updated to the wheel that
centralizes the cache. An install still on the old code will RECREATE the in-drive
folder on its next sync. Deleting is safe: the central location re-publishes any
still-live document on its next cache-miss (regeneration is cheap; no copy needed).

Usage:
  python bin/relocate_ingest_cache.py                  # dry-run: report only
  python bin/relocate_ingest_cache.py --delete-legacy  # actually delete
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcpbrain import config                                    # noqa: E402
from mcpbrain.fleet_storage import list_shared_drives          # noqa: E402
from mcpbrain.ingest_cache import CACHE_DIR                    # noqa: E402

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_service(home):
    from mcpbrain import auth
    services = auth.build_google_services(token_file=Path(home) / "google_token.json")
    svc = services.get("drive_service")
    if svc is None:
        raise SystemExit(
            "No Drive access in the stored token — run `mcpbrain setup` "
            "(or check --home).")
    return svc


def _find_cache_folder(service, drive_id):
    resp = service.files().list(
        q=(f"name = '{CACHE_DIR}' and '{drive_id}' in parents and trashed = false "
           f"and mimeType = '{_FOLDER_MIME}'"),
        corpora="drive", driveId=drive_id,
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        fields="files(id,name)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _count_children(service, folder_id):
    n, token = 0, None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="nextPageToken, files(id)", pageSize=1000, pageToken=token).execute()
        n += len(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return n


def scan(service):
    """Return [{'drive_id','drive_name','folder_id','count'}] for drives that have
    a top-level .mcpbrain-cache/ folder. Per-drive failures are isolated."""
    out = []
    for d in list_shared_drives(service):
        did = d.get("id")
        if not did:
            continue
        name = d.get("name") or "<unnamed>"
        try:
            fid = _find_cache_folder(service, did)
            if not fid:
                continue
            count = _count_children(service, fid)
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            print(f"  ! {name} ({did}): scan failed: {exc}")
            continue
        out.append({"drive_id": did, "drive_name": name,
                    "folder_id": fid, "count": count})
    return out


def delete_legacy(service, entries):
    """Delete each entry's .mcpbrain-cache/ folder (and its contents). Per-drive
    isolation; returns the number of folders deleted."""
    deleted = 0
    for e in entries:
        try:
            service.files().delete(
                fileId=e["folder_id"], supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            print(f"  ! {e['drive_name']} ({e['drive_id']}): delete failed: {exc}")
            continue
        deleted += 1
        print(f"  ✓ deleted {CACHE_DIR}/ from {e['drive_name']} "
              f"({e['drive_id']}) — {e['count']} artifact(s)")
    return deleted


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relocate_ingest_cache")
    ap.add_argument("--delete-legacy", action="store_true",
                    help="Actually delete (default is a dry-run report).")
    ap.add_argument("--home", default=None)
    ns = ap.parse_args(argv)

    home = ns.home or str(config.app_dir())
    service = _drive_service(home)
    entries = scan(service)
    if not entries:
        print("No legacy in-drive .mcpbrain-cache/ folders found.")
        return 0

    print(f"Found legacy cache in {len(entries)} drive(s):")
    for e in entries:
        print(f"  - {e['drive_name']} ({e['drive_id']}): {e['count']} artifact(s)")

    if not ns.delete_legacy:
        print("\nDry-run. Re-run with --delete-legacy to remove them "
              "(only after the whole fleet has updated).")
        return 0

    n = delete_legacy(service, entries)
    print(f"\nDeleted {n} legacy cache folder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
