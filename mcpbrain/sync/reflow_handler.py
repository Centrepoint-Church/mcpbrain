"""Queue handler for `reflow:<source>` items (2026-09-24 extraction-fidelity
spec §3-§4).

Re-fetch one owner, prove its source unchanged, re-extract and re-chunk it with
the current extractors and chunker, then apply `reflow.plan` via
`Store.apply_reflow` so already-extracted text keeps its enrichment. A changed
source takes that source's ORDINARY handler instead.

Never deletes on an empty or partial re-extraction: that raises, so work_queue
backs the item off (and after `_GIVE_UP_ATTEMPTS` the old chunks are stamped
`reflow_skipped="gave_up"` and the item completes). A plan that would drop a
whole lineage (e.g. an email attachment whose re-fetch silently failed) is
refused by apply_reflow with ValueError, which propagates the same way.

Guards, in order: the halt flag (set when apply_reflow's orphan guard fires --
a wrong remap must stop, not propagate); the per-cycle cap (items and
seconds); a missing service for the source; and an owner named by a pending OR
claimed enrichment unit. That last guard is load-bearing, not an optimisation:
drain applies a unit's extraction to the doc_ids the unit named, and through
the reflow_map fallback it would otherwise mark enriched a re-chunked chunk
containing text the extraction never saw.
"""
import logging
import time
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone

from googleapiclient.errors import HttpError

from mcpbrain import config, reflow
from mcpbrain.chunking import SPLIT_VERSION
from mcpbrain.embed import contextual_prefix
from mcpbrain.store import REFLOW_HALT_CURSOR, ReflowOrphanError
from mcpbrain.sync import queue
from mcpbrain.sync.blocks import extraction_version

log = logging.getLogger("mcpbrain.sync.reflow")

# Set by Store.apply_reflow itself when its orphan guard fires (every carry-over
# path, not just this handler); re-exported here for callers and tests.
HALT_CURSOR = REFLOW_HALT_CURSOR
# How long a row waits after a delayed DEFER (halted, no service, in-flight
# enrichment unit). Every reflow row shares the epoch modified_at, so an
# undelayed DEFER is re-selected at the head of the reflow rows every cycle and
# starves the rows behind it; the per-cycle-cap DEFER stays immediate.
DEFER_DELAY_S = 600
_GIVE_UP_ATTEMPTS = 5
_EPOCH = "1970-01-01T00:00:00"
# Bound on a run of consecutive TRANSIENT defers (rate limit / gateway /
# network) for one row. Without this a permanently-failing item that happens
# to classify transient would defer forever -- never spending one of
# _GIVE_UP_ATTEMPTS -- so the backlog would never reach zero and the
# backlog-end integrity check would never run. Past the bound, the same
# outcome is treated like any other failure (raised, so work_queue spends a
# real attempt via Store.fail_sync_item), which also resets the tally.
_TRANSIENT_DEFER_LIMIT = 12

def _http_status(exc) -> int | None:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) if resp is not None else None


# Rate limits and gateway/availability failures: the service, not the owner.
# A plain 500 still counts toward give-up (a file that always 500s must end).
_TRANSIENT_HTTP = frozenset({429, 502, 503, 504})


def _is_transient(exc) -> bool:
    if isinstance(exc, HttpError):
        return _http_status(exc) in _TRANSIENT_HTTP
    return isinstance(exc, (TimeoutError, ConnectionError))


