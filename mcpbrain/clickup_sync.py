"""ClickUp ⇄ actions sync (simplified, single-user, single-list).

Model (see docs/superpowers/specs/2026-06-08-clickup-sync-design.md):
  - ClickUp is the editing surface and authoritative for edits (name, org, due,
    priority, status) — those mirror INTO the brain.
  - The brain creates tasks for new open actions and closes tasks when an action
    is closed locally. It does not otherwise push edits up.

The link anchor is the ClickUp "Brain ID" number custom field <-> actions.id,
cached on the action row as clickup_task_id for fast lookup.

`client` is injected (defaults to mcpbrain.clickup) so the logic is unit-tested
against a fake transport with no network.
"""
from __future__ import annotations

import logging

from mcpbrain import clickup as _clickup, config
from mcpbrain.chunking import action_fingerprint

log = logging.getLogger(__name__)


def _fp(text: str) -> str:
    return action_fingerprint(text or "")


def compose_description(action: dict) -> str:
    """Create-only task description: action text + Context + Source.

    Written once on create so notes added in ClickUp afterwards survive.
    """
    parts = []
    text = (action.get("text") or "").strip()
    if text:
        parts.append(text)
    ctx = (action.get("context_tag") or "").strip()
    if ctx:
        parts.append(f"Context: {ctx}")
    src = (action.get("source_doc_id") or "").strip()
    if src.startswith("gmail-"):
        mid = src.split("-")[1] if "-" in src else ""
        if mid:
            parts.append(f"Source: https://mail.google.com/mail/u/0/#inbox/{mid}")
    elif src:
        parts.append(f"Source: {src}")
    return "\n\n".join(parts)


def _apply_inbound(store, action: dict, task: dict) -> dict:
    """Mirror ClickUp-authoritative fields onto a brain action. Returns the diff
    actually applied (for logging/tests)."""
    diff: dict = {}
    aid = action["id"]

    # status — ClickUp is authoritative for edits, but a brain-local close must
    # propagate OUT (handled by sync's outbound close), not be reverted here.
    # The reopen signal is the closed->open TRANSITION: if we last synced the
    # task as closed (clickup_closed truthy) and it is open now, the user
    # reopened it, so reopen the action regardless of who closed it. If the task
    # is open but we never saw it closed, that is a brain-close whose outbound
    # close has not applied yet — leave it.
    a_done = (action.get("status") or "").lower() in ("done", "closed")
    prev_closed = bool(action.get("clickup_closed"))
    if task["closed"] and not a_done:
        store.set_action_status(aid, "done", "clickup")
        diff["status"] = "done"
    elif (not task["closed"]) and a_done and prev_closed:
        store.set_action_status(aid, "open", "")
        diff["status"] = "open"

    # name (authoritative)
    if task["name"] and task["name"] != (action.get("text") or ""):
        store.set_action_text(aid, task["name"])
        diff["text"] = task["name"]

    # plain columns
    fields = {}
    if task["org"] and task["org"] != (action.get("org") or ""):
        fields["org"] = task["org"]
    if (task["deadline"] or "") != (action.get("deadline") or ""):
        fields["deadline"] = task["deadline"]
    if task["priority"] and task["priority"] != (action.get("priority") or ""):
        fields["priority"] = task["priority"]
    if fields:
        store.update_action_fields(aid, **fields)
        diff.update(fields)
    # Record the observed ClickUp closed-state for next cycle's reopen check.
    if bool(task["closed"]) != prev_closed:
        store.set_action_clickup_closed(aid, bool(task["closed"]))
    return diff


