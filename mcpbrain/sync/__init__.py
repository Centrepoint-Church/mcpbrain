from datetime import datetime, timedelta, timezone


def run_sync_cycle(store, embedder, *, gmail_service=None,
                   calendar_service=None, drive_service=None) -> dict:
    """Run a sync+embed cycle over whichever services are provided.

    For each provided service: run its source sync, then index_pending so the
    new chunks are embedded immediately. Returns counts: per-source items
    synced and total chunks embedded this cycle. Sources whose service is None
    are skipped.
    """
    from mcpbrain.index import index_pending
    from mcpbrain.sync.gmail import sync_gmail
    from mcpbrain.sync.calendar import sync_calendar
    from mcpbrain.sync.drive import sync_drive

    result = {"gmail": 0, "calendar": 0, "drive": 0, "embedded": 0}
    if gmail_service is not None:
        result["gmail"] = sync_gmail(gmail_service, store)
        result["embedded"] += index_pending(store, embedder)
    if calendar_service is not None:
        result["calendar"] = sync_calendar(calendar_service, store)
        result["embedded"] += index_pending(store, embedder)
    if drive_service is not None:
        result["drive"] = sync_drive(drive_service, store)
        result["embedded"] += index_pending(store, embedder)
    return result


def backfill_windows(
    now: datetime | None = None,
    recent_days: int = 30,
    window_days: int = 90,
    horizon_days: int = 1825,
) -> list[tuple[datetime, datetime]]:
    """Date windows for a recent-first backfill, newest first.

    Window 0 covers the most recent `recent_days`. Then successively older
    `window_days`-wide windows back to `horizon_days` ago. Windows are contiguous
    (each window's start equals the next window's end) and the last window's start
    is clamped to exactly the horizon. Recent data is enqueued first so search is
    useful immediately while older history backfills behind it.
    """
    if recent_days > horizon_days:
        raise ValueError(
            f"recent_days ({recent_days}) must be <= horizon_days ({horizon_days})")
    if now is None:
        now = datetime.now(timezone.utc)
    horizon = now - timedelta(days=horizon_days)
    windows: list[tuple[datetime, datetime]] = []
    recent_start = now - timedelta(days=recent_days)
    windows.append((recent_start, now))
    cursor = recent_start
    while cursor > horizon:
        start = cursor - timedelta(days=window_days)
        if start < horizon:
            start = horizon
        windows.append((start, cursor))
        cursor = start
    return windows


def gmail_query(start: datetime, end: datetime) -> str:
    """Gmail search query for a window. Gmail's `after:`/`before:` use YYYY/MM/DD."""
    return f"after:{start.strftime('%Y/%m/%d')} before:{end.strftime('%Y/%m/%d')}"


def initial_backfill(store, embedder, *, gmail_service=None, drive_service=None,
                     calendar_service=None, days: int = 10, now=None,
                     max_items: int | None = None) -> dict:
    """Bounded first-run population over the last `days`, then embed.

    Indexes recent Gmail (messages.list after:), Drive (files.list modifiedTime>),
    and Calendar (full fetch from time_min). Then runs index_pending once. Returns
    counts. `now` is injectable for deterministic tests.
    """
    from mcpbrain.index import index_pending
    from mcpbrain.sync.gmail import backfill_gmail
    from mcpbrain.sync.drive import backfill_drive
    from mcpbrain.sync.calendar import sync_calendar
    if now is None:
        now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    after_date = since.strftime("%Y/%m/%d")              # Gmail after:
    after_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")      # Drive / Calendar
    res = {"gmail": 0, "drive": 0, "calendar": 0, "embedded": 0}
    if gmail_service is not None:
        res["gmail"] = backfill_gmail(gmail_service, store, after_date, max_items)
    if drive_service is not None:
        res["drive"] = backfill_drive(drive_service, store, after_iso, max_items)
    if calendar_service is not None:
        res["calendar"] = sync_calendar(calendar_service, store, time_min=after_iso)
    res["embedded"] = index_pending(store, embedder)
    return res
