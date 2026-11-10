"""Apply, ask first or off, for every kind of automatic change.

Each writer the daemon runs by itself reads its mode with
config.auto_change_mode(home, kind) and branches:

- off: returns early, changing nothing
- ask: calls propose(), which queues a pending_changes row and a
  `pending_change` finding (Needs you). Nothing is written and no undoable
  change is recorded.
- apply: calls APPLY[kind], which makes the change and records the undoable
  change_log row (change_undo).

decide() settles a queued change: approving runs the same APPLY[kind] the
writer runs in apply mode, so the undoable change is recorded then; declining
leaves the data alone. Either way the pending_change finding is resolved with
a verdict, so it never reopens. An approval that stops part way is settled
'interrupted': Needs you offers Check it and Clear (clear()), never Apply.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from mcpbrain import config

log = logging.getLogger(__name__)

FINDING_TYPE = "pending_change"
KINDS = config.AUTO_CHANGE_KINDS

NOT_FOUND = "No such pending change"
ALREADY_DECIDED = "Already decided"
CHANGED_SINCE = "This has changed since it was proposed"

_PENDING, _APPLIED, _DECLINED = "pending", "applied", "declined"
# Claimed by one decide() while it runs, so two clicks can't both apply.
_DECIDING = "deciding"
# APPLY started and did not finish cleanly: part or all of the change may be
# made. Terminal: never offered for Apply again, only Check it and Clear.
_INTERRUPTED = "interrupted"
# An interrupted change the owner checked and marked handled.
_CLEARED = "cleared"
INTERRUPTED = "This may have been applied, check it before doing anything else"
# Fixed words for a failure the owner can only retry; the exception itself
# goes to the log and the row's error column, never to the page.
DECLINE_FAILED = "That couldn't be declined. Try again in a moment."
APPROVE_FAILED = "That couldn't be finished. Try again in a moment."
NOT_INTERRUPTED = "Only a change that stopped part way can be cleared"
# Status writes that must not be lost are retried with these pauses (seconds).
_RETRY_PAUSES = (0.05, 0.2, 0.5)


class NotApplied(Exception):
    """An APPLY function made no change; the message says why."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _payload_text(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


# --- queue -------------------------------------------------------------------

