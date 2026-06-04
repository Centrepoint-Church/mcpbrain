"""Dashboard data layer for the mcpbrain local dashboard.

Provides:
  - actions_today(store)   — bucketed actions from SQLite
  - calendar_today(home)   — today's Google Calendar events
  - clickup_today(home)    — today's ClickUp tasks due
  - mark_done(store, id)   — close an action
  - snooze(store, id, iso) — stub; not yet implemented
  - assemble(store, home)  — parallel fan-out returning all three + as_of
"""
from __future__ import annotations

import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain import clickup, config

log = logging.getLogger(__name__)

_PERTH = timezone(timedelta(hours=8))  # AWST — fixed UTC+8, no DST
_CAP = 20


def _today_perth() -> str:
    """Return today's date in Perth time as YYYY-MM-DD."""
    return datetime.now(_PERTH).date().isoformat()


def _now_iso() -> str:
    """Return current Perth time as a full ISO string."""
    return datetime.now(_PERTH).isoformat()


def _open_ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        import sqlite_vec  # noqa: PLC0415
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except Exception:
        pass  # sqlite_vec not installed or unavailable — degrade silently
    return db


def _row_to_action(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "text": row["text"],
        "deadline": row["deadline"] or "",
        "org": row["org"] or "",
        "project_id": row["project_id"] or "",
        "action_type": row["action_type"] or "next",
        "waiting_on": row["waiting_on"] or "",
        "source": row["source"] or "",
    }


def actions_today(store, owner: str | None = None) -> dict:
    """Bucket open actions into overdue / due_today / upcoming / blocked.

    Reads directly from store._path (read-only SQLite URI). Degrades
    gracefully if the actions table doesn't exist yet.

    owner: the dashboard user's name. Actions explicitly owned by someone
    else are excluded; ''/NULL owners are kept (extracted from the user's own
    inbox with no named owner, almost always theirs). None applies no filter.
    """
    today = _today_perth()
    path = store._path if hasattr(store, "_path") else store.path

    overdue: list[dict] = []
    due_today: list[dict] = []
    upcoming: list[dict] = []
    blocked: list[dict] = []

    try:
        db = _open_ro(path)
        try:
            # Check action_type column exists; use COALESCE either way so the
            # query works on older schemas without the column.
            cols = {r["name"] for r in db.execute("PRAGMA table_info(actions)").fetchall()}
            if "action_type" in cols:
                at_expr = "COALESCE(action_type, 'next') AS action_type"
            else:
                at_expr = "'next' AS action_type"
            if "waiting_on" in cols:
                wo_expr = "COALESCE(waiting_on,'') AS waiting_on"
            else:
                wo_expr = "'' AS waiting_on"

            owner_clause, params = "", []
            if owner is not None:
                owner_clause = (
                    " AND (owner IS NULL OR owner='' OR lower(owner)=lower(?))"
                )
                params.append(owner)
            rows = db.execute(
                f"SELECT id, text, COALESCE(deadline,'') AS deadline, "
                f"COALESCE(org,'') AS org, COALESCE(project_id,'') AS project_id, "
                f"{at_expr}, "
                f"{wo_expr}, COALESCE(source,'') AS source "
                f"FROM actions WHERE status='open'{owner_clause}",
                params,
            ).fetchall()
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        log.warning("actions_today: cannot read actions table: %s", exc)
        return {"overdue": [], "due_today": [], "upcoming": [], "blocked": []}

    for row in rows:
        action = _row_to_action(row)
        dl = action["deadline"]

        # blocked bucket (overlaps with others)
        if action["waiting_on"]:
            blocked.append(action)

        if dl and dl < today:
            overdue.append(action)
        elif dl == today:
            due_today.append(action)
        else:
            upcoming.append(action)

    # Sort and cap
    overdue.sort(key=lambda a: a["deadline"])
    overdue = overdue[:_CAP]

    due_today = due_today[:_CAP]

    # upcoming: sort by deadline ASC, empty deadline last
    upcoming.sort(key=lambda a: (a["deadline"] == "", a["deadline"]))
    upcoming = upcoming[:_CAP]

    blocked = blocked[:_CAP]

    return {
        "overdue": overdue,
        "due_today": due_today,
        "upcoming": upcoming,
        "blocked": blocked,
    }


