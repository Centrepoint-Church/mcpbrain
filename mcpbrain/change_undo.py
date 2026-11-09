"""Undo for the daemon's automatic changes, plus the "what changed" feed.

Every change the daemon makes by itself writes a change_log row. An undoable
one carries a revert_ref: new writers store JSON {"before": ..., "after": ...}
built by revert_json; a few older writers store a legacy string form
(`snoozed_until:<prev>`, `role:<old>`, `org:<old>`, `correction:<id>`) that the
undoers below still parse.

undo() refuses rather than clobbers: when the thing a change wrote no longer
holds what it wrote (a later change, a hand edit), the user is told to undo the
later change first and nothing is touched. A successful undo writes a
`change_undone` row whose ref_id is the undone change's id; that row is how a
second undo of the same change is refused.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

log = logging.getLogger(__name__)

CHANGED_SINCE = "This has changed since; undo the later change first"
ALREADY_UNDONE = "Already undone"
NOT_FOUND = "No such change"
NOT_UNDOABLE = "This change can't be undone"

_UNDONE = "change_undone"


class Refused(Exception):
    """An undo that must not go ahead; the message is the user-facing reason."""


def revert_json(before: dict, after: dict) -> str:
    """The revert_ref for an undoable change: what it replaced and what it wrote."""
    return json.dumps({"before": before, "after": after}, sort_keys=True, default=str)


def _payload(row: dict) -> dict | None:
    try:
        p = json.loads(row.get("revert_ref") or "")
    except (TypeError, ValueError):
        return None
    if isinstance(p, dict) and "before" in p and "after" in p:
        return p
    return None


def _need_payload(row: dict) -> dict:
    p = _payload(row)
    if p is None:
        raise Refused(NOT_UNDOABLE)
    return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- undoers: one per change_type -------------------------------------------
# Each takes (store, home, change_row) and either puts the prior value back or
# raises Refused. Store writes check-then-write on ONE write connection, so the
# "has it changed since" check and the restore cannot interleave with a writer.

def _undo_profile(store, home, row):
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    with store._connect(write=True) as db:
        cur = db.execute("SELECT profile, profile_updated_at FROM entities WHERE id=?",
                         (row["ref_id"],)).fetchone()
        if cur is None or (cur["profile"] or "") != (after.get("profile") or ""):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE entities SET profile=?, profile_updated_at=? WHERE id=?",
                   (before.get("profile") or "", before.get("profile_updated_at") or "",
                    row["ref_id"]))


def _undo_community_title(store, home, row):
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    with store._connect(write=True) as db:
        cur = db.execute("SELECT title, summary FROM community_summaries WHERE community_id=? "
                         "ORDER BY level LIMIT 1", (row["ref_id"],)).fetchone()
        if cur is None or ((cur["title"] or ""), (cur["summary"] or "")) != (
                after.get("title") or "", after.get("summary") or ""):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE community_summaries SET title=?, summary=? WHERE community_id=?",
                   (before.get("title") or "", before.get("summary") or "", row["ref_id"]))


def _undo_consolidation(store, home, row):
    """Delete the notes the pass wrote, put source tiers back, and take back any
    graduated memory file. A source chunk whose tier has moved on since (decay,
    a later promotion) keeps its current tier: only tiers still at what the pass
    set are restored."""
    from pathlib import Path
    from mcpbrain import config, records_write
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    repo = config.records_dir(home)
    grads = before.get("graduated") or []
    recorded = after.get("graduated_content")
    # Check every graduated file before changing anything: a file edited since
    # (by hand or by the gardener) must not be deleted. A row without recorded
    # content can't prove that, so it refuses too.
    # Under the records lock (shared with the daemon's records writers), so
    # the check and the removal see the same files and git never races.
    with records_write.repo_lock():
        for rel in grads:
            fp = Path(repo) / rel
            if not isinstance(recorded, dict) or rel not in recorded:
                raise Refused(CHANGED_SINCE)
            if fp.exists() and fp.read_text() != recorded[rel]:
                raise Refused(CHANGED_SINCE)
        for rel in grads:
            records_write.remove_memory(repo, Path(rel).stem, "undo: consolidation")
    after_tiers = after.get("tiers") or {}
    with store._connect(write=True) as db:
        for doc_id, tier in (before.get("tiers") or {}).items():
            db.execute("UPDATE chunks SET memory_tier=? WHERE doc_id=? "
                       "AND COALESCE(memory_tier,'')=?",
                       (tier, doc_id, after_tiers.get(doc_id, "hot")))
    # The same path ingest-cache cleanup uses: chunk rows plus their vec/FTS mirrors.
    store.delete_chunks(before.get("notes") or [])


def _undo_records_file(message: str, relpath_of: Callable[[dict], str]):
    def _undo(store, home, row):
        from pathlib import Path
        from mcpbrain import config, records_write
        p = _need_payload(row)
        before, after = p["before"], p["after"]
        repo = config.records_dir(home)
        rel = relpath_of(before)
        if not re.fullmatch(r"(reference|context)/[^/\\]+", rel or ""):
            raise Refused(NOT_UNDOABLE)
        fp = Path(repo) / rel
        # Check and restore under the records lock the daemon's writers share.
        with records_write.repo_lock():
            current = fp.read_text() if fp.exists() else None
            if current != after.get("content"):
                raise Refused(CHANGED_SINCE)
            records_write.restore_file(repo, rel, before.get("content"), message)
    return _undo


def _undo_bandit(store, home, row):
    from mcpbrain import config
    p = _need_payload(row)
    want = p["after"]["recall_max_distance"]
    if abs(config.recall_max_distance(home) - float(want)) >= 1e-9:
        raise Refused(CHANGED_SINCE)
    config.write_config(home, {"recall_max_distance": p["before"]["recall_max_distance"]})


# Every change that hides or restores an entity: an undo of any one of them is
# refused while a later, not-undone one of these touched the same entity.
_SUPPRESS_TYPES = ("tidy_suppress", "entity_suppressed", "entity_unsuppressed")


def _later_change_of(db, row: dict, types) -> bool:
    """A later, not-undone change of any of `types` to the same ref_id."""
    marks = ",".join("?" * len(types))
    return db.execute(
        f"SELECT 1 FROM change_log c WHERE c.change_type IN ({marks}) AND c.ref_id=? "
        "AND c.id>? AND NOT EXISTS (SELECT 1 FROM change_log u WHERE u.change_type=? "
        "AND u.ref_id=CAST(c.id AS TEXT)) LIMIT 1",
        (*types, row["ref_id"], row["id"], _UNDONE)).fetchone() is not None


def _undo_tidy_suppress(store, home, row):
    p = _need_payload(row)
    prior = p["before"].get("suppressed")
    wrote = p["after"].get("suppressed") or {}
    with store._connect(write=True) as db:
        cur = db.execute("SELECT reason, suppressed_at FROM entity_suppressions "
                         "WHERE entity_id=?", (row["ref_id"],)).fetchone()
        if cur is None or _later_change_of(db, row, _SUPPRESS_TYPES) or (
                (cur["reason"] or "", cur["suppressed_at"] or "")
                != (wrote.get("reason") or "", wrote.get("suppressed_at") or "")):
            raise Refused(CHANGED_SINCE)
        if prior:
            db.execute("UPDATE entity_suppressions SET reason=?, suppressed_at=? "
                       "WHERE entity_id=?",
                       (prior.get("reason") or "", prior.get("suppressed_at") or "",
                        row["ref_id"]))
        else:
            db.execute("DELETE FROM entity_suppressions WHERE entity_id=?",  # admin-delete-ok
                       (row["ref_id"],))


def _undo_tidy_org(store, home, row):
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    with store._connect(write=True) as db:
        cur = db.execute("SELECT org FROM entities WHERE id=?", (row["ref_id"],)).fetchone()
        if cur is None or (cur["org"] or "") != (after.get("org") or ""):
            raise Refused(CHANGED_SINCE)
        from mcpbrain.store import _field_is_locked
        if _field_is_locked(db, row["ref_id"], "org"):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE entities SET org=?, org_valid_from=? WHERE id=?",  # lock-exempt: guarded by _field_is_locked above
                   (before.get("org") or "", before.get("org_valid_from") or "",
                    row["ref_id"]))


def _undo_tidy_org_rewrite(store, home, row):
    """Relabel back only the entities still carrying the canonical org the
    rewrite gave them; one moved on since is left alone."""
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    ids = list(before.get("entity_ids") or [])
    if not ids:
        raise Refused(NOT_UNDOABLE)
    with store._connect(write=True) as db:
        marks = ",".join("?" * len(ids))
        cur = db.execute(
            f"UPDATE entities SET org=? WHERE org=? AND id IN ({marks}) AND id NOT IN "  # lock-exempt: excludes user-locked orgs below
            "(SELECT entity_id FROM entity_field_locks WHERE field='org')",
            (before["org"], after["org"], *ids))
        if cur.rowcount == 0:
            raise Refused(CHANGED_SINCE)


def _undo_tidy_owner(store, home, row):
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    with store._connect(write=True) as db:
        cur = db.execute("SELECT owner, owner_entity_id FROM actions WHERE id=?",
                         (row["ref_id"],)).fetchone()
        if cur is None or (cur["owner"] or "", cur["owner_entity_id"] or "") != (
                after.get("owner") or "", after.get("owner_entity_id") or ""):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE actions SET owner=?, owner_entity_id=? WHERE id=?",
                   (before.get("owner") or "", before.get("owner_entity_id") or "",
                    row["ref_id"]))


def _undo_tidy_merge(store, home, row):
    from mcpbrain.store import _unmerge_tx
    p = _need_payload(row)
    snap = p["before"]["snapshot"]
    try:
        with store._connect(write=True) as db:
            if db.execute("SELECT 1 FROM entity_merge_log WHERE id=?",
                          (p["after"]["merge_log_id"],)).fetchone() is None:
                raise Refused(CHANGED_SINCE)
            _unmerge_tx(db, snap)
    except ValueError as exc:
        log.info("change_undo: merge %s not undone: %s", row["id"], exc)
        raise Refused(CHANGED_SINCE) from exc
    except sqlite3.IntegrityError as exc:
        # _unmerge_tx defers FK checks to COMMIT; the whole restore rolled back.
        log.info("change_undo: merge %s not undone: %s", row["id"], exc)
        raise Refused(CHANGED_SINCE) from exc
    _graph_changed()


def _undo_action_status(store, home, row):
    p = _need_payload(row)
    before, after = p["before"], p["after"]
    with store._connect(write=True) as db:
        cur = db.execute("SELECT status, resolved_by, resolved_at FROM actions WHERE id=?",
                         (row["ref_id"],)).fetchone()
        fields = ("status", "resolved_by", "resolved_at")
        if cur is None or any((cur[f] or "") != (after.get(f) or "") for f in fields):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE actions SET status=?, resolved_by=?, resolved_at=?, updated_at=? "
                   "WHERE id=?",
                   (before.get("status") or "open", before.get("resolved_by") or "",
                    before.get("resolved_at") or "", _now_iso(), row["ref_id"]))


# --- legacy revert_ref forms -------------------------------------------------

def _legacy_value(row, prefix: str) -> str:
    ref = row.get("revert_ref") or ""
    if not ref.startswith(prefix):
        raise Refused(NOT_UNDOABLE)
    return ref[len(prefix):]


def _summary_value(summary: str, pattern: str) -> str | None:
    """The new value a legacy writer quoted (repr) in its summary, or None."""
    m = re.search(pattern, summary or "")
    if not m:
        return None
    try:
        return ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return None


def _undo_snooze(store, home, row):
    prev = _legacy_value(row, "snoozed_until:")
    until = re.search(r"until (\S+)$", row.get("summary") or "")
    with store._connect(write=True) as db:
        cur = db.execute("SELECT snoozed_until FROM actions WHERE id=?",
                         (row["ref_id"],)).fetchone()
        # No date in the summary means nothing to check against: refuse rather
        # than overwrite a snooze set since.
        if cur is None or until is None or (cur["snoozed_until"] or "") != until.group(1):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE actions SET snoozed_until=?, updated_at=? WHERE id=?",
                   (prev or None, _now_iso(), row["ref_id"]))


def _undo_role(store, home, row):
    """profile_audit wrote its role as a 'profile_audit' observation, retiring
    the prior profile_audit row. Invalidate the row it wrote and reopen the one
    it retired, if that held the old value."""
    old = _legacy_value(row, "role:")
    new = _summary_value(row.get("summary"), r"Role corrected to (.+) for entity ")
    eid = row["ref_id"]
    with store._connect(write=True) as db:
        cur = db.execute(
            "SELECT id, value, valid_from FROM entity_observations WHERE entity_id=? "
            "AND attribute='role' AND source='profile_audit' AND valid_to IS NULL "
            "AND (invalidated_at IS NULL OR invalidated_at='') ORDER BY id DESC LIMIT 1",
            (eid,)).fetchone()
        if cur is None or (new is not None and cur["value"] != new):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE entity_observations SET invalidated_at=? WHERE id=?",
                   (_now_iso(), cur["id"]))
        if old:
            prior = db.execute(
                "SELECT id FROM entity_observations WHERE entity_id=? AND attribute='role' "
                "AND source='profile_audit' AND value=? AND valid_to=? "
                "AND (invalidated_at IS NULL OR invalidated_at='') ORDER BY id DESC LIMIT 1",
                (eid, old, cur["valid_from"])).fetchone()
            if prior is not None:
                db.execute("UPDATE entity_observations SET valid_to=NULL WHERE id=?",
                           (prior["id"],))


def _undo_org_corrected(store, home, row):
    old = _legacy_value(row, "org:")
    new = _summary_value(row.get("summary"), r"Org corrected to (.+) for entity ")
    from mcpbrain.store import _field_is_locked
    with store._connect(write=True) as db:
        cur = db.execute("SELECT org FROM entities WHERE id=?", (row["ref_id"],)).fetchone()
        if cur is None or (new is not None and (cur["org"] or "") != new):
            raise Refused(CHANGED_SINCE)
        if _field_is_locked(db, row["ref_id"], "org"):
            raise Refused(CHANGED_SINCE)
        db.execute("UPDATE entities SET org=? WHERE id=?",  # lock-exempt: guarded by _field_is_locked above
                   (old, row["ref_id"]))


def _undo_graph_correction(store, home, row):
    from mcpbrain import graph_corrections
    ref = _legacy_value(row, "correction:")
    try:
        cid = int(ref)
    except ValueError as exc:
        raise Refused(NOT_UNDOABLE) from exc
    out = graph_corrections.undo(store, cid)
    if out.get("status") != "reverted":
        raise Refused(out.get("error") or CHANGED_SINCE)


# graph_view.suppress_entity always writes this reason; a legacy
# entity_suppressed row carries no revert_ref, so the reason is the evidence
# that the suppression in place is still the one this row recorded.
_GRAPH_UI_REASON = "graph-ui"


def _undo_entity_suppressed(store, home, row):
    with store._connect(write=True) as db:
        cur = db.execute("SELECT reason FROM entity_suppressions WHERE entity_id=?",
                         (row["ref_id"],)).fetchone()
        if (cur is None or (cur["reason"] or "") != _GRAPH_UI_REASON
                or _later_change_of(db, row, _SUPPRESS_TYPES)):
            raise Refused(CHANGED_SINCE)
        db.execute("DELETE FROM entity_suppressions WHERE entity_id=?",  # admin-delete-ok
                   (row["ref_id"],))
    _graph_changed()


def _undo_entity_unsuppressed(store, home, row):
    """Hide the entity again with the suppression the restore removed (same
    reason and time), if it is still visible and nothing has hidden or
    restored it since."""
    p = _need_payload(row)
    prior = p["before"].get("suppressed") or {}
    with store._connect(write=True) as db:
        cur = db.execute("SELECT 1 FROM entity_suppressions WHERE entity_id=?",
                         (row["ref_id"],)).fetchone()
        exists = db.execute("SELECT 1 FROM entities WHERE id=?",
                            (row["ref_id"],)).fetchone()
        if cur is not None or exists is None or _later_change_of(db, row, _SUPPRESS_TYPES):
            raise Refused(CHANGED_SINCE)
        db.execute("INSERT INTO entity_suppressions(entity_id, reason, suppressed_at) "
                   "VALUES(?, ?, ?)",
                   (row["ref_id"], prior.get("reason") or "",
                    prior.get("suppressed_at") or _now_iso()))
    _graph_changed()


def _undo_fact_added(store, home, row):
    """Remove the fact the owner added and reopen the manual fact it closed,
    if both still read as the change left them: the new one current with the
    same entity, attribute and value; each closed one still closed on that
    day and not invalidated."""
    p = _need_payload(row)
    wrote = p["after"].get("fact") or {}
    oid = wrote.get("id")
    if not oid:
        raise Refused(NOT_UNDOABLE)
    closed = [int(i) for i in (p["before"].get("closed") or [])]
    closed_on = p["after"].get("closed_on")
    with store._connect(write=True) as db:
        cur = db.execute(
            "SELECT entity_id, attribute, value, valid_to, invalidated_at "
            "FROM entity_observations WHERE id=?", (oid,)).fetchone()
        if (cur is None or cur["entity_id"] != row["ref_id"]
                or (cur["attribute"], cur["value"]) != (wrote.get("attribute"), wrote.get("value"))
                or cur["valid_to"] is not None or cur["invalidated_at"] is not None):
            raise Refused(CHANGED_SINCE)
        for cid in closed:
            old = db.execute("SELECT valid_to, invalidated_at FROM entity_observations "
                             "WHERE id=?", (cid,)).fetchone()
            if old is None or old["valid_to"] != closed_on or old["invalidated_at"] is not None:
                raise Refused(CHANGED_SINCE)
        db.execute("DELETE FROM entity_observations WHERE id=?", (oid,))  # admin-delete-ok
        if closed:
            marks = ",".join("?" * len(closed))
            db.execute(f"UPDATE entity_observations SET valid_to=NULL WHERE id IN ({marks})",
                       closed)
    _graph_changed()


def _undo_finding_dismissed(store, home, row):
    """Reopen the findings a dismiss closed, if they are still closed by it."""
    p = _need_payload(row)
    ids = [int(i) for i in (p["before"].get("ids") or [])]
    verdict = p["after"].get("verdict")
    if not ids:
        raise Refused(NOT_UNDOABLE)
    marks = ",".join("?" * len(ids))
    with store._connect(write=True) as db:
        cur = db.execute(
            f"UPDATE proactive_findings SET resolved_at=NULL, verdict=NULL "
            f"WHERE id IN ({marks}) AND resolved_at IS NOT NULL AND verdict IS ?",
            (*ids, verdict))
        if cur.rowcount == 0:
            raise Refused(CHANGED_SINCE)


def _undo_chunks_reincluded(store, home, row):
    """Put every re-included chunk back exactly as it was (enrich_state,
    enriched, enriched_version, enrich_attempts and the force_enrich flag), if
    every one still reads as the re-include left it. One that enrichment, a
    later re-include or a refetch has moved on refuses the whole undo."""
    from mcpbrain.store import _chunk_enrich_snapshot
    p = _need_payload(row)
    before = p["before"].get("chunks") or {}
    after = p["after"].get("chunks") or {}
    if not before or set(before) != set(after):
        raise Refused(NOT_UNDOABLE)
    with store._connect(write=True) as db:
        metas = {}
        for doc_id, wrote in after.items():
            cur = db.execute("SELECT metadata, enrich_state, enriched, enriched_version, "
                             "enrich_attempts FROM chunks WHERE doc_id=?",
                             (doc_id,)).fetchone()
            if cur is None:
                raise Refused(CHANGED_SINCE)
            try:
                meta = json.loads(cur["metadata"] or "{}")
            except (TypeError, ValueError):
                meta = {}
            meta = meta if isinstance(meta, dict) else {}
            if _chunk_enrich_snapshot(cur, meta) != wrote:
                raise Refused(CHANGED_SINCE)
            metas[doc_id] = meta
        for doc_id, prior in before.items():
            meta = metas[doc_id]
            if prior.get("force_enrich") is None:
                meta.pop("force_enrich", None)
            else:
                meta["force_enrich"] = prior["force_enrich"]
            db.execute("UPDATE chunks SET enrich_state=?, enriched=?, enriched_version=?, "
                       "enrich_attempts=?, metadata=? WHERE doc_id=?",
                       (prior.get("enrich_state") or "", int(prior.get("enriched") or 0),
                        int(prior.get("enriched_version") or 0),
                        int(prior.get("enrich_attempts") or 0), json.dumps(meta), doc_id))


def _graph_changed() -> None:
    try:
        from mcpbrain import entity_resource
        entity_resource.invalidate_cache()
    except Exception:  # noqa: BLE001 -- the undo itself already committed
        pass


UNDO: dict[str, Callable] = {
    "profile_updated": _undo_profile,
    "community_titled": _undo_community_title,
    "consolidation_pass": _undo_consolidation,
    "voice_apply": _undo_records_file("undo: voice", lambda b: "context/voice.md"),
    "gardener_write": _undo_records_file("undo: gardener", lambda b: b["path"]),
    "bandit_applied": _undo_bandit,
    "tidy_suppress": _undo_tidy_suppress,
    "tidy_org": _undo_tidy_org,
    "tidy_owner": _undo_tidy_owner,
    "tidy_merge": _undo_tidy_merge,
    "tidy_org_rewrite": _undo_tidy_org_rewrite,
    "capture_action_update": _undo_action_status,
    "finding_dismissed": _undo_finding_dismissed,
    "entity_unsuppressed": _undo_entity_unsuppressed,
    "fact_added": _undo_fact_added,
    "chunks_reincluded": _undo_chunks_reincluded,
    # legacy revert_ref forms
    "action_snoozed": _undo_snooze,
    "role_corrected": _undo_role,
    "org_corrected": _undo_org_corrected,
    "graph_corrected": _undo_graph_correction,
    "entity_suppressed": _undo_entity_suppressed,
}

# Undoable without any revert_ref: the change itself names what to reverse.
_NO_REF_NEEDED = frozenset({"entity_suppressed"})


def _is_undoable(row: dict) -> bool:
    ct = row.get("change_type")
    if ct not in UNDO:
        return False
    return bool(row.get("revert_ref")) or ct in _NO_REF_NEEDED


def _later_open_change(db, row: dict) -> bool:
    """A later, not-undone change of the same type to the same thing. Undoing
    out of order would put back a value the later change already replaced."""
    if not row.get("ref_id"):
        return False
    return db.execute(
        "SELECT 1 FROM change_log c WHERE c.change_type=? AND c.ref_id=? AND c.id>? "
        "AND NOT EXISTS (SELECT 1 FROM change_log u WHERE u.change_type=? "
        "AND u.ref_id=CAST(c.id AS TEXT)) LIMIT 1",
        (row["change_type"], row["ref_id"], row["id"], _UNDONE)).fetchone() is not None


def undo(store, home, change_id: int) -> dict:
    """Undo one recorded change. Returns {"ok": bool, "reason": str}; reason is
    NOT_FOUND for an unknown id."""
    with store._connect() as db:
        r = db.execute("SELECT * FROM change_log WHERE id=?", (int(change_id),)).fetchone()
        if r is None or r["change_type"] == _UNDONE:
            return {"ok": False, "reason": NOT_FOUND}
        row = dict(r)
        if db.execute("SELECT 1 FROM change_log WHERE change_type=? AND ref_id=?",
                      (_UNDONE, str(row["id"]))).fetchone():
            return {"ok": False, "reason": ALREADY_UNDONE}
        if not _is_undoable(row):
            return {"ok": False, "reason": NOT_UNDOABLE}
        if _later_open_change(db, row):
            return {"ok": False, "reason": CHANGED_SINCE}
    try:
        UNDO[row["change_type"]](store, home, row)
    except Refused as exc:
        return {"ok": False, "reason": str(exc)}
    except RuntimeError as exc:
        from mcpbrain import records_write
        if isinstance(exc, records_write.RecordsBusy):
            return {"ok": False, "reason": records_write.BUSY}
        log.exception("change_undo: undo of change %s failed", row["id"])
        return {"ok": False, "reason": f"Undo failed: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- surface, don't crash the route
        log.exception("change_undo: undo of change %s failed", row["id"])
        return {"ok": False, "reason": f"Undo failed: {exc}"}
    store.record_change(_UNDONE, ref_id=str(row["id"]),
                        summary=f"Undid: {row.get('summary') or row['change_type']}",
                        source="dashboard")
    return {"ok": True, "reason": ""}


# --- feed --------------------------------------------------------------------

def _sql_stamp(dt: datetime) -> str:
    """change_log.created_at is SQLite CURRENT_TIMESTAMP: UTC 'YYYY-MM-DD HH:MM:SS'."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


