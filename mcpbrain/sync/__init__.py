import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


# Progressive-backfill defaults. Tuned so backfill drains without blocking the
# live delta-sync that shares the same loop iteration:
#   - 90-day windows: small enough that one window fits in a sync cycle even
#     for high-volume mailboxes; large enough that a decade finishes in ~40 cycles
#   - 200 items per source per step: bounds Google API spend per cycle
# Stops only when a source has produced ZERO items for STOP_AFTER_EMPTY_WINDOWS
# consecutive windows — i.e. the daemon has walked past the start of that
# account's history. No fixed horizon: a 20-year-old Gmail account backfills
# in full, and once the floor passes the user's earliest message the empty-
# window counter trips and that source goes idle.
_BACKFILL_WINDOW_DAYS = 90
_BACKFILL_MAX_PER_SOURCE = 200
_STOP_AFTER_EMPTY_WINDOWS = 4   # 4 × 90d = ~1 year of empty before declaring done


def run_sync_cycle(store, embedder, *, gmail_service=None,
                   calendar_service=None, drive_service=None, home=None) -> dict:
    """Run a sync+embed cycle over whichever services are provided.

    For each provided service: run its source delta-sync, then index_pending
    so the new chunks are embedded immediately. After the live deltas, run
    one progressive-backfill step that walks one historical window per source
    (newest-to-oldest) so the corpus eventually contains everything. Live
    delta-sync runs FIRST every cycle so anything new always reaches the store
    before older history is processed. Returns counts: per-source items
    synced, backfill counts, and total chunks embedded this cycle.

    When `drive_service` and `home` are both given AND `config.ingest_cache_enabled(home)`
    AND `config.fleet_pin(home).is_pinned`, also runs the Shared Drive ingest-cache
    path (spec §A): `sync_shared_drives`, embed the misses, then `publish_file` each
    miss now that its vectors exist. Adds `"shared_drives"` (per-drive processed
    counts) and `"revoked_drives"` to the result.

    Strictly additive AND non-fatal: with `home=None` (every caller before this
    feature) this block never runs and existing behaviour for gmail/calendar/
    My-Drive sync is unchanged; and ANY exception raised anywhere inside the
    whole shared-drive block (including `sync_shared_drives` itself, e.g. a
    Drive-API outage in `list_shared_drives`) is caught, logged, and skipped for
    this cycle — it can never abort the gmail/calendar/My-Drive sync that ran
    before it, nor the backfill step/return that runs after it.
    """
    from mcpbrain.index import index_pending
    from mcpbrain.sync.gmail import sync_gmail
    from mcpbrain.sync.calendar import sync_calendar
    from mcpbrain.sync.drive import sync_drive

    result = {"gmail": 0, "calendar": 0, "drive": 0, "embedded": 0}
    if gmail_service is not None:
        result["gmail"] = sync_gmail(gmail_service, store)
        result["embedded"] += index_pending(store, embedder, home=home)
    if calendar_service is not None:
        result["calendar"] = sync_calendar(calendar_service, store)
        result["embedded"] += index_pending(store, embedder, home=home)
    if drive_service is not None:
        result["drive"] = sync_drive(drive_service, store)
        result["embedded"] += index_pending(store, embedder, home=home)

    # Shared Drive ingest cache (spec §A). Gated: needs a drive service, a home
    # to read config from, the cache enabled, and a fleet pin present. Without a
    # pin this is a no-op and drive sync behaves exactly as before. The WHOLE
    # block is wrapped in try/except so this optional feature can NEVER abort
    # the cycle — everything above (gmail/calendar/My-Drive) already ran, and
    # the progressive-backfill step + return below still run regardless of
    # whether this block succeeds, fails, or is skipped.
    if drive_service is not None and home is not None:
        try:
            from mcpbrain import config
            # Cheapest check first: ingest_cache_enabled is a single config-dict
            # read; fleet_pin additionally constructs a FleetPin object, so it's
            # only built once the cheaper check passes. is_pinned is checked last.
            ingest_cache_on = config.ingest_cache_enabled(home)
            pin = config.fleet_pin(home) if ingest_cache_on else None
            if ingest_cache_on and pin.is_pinned:
                from mcpbrain.sync.drive import sync_shared_drives
                from mcpbrain.fleet_storage import drive_cache_storage
                from mcpbrain import ingest_cache
                sd = sync_shared_drives(
                    drive_service, store, pin=pin,
                    storage_factory=lambda d: drive_cache_storage(drive_service, d))
                # Embed the misses, THEN publish them (publish reads vectors back).
                result["embedded"] += index_pending(store, embedder, home=home)
                # config.owner_email can return "" when unconfigured. Rather than
                # stamp published artifacts with an empty published_by, skip
                # publishing this cycle — files are still synced/embedded locally
                # either way, so nothing is lost, just not shared to the fleet yet.
                # Matches the codebase's existing precedent for a required-but-
                # unconfigured identity field (config.is_configured gates
                # enrichment entirely rather than substituting a placeholder).
                # Logged once per cycle here, not once per file/drive.
                published_by = config.owner_email(home)
                if not published_by:
                    log.warning(
                        "sync: owner_email unconfigured; shared-drive artifacts "
                        "will not be published to the fleet cache this cycle "
                        "(files still synced and embedded locally)")
                per_drive = {}
                for drive_id, info in sd.items():
                    if drive_id == "_revoked":
                        continue
                    fs = info["storage"]
                    if published_by:
                        for file_id, content_hash in info["miss"]:
                            try:
                                ingest_cache.publish_file(
                                    store, fs, drive_id, file_id, content_hash, pin,
                                    published_by=published_by)
                            except Exception as exc:  # noqa: BLE001 — publish is
                                # best-effort; a transient failure on one file must
                                # not abort the rest of the cycle
                                log.info("sync: publish_file skipped for drive %s file %s: %s",
                                         drive_id, file_id, exc)
                    per_drive[drive_id] = info["processed"]
                result["shared_drives"] = per_drive
                result["revoked_drives"] = sd.get("_revoked", [])
        except Exception as exc:  # noqa: BLE001 — optional feature; must never
            # abort the rest of the cycle (pre-existing sync + the subsequent
            # backfill step must run whether or not this succeeds)
            log.warning("sync: shared-drive block failed (skipped this cycle): %s", exc)

    # One backfill step per cycle, AFTER the live deltas. Bounded by
    # max_per_source so a slow cycle never starves new items.
    bf = progressive_backfill_step(
        store,
        gmail_service=gmail_service,
        drive_service=drive_service,
        calendar_service=calendar_service,
    )
    result["backfill"] = bf
    if any(bf.get(k, 0) for k in ("gmail", "drive", "calendar")):
        result["embedded"] += index_pending(store, embedder, home=home)
    return result


