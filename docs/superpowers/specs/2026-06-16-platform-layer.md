# Platform Layer — Org Fleet Visibility for mcpbrain

> Spec date: 2026-06-16. Baseline: mcpbrain 0.0.6 (autonomous, subscription-only).
> Covers roadmap items 6a (fleet view), 6b (per-user lifecycle + central config), 6c (support telemetry — deferred, in-person support sufficient), 6d (quota awareness via enrichment probe).

---

## Problem

Every mcpbrain install is an island. The maintainer has no cross-user visibility: no way to know who installed successfully, whose enrichment stalled, or whose daemon stopped. Supporting more than a handful of users without this visibility is blind.

---

## Scope

**In:**
- Per-user health beacon written to a Shared Drive fleet folder (6a)
- `mcpbrain fleet-report` CLI that aggregates beacons into a viewable HTML report (6a)
- Central org-config override file on the Shared Drive (6b)
- Enrichment staleness as quota/task-failure signal (6d)
- Backup escrow fix: repoint from personal Drive to Shared Drive (pre-existing bug)

**Out:**
- Support telemetry / remote log access (6c) — in-person support is sufficient
- Automated offboarding — removing from Claude Team is the only step required; the beacon goes stale naturally and the HTML report flags it
- An admin flag or separate install — Josh's install is identical to everyone else's

**Non-code prerequisite (DONE):**
Both Shared Drive subfolders exist on the Centrepoint Shared Drive:
- `mcpbrain-fleet/` — folder ID `1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o` → ships as the pre-filled default for `fleet.folder_id`.
- `mcpbrain-escrow/` — folder ID `1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi` → ships as the pre-filled default for `fleet.escrow_folder_id` (consumed by the backup-enable flow, replacing the personal-Drive auto-create bug).

---

## Architecture

### Shared Drive layout

All files live under a single `mcpbrain-fleet/` subfolder on the Centrepoint org Shared Drive (all employees have access via existing Google Workspace sharing):

```
mcpbrain-fleet/
  <user_email>.json       # per-user health beacon (written hourly by daemon)
  status.html             # aggregated fleet report (written by mcpbrain fleet-report)
  org-config.json         # central config overrides (admin edits manually)
mcpbrain-escrow/
  <user_email>.key        # backup escrow key (written at backup enable time)
```

The folder ID is stored in per-user config as `fleet.folder_id`. If absent, all fleet behaviour is silently skipped — non-org installs are unaffected.

### Config changes

`config.json` gains a `fleet` block holding **both** Shared Drive folder IDs:

```json
{
  "fleet": {
    "folder_id": "<Drive folder ID for mcpbrain-fleet subfolder>",
    "escrow_folder_id": "<Drive folder ID for mcpbrain-escrow subfolder>"
  }
}
```

Both are set during `mcpbrain setup` (wizard). **Config-merge safety:** `config.write_config` is a shallow merge (nested dicts are replaced wholesale), so to avoid two writers clobbering each other, the **wizard is the sole writer of the `fleet` block** (posts both keys together) and `backup_setup.enable_backup` only *reads* `fleet.escrow_folder_id` — it writes the runtime value into the existing `backup.shared_drive_id` field (fixing the pre-existing bug where it auto-created a personal-Drive folder). So the `fleet` block is the source of truth set by the wizard; `backup.shared_drive_id` is the derived runtime value.

---

## Components

### `mcpbrain/fleet.py` (new)

Four functions, each independently testable:

**`write_beacon(home, drive_service) → None`**
Calls `probes.all_connections(home, store=None)`, attaches `user_email` (from `config.owner_email`), `mcpbrain_version`, and `reported_at` (UTC ISO), and uploads the result as `<user_email>.json` to the fleet folder. Errors are logged and swallowed — a failed beacon write never affects the daemon.

**`read_org_config(home, drive_service) → dict`**
Downloads `org-config.json` from the fleet folder. Returns `{}` if the file is absent or the download fails. The daemon merges this into runtime config on startup.

**`generate_report(beacons: list[dict]) → str`**
Pure function — no Drive calls. Accepts a list of parsed beacon dicts, returns an HTML string: a table with one row per user, colour-coded probe cells (green = ok, amber = needs_action, grey = not_started), a ⚠️ "stale" badge on rows where `reported_at` is >48h old, and a "Last generated" timestamp at the top.

