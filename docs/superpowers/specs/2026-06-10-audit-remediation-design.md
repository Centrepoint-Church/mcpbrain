# Audit Remediation — design

**Date:** 2026-06-10
**Status:** approved (brainstorm), pending spec review
**Owner:** Josh Kemp

## Goal

Fix every issue from the post-implementation audit of the productization series
(`2026-06-09-mcpbrain-productization-design.md`), critical through low, so the
shipped product actually meets its stated goals: silent auto-update that runs,
verified (not presence-only) connection status, multi-user-correct timezones, a
single-writer invariant that holds during backfill, and a tray that proactively
signals problems. The suite is green today (1223 passed) — these are gaps tests
don't cover, so each fix lands test-first.

## Resolved design decisions (from brainstorm)

- **Auto-update:** ON by default for configured installs, **daily**; restart runs
  **outside the write lock**.
- **Probes:** a **periodic verify cadence (~hourly) + cached result**; the 3s
  status poll stays cheap/offline.
- **Backfill:** **single-flight + pause the daemon's writes** during an in-process
  run (no second writer, no subprocess).
- **Timezone:** **required config** (wizard field); no silent default.

---

## A. Auto-update on by default + lock-safe restart (audit #1)

`maybe_auto_update` (daemon.py) currently sits in `_run_periodic_passes` inside
`with self._lock:`, and `auto_update_interval_s` defaults to `None` (OFF).

- **Default ON:** when the install `is_configured` and no explicit
  `cadences.auto_update_interval_s` is set, the effective interval is **86400s
  (daily)**. An explicit `0`/absent-with-unconfigured stays OFF. (Implement as a
  default in the interval resolution, not by mutating config.)
- **Lock-safe restart:** the in-loop pass only **detects** a newer version (cheap
  index check) and returns a signal; it does **not** run `uv tool install` /
  `restart_agent` while holding the lock. `run()` sees the signal, exits the
  `with self._lock:` block, and only then calls `update.update_from_index()`
  (which reinstalls + restarts). So the install/restart never overlaps a held
  write lock.
- **`maybe_auto_update` is exempt from the §E gate** — updates happen regardless
  of identity config.

## B. Periodic verify cadence + cached probes (audit #4)

Split the probe layer:

- **Cheap probes (every `status()` poll, offline):** presence + local file state +
  heartbeat *mtime/staleness* (a file read, not network). These run inline in
  `status()` as today.
- **Verified probes (network, cadence ~3600s):** a new `maybe_verify_connections`
  pass calls the expensive checks — ClickUp test API call (key + list resolve),
  Google token validity (a real refresh probe), backup snapshot age, Claude
  heartbeat freshness — and writes `<app_dir>/connections.json`
  (`{name: {state, detail, last_verified}}`) atomically.
- `probes.all_connections(home, store)` returns the **merge**: the cached verified
  result overlaid with cheap live state (e.g. presence flipping to `not_started`
  the instant a key is removed; heartbeat staleness computed live). Absent cache →
  fall back to cheap presence (so a fresh install still shows sane state before the
  first verify tick).