_SKIP = "ingest_skip"
# An ingest_skip summary reads "<kind>: <detail>"; its kind is the part before
# the colon (the whole summary when there is none).
_SKIP_KIND_SQL = ("CASE WHEN instr(COALESCE(summary,''), ':') > 0 "
                  "THEN substr(summary, 1, instr(summary, ':') - 1) "
                  "ELSE COALESCE(summary,'') END")


def feed(store, *, hours: int = 24, now=None, limit: int = 500) -> list[dict]:
    """The last `hours` of changes, newest first. Rows of the same type made in
    the same minute share a `group`, so the page can fold a burst (six profiles
    in one drain) into one line.

    Ingest skips are folded here, server-side: one row per skip kind per hour,
    carrying `count` (and the newest skip's summary; its ref_id only when the
    row stands for a single skip). A heavy skip day (thousands of unreadable
    files) therefore cannot push the day's other changes past `limit`. Every
    row carries `count` (1 for an ordinary change)."""
    now = now or datetime.now(timezone.utc)
    cutoff = _sql_stamp(now - timedelta(hours=hours))
    upper = _sql_stamp(now)
    with store._connect() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT * FROM change_log WHERE change_type NOT IN (?, ?) AND created_at >= ? "
            "AND created_at <= ? ORDER BY id DESC LIMIT ?",
            (_UNDONE, _SKIP, cutoff, upper, limit))]
        skips = [dict(r) for r in db.execute(
            f"SELECT c.*, g.n AS skip_count FROM (SELECT MAX(id) AS id, COUNT(*) AS n "
            f"FROM change_log WHERE change_type=? AND created_at >= ? AND created_at <= ? "
            f"GROUP BY {_SKIP_KIND_SQL}, substr(created_at, 1, 13)) g "
            f"JOIN change_log c ON c.id = g.id ORDER BY c.id DESC LIMIT ?",
            (_SKIP, cutoff, upper, limit))]
        undone = {r[0] for r in db.execute(
            "SELECT ref_id FROM change_log WHERE change_type=?", (_UNDONE,))}
    out = []
    for r in sorted(rows + skips, key=lambda x: x["id"], reverse=True)[:limit]:
        is_undone = str(r["id"]) in undone
        count = int(r.get("skip_count") or 1)
        if r["change_type"] == _SKIP:
            kind = (r.get("summary") or "").split(":", 1)[0]
            group = f"{_SKIP}:{kind}@{(r.get('created_at') or '')[:13]}"
        else:
            group = f"{r['change_type']}@{(r.get('created_at') or '')[:16]}"
        out.append({
            "id": r["id"],
            "change_type": r["change_type"],
            "summary": r.get("summary") or "",
            "ref_id": (r.get("ref_id") or "") if count == 1 else "",
            "created_at": r.get("created_at") or "",
            "undoable": _is_undoable(r) and not is_undone,
            "undone": is_undone,
            "group": group,
            "count": count,
        })
    return out