**`write_report(home, drive_service) → None`**
Lists all `*.json` files in the fleet folder (excluding `org-config.json`), parses them, calls `generate_report`, and uploads the result as `status.html`. Prints "No beacons found" and exits cleanly if the folder is empty.

### Beacon JSON format

```json
{
  "user_email": "john@centrepoint.church",
  "mcpbrain_version": "0.0.6",
  "reported_at": "2026-06-16T03:00:00Z",
  "probes": {
    "google":     {"state": "ok", "detail": "Connected"},
    "claude":     {"state": "ok"},
    "clickup":    {"state": "needs_action", "detail": "API key missing"},
    "backup":     {"state": "ok"},
    "records":    {"state": "ok"},
    "enrichment": {"state": "needs_action", "detail": "No enrichment in 48h"}
  }
}
```

Exactly `probes.all_connections()` output plus three top-level fields. No new probe logic required.

### `mcpbrain/agents.py` (modified)

New `_fleet_beacon_plist()` / `_fleet_beacon_schtasks_args()` generator pair (same pattern as existing prune + health cadences). Added to `install_cadences()` only when `fleet.folder_id` is configured; silently skipped otherwise. Cadence: hourly.

### `mcpbrain/daemon.py` (modified)

On startup: if `fleet.folder_id` is set, call `fleet.read_org_config(home, drive_service)` and merge the result into runtime config. Merge rules: org-config is merged shallowly into runtime config. Any key in the blocklist is silently dropped regardless of its value in org-config. Blocklisted keys (never overridable): `owner_email`, `owner_name`, `clickup_api_key`, `backup.escrow_key`, `fleet.folder_id`, `backup.shared_drive_id`, and any OAuth token fields. Unknown keys are ignored.

### `mcpbrain/cli.py` (modified)

New `fleet-report` subcommand. Builds `drive_service` from the user's OAuth token, calls `fleet.write_report(home, drive_service)`, and prints the Drive URL of `status.html`. Exits with a clear message if `fleet.folder_id` is not set.

### `mcpbrain/backup_setup.py` (modified)

`_resolve_shared_drive()` currently searches the user's personal Drive for a folder named `mcpbrain-escrow` — this is a bug. Fix: read `fleet.escrow_folder_id` from config (set during wizard setup) instead of a Drive search, and write the resolved value into `backup.shared_drive_id`. `_escrow_key_to_drive` uploads to that folder with `supportsAllDrives=True` so it reaches the Shared Drive. If `fleet.escrow_folder_id` is unset, raise a clear error.

### Wizard `index.html` (modified)

New optional "Fleet setup" section below backup:
- "Fleet folder ID" text input → saved to `fleet.folder_id`, **pre-filled with the org default** `1CI_oP_Ux6WxdHrIqTZkQKCPAgijZl19o` so a Centrepoint user just clicks through; editable/clearable for non-org installs.
- Help text: "This is the Centrepoint mcpbrain-fleet folder. Leave as-is, or clear it if you're not part of the org fleet."
- An "Escrow folder ID" input → saved to `fleet.escrow_folder_id`, **pre-filled with the org default** `1lSu2k70_0z6qDvKH2b_6Xi2CU3MI2sCi`. The wizard posts both fleet keys together (sole writer of the `fleet` block). The "Enable backup" flow then reads `fleet.escrow_folder_id` instead of auto-creating a personal Drive folder.

---

## Fleet report HTML

```
mcpbrain Fleet Status — generated 2026-06-16 11:47 AWST

User                        Ver    Last seen   Google  Claude  ClickUp  Backup  Records  Enrichment
john@centrepoint.church     0.0.6  2h ago      ✅      ✅      ⚠️       ✅      ✅       ⚠️
sarah@centrepoint.church    0.0.6  14h ago     ✅      ✅      ✅       ✅      ✅       ✅
mike@centrepoint.church     0.0.5  ⚠️ 3d ago   ✅      ✅      ✅       ❌      ✅       ✅
```

- ✅ = ok, ⚠️ = needs_action, ❌ = not_started, grey = unknown
- Rows with `reported_at` >48h show ⚠️ on "Last seen" — covers offboarded users and broken daemons
- No automatic cleanup of stale rows — admin deletes the JSON from Drive when appropriate

