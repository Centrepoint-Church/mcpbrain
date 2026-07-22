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


def _find_cache_folders(service, drive_id):
    """ALL top-level .mcpbrain-cache/ folder ids under the drive root. Drive does
    not enforce name uniqueness, so a drive can hold more than one same-named
    folder (a resolved _ensure_folder race, etc.) — return every match, not just
    the first, so a single pass fully cleans the drive. Paginated."""
    ids, token = [], None
    while True:
        resp = service.files().list(
            q=(f"name = '{CACHE_DIR}' and '{drive_id}' in parents and trashed = false "
               f"and mimeType = '{_FOLDER_MIME}'"),
            corpora="drive", driveId=drive_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="nextPageToken, files(id)", pageSize=100, pageToken=token).execute()
        ids.extend(f["id"] for f in resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids


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
    """Return [{'drive_id','drive_name','folder_ids','count'}] for drives that have
    at least one top-level .mcpbrain-cache/ folder. `folder_ids` lists EVERY such
    folder (duplicates included) and `count` sums children across all of them, so
    the report and a subsequent delete both cover duplicates. Per-drive failures
    are isolated."""
    out = []
    for d in list_shared_drives(service):
        did = d.get("id")
        if not did:
            continue
        name = d.get("name") or "<unnamed>"
        try:
            fids = _find_cache_folders(service, did)
            if not fids:
                continue
            count = sum(_count_children(service, fid) for fid in fids)
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            print(f"  ! {name} ({did}): scan failed: {exc}")
            continue
        out.append({"drive_id": did, "drive_name": name,
                    "folder_ids": fids, "count": count})
    return out


def delete_legacy(service, entries):
    """Delete EVERY .mcpbrain-cache/ folder (and its contents) for each entry,
    duplicates included. Per-folder isolation so one failed delete never aborts the
    rest. Returns the number of FOLDERS deleted."""
    deleted = 0
    for e in entries:
        drive_deleted = 0
        for fid in e["folder_ids"]:
            try:
                service.files().delete(fileId=fid, supportsAllDrives=True).execute()
            except Exception as exc:  # noqa: BLE001 — isolate one folder's failure
                print(f"  ! {e['drive_name']} ({e['drive_id']}): delete failed: {exc}")
                continue
            deleted += 1
            drive_deleted += 1
        if drive_deleted:
            dup = f" across {drive_deleted} folders" if drive_deleted > 1 else ""
            print(f"  ✓ deleted {CACHE_DIR}/ from {e['drive_name']} "
                  f"({e['drive_id']}) — {e['count']} artifact(s){dup}")
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
        dup = f", {len(e['folder_ids'])} folders" if len(e["folder_ids"]) > 1 else ""
        print(f"  - {e['drive_name']} ({e['drive_id']}): {e['count']} artifact(s){dup}")

    if not ns.delete_legacy:
        print("\nDry-run. Re-run with --delete-legacy to remove them "
              "(only after the whole fleet has updated).")
        return 0

    n = delete_legacy(service, entries)
    print(f"\nDeleted {n} legacy cache folder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
