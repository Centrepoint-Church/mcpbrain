# Centralize the shared-drive ingest cache into the Backups drive

**Date:** 2026-07-22
**Status:** Design — approved approach, pending spec review
**Owner:** Josh

## Problem

The shared-drive ingest cache (spec §A) writes a `.mcpbrain-cache/` folder to the
**root of every Google Shared Drive** the account can enumerate (`sync/drive.py`
`sync_shared_drives` → `drive_cache_storage(service, drive_id)`, rooted at the source
drive; artifacts addressed under `ingest_cache.CACHE_DIR = ".mcpbrain-cache"`).

That folder is visible to **every member of every team drive**. Team members don't
know what it is, and it reads as unexplained clutter (Google Drive can't hide a
dot-prefixed folder the way a Unix filesystem does).

**Goal:** stop putting mcpbrain folders in team drives. Consolidate all ingest-cache
artifacts into the single **"MCPBrain Backups" shared drive** (the drive that already
holds per-user encrypted backups + escrow keys), which all fleet members can access.

## Accepted trade-off (decided)

Today the cache lives *inside* the drive it describes, so **Drive membership is the
access control**: an artifact (extracted text + embeddings derived from a drive's docs)
is readable only by members of that drive. Centralizing into one drive means **anyone
who can open the Backups drive can read cached content derived from every source
drive**, including drives they aren't a member of.

This is accepted: all team members already have access to the Backups drive (so
cache-sharing still works), and removing the team-drive clutter is the priority. The
confidentiality change is understood and intended.

## Approach (chosen: A)

Move the cache **without touching `ingest_cache.py`**. The per-drive scoping comes
entirely from where the `fleet_storage` object is rooted plus the fixed `CACHE_DIR`
prefix. So we change only *what storage each drive is handed*: a storage rooted in the
Backups drive under a per-source-drive subfolder.

Each source drive still gets its **own** storage instance (its own subfolder), so
`list_paths(CACHE_DIR + "/")`, `gc_superseded`, `bootstrap_drive`, `sweep_drive`,
`remove_file_artifacts`, and revocation all keep working unchanged — they simply resolve
to a different physical location.

### Resulting layout

```
MCPBrain Backups (shared drive)/
├── mcpbrain-escrow/                       ← existing (backups + escrow keys)
└── ingest-cache/                          ← NEW top-level folder
    ├── <source_drive_A_id>/.mcpbrain-cache/<file_id>.<hash>.<pf8>.mbc.gz
    └── <source_drive_B_id>/.mcpbrain-cache/<file_id>.<hash>.<pf8>.mbc.gz
```

Team drives get **zero** mcpbrain folders going forward.

### Rejected alternatives

- **B — leave in-drive, rename + drop a README.** Doesn't remove the folder from team
  drives (Drive can't hide dotfolders); fails the goal.
- **C — target the org *fleet folder* instead of the Backups drive.** Mechanically
  identical to A; the Backups drive was specifically requested.

## Components

### 1. `DriveFleetStorage.base_path` (`mcpbrain/fleet_storage.py`)

Add an optional `base_path: str = ""` constructor param. When set, it is a `/`-separated
folder path prepended to **every** path the instance resolves, so the instance behaves as
if rooted at `<root>/<base_path>`.

- `_resolve_parent` / `_resolve_file`: prepend `base_path` components ahead of the
  caller's path components (subject to the same `create` flag — so reads don't create the
  base folder, writes do, matching existing `create_parents` behaviour).
- `list_paths(prefix)`: resolve under `<root>/<base_path>` and **strip the `base_path`
  prefix from every returned path**, so callers still receive paths relative to their own
  root (e.g. `.mcpbrain-cache/<file>`). This is essential — `_cache_names` parses returned
  paths expecting them to start with `CACHE_DIR/`.
- `delete` / `get_bytes` / `put_bytes`: no change beyond going through the updated
  `_resolve_*`.

`base_path=""` (default) is a no-op — existing `fleet_folder_storage` /
`drive_cache_storage` callers are unaffected.

### 2. Backups-drive resolver (`mcpbrain/backup.py`)

`backups_drive_id(home, drive_service, store) -> str | None`:

- Resolve the escrow **folder** id via the existing single source of truth
  `restore._escrow_folder(home)` (prefers `fleet.escrow_folder_id`, else
  `org_defaults.ESCROW_FOLDER_ID`).
- Resolve the folder's containing shared drive:
  `drive_service.files().get(fileId=<escrow_folder_id>, fields="driveId",
  supportsAllDrives=True)["driveId"]`.
- **Memoize** in the store `meta` table keyed by the escrow folder id (same lightweight
  meta pattern `note_drive_presence` uses) so it's a one-time API call, not per-cycle.
- Return `None` on any failure (no escrow folder, no `driveId`, API error) → caller falls
  back to in-drive storage (safe degradation).

### 3. Centralized cache factory (`mcpbrain/fleet_storage.py`)

`centralized_cache_storage(drive_service, backups_drive_id, source_drive_id)`:

```python
return DriveFleetStorage(
    drive_service, backups_drive_id, root_is_drive=True,
    base_path=f"ingest-cache/{source_drive_id}",
)
```

Keep `drive_cache_storage` (in-drive) as-is for the fallback path.

### 4. Config flag (`mcpbrain/config.py`)

`ingest_cache_central(home) -> bool`, **default True** (mirrors `ingest_cache_enabled`).
Org-config-flippable via `org-config.json` so the whole fleet can be reverted to in-drive
without a code change.

### 5. Wire the factory (`mcpbrain/sync/__init__.py`, `mcpbrain/onboarding.py`)

A single shared helper builds the storage_factory both call sites use:

```python
def _cache_storage_factory(home, drive_service, store):
    if config.ingest_cache_central(home):
        bdid = backup.backups_drive_id(home, drive_service, store)
        if bdid:
            return lambda d: centralized_cache_storage(drive_service, bdid, d)
    return lambda d: drive_cache_storage(drive_service, d)   # in-drive fallback
```

- `sync/__init__.py`: replace the inline
  `storage_factory=lambda d: drive_cache_storage(drive_service, d)` in `run_sync_cycle`.
- `onboarding.py`: `_default_make_drive_storage` uses the same helper so
  `bootstrap_drive` reads from the centralized location too. (When it degrades to
  in-drive, behaviour is exactly today's.)

### 6. One-shot legacy cleanup (`bin/relocate_ingest_cache.py`)

Consistent with `bin/consolidate.py` (attended admin migration, run once **after** the
fleet has updated).

- Default: **dry-run** — enumerate shared drives (`list_shared_drives`), report each
  drive that has a top-level `.mcpbrain-cache/` folder and its artifact count.
- `--delete-legacy`: delete the top-level `.mcpbrain-cache/` folder from each enumerated
  shared drive. Safe because the centralized location re-publishes any still-live doc on
  its next cache-miss (regeneration is cheap; no migration/copy needed).
- Per-drive isolation (one drive's failure never aborts others); logs what it deleted.
- **Run only after all installs are on the new wheel** — otherwise an old-version install
  recreates the folder on its next cycle. Documented in the script header and the runbook.

## Data flow (unchanged except root)

1. `run_sync_cycle` builds `storage_factory` via `_cache_storage_factory`.
2. Per drive, `sync_shared_drive` calls `try_import` (read central) then extracts misses.
3. `_publish_drive_misses` → `publish_file` writes to the centralized subfolder.
4. `gc_superseded_batch` / revocation / bootstrap operate on the same per-drive storage —
   scoped correctly because each drive's storage has its own `base_path`.

## Error handling / degradation

- Backups drive can't be resolved → `_cache_storage_factory` returns the in-drive factory;
  everything works exactly as today. No hard failure.
- `ingest_cache_central` off → in-drive, as today.
- Mixed-version fleet during rollout: old installs read/write in-drive, new installs
  read/write central. Each reads only its own location, so a doc may be extracted twice
  until everyone updates. **Transient and self-healing; no corruption.** (Distributed
  plugin — relocation reaches team members only when the wheel is released and daemons
  auto-update.)
- Legacy in-drive folders are **not** read by new installs once centralized; the cleanup
  script removes them.

## Testing

Scope runs to edited + directly-impacted files (Josh runs the full suite himself).

- `tests/test_fleet_storage_drive.py`: `base_path` prepends on write/read/delete; the
  `list_paths` **strip** returns caller-relative paths; `base_path=""` is a no-op.
- `tests/test_ingest_cache_*` (`roundtrip`, `lifecycle`, `revocation`): re-run against a
  `base_path`-rooted storage to prove publish → import → GC → revocation are unaffected by
  relocation (they should already pass unchanged since `ingest_cache.py` is untouched).
- `tests/test_backup.py` (or new `test_backups_drive_id`): `backups_drive_id` resolves via
  escrow folder `driveId`, memoizes in meta, returns `None` on failure.
- `tests/test_sync_cycle.py` / `tests/test_onboarding_bootstrap.py`: `_cache_storage_factory`
  picks central when a Backups drive resolves and the flag is on; falls back to in-drive
  otherwise.
- New `tests/test_relocate_ingest_cache.py`: dry-run reports; `--delete-legacy` deletes
  top-level `.mcpbrain-cache` per drive with per-drive isolation.

## Follow-up (non-blocking, flagged during design)

The Backups drive also holds `mcpbrain-escrow/<user>.key` — the admin recovery keys that
decrypt each user's backup. Since **all team members can open that drive**, anyone could
read those keys and decrypt anyone's backup. This predates this change but is worth
locking down (restrict the `mcpbrain-escrow/` folder's permissions to admins). Tracked
here as a follow-up, not part of this work.

## Out of scope

- Migrating (copying) existing artifacts to the new location — regeneration on cache-miss
  covers it; no copy step.
- Encrypting the ingest cache (it remains gzip-JSON; only its location changes).
- Changing the fleet-folder / org-graph snapshot subsystem (B/C) — untouched.
