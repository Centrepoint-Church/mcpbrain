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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)

_BASE = "https://api.clickup.com/api/v2"

# --- two-way sync field map ---------------------------------------------------
# IDs are install-specific (discovered via the ClickUp API) and configured in
# config.json as clickup_org_options: {"orgname": "<uuid>", ...}.
# The link anchor is the NATIVE ClickUp task id (cached on the action as
# clickup_task_id) — no custom field is used for linking. Org is the one custom
# field we read/write (a genuine categorisation, not sync plumbing).
# ClickUp priority int (1=urgent..4=low) <-> brain priority name
_PRIORITY_INT = {"urgent": 1, "high": 2, "normal": 3, "low": 4}
_PRIORITY_NAME = {v: k for k, v in _PRIORITY_INT.items()}
# Default closed status — overridden per-install via config.clickup_closed_status(home).
_CLOSED_STATUS = "complete"   # the list's done-type status label


def _tz(tz: str):
    """Resolve an IANA tz string to a tzinfo, or None when unset/invalid."""
    if not tz:
        return None
    try:
        return ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — bad tz string => treat as unset
        return None


def deadline_to_due_ms(deadline: str | None, *, tz: str) -> int | None:
    """YYYY-MM-DD (tz midnight) -> epoch ms, or None for empty/invalid/unset tz."""
    z = _tz(tz)
    if not deadline or z is None:
        return None
    try:
        d = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=z)
    except ValueError:
        return None
    return int(d.timestamp() * 1000)


def due_ms_to_deadline(due_ms, *, tz: str) -> str | None:
    """epoch ms -> YYYY-MM-DD (tz), or None for falsy input or unset tz."""
    z = _tz(tz)
    if not due_ms or z is None:
        return None
    try:
        return datetime.fromtimestamp(int(due_ms) / 1000, tz=z).strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def priority_to_int(name: str | None) -> int | None:
    return _PRIORITY_INT.get((name or "").strip().lower())


def int_to_priority(value) -> str:
    """ClickUp priority (int or {priority/id}) -> brain priority name ('' if none)."""
    if value in (None, "", 0):
        return ""
    if isinstance(value, dict):
        value = value.get("id") or value.get("priority")
    try:
        return _PRIORITY_NAME.get(int(value), "")
    except (TypeError, ValueError):
        return ""


def org_to_option_id(org: str | None, org_options: dict | None = None) -> str | None:
    """Look up the ClickUp dropdown option id for an org name.

    org_options maps lowercased org name → option uuid (from config).
    Returns None when unmapped or when org_options is empty.
    """
    mapping = org_options or {}
    return mapping.get((org or "").strip().lower()) or None


def option_id_to_org(option_id, org_options: dict | None = None) -> str:
    """Reverse-map a ClickUp dropdown option id to a lowercased org name."""
    mapping = org_options or {}
    by_id = {v: k for k, v in mapping.items()}
    return by_id.get(option_id, "")


def status_is_closed(status_obj) -> bool:
    """True if a ClickUp status object is a done/closed type (label-agnostic)."""
    if isinstance(status_obj, dict):
        return (status_obj.get("type") or "").lower() in ("closed", "done")
    return False


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


def _iso_to_ms(iso: str, *, tz: str) -> int | None:
    """Convert a YYYY-MM-DD string to unix milliseconds at the END of that day (tz).

    due_date_lte means "due on or before this date" and ClickUp compares raw ms
    timestamps, so the cutoff must be the last instant of the day. A start-of-day
    cutoff would exclude tasks due later the same day.

    Returns None when tz is unset/invalid or the string is not a valid date.
    """
    z = _tz(tz)
    if z is None:
        return None
    try:
        day_start = datetime.strptime(iso, "%Y-%m-%d").replace(tzinfo=z)
        day_end = day_start + timedelta(days=1)
        return int(day_end.timestamp() * 1000) - 1
    except ValueError:
        log.warning("_iso_to_ms: invalid date string %r — skipping due_date_lte param", iso)
        return None


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
    token = config.clickup_api_key(home).strip()
    list_id = config.clickup_list_id(home).strip()
    if not token or not list_id:
        return []

    params: dict[str, str] = {"include_closed": "false"}
    if due_date_lte:
        ms = _iso_to_ms(due_date_lte, tz=config.user_timezone(home))
        if ms is not None:
            params["due_date_lte"] = str(ms)

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
    except json.JSONDecodeError as exc:
        log.warning("ClickUp search_tasks non-JSON response: %s", exc)
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
    token = config.clickup_api_key(home).strip()
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


# --- two-way sync transport -------------------------------------------------

