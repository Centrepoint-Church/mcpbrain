"""Thin ClickUp v2 REST client for the mcpbrain dashboard.

Uses stdlib urllib only — no requests dependency.

Auth: ClickUp v2 personal tokens (pk_*) are passed verbatim in the
``Authorization`` header. Token and list ID are read from
``~/.mcpbrain/config.json`` via ``config.read_config(home)``.

Both functions degrade gracefully: if the config is missing or incomplete
they return early with a safe empty value so the dashboard never hard-fails
due to a missing ClickUp config.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import config

log = logging.getLogger(__name__)

_BASE = "https://api.clickup.com/api/v2"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": token,
        "Content-Type": "application/json",
    }


def _ms_to_iso(ms_str: str | None) -> str:
    """Convert a ClickUp due_date (unix ms string) to YYYY-MM-DD, or '' if absent."""
    if not ms_str:
        return ""
    try:
        ts = int(ms_str) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return ""


def _iso_to_ms(iso: str) -> int:
    """Convert a YYYY-MM-DD string to unix milliseconds (start of day UTC)."""
    dt = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def search_tasks(home, *, due_date_lte: str | None = None) -> list[dict]:
    """Return open tasks from the configured ClickUp list.

    Args:
        home: Path to the mcpbrain config directory (passed to config.read_config).
        due_date_lte: Optional ISO date string (YYYY-MM-DD). When given, only tasks
            with a due date on or before this date are returned.

    Returns:
        List of dicts with keys: id, name, status, due_date, url.
        Returns [] if config is missing, creds are absent, or any HTTP error occurs.
    """
    cfg = config.read_config(home)
    token = cfg.get("clickup_api_key", "").strip()
    list_id = cfg.get("clickup_list_id", "").strip()
    if not token or not list_id:
        return []

    params: dict[str, str] = {"include_closed": "false"}
    if due_date_lte:
        params["due_date_lte"] = str(_iso_to_ms(due_date_lte))

    url = f"{_BASE}/list/{list_id}/task?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers(token))

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        log.warning("ClickUp search_tasks HTTP %s: %s", exc.code, exc.reason)
        return []
    except (urllib.error.URLError, OSError) as exc:
        log.warning("ClickUp search_tasks network error: %s", exc)
        return []

    tasks = []
    for t in data.get("tasks") or []:
        status_name = ""
        status_block = t.get("status")
        if isinstance(status_block, dict):
            status_name = status_block.get("status", "")
        elif isinstance(status_block, str):
            status_name = status_block

        tasks.append({
            "id": t.get("id", ""),
            "name": t.get("name", ""),
            "status": status_name,
            "due_date": _ms_to_iso(t.get("due_date")),
            "url": t.get("url", ""),
        })
    return tasks


def update_task_status(home, task_id: str, status: str) -> bool:
    """Update the status of a ClickUp task.

    Args:
        home: Path to the mcpbrain config directory.
        task_id: The ClickUp task ID.
        status: The new status string.

    Returns:
        True on success, False on any error.
    """
    cfg = config.read_config(home)
    token = cfg.get("clickup_api_key", "").strip()
    if not token:
        return False

    url = f"{_BASE}/task/{task_id}"
    body = json.dumps({"status": status}).encode()
    req = urllib.request.Request(url, data=body, headers=_headers(token), method="PUT")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as exc:
        log.warning("ClickUp update_task_status HTTP %s for task %s: %s", exc.code, task_id, exc.reason)
        return False
    except (urllib.error.URLError, OSError) as exc:
        log.warning("ClickUp update_task_status network error for task %s: %s", task_id, exc)
        return False