def calendar_today(home) -> list[dict]:
    """Return today's Google Calendar events, parsed for the dashboard.

    Degrades to [] if calendar scope is missing or any error occurs.
    """
    try:
        # Late import so the module doesn't break if google-api-python-client
        # isn't installed in this environment.
        from mcpbrain import auth  # noqa: PLC0415

        try:
            services = auth.build_google_services(token_file=auth.token_path())
        except Exception as exc:
            log.warning("calendar_today: could not load Google credentials: %s", exc)
            return []

        if "calendar_service" not in services:
            log.info("calendar_today: calendar_service not available (scope missing or token absent)")
            return []

        service = services["calendar_service"]

        # Today's window in UTC. Perth is UTC+8, so midnight Perth = today 16:00 UTC previous day.
        today_perth = datetime.now(_PERTH).date()
        start_perth = datetime(today_perth.year, today_perth.month, today_perth.day,
                               0, 0, 0, tzinfo=_PERTH)
        end_perth = start_perth + timedelta(days=1)
        time_min = start_perth.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = end_perth.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=_CAP,
            )
            .execute()
        )

        events = []
        for item in result.get("items", []):
            start_block = item.get("start", {})
            end_block = item.get("end", {})

            all_day = "date" in start_block and "dateTime" not in start_block

            if all_day:
                start_str = ""
                end_str = ""
            else:
                raw_start = start_block.get("dateTime", "")
                raw_end = end_block.get("dateTime", "")
                # Parse to local (Perth) HH:MM
                try:
                    dt_start = datetime.fromisoformat(raw_start)
                    start_str = dt_start.astimezone(_PERTH).strftime("%H:%M")
                except (ValueError, TypeError):
                    start_str = ""
                try:
                    dt_end = datetime.fromisoformat(raw_end)
                    end_str = dt_end.astimezone(_PERTH).strftime("%H:%M")
                except (ValueError, TypeError):
                    end_str = ""

            events.append({
                "id": item.get("id", ""),
                "title": item.get("summary", "(no title)"),
                "start": start_str,
                "end": end_str,
                "all_day": all_day,
                "location": item.get("location", ""),
                "has_pack": False,
            })

        return events

    except Exception as exc:  # noqa: BLE001
        log.warning("calendar_today: unexpected error: %s", exc)
        return []


def clickup_today(home) -> list[dict]:
    """Return ClickUp tasks due today or earlier.

    Returns the search_tasks result directly (already [] on missing config).
    """
    today = _today_perth()
    return clickup.search_tasks(home, due_date_lte=today)


def mark_done(store, action_id: int) -> bool:
    """Mark a unified action as done. Returns True if a row was updated.

    Routes through Store.set_action_status so the write goes through the same
    connection discipline as every other store write (post-Phase-1 review fix:
    the original bare sqlite3.connect predated the store being passed whole
    to ControlServer).
    """
    try:
        return store.set_action_status(
            action_id, "done", resolved_by="dashboard", only_if_open=True) > 0
    except sqlite3.Error as exc:
        log.warning("mark_done: error updating action %s: %s", action_id, exc)
        return False


def snooze(store, action_id: int, until_iso: str) -> bool:
    """Stub — snoozed_until column not yet confirmed. Returns False."""
    log.info("snooze not yet implemented (action_id=%s, until=%s)", action_id, until_iso)
    return False


def changes_digest(store) -> dict:
    """Recent system writes + open findings for the dashboard Review card.

    Uses the Store methods directly: the dashboard runs in the daemon process
    (same writer), so this is safe; the read-only URI pattern in actions_today
    predates the store being passed in whole.
    """
    try:
        return {"changes": store.recent_changes(limit=20),
                "findings": store.open_findings()[:20]}
    except Exception as exc:  # noqa: BLE001 — degrade, never break the dashboard
        log.warning("changes_digest failed: %s", exc)
        return {"changes": [], "findings": []}


def assemble(store, home) -> dict:
    """Fan-out to all four data sources in parallel and return combined payload."""
    owner = config.owner_name(home)
    with ThreadPoolExecutor(max_workers=4) as pool:
        fut_actions = pool.submit(actions_today, store, owner)
        fut_calendar = pool.submit(calendar_today, home)
        fut_clickup = pool.submit(clickup_today, home)
        fut_digest = pool.submit(changes_digest, store)

        try:
            actions_result = fut_actions.result()
        except Exception as exc:
            log.warning("assemble: actions_today failed: %s", exc)
            actions_result = {"overdue": [], "due_today": [], "upcoming": [], "blocked": []}

        try:
            calendar_result = fut_calendar.result()
        except Exception as exc:
            log.warning("assemble: calendar_today failed: %s", exc)
            calendar_result = []

        try:
            clickup_result = fut_clickup.result()
        except Exception as exc:
            log.warning("assemble: clickup_today failed: %s", exc)
            clickup_result = []

        try:
            digest_result = fut_digest.result()
        except Exception as exc:
            log.warning("assemble: changes_digest failed: %s", exc)
            digest_result = {"changes": [], "findings": []}

    return {
        "actions": actions_result,
        "calendar": calendar_result,
        "clickup": clickup_result,
        "changes": digest_result["changes"],
        "findings": digest_result["findings"],
        "as_of": _now_iso(),
    }
