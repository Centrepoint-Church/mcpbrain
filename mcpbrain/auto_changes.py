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
a verdict, so it never reopens.
"""
from __future__ import annotations

import json
import logging
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
    statuses = (_PENDING, _DECIDING, _DECLINED) if include_declined else (_PENDING, _DECIDING)
    marks = ",".join("?" * len(statuses))
    with store._connect() as db:
        return {r[0] for r in db.execute(
            f"SELECT target FROM pending_changes WHERE kind=? AND status IN ({marks})",
            (kind, *statuses))}


def list_pending(store) -> list[dict]:
    """Changes waiting for review, oldest first."""
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT id, kind, target, summary, created_at FROM pending_changes "
            "WHERE status=? ORDER BY id", (_PENDING,)).fetchall()]


def _close_finding(store, pid: int, verdict: str) -> None:
    with store._connect() as db:
        row = db.execute("SELECT id FROM proactive_findings WHERE finding_type=? AND ref_id=?",
                         (FINDING_TYPE, str(pid))).fetchone()
    if row is not None:
        store.resolve_finding(row["id"], verdict=verdict)


def decide(store, home, pending_id: int, *, approve: bool) -> dict:
    """Approve (apply through APPLY[kind]) or decline one queued change.

    Returns {"ok": bool, "reason": str}. An approval whose APPLY makes no
    change (the item changed since, a guard refused) leaves the change pending
    with the reason, so the owner can decline it."""
    home = str(home)
    with store._connect(write=True) as db:
        row = db.execute("SELECT * FROM pending_changes WHERE id=?",
                         (int(pending_id),)).fetchone()
        if row is None:
            return {"ok": False, "reason": NOT_FOUND}
        claimed = db.execute(
            "UPDATE pending_changes SET status=? WHERE id=? AND status=?",
            (_DECIDING, row["id"], _PENDING)).rowcount
        if not claimed:
            return {"ok": False, "reason": ALREADY_DECIDED}
    row = dict(row)
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}

    if not approve:
        _settle(store, row["id"], _DECLINED, None)
        _close_finding(store, row["id"], "declined")
        _on_decline(store, row["kind"], payload)
        return {"ok": True, "reason": ""}

    try:
        change_id = APPLY[row["kind"]](store, home, payload)
    except NotApplied as exc:
        _settle(store, row["id"], _PENDING, None)
        return {"ok": False, "reason": str(exc) or "Nothing to change"}
    except Exception as exc:  # noqa: BLE001 -- surface it, keep the review
        log.exception("auto_changes: applying pending change %s failed", row["id"])
        _settle(store, row["id"], _PENDING, None)
        return {"ok": False, "reason": f"Apply failed: {exc}"}
    _settle(store, row["id"], _APPLIED, change_id or None)
    _close_finding(store, row["id"], "applied")
    return {"ok": True, "reason": ""}


def _settle(store, pid: int, status: str, change_id) -> None:
    decided = "" if status == _PENDING else _now()
    with store._connect(write=True) as db:
        db.execute("UPDATE pending_changes SET status=?, decided_at=?, change_log_id=? "
                   "WHERE id=?", (status, decided, change_id, pid))


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
    except (ValueError, FileNotFoundError) as exc:
        raise NotApplied(str(exc)) from exc
    if not committed:
        raise NotApplied("The file already says this")
    return cid


def _apply_voice(store, home, payload) -> int:
    from mcpbrain.voice_apply import apply_suggestions
    out = apply_suggestions(store, home, suggestion_ids=payload.get("suggestion_ids"))
    if out.get("blocked"):
        raise NotApplied(str(out["blocked"]))
    if not out.get("applied"):
        raise NotApplied("These voice suggestions are no longer waiting")
    return out.get("change_id") or 0


def _apply_consolidation(store, home, payload) -> int:
    from mcpbrain import consolidation
    cid = consolidation.apply_proposal(store, home, list(payload.get("doc_ids") or []),
                                       payload.get("summary") or "")
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
    from mcpbrain import org_curate, review_apply
    return {
        "review_orphan": lambda s, v, cap, home: review_apply.apply_orphan_verdicts(
            s, v, cap=cap),
        "review_missing_org": lambda s, v, cap, home: review_apply.apply_missing_org_verdicts(
            s, v, cap=cap, home=home),
        "review_ownerless": lambda s, v, cap, home: review_apply.apply_ownerless_verdicts(
            s, v, cap=cap),
        "review_org": lambda s, v, cap, home: review_apply.apply_org_verdicts(
            s, v, cap=cap, home=home),
        "merge_answers": lambda s, v, cap, home: review_apply.apply_duplicate_verdicts(
            s, v, cap=cap),
        "org_merge_review": lambda s, v, cap, home: org_curate.apply_org_merge_answers(
            s, v, cap=cap),
    }


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
    "proposed" and "capped" in ask mode."""
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
    proposed = capped = 0
    for v in queue:
        ts = _tidy_target_and_summary(store, block, v)
        if ts is None:
            continue
        if proposed >= cap:
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
    with store._connect() as db:
        before = db.execute("SELECT COALESCE(MAX(id), 0) FROM change_log").fetchone()[0]
    appliers[block](store, [verdict], 1, home)
    with store._connect() as db:
        row = db.execute(
            "SELECT id FROM change_log WHERE id>? AND change_type LIKE 'tidy\\_%' ESCAPE '\\' "
            "ORDER BY id LIMIT 1", (before,)).fetchone()
    if row is None:
        raise NotApplied("This no longer applies")
    return row["id"]


APPLY: dict[str, Callable] = {
    "profiles": _apply_profiles,
    "circles": _apply_circles,
    "reference": _apply_reference,
    "voice": _apply_voice,
    "consolidation": _apply_consolidation,
    "tidy": _apply_tidy,
    "draft_style": _apply_draft_style,
}