def propose(store, kind: str, target: str, summary: str, payload: dict) -> int:
    """Queue an automatic change for the owner's review. Returns its id.

    One pending row per (kind, target): proposing the same target again
    replaces the queued payload and summary (the newer proposal is the one to
    review). A graph tidy-up the owner already declined is not queued again
    (its target names one decision: a finding or a merge pair); the declined
    row's id is returned and nothing is added.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown automatic change kind: {kind!r}")
    target = str(target)
    text = _payload_text(payload)
    with store._connect(write=True) as db:
        row = db.execute(
            "SELECT id FROM pending_changes WHERE kind=? AND target=? AND status=? "
            "ORDER BY id DESC LIMIT 1", (kind, target, _PENDING)).fetchone()
        if row is not None:
            db.execute("UPDATE pending_changes SET payload=?, summary=? WHERE id=?",
                       (text, summary, row["id"]))
            pid = row["id"]
        else:
            if kind == "tidy":
                declined = db.execute(
                    "SELECT id FROM pending_changes WHERE kind=? AND target=? AND status=? "
                    "ORDER BY id DESC LIMIT 1", (kind, target, _DECLINED)).fetchone()
                if declined is not None:
                    return declined["id"]
            pid = db.execute(
                "INSERT INTO pending_changes(kind, target, summary, payload, status) "
                "VALUES(?,?,?,?,?)", (kind, target, summary, text, _PENDING)).lastrowid
    store.record_finding(FINDING_TYPE, str(pid), summary=summary, severity="info")
    return pid


def pending_targets(store, kind: str, *, include_declined: bool = False) -> set[str]:
    """Targets of `kind` waiting for review (and, optionally, declined), so a
    builder can skip asking the LLM about them again."""
    statuses = (_PENDING, _DECIDING, _INTERRUPTED)
    if include_declined:
        statuses += (_DECLINED,)
    marks = ",".join("?" * len(statuses))
    with store._connect() as db:
        return {r[0] for r in db.execute(
            f"SELECT target FROM pending_changes WHERE kind=? AND status IN ({marks})",
            (kind, *statuses))}


def list_pending(store) -> list[dict]:
    """Changes waiting for the owner, oldest first: queued ones (status
    'pending': Apply or Decline) and ones that stopped part way (status
    'interrupted': Check it, which opens `check`, or Clear)."""
    with store._connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT id, kind, target, summary, payload, status, created_at FROM pending_changes "
            "WHERE status IN (?, ?) ORDER BY id", (_PENDING, _INTERRUPTED)).fetchall()]
    out = []
    for r in rows:
        payload = r.pop("payload")
        if r["status"] == _INTERRUPTED:
            try:
                r["check"] = _check_target(store, r["kind"], r["target"],
                                           json.loads(payload or "{}"))
            except Exception:  # noqa: BLE001 -- Settings is always somewhere to look
                r["check"] = _SETTINGS_CHECK
        out.append(r)
    return out


_SETTINGS_CHECK = {"href": "/settings#automatic", "label": "Automatic changes in Settings"}


def _entity_check(store, eid) -> dict:
    eid = str(eid or "")
    if not eid:
        return _SETTINGS_CHECK
    try:
        e = store.get_entity(eid)
    except Exception:  # noqa: BLE001
        e = None
    if not e:
        return _SETTINGS_CHECK
    return {"href": "/graph?entity=" + eid, "label": (e.get("name") or "the entity") + " in the graph"}


def _check_target(store, kind: str, target: str, payload: dict) -> dict:
    """What an interrupted change touched, for its Check it action: a graph
    entity ({href, label}), a records file ({file}), or Settings."""
    if kind == "profiles":
        return _entity_check(store, payload.get("entity_id") or target)
    if kind == "circles":
        return {"href": "/graph?view=circles", "label": "Circles in the graph"}
    if kind == "voice":
        return {"file": "context/voice.md"}
    if kind == "reference":
        lane, name = payload.get("lane"), str(payload.get("filename") or "")
        if lane in ("reference", "context") and name and "/" not in name and "\\" not in name:
            return {"file": f"{lane}/{name}"}
        return _SETTINGS_CHECK
    if kind == "tidy":
        if target.startswith("pair:"):
            return _entity_check(store, target[5:].split("|")[0])
        if (payload.get("block") or "") != "review_ownerless":
            f = store.get_finding((payload.get("verdict") or {}).get("finding_id"))
            if f:
                return _entity_check(store, f["ref_id"])
    return _SETTINGS_CHECK


def _close_finding(store, pid: int, verdict: str) -> None:
    with store._connect() as db:
        row = db.execute("SELECT id FROM proactive_findings WHERE finding_type=? AND ref_id=?",
                         (FINDING_TYPE, str(pid))).fetchone()
    if row is not None:
        store.resolve_finding(row["id"], verdict=verdict)


def decide(store, home, pending_id: int, *, approve: bool) -> dict:
    """Approve (apply through APPLY[kind]) or decline one queued change.

    Returns {"ok": bool, "reason": str} (plus "status": "interrupted" when an
    approval stopped part way). An approval whose APPLY raises NotApplied
    (nothing changed: the item changed since, a guard refused) leaves the
    change pending with the reason, so the owner can decline it.

    The row is claimed ('deciding') so two clicks can't both apply it. Once
    APPLY has started, any other exception settles it 'interrupted' (Check it
    or Clear, never Apply again). A failed decline goes back to 'pending'; a
    crash mid-decision is settled by recover_stale()."""
    home = str(home)
    with store._connect(write=True) as db:
        row = db.execute("SELECT * FROM pending_changes WHERE id=?",
                         (int(pending_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": NOT_FOUND}
        claimed = db.execute(
            "UPDATE pending_changes SET status=?, decided_at=? WHERE id=? AND status=?",
            (_DECIDING, _now(), row["id"], _PENDING)).rowcount
        if not claimed:
            return {"ok": False, "reason": ALREADY_DECIDED}
    row = dict(row)
    try:
        return _decide_claimed(store, home, row, approve)
    except Exception as exc:  # noqa: BLE001 -- never leave the row claimed
        log.exception("auto_changes: deciding pending change %s failed", row["id"])
        if not approve and _status(store, row["id"]) == _DECLINED:
            # The decline itself was saved; only a later step failed (logged).
            return {"ok": True, "reason": ""}
        _release(store, row["id"], f"Failed: {exc}")
        return {"ok": False, "reason": APPROVE_FAILED if approve else DECLINE_FAILED}


def _status(store, pid: int) -> str:
    try:
        with store._connect() as db:
            r = db.execute("SELECT status FROM pending_changes WHERE id=?", (pid,)).fetchone()
        return r["status"] if r is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _decide_claimed(store, home: str, row: dict, approve: bool) -> dict:
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    if not approve:
        _settle(store, row["id"], _DECLINED, None)
        _close_finding(store, row["id"], "declined")
        _on_decline(store, row["kind"], payload)
        return {"ok": True, "reason": ""}
    # While APPLY runs, every change it records also marks this row applied in
    # the change's own transaction (store.SETTLING_PENDING), so a failure after
    # the change lands never returns the row to the queue.
    from mcpbrain.store import SETTLING_PENDING
    token = SETTLING_PENDING.set(row["id"])
    try:
        change_id = APPLY[row["kind"]](store, home, payload)
    except NotApplied as exc:
        reason = str(exc) or "Nothing to change"
        if _landed(store, row["id"]):
            return _interrupt(store, row["id"], f"Stopped part way: {reason}")
        _release(store, row["id"], reason)
        return {"ok": False, "reason": reason}
    except Exception as exc:  # noqa: BLE001 -- part of it may be made: never Apply again
        log.exception("auto_changes: applying pending change %s stopped part way", row["id"])
        return _interrupt(store, row["id"], f"Stopped part way: {exc}")
    finally:
        SETTLING_PENDING.reset(token)
    return _finish_applied(store, row["id"], change_id, "")


def _landed(store, pid: int) -> bool:
    """Did a change APPLY recorded already mark this row applied?"""
    return _status(store, pid) == _APPLIED


def _finish_applied(store, pid: int, change_id, error: str) -> dict:
    """The change is made: settle the row applied and close its finding. If
    that bookkeeping fails, the row is still settled applied with the error
    noted; it is never returned to pending, so it can't be applied twice."""
    try:
        _settle(store, pid, _APPLIED, change_id or None, error)
    except Exception as exc:  # noqa: BLE001
        log.exception("auto_changes: recording applied change %s failed", pid)
        _mark_applied(store, pid, f"Applied, but recording it failed: {exc}")
    try:
        _close_finding(store, pid, "applied")
    except Exception:  # noqa: BLE001 -- the change stands; the finding can be closed by hand
        log.exception("auto_changes: closing the finding for change %s failed", pid)
    return {"ok": True, "reason": ""}


