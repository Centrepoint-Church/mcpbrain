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
import json
import logging
import time
from contextlib import closing, nullcontext
from pathlib import Path

from googleapiclient.errors import HttpError

from mcpbrain import config, reflow
from mcpbrain.chunking import SPLIT_VERSION
from mcpbrain.embed import contextual_prefix
from mcpbrain.store import ReflowOrphanError
from mcpbrain.sync import queue
from mcpbrain.sync.blocks import extraction_version

log = logging.getLogger("mcpbrain.sync.reflow")

HALT_CURSOR = "reflow:halted"
_GIVE_UP_ATTEMPTS = 5
_EPOCH = "1970-01-01T00:00:00"

# Unit-file keys naming chunk owners / chunks (see prepare.write_units and
# thread_enrich.reassemble_thread). Anything that names an id is collected; a
# false positive only defers an item, which is the safe direction.
_REF_KEYS = frozenset({"thread_id", "message_id", "doc_id", "file_id",
                       "event_id", "session_id"})
_REF_LIST_KEYS = frozenset({"part_doc_ids", "chunk_doc_ids", "doc_ids"})


def _collect_refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _REF_KEYS and isinstance(v, (str, int)) and v != "":
                out.add(str(v))
            elif k in _REF_LIST_KEYS:
                if isinstance(v, list):
                    for d in v:
                        if isinstance(d, (str, int)) and d != "":
                            out.add(str(d))
                        else:
                            _collect_refs(d, out)
                else:
                    _collect_refs(v, out)
            elif isinstance(v, (dict, list)):
                _collect_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, out)


def _http_status(exc) -> int | None:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) if resp is not None else None