- **Freshness added:** `probe_claude` → `needs_action`/idle when the heartbeat is
  older than a window (default 14 days — covers users who open Claude weekly);
  `probe_backup` → `needs_action` when the snapshot is older than the backup
  interval (or 0 bytes); `probe_clickup` → `needs_action` when the verified call
  failed (revoked/typo'd key) or timezone is unset (see §D); `probe_google` →
  `needs_action` when the cached refresh probe failed.
- The verify cadence is also gated on `is_configured` (no point before setup).

## C. Backfill single-flight + pause daemon writes (audit #6)

- `Daemon.start_enrich_backfill` acquires a **non-blocking `_backfill_lock`**
  (mirroring `_auth_lock`); a second `/api/enrich-backfill/start` while one runs is
  a no-op.
- While a backfill runs, set a `_backfill_active` `threading.Event`; `run_one`
  returns early (like pause) when it's set, so the daemon's own write cycle does
  not overlap the backfill — only one writer at a time. Clear the flag + release
  the lock in a `finally`.
- The cancel flag (`enrich_backfill.request_cancel`) is unchanged and still
  honored in the loop.

## D. Timezone as required config (audit #2)

- New `config.user_timezone(home) -> str` reading the `timezone` key; **no
  default** (returns `""` when unset).
- `clickup.py` `deadline_to_due_ms` / `due_ms_to_deadline` / `_iso_to_ms` take the
  configured tz via `zoneinfo.ZoneInfo(config.user_timezone(home))`. The hardcoded
  `_PERTH` constant is removed. These functions gain a `home`/`tz` parameter
  (callers already have `home`).
- When `timezone` is unset, ClickUp deadline conversion returns `None` (no silent
  wrong-tz) and `probe_clickup` reports `needs_action` ("set your timezone").
- The wizard "About you" step gains a **required** timezone field (a `<select>` of
  IANA zones, defaulting the picker to the browser's detected zone for
  convenience, but the value must be saved to config). `saveProfile` posts
  `timezone`.

## E. Gate graph-writing cadences on is_configured (audit #5)

`_run_periodic_passes` and the separately-called `maybe_resolve` must skip the
graph-writers (`maybe_resolve`, `maybe_communities`, `maybe_proactive`,
`maybe_waiting_on`, `maybe_blocks`, `maybe_audit`, `maybe_clickup_sync`) when
`not config.is_configured(home)`. `maybe_auto_update` and
`maybe_verify_connections`… — auto_update runs always; verify_connections is
itself gated (§B). Implement as a single `is_configured` check that selects which
passes run, so the gate is visible in one place.

## F. Faithful `prune_hot_md` port (audit #3)

Replace the line-based `records_cadences.prune_hot_md` with a faithful port of the
**block-based** algorithm from `~/joshbrain/bin/prune_hot_md.py`: parse blank-line-
separated blocks so a multi-line dated entry (bullet + continuation lines) is
dropped/kept as a unit, collapse consecutive blank runs, and strip leading/trailing
blanks. Add a `--dry-run` flag (report count, write nothing) and append dropped
entries to `<app_dir>/logs/records_prune.log`. The `records-prune` subcommand still
commits via `records_write._commit_file`.

## G. Tray attention notification + icon state (audit #7)

`run_tray`'s loop:
- fire `icon.notify(detail, "mcpbrain")` on a **new** `attention()` entry (track
  `last_attention`), in addition to the existing review-count notification;
- tint the icon by `icon_state()` — `_make_icon_image(state)` returns blue
  (running), grey (paused), orange/red (attention), light-grey (unavailable);
  re-render `icon.icon` when the state changes.

## H. Low items

- **Update-channel guard:** `update._index_url()` keeps the `CHANGE-ME`
  placeholder default (maintainer deployment value), but `maybe_auto_update` /
  `update.main` detect an unconfigured (`CHANGE-ME`) URL and **log a clear
  warning + no-op** rather than fetching a bogus host; `probe`/status surface
  "update channel not configured". Documented in `docs/DISTRIBUTION.md`.
- **Version compare:** use `packaging.version.Version` (add `packaging` to
  dependencies — it's already present transitively) for `_latest_version` /
  `_should_update`, tolerant of pre-releases; the wheel filename scan still finds
  candidates, but comparison/sort is PEP 440.
- **Perf:** cache `ensure_records_repo` success per daemon process (a
  `set()`/flag keyed by repo path) so `drain` doesn't shell `git config` twice
  every cycle.
- **Tests:** add the integration coverage the audit flagged — auto-update
  detect-then-restart-outside-lock path, backfill single-flight + pause, the
  verify cadence writing/reading the cache, prune block behavior (multi-line
  entry dropped as a unit), tray notify-on-attention, timezone-driven ClickUp
  conversion.

---

## Data model / config changes

- `config.json` new keys: `timezone` (IANA string, required for ClickUp);
  optional `cadences.verify_interval_s` (default 3600 when configured) and the
  auto-update default (86400 daily) applied in interval resolution.
- New app-dir artifact: `connections.json` (verified-probe cache);
  `logs/records_prune.log`.

## New / changed code (by area)

- `daemon.py` — auto-update detect/signal split + lock-safe restart in `run()`;
  `maybe_verify_connections` cadence; `is_configured` gate over the graph-writers;
  `_backfill_lock` + `_backfill_active` in `start_enrich_backfill`/`run_one`;
  `verify_interval_s` wiring in `_cadences_from_config`/`apply_config`.
- `probes.py` — split cheap vs verified; cache read/merge; freshness windows.
- `config.py` — `user_timezone`; default-interval helpers.
- `clickup.py` — remove `_PERTH`; tz-parameterised conversions; needs-tz no-op.
- `records_cadences.py` — block-based prune + `--dry-run` + log.
- `update.py` — `packaging.version` compare; `CHANGE-ME` guard.
- `tray.py` — attention notify + state-tinted icon.
- `records.py`/`drain.py` — cache ensure_records_repo per process.
- `wizard/index.html` — required timezone field; post `timezone`.
- `pyproject.toml` — declare `packaging`.
- `docs/DISTRIBUTION.md` — index-URL setup + the guard's message.

## Testing

TDD per fix. Key new tests: auto-update OFF→default-on when configured + detect
signals without installing under lock; verify-cadence writes connections.json and
status() reads it; clickup conversion correct for a non-Perth tz and no-ops when
tz unset; gate skips graph-writers unconfigured; prune drops a multi-line block as
a unit + `--dry-run` writes nothing; tray fires notify on new attention;
backfill second start is a no-op and run_one skips while active.

## Risks / notes

- The auto-update lock-safe restart changes `run()`'s control flow — ensure the
  daemon still exits cleanly on stop and that a failed update doesn't wedge the
  loop (catch + log, continue).
- `packaging` must be a real dependency, not assumed transitive — declare it.
- Pausing the daemon during backfill means sync stalls for the backfill duration;
  acceptable for a one-shot catch-up, and the status home shows "backfilling".