def _settle(store, pid: int, status: str, change_id, error: str = "") -> None:
    with store._connect(write=True) as db:
        db.execute("UPDATE pending_changes SET status=?, decided_at=?, "
                   "change_log_id=COALESCE(?, change_log_id), error=? WHERE id=?",
                   (status, _now(), change_id, error[:500], pid))


def _write_status(store, sql: str, params: tuple, what: str) -> bool:
    """Run one status write, retried with short pauses. True if it ran."""
    for pause in (*_RETRY_PAUSES, None):
        try:
            with store._connect(write=True) as db:
                db.execute(sql, params)
            return True
        except Exception:  # noqa: BLE001
            if pause is None:
                log.exception("auto_changes: could not %s", what)
                return False
            time.sleep(pause)
    return False


def _mark_applied(store, pid: int, error: str) -> None:
    """Last resort after a failed _settle: status and error only, retried. If
    it still fails the row stays 'deciding', and recover_stale() settles it
    'interrupted' at the next start, never 'pending'."""
    _write_status(store, "UPDATE pending_changes SET status=?, decided_at=?, error=? WHERE id=?",
                  (_APPLIED, _now(), error[:500], pid), f"mark pending change {pid} applied")


def _interrupt(store, pid: int, error: str) -> dict:
    """APPLY stopped part way: settle the row 'interrupted' with the error
    (retried; if it still fails, recover_stale() settles it at the next
    start). Its finding stays open, so Needs you keeps it until Clear."""
    _write_status(store, "UPDATE pending_changes SET status=?, decided_at=?, error=? "
                  "WHERE id=? AND status IN (?, ?)",
                  (_INTERRUPTED, _now(), error[:500], pid, _DECIDING, _APPLIED),
                  f"mark pending change {pid} interrupted")
    return {"ok": False, "reason": INTERRUPTED, "status": _INTERRUPTED}