class ReflowContext:
    """One sync cycle's reflow worker. `handle(item)` returns None (done:
    reflowed, routed to the ordinary path, or stamped) or `queue.DEFER`; it
    raises to have work_queue back the item off.

    `normal_handlers`, when given, is the cycle's own work_queue handler dict:
    a changed source is then worked by exactly the ordinary handler (with the
    cycle's folder cache, bulk section and, for a Shared Drive file, its fleet
    storage). Without it the source modules' handlers are called directly.
    `bulk_section` brackets the store writes (apply_reflow, stamps).
    `record_publish=False` never queues a shared-drive publish: the attended
    dry run (bin/reflow_dryrun.py) works a store COPY and must leave nothing
    that could reach the fleet."""

    def __init__(self, store, embedder, home, *, drive_service=None, gmail_service=None,
                 calendar_service=None, anarlog_db=None, max_items: int = 10,
                 max_seconds: float = 15.0, clock=time.monotonic,
                 normal_handlers: dict | None = None, bulk_section=None,
                 defer_delay_s: float = DEFER_DELAY_S, record_publish: bool = True):
        self.store, self.embedder, self.home = store, embedder, str(home)
        self.drive, self.gmail, self.calendar, self.anarlog_db = (
            drive_service, gmail_service, calendar_service, anarlog_db)
        self.max_items, self.max_seconds, self.clock = max_items, max_seconds, clock
        self.normal_handlers = normal_handlers
        self.bulk_section = bulk_section or nullcontext
        self.defer_delay_s = defer_delay_s
        self.record_publish = record_publish
        self._done = 0
        self._started = None
        self._folder_cache: dict = {}
        self._pending_publish: tuple | None = None

    # ---- guards -----------------------------------------------------------
    def _over_cap(self) -> bool:
        if self._started is None:
            self._started = self.clock()
        return (self._done >= self.max_items
                or self.clock() - self._started > self.max_seconds)

    def _service_missing(self, kind: str) -> bool:
        return {"drive": self.drive, "gmail": self.gmail, "calendar": self.calendar,
                "anarlog": self.anarlog_db}.get(kind) is None

    def _defer_later(self, item):
        """DEFER with a delay: next_attempt_at moves, attempts do not."""
        until = (datetime.now(timezone.utc).replace(tzinfo=None)
                 + timedelta(seconds=self.defer_delay_s)).isoformat()
        self.store.defer_sync_item(item["source"], item["ref_id"], until)
        return queue.DEFER

    # ---- entry ------------------------------------------------------------
    def handle(self, item):
        if self.store.get_cursor(HALT_CURSOR):
            return self._defer_later(item)
        if self._over_cap():
            return queue.DEFER            # immediate: next cycle, same place
        kind = item["source"].split(":", 1)[1].split(":", 1)[0]
        if kind not in ("drive", "gmail", "anarlog", "calendar"):
            # Complete it: raising here came before the give-up check, so the
            # row would back off and retry forever. Nothing emits one today.
            log.warning("reflow: dropping row with unknown source %r (%s)",
                        item["source"], item.get("ref_id"))
            return None
        owner = item["ref_id"]
        self._cur = (owner, kind)          # for outcome records (_stamp/_normal)
        if self._service_missing(kind):
            # Transient (not authed this cycle, anarlog switched off since the
            # row was queued): wait. A PERMANENTLY unavailable source is never
            # seeded, and the seed frees its queued rows (daemon._run_reflow_seed).
            return self._defer_later(item)
        old = self.store.owner_chunks(self._prefixes(kind, owner))
        if not old:
            return None                   # nothing left to reflow
        refs = reflow.pending_unit_refs(self.home)
        if owner in refs or any(r["doc_id"] in refs for r in old):
            return self._defer_later(item)
        self._done += 1
        if int(item.get("attempts") or 0) >= _GIVE_UP_ATTEMPTS:
            log.warning("reflow: giving up on %s %s after %s attempts (%s)", kind,
                        owner, item.get("attempts"), item.get("last_error") or "")
            self._stamp(old, "gave_up")
            return None
        self._pending_publish = None
        try:
            new = getattr(self, f"_new_{kind}")(owner, old)
        except Exception as exc:  # noqa: BLE001 — classified, then re-raised
            if not _is_transient(exc):
                raise
            # A rate limit / outage is not this owner's fault: wait, without
            # spending one of its _GIVE_UP_ATTEMPTS (five of them used to
            # stamp it gave_up for good during a long outage) -- UNLESS this
            # row has now deferred transiently _TRANSIENT_DEFER_LIMIT times in
            # a row, at which point it is no longer plausibly "the service,
            # not the owner" and is treated like any other failure so the
            # 5-attempt give-up stamp eventually applies.
            until = (datetime.now(timezone.utc).replace(tzinfo=None)
                     + timedelta(seconds=self.defer_delay_s)).isoformat()
            count = self.store.defer_sync_item_transient(
                item["source"], item["ref_id"], until)
            if count > _TRANSIENT_DEFER_LIMIT:
                log.warning("reflow: %s %s hit the transient-defer bound "
                            "(%d); spending an attempt: %s",
                            kind, owner, count, exc)
                raise
            log.info("reflow: %s %s deferred on a transient error (%d/%d): %s",
                      kind, owner, count, _TRANSIENT_DEFER_LIMIT, exc)
            return queue.DEFER
        if new is None:
            return None                   # routed to the ordinary path, or stamped
        if any((c.metadata or {}).get("extraction_partial") for c in new):
            raise RuntimeError(f"reflow {kind} {owner}: partial re-extraction")
        p = reflow.plan(old, new)
        # A Gmail message is immutable per id (spec §3): differing text is
        # always the store's own history -- a legacy positional tail an older
        # chunker left behind, an ambiguous seam overlap -- never an edit, so
        # Gmail ALWAYS applies the plan (covered rows carry, uncovered ones
        # re-enrich, legacy ids are deletes remapped onto their text). Only
        # calendar and anarlog, whose sources can change, are tested.
        if kind in ("anarlog", "calendar") and self._source_changed(p, old, new, kind):
            # These sources' prose extraction is unchanged, so differing text
            # means the SOURCE changed: take the ordinary path.
            self._normal(kind, owner, old)
            self._drop_stale_tail(kind, old, new)
            return None
        vectors = self._embed([r.chunk for r in p.rows])
        try:
            with self.bulk_section():
                stats = self.store.apply_reflow(owner, kind, p, vectors,
                                                home=self.home)
        except ReflowOrphanError as exc:
            # apply_reflow has already rolled back AND set the halt cursor.
            log.error("reflow halted: %s", exc)
            raise
        if self._pending_publish is not None and self.record_publish:
            # Only now: had embed or apply_reflow raised, a pending row would
            # publish the OLD chunks fleet-wide under the new fingerprint.
            self.store.record_pending_publish(*self._pending_publish)
        log.info("reflow: %s %s -> %s", kind, owner, stats)
        return None

    def _drop_stale_tail(self, kind: str, old, new) -> None:
        """The ordinary Calendar handler only UPSERTS, so a changed event that
        now yields fewer (or differently-keyed) chunks leaves its old ids
        behind (e.g. cal-E-2 at the old split_version), which the selector
        re-queues forever. Sweep a lineage's old ids missing from the new set
        through Store.sweep_changed_chunks: their local relations are
        invalidated, as the ordinary change path does, and every OTHER
        reference (observations, actions, recall feedback, chunk_quality) is
        remapped to the lineage's first new chunk and logged in reflow_map
        ('source_changed') -- so nothing is left naming a deleted id.

        ONLY for a lineage the ordinary handler demonstrably wrote: every new
        id of it is now in the store with the new chunk's content_hash AND
        metadata. The handler does its own fetch, which can differ from this
        reflow's (the event changed again or went away in between); any
        lineage it did not write is left exactly as it was. (Gmail never takes
        the ordinary path; anarlog's handler sweeps its own stale ids;
        Drive's upsert_file_chunks does.)"""
        if kind != "calendar":
            return
        by_key: dict[str, list] = {}
        for c in new:
            by_key.setdefault(reflow.lineage_key(c.doc_id, c.metadata or {}), []).append(c)
        first: dict[str, str] = {}
        for key, chunks in by_key.items():
            ok = True
            for c in chunks:
                row = self.store.get_chunk(c.doc_id)
                if (row is None or row["content_hash"] != c.content_hash
                        or (row["metadata"] or {}) != (c.metadata or {})):
                    ok = False
                    break
            if ok:
                first[key] = chunks[0].doc_id
        new_ids = {c.doc_id for c in new}
        remap = {}
        for r in old:
            key = reflow.lineage_key(r["doc_id"], r["metadata"] or {})
            if r["doc_id"] not in new_ids and key in first:
                remap[r["doc_id"]] = first[key]
        if not remap:
            return
        owner = getattr(self, "_cur", (None, None))[0] or ""
        with self.bulk_section():
            self.store.sweep_changed_chunks(owner, remap,
                                            invalidate_reason="reflow_source_changed",
                                            map_reason="source_changed")

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _prefixes(kind: str, owner: str) -> list[str]:
        return {"drive": [f"gdrive-{owner}-"], "gmail": [f"gmail-{owner}-"],
                "anarlog": [f"anarlog-{owner}-"], "calendar": [f"cal-{owner}"]}[kind]

    @staticmethod
    def _source_changed(p, old, new, kind: str = "calendar") -> bool:
        """Calendar / anarlog: has the SOURCE changed? (Gmail never asks --
        messages are immutable, see handle.)

        Per lineage present on both sides, the source is unchanged iff the
        text is contained BOTH ways, on normalised text:
          (i) every span of every new chunk is found in the stitched old text;
          (ii) every old chunk's text is found in the stitched new text (or in
               the plain concatenation of the new texts, tolerating an
               ambiguous seam overlap).
        Stitched equality was too strict: calendar sync is upsert-only, so a
        pre-split `cal-<eid>` row survives beside its `cal-<eid>-0/-1` split
        and the old text holds the event twice -- (ii) still passes for that
        duplicate, while an edit (changed text fails (i)) or a removal (the
        removed text fails (ii)) does not.

        A lineage only in `new` is a change (new content appeared). A lineage
        only in `old` is a change for anarlog only: its sessions are mutable
        and read from a local database, so a missing part (notes deleted) is
        the source's real state, and the ordinary handler sweeps it. For
        calendar it is never read as one: it is a failed re-extraction, and
        apply_reflow's lineage_gone refusal is what must handle it."""
        def lk(doc_id, md):
            return reflow.lineage_key(doc_id, md or {})
        old_by: dict[str, list] = {}
        for r in old:
            old_by.setdefault(lk(r["doc_id"], r["metadata"]), []).append(r)
        new_by: dict[str, list] = {}
        for c in new:
            new_by.setdefault(lk(c.doc_id, c.metadata), []).append(c)
        for key, chunks in new_by.items():
            rows = old_by.get(key)
            if rows is None:
                return True
            if key not in p.unequal:
                continue
            if not reflow.contained_both_ways(rows, chunks):
                return True
        if kind == "anarlog" and set(old_by) - set(new_by):
            return True
        return False

    def _embed(self, chunks) -> list:
        use = config.contextual_retrieval_enabled(self.home)
        passages = [(contextual_prefix(c.metadata) + c.text) if use else c.text
                    for c in chunks]
        return self.embedder.embed_passages(passages)

    def _record(self, outcome: str) -> None:
        owner, kind = getattr(self, "_cur", (None, None))
        if owner is not None:
            self.store.record_reflow_outcome(owner, kind, outcome)

    def _stamp(self, old, reason: str) -> None:
        """Mark an owner as not reflowable so the selector stops matching it,
        and record the outcome so visibility counts it."""
        with self.bulk_section():
            self._record(reason)
            for r in old:
                md = r["metadata"] or {}
                patch = {"split_version": SPLIT_VERSION, "reflow_skipped": reason}
                mime = md.get("mime_type") or md.get("attachment_mime") or ""
                if extraction_version(mime):
                    patch["extraction_version"] = extraction_version(mime)
                self.store.patch_chunk_metadata(r["doc_id"], **patch)

    def _gmail_fetch_attachments(self, old) -> bool:
        # Reflow re-chunks what exists: attachments are fetched when the user
        # has them on, or when this message already carries attachment chunks
        # (so turning the setting off later does not make them "gone").
        if any((r["metadata"] or {}).get("content_type") == "email_attachment"
               or "-att-" in r["doc_id"] for r in old):
            return True
        return bool(config.gmail_attachments(self.home))

    def _normal(self, kind: str, owner: str, old) -> None:
        source = kind
        if kind == "drive":
            from mcpbrain.org_contracts import DRIVE_ID_META_KEY
            drive_id = (old[0]["metadata"] or {}).get(DRIVE_ID_META_KEY)
            if drive_id:
                source = f"drive:{drive_id}"
        item = {"source": source, "ref_id": owner, "event": "upsert", "version": "",
                "modified_at": _EPOCH, "attempts": 0}
        if self.normal_handlers is not None and kind in self.normal_handlers:
            self.normal_handlers[kind](item)
            self._record("ordinary")
            return
        if kind == "drive":
            if source != "drive":
                # A Shared Drive file needs that drive's fleet storage and pin,
                # which only the cycle's own handler has.
                raise RuntimeError(f"reflow: {owner} changed in {source}; "
                                   "no shared-drive handler this cycle")
            from mcpbrain.sync import drive
            drive.handle_drive_item(self.drive, self.store, item,
                                    folder_cache=self._folder_cache,
                                    bulk_section=self.bulk_section)
        elif kind == "gmail":
            from mcpbrain.sync import gmail
            gmail.handle_gmail_item(self.gmail, self.store, item,
                                    fetch_attachments=self._gmail_fetch_attachments(old),
                                    bulk_section=self.bulk_section)
        elif kind == "anarlog":
            from mcpbrain.sync import anarlog
            anarlog.handle_anarlog_item(self.store, item, db_path=self.anarlog_db,
                                        bulk_section=self.bulk_section)
        elif kind == "calendar":
            from mcpbrain.sync import calendar
            calendar.handle_calendar_item(self.calendar, self.store, item,
                                          bulk_section=self.bulk_section)
        self._record("ordinary")

    def _new_drive(self, fid, old):
        from mcpbrain.org_contracts import DRIVE_ID_META_KEY
        from mcpbrain.sync import drive
        try:
            fmeta = self.drive.files().get(
                fileId=fid, supportsAllDrives=True,
                fields="id,name,mimeType,modifiedTime,version,parents,md5Checksum,"
                       "size,owners,driveId").execute(num_retries=3)
        except HttpError as exc:
            if _http_status(exc) == 404:
                self._stamp(old, "source_gone")     # the delta sync owns removal
                return None
            raise
        stored = (old[0]["metadata"] or {}).get("modified", "")
        if fmeta.get("modifiedTime", "") != stored:
            self._normal("drive", fid, old)
            return None
        content = drive.fetch_content(self.drive, fmeta, store=self.store)
        if content is None:
            self._stamp(old, "unsupported")
            return None
        if content.partial or (not content.text and not content.tables):
            raise RuntimeError(f"reflow {fid}: empty or partial re-extraction")
        drive_id = (old[0]["metadata"] or {}).get(DRIVE_ID_META_KEY)
        chunks = drive.normalise_drive(
            fmeta, content.text, drive_id=drive_id, tables=content.tables,
            blocks=content.blocks,
            folder=drive.folder_path(self.drive, fmeta, self._folder_cache))
        if not chunks:
            raise RuntimeError(f"reflow {fid}: re-extraction produced no chunks")
        if drive_id:
            # The shared-drive ingest cache republishes the file under the new
            # extraction fingerprint -- recorded by handle() only AFTER
            # apply_reflow commits (see there).
            self._pending_publish = (drive_id, fid, drive._file_content_hash(fmeta))
        return chunks

    def _new_gmail(self, mid, old):
        from mcpbrain.sync import gmail
        from mcpbrain.sync import normalise as nm
        raw, atts = gmail._fetch_one(self.gmail, mid,
                                     fetch_attachments=self._gmail_fetch_attachments(old),
                                     att_report={})
        if raw is None:
            self._stamp(old, "source_gone")
            return None
        chunks = nm.normalise_gmail(raw) + list(atts)
        if not chunks:
            raise RuntimeError(f"reflow {mid}: re-extraction produced no chunks")
        return chunks

    def _new_anarlog(self, sid, old):
        from mcpbrain.sync import anarlog
        with closing(anarlog.connect_ro(self.anarlog_db)) as db:
            session = anarlog.read_session(db, sid)
        if session is None:
            self._stamp(old, "source_gone")
            return None
        chunks = anarlog.normalise_session(session)
        if not chunks:
            raise RuntimeError(f"reflow {sid}: re-extraction produced no chunks")
        return chunks

    def _new_calendar(self, eid, old):
        from mcpbrain.sync import calendar
        try:
            ev = self.calendar.events().get(calendarId="primary", eventId=eid
                                            ).execute(num_retries=3)
        except HttpError as exc:
            if _http_status(exc) in (404, 410):
                self._stamp(old, "source_gone")
                return None
            raise
        chunks = calendar.normalise_calendar(ev)
        if not chunks:
            self._stamp(old, "source_gone")         # cancelled / emptied
            return None
        return chunks