def _floor_dt(store, key: str, default: datetime) -> datetime:
    """Read a backfill-floor cursor as a tz-aware UTC datetime, or default."""
    raw = store.get_cursor(key)
    if not raw:
        return default
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def progressive_backfill_step(
    store,
    *,
    gmail_service=None,
    drive_service=None,
    calendar_service=None,
    window_days: int = _BACKFILL_WINDOW_DAYS,
    max_per_source: int = _BACKFILL_MAX_PER_SOURCE,
    stop_after_empty_windows: int = _STOP_AFTER_EMPTY_WINDOWS,
    now: datetime | None = None,
) -> dict:
    """Run ONE backfill window per source, walking newest -> oldest. No horizon.

    Each source maintains a "floor" cursor (`<source>_backfill_until`) holding
    the start-of-window for the next step (initial state: floor = now). Each
    call processes [floor - window_days, floor], advances the floor backward,
    and tracks consecutive empty windows in `<source>_backfill_empty`. When the
    empty counter reaches `stop_after_empty_windows` (default ~1 year), that
    source's `*_done` flag flips and subsequent calls are no-ops. This lets a
    20-year-old account backfill in full and naturally stop once the daemon
    has walked past the earliest message.

    Side effects: only `upsert_chunk` and `set_cursor` on backfill keys. Does
    NOT touch the live delta cursors (gmail historyId, drive pageToken,
    calendar syncToken). Errors per source are isolated — a failed window
    leaves that source's floor untouched so the next cycle retries it.
    """
    from mcpbrain.sync.gmail import backfill_gmail
    from mcpbrain.sync.drive import backfill_drive
    from mcpbrain.sync.calendar import backfill_calendar_window

    if now is None:
        now = datetime.now(timezone.utc)
    result = {"gmail": 0, "drive": 0, "calendar": 0,
              "gmail_done": False, "drive_done": False, "calendar_done": False}

    def _empty_count(key: str) -> int:
        raw = store.get_cursor(key)
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    def _step(floor_key: str, empty_key: str, run, done_key: str) -> int:
        if _empty_count(empty_key) >= stop_after_empty_windows:
            result[done_key] = True
            return 0
        end = _floor_dt(store, floor_key, default=now)
        start = end - timedelta(days=window_days)
        try:
            n = run(start, end)
        except Exception:  # noqa: BLE001 — one source's failure must not stall others
            return 0
        store.set_cursor(floor_key, start.isoformat())
        # Reset the empty counter on any hit; otherwise increment so a long
        # empty tail eventually trips the done flag.
        if n > 0:
            store.set_cursor(empty_key, "0")
        else:
            store.set_cursor(empty_key, str(_empty_count(empty_key) + 1))
            if _empty_count(empty_key) >= stop_after_empty_windows:
                result[done_key] = True
        return n

    if gmail_service is not None:
        def _gmail(start, end):
            return backfill_gmail(
                gmail_service, store,
                after=start.strftime("%Y/%m/%d"),
                before=end.strftime("%Y/%m/%d"),
                max_messages=max_per_source,
            )
        result["gmail"] = _step("gmail_backfill_until", "gmail_backfill_empty",
                                _gmail, "gmail_done")

    if drive_service is not None:
        def _drive(start, end):
            return backfill_drive(
                drive_service, store,
                modified_after=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                modified_before=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_files=max_per_source,
            )
        result["drive"] = _step("drive_backfill_until", "drive_backfill_empty",
                                _drive, "drive_done")

    if calendar_service is not None:
        def _cal(start, end):
            return backfill_calendar_window(
                calendar_service, store,
                time_min=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                time_max=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_events=max_per_source,
            )
        result["calendar"] = _step("calendar_backfill_until", "calendar_backfill_empty",
                                   _cal, "calendar_done")

    return result




def backfill_progress(store) -> dict:
    """Per-source indexing-backfill progress for the status UI.

    `reached` is the floor cursor (how far back this source has indexed; None if
    not started). `done` is True once the empty-window counter hit the stop
    threshold (the source has walked past its earliest item)."""
    out = {}
    for src in ("gmail", "drive", "calendar"):
        reached = store.get_cursor(f"{src}_backfill_until")
        try:
            empty = int(store.get_cursor(f"{src}_backfill_empty") or 0)
        except ValueError:
            empty = 0
        out[src] = {"reached": reached, "done": empty >= _STOP_AFTER_EMPTY_WINDOWS}
    return out