def clear(store, pending_id: int) -> dict:
    """The owner checked an interrupted change: mark it handled ('cleared')
    and close its finding. Returns {"ok": bool, "reason": str}."""
    with store._connect(write=True) as db:
        row = db.execute("SELECT id FROM pending_changes WHERE id=?",
                         (int(pending_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": NOT_FOUND}
        n = db.execute("UPDATE pending_changes SET status=?, decided_at=? WHERE id=? AND status=?",
                       (_CLEARED, _now(), row["id"], _INTERRUPTED)).rowcount
    if not n:
        return {"ok": False, "reason": NOT_INTERRUPTED}
    _close_finding(store, row["id"], "cleared")
    return {"ok": True, "reason": ""}


def _release(store, pid: int, error: str) -> None:
    """Put a claimed row back in the queue with why it didn't go through.
    Best effort: if even this write fails, recover_stale() picks it up."""
    try:
        with store._connect(write=True) as db:
            db.execute("UPDATE pending_changes SET status=?, decided_at='', error=? "
                       "WHERE id=? AND status=?", (_PENDING, error[:500], pid, _DECIDING))
    except Exception:  # noqa: BLE001
        log.exception("auto_changes: could not release pending change %s", pid)


def recover_stale(store, *, claimed_before: str | None = None,
                  older_than_s: float | None = None) -> int:
    """Settle rows stuck in 'deciding' (a crash or a failed status write
    between claim and settle). A row with a claim time had its decision
    started, so part of the change may be made: it becomes 'interrupted'
    (Check it or Clear), never 'pending'. A row with no claim time (written
    before claims were stamped) goes back to 'pending'. Only rows claimed
    before `claimed_before` (an ISO UTC stamp, as _now() writes) or more than
    `older_than_s` seconds ago are touched, so a decision in flight right now
    is left alone; with neither, every such row. The daemon calls this at
    startup with its own start time. Returns the count."""
    if older_than_s is not None:
        cutoff = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - older_than_s, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        claimed_before = min(claimed_before, cutoff) if claimed_before else cutoff
    started = ("UPDATE pending_changes SET status=?, "
               "error='Interrupted before it finished' "
               "WHERE status=? AND COALESCE(decided_at,'')!=''")
    started_params: list = [_INTERRUPTED, _DECIDING]
    if claimed_before is not None:
        started += " AND decided_at < ?"
        started_params.append(claimed_before)
    with store._connect(write=True) as db:
        n = db.execute(started, started_params).rowcount
        n += db.execute("UPDATE pending_changes SET status=?, decided_at='' "
                        "WHERE status=? AND COALESCE(decided_at,'')=''",
                        (_PENDING, _DECIDING)).rowcount
    return n

def _on_decline(store, kind: str, payload: dict) -> None:
    """Make a decline stick where the source would otherwise re-queue it:

    - tidy: close the review finding the verdict came from, with a verdict, so
      the daily review neither re-asks the AI nor re-queues it.
    - voice: mark those suggestions declined in their own table, so the next
      cycle doesn't queue them again.
    """
    if kind == "voice":
        ids = [int(i) for i in (payload.get("suggestion_ids") or [])
               if str(i).lstrip("-").isdigit()]
        if ids:
            marks = ",".join("?" * len(ids))
            with store._connect(write=True) as db:
                db.execute(f"UPDATE voice_suggestions SET status='declined' "
                           f"WHERE status='pending' AND id IN ({marks})", ids)
        return
    if kind != "tidy":
        return
    fid = (payload.get("verdict") or {}).get("finding_id")
    if fid is not None:
        try:
            store.resolve_finding(int(fid), verdict="declined_by_human")
        except (TypeError, ValueError):
            pass


# --- apply functions: one per kind ---------------------------------------------
# Each takes (store, home, payload), makes the change the writer would make in
# apply mode, and returns the change_log id of the undoable change. NotApplied
# means nothing was changed.

def _apply_profiles(store, home, payload) -> int:
    from mcpbrain.profile_synth import write_profile
    cid = write_profile(store, payload.get("entity_id") or "", payload.get("profile") or "",
                        payload.get("updated_at") or datetime.now(timezone.utc).isoformat(),
                        source="profile_synthesis")
    if not cid:
        raise NotApplied("That person is no longer in the graph")
    return cid


def _apply_circles(store, home, payload) -> int:
    from mcpbrain.community_synth import set_community_title
    cid = set_community_title(store, payload.get("community_id"), payload.get("title") or "",
                              payload.get("summary"), source="community_synthesis")
    if not cid:
        raise NotApplied("That circle no longer exists")
    return cid


def _apply_reference(store, home, payload) -> int:
    from pathlib import Path
    from mcpbrain import tools
    if "base" in payload:
        # Queued as a whole-file rewrite of what the file held then; writing it
        # over a later edit would silently throw that edit away.
        _repo, _rel, target = tools._gardener_target(home, payload)
        current = Path(target).read_text() if Path(target).is_file() else None
        if current != payload["base"]:
            raise NotApplied(CHANGED_SINCE)
    try:
        committed, cid = tools.gardener_write(store, home, payload)
    except FileNotFoundError as exc:
        log.info("auto_changes: reference change not applied: %s", exc)
        raise NotApplied("That file isn't in the records folder any more") from exc
    except ValueError as exc:
        log.info("auto_changes: reference change not applied: %s", exc)
        raise NotApplied("That file can't be changed this way") from exc
    if not committed:
        raise NotApplied("The file already says this")
    return cid


def _apply_voice(store, home, payload) -> int:
    from mcpbrain.voice_apply import apply_suggestions
    out = apply_suggestions(store, home, suggestion_ids=payload.get("suggestion_ids"))
    if out.get("blocked"):
        log.info("auto_changes: voice change not applied: %s", out["blocked"])
        blocked = str(out["blocked"])
        raise NotApplied("Voice changes are cooling down; try again in a few days"
                         if blocked.startswith("cooldown") else
                         "Voice changes are switched off" if "not enabled" in blocked else
                         "These voice suggestions are no longer waiting"
                         if "no pending" in blocked else
                         "The voice file couldn't be updated")
    if not out.get("applied"):
        raise NotApplied("These voice suggestions are no longer waiting")
    return out.get("change_id") or 0


def _apply_consolidation(store, home, payload) -> int:
    from mcpbrain import consolidation
    cid = consolidation.apply_proposal(store, home, list(payload.get("doc_ids") or []),
                                       payload.get("summary") or "",
                                       standing=payload.get("standing") is True,
                                       title=payload.get("title") or "",
                                       description=payload.get("description") or "")
    if not cid:
        raise NotApplied(CHANGED_SINCE)
    return cid


def _apply_draft_style(store, home, payload) -> int:
    from mcpbrain import threshold_bandit
    return threshold_bandit.apply_threshold(store, home, float(payload["recommended"]))


# --- graph tidy-up ---------------------------------------------------------------
# The daily review and the merge adjudication push verdicts in blocks; each
# block has its own applier (review_apply / org_curate). A tidy pending row
# holds one verdict and the block it came from.

def _appliers() -> dict[str, Callable]:
    """block -> applier(store, verdicts, cap, home, change_ids=None). change_ids,
    when given, receives the change_log id of every change the applier made."""
    from mcpbrain import org_curate, review_apply

    def _ids(change_ids):
        return {} if change_ids is None else {"change_ids": change_ids}
    return {
        "review_orphan": lambda s, v, cap, home, ids=None: review_apply.apply_orphan_verdicts(
            s, v, cap=cap, **_ids(ids)),
        "review_missing_org": lambda s, v, cap, home, ids=None:
            review_apply.apply_missing_org_verdicts(s, v, cap=cap, home=home, **_ids(ids)),
        "review_ownerless": lambda s, v, cap, home, ids=None:
            review_apply.apply_ownerless_verdicts(s, v, cap=cap, **_ids(ids)),
        "review_org": lambda s, v, cap, home, ids=None: review_apply.apply_org_verdicts(
            s, v, cap=cap, home=home, **_ids(ids)),
        "merge_answers": lambda s, v, cap, home, ids=None:
            review_apply.apply_duplicate_verdicts(s, v, cap=cap, **_ids(ids)),
        "org_merge_review": lambda s, v, cap, home, ids=None:
            org_curate.apply_org_merge_answers(s, v, cap=cap, **_ids(ids)),
    }


# The applier result keys that count a write against the per-run cap.
_SPENT_KEYS = ("suppressed", "assigned", "owner_assigned", "canonicalized", "suggested",
               "merged")


TIDY_BLOCKS = ("review_orphan", "review_missing_org", "review_ownerless", "review_org",
               "merge_answers", "org_merge_review")


def _changes_graph(block: str, v: dict) -> bool:
    """Would the applier change the graph for this verdict? Only these are
    queued in ask mode; the rest (keep, skip, external, a pair judged distinct)
    just close a finding or note a pair, and pass straight through."""
    verdict = v.get("verdict")
    if block == "review_orphan":
        return verdict == "suppress"
    if block == "review_missing_org":
        return verdict == "assign" and bool(v.get("org"))
    if block == "review_ownerless":
        return verdict == "owner" and bool(v.get("owner"))
    if block == "review_org":
        return verdict == "canonicalize" and bool(v.get("canonical_org"))
    return v.get("same") is True     # merge_answers, org_merge_review


def _name(store, eid: str) -> str:
    try:
        e = store.get_entity(eid)
    except Exception:  # noqa: BLE001
        e = None
    return (e or {}).get("name") or eid


def _tidy_target_and_summary(store, block: str, v: dict) -> tuple[str, str] | None:
    if block in ("merge_answers", "org_merge_review"):
        pair = v.get("pair_id") or ""
        ids = pair.split("|")
        if len(ids) != 2 or not all(ids) or ids[0] == ids[1]:
            return None
        return f"pair:{pair}", f"Merge {_name(store, ids[0])} and {_name(store, ids[1])}"
    f = store.get_finding(v.get("finding_id"))
    if not f or f["resolved_at"]:
        return None
    ref = f["ref_id"]
    target = f"finding:{f['id']}"
    if block == "review_orphan":
        return target, f"Hide {_name(store, ref)}, which has no links"
    if block == "review_missing_org":
        return target, f"Set {_name(store, ref)}'s organisation to {v.get('org')}"
    if block == "review_ownerless":
        return target, f"Set the owner of action {ref} to {v.get('owner')}"
    if f["finding_type"] == "lint:duplicate_org":
        return target, f"Relabel organisation {ref} as {v.get('canonical_org')}"
    return target, f"Set {_name(store, ref)}'s organisation to {v.get('canonical_org')}"


def run_tidy(store, home, block: str, items: list, *, cap: int) -> dict:
    """The tidy-up writer's branch for one pushed block of verdicts.

    apply: the block's applier, exactly as before. off: nothing. ask: each
    verdict that would change the graph becomes one pending row (at most `cap`
    per call, the same review_max_apply_per_run cap apply mode uses); the
    others go through the applier as usual. Returns the applier's counts, plus
    "proposed" and "capped" in ask mode. In ask mode `cap` bounds the writes
    the pass-through verdicts make plus the proposals queued, together."""
    home = str(home)
    mode = config.auto_change_mode(home, "tidy")
    applier = _appliers()[block]
    items = list(items or [])
    if mode == "off":
        # An empty batch: the applier's zero counts, nothing touched.
        return applier(store, [], cap, home)
    if mode == "apply":
        return applier(store, items, cap, home)
    queue = [v for v in items if isinstance(v, dict) and _changes_graph(block, v)]
    rest = [v for v in items if not (isinstance(v, dict) and _changes_graph(block, v))]
    out = dict(applier(store, rest, cap, home))
    spent = sum(int(out.get(k) or 0) for k in _SPENT_KEYS)
    proposed = capped = 0
    for v in queue:
        ts = _tidy_target_and_summary(store, block, v)
        if ts is None:
            continue
        if spent + proposed >= cap:
            capped += 1
            continue
        propose(store, "tidy", ts[0], ts[1], {"block": block, "verdict": v})
        proposed += 1
    out["proposed"] = proposed
    out["capped"] = out.get("capped", 0) + capped
    return out


def _apply_tidy(store, home, payload) -> int:
    block, verdict = payload.get("block"), payload.get("verdict")
    appliers = _appliers()
    if block not in appliers or not isinstance(verdict, dict):
        raise NotApplied("This change can't be applied")
    # The store hands back the id of the row this write recorded; no guessing
    # by order, which a concurrent tidy write could break.
    change_ids: list[int] = []
    appliers[block](store, [verdict], 1, home, change_ids)
    if not change_ids:
        raise NotApplied("This no longer applies")
    return change_ids[0]


APPLY: dict[str, Callable] = {
    "profiles": _apply_profiles,
    "circles": _apply_circles,
    "reference": _apply_reference,
    "voice": _apply_voice,
    "consolidation": _apply_consolidation,
    "tidy": _apply_tidy,
    "draft_style": _apply_draft_style,
}