def _api(token: str, method: str, path: str, body: dict | None = None,
         timeout: int = 15) -> tuple[dict | None, int | None]:
    """Issue a ClickUp REST call. Returns (parsed_json, status_code) on success,
    (None, status_code) on HTTP error, or (None, None) on network/parse error.

    Every failure is logged and swallowed so a sync pass degrades gracefully and
    never crashes the daemon loop. Callers that need to distinguish HTTP error
    codes (e.g. 422 custom-field rejection vs 401 auth failure) can inspect the
    returned status code.
    """
    url = f"{_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(token), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status_code = resp.status
        return (json.loads(raw) if raw else {}), status_code
    except urllib.error.HTTPError as exc:
        log.warning("ClickUp %s %s HTTP %s: %s", method, path, exc.code, exc.reason)
        return None, exc.code
    except (urllib.error.URLError, OSError) as exc:
        log.warning("ClickUp %s %s network error: %s", method, path, exc)
    except json.JSONDecodeError as exc:
        log.warning("ClickUp %s %s non-JSON response: %s", method, path, exc)
    return None, None


def _normalise_task(t: dict, org_field_id: str = "",
                    org_options: dict | None = None, tz: str = "") -> dict:
    """Flatten a raw ClickUp task into the fields the sync cares about.

    The link anchor is the native task id; no Brain ID custom field is read.
    """
    org = ""
    for cf in t.get("custom_fields") or []:
        if org_field_id and cf.get("id") == org_field_id:
            option_id = cf.get("value")
            org = option_id_to_org(option_id, org_options)
            if option_id and not org:
                log.warning(
                    "clickup: unknown org option_id %r — update clickup_org_options in config",
                    option_id,
                )
    assignees = [a.get("id") for a in (t.get("assignees") or [])]
    return {
        "id": t.get("id", ""),
        "name": t.get("name", ""),
        "closed": status_is_closed(t.get("status")),
        "status": (t.get("status") or {}).get("status", "") if isinstance(t.get("status"), dict) else "",
        "org": org,
        "priority": int_to_priority(t.get("priority")),
        "deadline": due_ms_to_deadline(t.get("due_date"), tz=tz),
        "assignees": assignees,
        "url": t.get("url", ""),
    }


def list_tasks_full(home, *, include_closed: bool = True) -> list[dict]:
    """Return all tasks on the configured list, normalised, paginated through.

    Returns [] if config is missing or any page errors.
    """
    token = config.clickup_api_key(home).strip()
    list_id = config.clickup_list_id(home).strip()
    if not token or not list_id:
        return []
    org_field = config.clickup_org_field_id(home).strip()
    org_options = config.clickup_org_options(home)
    tz = config.user_timezone(home)
    out, page = [], 0
    while True:
        params = {"include_closed": "true" if include_closed else "false",
                  "subtasks": "false", "page": str(page)}
        path = f"/list/{list_id}/task?" + urllib.parse.urlencode(params)
        data, _status = _api(token, "GET", path)
        if not data:
            break
        tasks = data.get("tasks") or []
        out.extend(_normalise_task(t, org_field, org_options, tz=tz) for t in tasks)
        if data.get("last_page") or len(tasks) == 0:
            break
        page += 1
        if page > 50:   # safety bound (5000 tasks)
            log.warning("ClickUp list_tasks_full hit page cap")
            break
    return out


def create_task(home, *, name: str, description: str = "",
                deadline: str = "", priority: str = "", org: str = "") -> dict | None:
    """Create a task on the configured list. The native task id it returns is
    the link anchor (caller caches it). Returns the created task dict (with
    'id') on success, None on failure."""
    token = config.clickup_api_key(home).strip()
    list_id = config.clickup_list_id(home).strip()
    if not token or not list_id:
        return None
    uid = config.clickup_user_id(home)
    body: dict = {"name": name, "assignees": [uid] if uid else []}
    org_field = config.clickup_org_field_id(home).strip()
    org_opt = org_to_option_id(org, config.clickup_org_options(home))
    if org_opt and org_field:
        body["custom_fields"] = [{"id": org_field, "value": org_opt}]
    if description:
        body["description"] = description
    due_ms = deadline_to_due_ms(deadline, tz=config.user_timezone(home))
    if due_ms is not None:
        body["due_date"] = due_ms
        body["due_date_time"] = False
    pri = priority_to_int(priority)
    if pri is not None:
        body["priority"] = pri
    created, status = _api(token, "POST", f"/list/{list_id}/task", body)
    if created is None and status == 422 and "custom_fields" in body:
        # A 422 indicates a rejected custom field (e.g. plan's custom-field-usage
        # quota). Retry without custom fields — the native task id is the link
        # anchor, Org is a nice-to-have. Do NOT retry on auth (401) or other
        # errors, since that would silently create unintended tasks.
        body.pop("custom_fields")
        log.warning("ClickUp create_task retrying without custom fields for %r (422)", name)
        created, _status = _api(token, "POST", f"/list/{list_id}/task", body)
    return created


def close_task(home, task_id: str) -> bool:
    """Set a task to the list's closed-type status. Returns True on success.

    The status label is read from config.clickup_closed_status(home) so each
    install can use its own done-type label (e.g. "done", "finished"). Falls
    back to the module-level _CLOSED_STATUS default ("complete") when home is
    not provided or the config key is absent.
    """
    token = config.clickup_api_key(home).strip()
    if not token:
        return False
    closed_status = config.clickup_closed_status(home) if home else _CLOSED_STATUS
    data, _status = _api(token, "PUT", f"/task/{task_id}", {"status": closed_status})
    return data is not None