def import_baseline(store, home, *, client=_clickup, dry_run: bool = True) -> dict:
    """One-off cutover: adopt the current ClickUp list as the brain's baseline.

    For each task: link to an existing action by exact normalised-text match
    (cache clickup_task_id, re-anchor Brain ID, mirror authoritative fields), or
    create a new action from the task. Idempotent: a re-run links already-linked
    tasks via their cached id and creates nothing new.

    dry_run=True returns the plan without writing. Returns
    {"link": [...], "create": [...]} of (task_id, action_id|None, name).
    """
    tasks = client.list_tasks_full(home, include_closed=True)
    actions = store.unified_actions()
    # First action per fingerprint that has no ClickUp link yet.
    fp_map: dict = {}
    linked_action_ids = set()
    for a in actions:
        if a.get("clickup_task_id"):
            linked_action_ids.add(a["id"])
            continue
        fp = a.get("text_fingerprint") or _fp(a.get("text", ""))
        if fp:
            fp_map.setdefault(fp, a)

    plan = {"link": [], "create": []}
    used = set()
    for t in tasks:
        # already linked to an action via cached native task id?
        existing = store.action_by_clickup_id(t["id"])
        if existing:
            plan["link"].append((t["id"], existing["id"], t["name"]))
            if not dry_run:
                _apply_inbound(store, existing, t)
            continue
        fp = _fp(t["name"])
        a = fp_map.get(fp)
        if a and a["id"] not in used:
            used.add(a["id"])
            plan["link"].append((t["id"], a["id"], t["name"]))
            if not dry_run:
                store.set_action_clickup_id(a["id"], t["id"])
                _apply_inbound(store, {**a, "clickup_task_id": t["id"]}, t)
        else:
            plan["create"].append((t["id"], None, t["name"]))
            if not dry_run:
                new_id = store.add_unified_action(
                    text=t["name"], owner=config.owner_name(home),
                    status="done" if t["closed"] else "open",
                    deadline=t["deadline"], org=t["org"], source="clickup",
                    text_fingerprint=_fp(t["name"]),
                    clickup_task_id=t["id"], priority=t["priority"])
                store.set_action_clickup_closed(new_id, bool(t["closed"]))
                if t["closed"]:
                    store.set_action_status(new_id, "done", "clickup")
    return plan


def _sync_floor(store) -> int:
    """Action-id cutover floor: outbound never pushes actions with id <= floor.

    Set once at baseline import to the pre-existing max action id so the Mac's
    pre-cutover email-extraction backlog (which the adopted ClickUp list already
    represents) is not re-pushed as duplicates. 0 = no floor.
    """
    try:
        return int(store.get_meta("clickup_sync_floor") or 0)
    except (TypeError, ValueError):
        return 0


def sync(store, home, *, client=_clickup) -> dict:
    """One ongoing sync cycle: inbound (authoritative) then outbound (create +
    close only). Returns a small summary dict."""
    summary = {"inbound": 0, "created": 0, "closed": 0}
    floor = _sync_floor(store)
    tasks = client.list_tasks_full(home, include_closed=True)
    tasks_by_id = {t["id"]: t for t in tasks}
    existing_fps = {_fp(t["name"]) for t in tasks}

    # 1. Inbound: mirror ClickUp -> brain for linked tasks (matched by the
    #    native task id cached on the action).
    for t in tasks:
        a = store.action_by_clickup_id(t["id"])
        if a is not None:
            if _apply_inbound(store, a, t):
                summary["inbound"] += 1

    # 2. Outbound create: open actions with no link and no matching task.
    for a in store.unified_actions(status="open"):
        if a.get("clickup_task_id"):
            continue
        if floor and a["id"] <= floor:
            continue  # pre-cutover backlog: adopted list already represents it
        if _fp(a.get("text", "")) in existing_fps:
            continue  # a task already represents this; don't duplicate
        created = client.create_task(
            home, name=a["text"],
            description=compose_description(a), deadline=a.get("deadline", ""),
            priority=a.get("priority", ""), org=a.get("org", ""))
        if created and created.get("id"):
            store.set_action_clickup_id(a["id"], created["id"])
            store.set_action_clickup_closed(a["id"], False)   # created open
            summary["created"] += 1

    # 3. Outbound close: action closed locally but its task still open.
    for a in store.unified_actions():
        tid = a.get("clickup_task_id")
        if not tid or (a.get("status") or "").lower() not in ("done", "closed"):
            continue
        t = tasks_by_id.get(tid)
        if t is not None and not t["closed"]:
            if client.close_task(home, tid):
                store.set_action_clickup_closed(a["id"], True)
                summary["closed"] += 1
    return summary