---

## Quota awareness (6d)

No new probe logic required. `probe_enrichment()` already returns `needs_action` when enrichment hasn't produced output recently. If the Cowork scheduled task stopped running (quota hit, laptop asleep, task deleted), enrichment goes stale within 48h and the fleet report flags it amber. This is the quota signal.

---

## Offboarding (6b)

The only required step is removing the user from Claude Team in claude.ai/settings. Their daemon continues to run but enrichment stops (no Claude subscription). Their beacon goes stale (>48h) and the fleet report shows them as stale. No mcpbrain-side offboarding code is required.

---

## Error handling

| Failure | Behaviour |
|---|---|
| Beacon write fails (Drive down, token expired) | Logged, swallowed. Daemon continues. |
| `fleet-report` — fleet folder empty | "No beacons found" message, no HTML written, exit 0 |
| `fleet-report` — `fleet.folder_id` not set | "fleet.folder_id not set — run mcpbrain setup to configure.", exit 1 |
| Org-config download fails | `read_org_config` returns `{}`, daemon uses local config |
| Org-config contains blocklisted key | Key silently ignored |
| Beacon JSON malformed | `write_report` skips that file, logs a warning |

---

## Testing

- `tests/test_fleet.py` — unit tests for `write_beacon` (mock Drive, assert JSON shape), `read_org_config` (missing file → `{}`; present → merged dict), `generate_report` (pure function, assert HTML contains user rows + colour classes + stale badge), org-config blocklist (blocklisted keys not applied)
- `tests/test_agents_cadence_xplat.py` — assert beacon cadence present when `fleet.folder_id` set, absent when not (Spec 1 owns this file; Spec 4 adds its Windows asserts in a separate new file to avoid collision)
- `tests/test_backup_setup.py` — assert escrow write uses configured folder ID, not a Drive search
- No integration test against live Drive — Drive calls are fully mockable at the `drive_service` boundary

---

## Suggested sequencing

1. `fleet.py` + unit tests (no daemon changes yet — validate the core logic first)
2. Wizard fleet folder ID fields + config write
3. Backup escrow fix (use configured folder ID)
4. Daemon: org-config read on startup
5. Daemon: beacon cadence (agents.py + daemon wiring)
6. CLI: `fleet-report` subcommand
7. Install skill (`plugin/skills/install/SKILL.md`): **all** edits to this file land here — (a) the fleet folder ID note, and (b) the onboarding copy delegated from Spec 4 #9 (Cowork "My Brain" project: exact name + instructions + the resolved `mcpbrain home` working-folder path as a single copy-paste). Consolidating both here keeps Spec 1 the sole editor of this file so the parallel worktrees never collide.

---

## Dependencies (for parallel-worktree execution)

**Files this worktree owns exclusively:** `mcpbrain/fleet.py` (new), `mcpbrain/daemon.py`, `mcpbrain/backup_setup.py`, `mcpbrain/wizard/index.html`, `mcpbrain/control_api.py` (if the enable endpoint needs the folder ID), new `tests/test_fleet.py`.

**Files SHARED with another spec (expect a small merge conflict):**
- `mcpbrain/cli.py` — Spec 1 adds the `fleet-report` subcommand; **Spec 3 adds `doctor`** to the same registration tuple + dispatch dict. Whichever merges second resolves a ~2-line conflict. No logic dependency — the adds are independent.
- `mcpbrain/agents.py` + `tests/test_agents_cadence_xplat.py` — owned by Spec 1 (beacon cadence). **Spec 4 deliberately does NOT extend this test file** (its Windows asserts live in a new file), so there is no conflict here despite the roadmap's original plan.
- `plugin/skills/install/SKILL.md` — **Spec 1 is the sole editor**; it carries Spec 4 #9's onboarding paragraph. Spec 4 does not touch this file.

**Depends on other specs' new code:** none. Builds entirely against current 0.0.6.

**Provides to other specs:** the consolidated `install/SKILL.md` edit (carries Spec 4's onboarding copy). No code symbols are consumed by other specs.

**Shared read-only:** `probes.all_connections` (also read by Specs 2 + 3; none of the three modify `probes.py`).