class ReflowContext:
    """One sync cycle's reflow worker. `handle(item)` returns None (done:
    reflowed, routed to the ordinary path, or stamped) or `queue.DEFER`; it
    raises to have work_queue back the item off.

    `normal_handlers`, when given, is the cycle's own work_queue handler dict:
    a changed source is then worked by exactly the ordinary handler (with the
    cycle's folder cache, bulk section and, for a Shared Drive file, its fleet
    storage). Without it the source modules' handlers are called directly.
    `bulk_section` brackets the store writes (apply_reflow, stamps)."""

    def __init__(self, store, embedder, home, *, drive_service=None, gmail_service=None,
                 calendar_service=None, anarlog_db=None, max_items: int = 10,
                 max_seconds: float = 15.0, clock=time.monotonic,
                 normal_handlers: dict | None = None, bulk_section=None):
        self.store, self.embedder, self.home = store, embedder, str(home)
        self.drive, self.gmail, self.calendar, self.anarlog_db = (
            drive_service, gmail_service, calendar_service, anarlog_db)
        self.max_items, self.max_seconds, self.clock = max_items, max_seconds, clock
        self.normal_handlers = normal_handlers
        self.bulk_section = bulk_section or nullcontext
        self._done = 0
        self._started = None
        self._unit_refs: set[str] | None = None
        self._unit_stamp = None
        self._folder_cache: dict = {}

    # ---- guards -----------------------------------------------------------
    def _queue_stamp(self):
        q = Path(self.home) / "enrich_queue"
        out = []
        for d in (q / "units", q / "claims"):
            try:
                out.append(d.stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

    def _pending_unit_refs(self) -> set[str]:
        """Every id named by an enrichment unit that is pending (units/) or
        claimed (claims/<uid> -> units/<uid>.json). Re-scanned whenever either
        directory changes, so a unit written mid-cycle is still seen."""
        stamp = self._queue_stamp()
        if self._unit_refs is not None and stamp == self._unit_stamp:
            return self._unit_refs
        q = Path(self.home) / "enrich_queue"
        paths: set[Path] = set()
        try:
            paths.update((q / "units").glob("*.json"))
        except OSError:
            pass
        try:
            for claim in (q / "claims").iterdir():
                uid = claim.name[:-5] if claim.name.endswith(".json") else claim.name
                paths.add(q / "units" / f"{uid}.json")
        except OSError:
            pass
        refs: set[str] = set()
        for p in paths:
            try:
                _collect_refs(json.loads(p.read_text(encoding="utf-8")), refs)
            except (OSError, ValueError):
                continue
        self._unit_refs, self._unit_stamp = refs, stamp
        return refs

    def _over_cap(self) -> bool:
        if self._started is None:
            self._started = self.clock()
        return (self._done >= self.max_items
                or self.clock() - self._started > self.max_seconds)

    def _service_missing(self, kind: str) -> bool:
        return {"drive": self.drive, "gmail": self.gmail, "calendar": self.calendar,
                "anarlog": self.anarlog_db}.get(kind) is None

    # ---- entry ------------------------------------------------------------
    def handle(self, item):
        if self.store.get_cursor(HALT_CURSOR):
            return queue.DEFER
        if self._over_cap():
            return queue.DEFER
        kind = item["source"].split(":", 1)[1].split(":", 1)[0]
        if kind not in ("drive", "gmail", "anarlog", "calendar"):
            raise ValueError(f"reflow: unknown source {item['source']!r}")
        if self._service_missing(kind):
            return queue.DEFER            # not authed / not enabled this cycle
        owner = item["ref_id"]
        old = self.store.owner_chunks(self._prefixes(kind, owner))
        if not old:
            return None                   # nothing left to reflow
        refs = self._pending_unit_refs()
        if owner in refs or any(r["doc_id"] in refs for r in old):
            return queue.DEFER
        self._done += 1
        if int(item.get("attempts") or 0) >= _GIVE_UP_ATTEMPTS:
            log.warning("reflow: giving up on %s %s after %s attempts (%s)", kind,
                        owner, item.get("attempts"), item.get("last_error") or "")
            self._stamp(old, "gave_up")
            return None
        new = getattr(self, f"_new_{kind}")(owner, old)
        if new is None:
            return None                   # routed to the ordinary path, or stamped
        if any((c.metadata or {}).get("extraction_partial") for c in new):
            raise RuntimeError(f"reflow {kind} {owner}: partial re-extraction")
        p = reflow.plan(old, new)
        if kind in ("gmail", "anarlog", "calendar") and self._source_changed(p, old, new):
            # These sources' prose extraction is unchanged, so differing text
            # means the SOURCE changed: take the ordinary path.
            self._normal(kind, owner, old)
            return None
        vectors = self._embed([r.chunk for r in p.rows])
        try:
            with self.bulk_section():
                stats = self.store.apply_reflow(owner, kind, p, vectors,
                                                home=self.home)
        except ReflowOrphanError as exc:
            self.store.set_cursor(HALT_CURSOR, str(exc)[:500])
            log.error("reflow halted: %s", exc)
            raise
        log.info("reflow: %s %s -> %s", kind, owner, stats)
        return None

    # ---- helpers ----------------------------------------------------------
    @staticmethod
    def _prefixes(kind: str, owner: str) -> list[str]:
        return {"drive": [f"gdrive-{owner}-"], "gmail": [f"gmail-{owner}-"],
                "anarlog": [f"anarlog-{owner}-"], "calendar": [f"cal-{owner}"]}[kind]

    @staticmethod
    def _source_changed(p, old, new) -> bool:
        """True when a lineage present on BOTH sides differs and is not a
        block-extracted one (whose text is EXPECTED to differ), or a lineage
        appears that is neither block-extracted nor Gmail-attachment-shaped.

        A lineage present only in `old` is never read as a source change: for
        these immutable/prose sources it is a failed re-extraction (e.g. an
        attachment whose fetch failed), and apply_reflow's lineage_gone
        refusal is what must handle it -- routing it to the ordinary path
        would re-ingest the message without that attachment."""
        def lk(doc_id, md):
            return reflow.lineage_key(doc_id, md or {})
        mime = {}
        for r in old:
            md = r["metadata"] or {}
            mime.setdefault(lk(r["doc_id"], md), md.get("attachment_mime", ""))
        for c in new:
            md = c.metadata or {}
            mime[lk(c.doc_id, md)] = md.get("attachment_mime", "")
        old_keys = {lk(r["doc_id"], r["metadata"]) for r in old}
        new_keys = {lk(c.doc_id, c.metadata) for c in new}
        for k in p.unequal:
            if k in old_keys and k not in new_keys:
                continue
            if extraction_version(mime.get(k, "")):
                continue
            return True
        return False

    def _embed(self, chunks) -> list:
        use = config.contextual_retrieval_enabled(self.home)
        passages = [(contextual_prefix(c.metadata) + c.text) if use else c.text
                    for c in chunks]
        return self.embedder.embed_passages(passages)

    def _stamp(self, old, reason: str) -> None:
        """Mark an owner as not reflowable so the selector stops matching it."""
        with self.bulk_section():
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
            # extraction fingerprint once it is embedded (apply_reflow embeds).
            self.store.record_pending_publish(drive_id, fid, drive._file_content_hash(fmeta))
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