# Ids of entities that gained a relation or observation from a source first
# seen at or after :cutoff. Shared by added_since and the graph's "added in the
# last N hours" filter (graph_view.graph_canvas(since_hours=)), so both agree.
ADDED_SINCE_IDS_SQL = (
    "SELECT r.entity_a FROM relation_sources rs "
    "  JOIN entity_relations r ON r.id = rs.relation_id WHERE rs.first_seen >= :cutoff "
    "UNION SELECT r.entity_b FROM relation_sources rs "
    "  JOIN entity_relations r ON r.id = rs.relation_id WHERE rs.first_seen >= :cutoff "
    "UNION SELECT o.entity_id FROM observation_sources os "
    "  JOIN entity_observations o ON o.id = os.observation_id WHERE os.first_seen >= :cutoff")


def added_cutoff(hours: int) -> str:
    """The first_seen cutoff for "added in the last `hours`" (UTC, ISO)."""
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def added_since(store, *, hours: int) -> list[dict]:
    """Entities that gained a relation or observation from a source first seen
    in the last `hours` (relation_sources / observation_sources.first_seen)."""
    with store._connect() as db:
        rows = db.execute(
            f"SELECT e.id AS entity_id, e.name, e.type FROM entities e WHERE e.id IN ("
            f"{ADDED_SINCE_IDS_SQL}) "
            "AND e.id NOT IN (SELECT entity_id FROM entity_suppressions) "
            "ORDER BY e.name",
            {"cutoff": added_cutoff(hours)}).fetchall()
    return [{"entity_id": r["entity_id"], "name": r["name"], "type": r["type"]} for r in rows]
