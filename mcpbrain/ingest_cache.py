"""ACL-gated shared-drive ingest cache (spec §A2).

A `.mcpbrain-cache/` folder at the root of each shared drive holds one gzip-JSON
CacheArtifact per (file × content-version × embedding-pipeline). Because the
artifact lives inside the drive it describes, Google's ACLs ARE the access
control — no mcpbrain-side ACL logic exists or is needed.

`content_hash` throughout this module is the Drive FILE-VERSION id (md5Checksum,
or a hash of the native revision) — it must be knowable before extraction so the
read path can decide whether to extract at all. Per-chunk row content_hash stays
the text hash and is recomputed on import.

All entry points fail safe: a miss / mismatch / corruption returns False (or a
no-op) and the caller falls back to the local extract+embed pipeline.
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import re
import struct
from datetime import datetime, timezone

from mcpbrain.chunking import CHUNKER_VERSION
from mcpbrain.chunking import content_hash as _text_hash
from mcpbrain.org_contracts import (
    CacheArtifact, CacheChunk, DRIVE_ID_META_KEY,
    artifact_filename, pipeline_fingerprint,
)
from mcpbrain.store import ENRICH_LOGIC_VERSION, _meta_extract

log = logging.getLogger(__name__)

CACHE_DIR = ".mcpbrain-cache"


class ImportDeferred(RuntimeError):
    """A carry-over import was refused because an enrichment unit naming this
    file is still pending or claimed (spec 2026-09-24 §3 guard). The caller
    must retry LATER (work_queue backoff), never fall back to a local
    re-extract: that has the same exposure — drain would later mark the
    re-chunked, never-extracted text enriched."""


# -- filename / path helpers ------------------------------------------------

def _version_int(value) -> int:
    """The integer inside a chunker_version string — '2' -> 2, 'v1' -> 1, and 0
    when there is no digit at all. Both spellings are live: the code constant is
    an int (chunking.CHUNKER_VERSION) while the fleet-distributed org pin has
    historically carried 'v1'."""
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else 0


def effective_chunker_version(pin, mime: str = "") -> str:
    """The chunker version THIS install keys its cache artifacts on.

    The org pin's value, unless it LAGS the local code constant — then the local
    constant wins. The pin is fleet-distributed, so trusting it as a strict
    ceiling made a local chunker bump invisible to the cache: `pipeline_
    fingerprint` is keyed off the pin, `config.fleet_pin`'s new default only
    applies when the key is ABSENT, and the live pin sets it explicitly ('v1').
    Installs running post-spec-2 code therefore kept reading and writing
    artifacts at the pre-spec-2 fingerprint — importing old-shape chunks with no
    way to know, which is exactly what bumping CHUNKER_VERSION was supposed to
    prevent. Flooring it here restores that intent with no fleet-wide
    org-config.json edit as a prerequisite.

    This does NOT rewrite what the pin stores or distributes; it only refuses to
    let a stale pin lower the LOCAL fingerprint and import gate. A pin that is
    AHEAD of this install's code is honoured verbatim (its exact string, so
    installs sharing that pin still agree on the artifact path).

    Every install on the same code version computes the same value, so cache
    artifacts stay shareable — installs on older code simply keep using their
    own, differently-fingerprinted path, and the two coexist without churn.

    `mime` appends the file type's extraction_version so an extractor change
    invalidates only that type's artifacts, never spreadsheets. Empty/unknown
    mimes (extraction_version 0, e.g. spreadsheets or no mime known) leave the
    base version untouched, so every existing caller that doesn't pass `mime`
    keeps today's exact fingerprint.
    """
    base = (str(pin.chunker_version)
            if _version_int(pin.chunker_version) >= CHUNKER_VERSION
            else str(CHUNKER_VERSION))
    from mcpbrain.sync.blocks import extraction_version
    xv = extraction_version(mime)
    return f"{base}+x{xv}" if xv else base


def _pf8(pin, mime: str = "") -> str:
    return pipeline_fingerprint(
        pin.embed_model, pin.dim, effective_chunker_version(pin, mime))[:8]


def _artifact_path(file_id: str, content_hash: str, pin, mime: str = "") -> str:
    return (f"{CACHE_DIR}/"
            f"{artifact_filename(file_id, content_hash, pin.embed_model, pin.dim, effective_chunker_version(pin, mime))}")


def _current_pipeline_fingerprints(pin) -> set[str]:
    """Every pf8 this install currently reads as 'not stale': the base
    (unsuffixed) fingerprint plus every block-extracted MIME's
    +x<N>-suffixed one (sync.blocks.EXTRACTION_VERSIONS). Several MIMEs can
    share one pf8 when their extraction_version numbers collide (they all do
    today, at 1) — a set is enough here since GC/bootstrap only need to know
    whether a listed artifact's fingerprint is CURRENT at all, not which MIME
    produced it (see _artifact_mime for that).

    A pf8 missing from this set is either a different embed_model/dim/
    chunker_version pipeline (coexists untouched — spec A2) or a stale
    pre-extraction-fidelity block-MIME artifact (exactly what the +x<N>
    suffix exists to flush out of GC/bootstrap)."""
    from mcpbrain.sync.blocks import EXTRACTION_VERSIONS
    out = {_pf8(pin)}
    out.update(_pf8(pin, mime) for mime in EXTRACTION_VERSIONS)
    return out


def _artifact_mime(art: CacheArtifact) -> str:
    """Best-effort recovery of the MIME an artifact's chunks were extracted
    from, straight from the chunk metadata publish_file stamped (drive.py
    stamps `metadata["mime_type"]` on every Drive-sourced chunk, independent
    of this module). Read from the artifact itself rather than reverse-
    mapping its pf8 through EXTRACTION_VERSIONS, since a shared pf8 can't
    always be traced back to one specific MIME."""
    if not art.chunks:
        return ""
    return (art.chunks[0].metadata or {}).get("mime_type", "")


def _parse_name(name: str):
    """`<file_id>.<hash12>.<pf8>.mbc.gz` -> (file_id, hash12, pf8), else None.
    Drive file ids never contain '.', so an rsplit is unambiguous."""
    if not name.endswith(".mbc.gz"):
        return None
    base = name[: -len(".mbc.gz")]
    parts = base.rsplit(".", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _decode_vec(embedding_b64: str, dim: int) -> list[float]:
    raw = base64.b64decode(embedding_b64)
    if len(raw) != dim * 4:
        raise ValueError(f"embedding length {len(raw)} != dim {dim} * 4")
    return list(struct.unpack(f"<{dim}f", raw))


def _encode_vec(vector) -> str:
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


# -- read path --------------------------------------------------------------

def _write_rows(store, art: CacheArtifact, rows: list[dict], mark_enriched: bool,
                logic_v: int, home) -> tuple[bool, bool]:
    """Write a validated artifact's rows: through the reflow carry-over when
    this install already holds the same file content, else a plain replace.

    Returns (written, apply_extraction). written False = store write failed
    (caller treats it as a cache miss). apply_extraction says whether the
    artifact's cached extraction should now be applied to the graph: on the
    plain path whenever the artifact is marked enriched (unchanged); on the
    carry-over path only when the import marked text enriched that this
    install had NOT extracted -- re-applying a peer's extraction over content
    already extracted locally writes a second set of Drive-sourced
    actions/decisions (graph_write dedups relations, not those).

    Raises store.ReflowOrphanError (rolled back, nothing written) rather than
    fall back to a replace that would silently strand this install's
    provenance, and ImportDeferred when an in-flight enrichment unit names
    the file."""
    done = _reflow_rows(store, art, rows, mark_enriched, logic_v, home)
    if done is not None:
        return done
    return _replace_rows(store, art, rows), mark_enriched


def _reflow_rows(store, art: CacheArtifact, rows: list[dict], mark_enriched: bool,
                 logic_v: int, home) -> tuple[bool, bool] | None:
    """Carry-over import (spec 2026-09-24 §3/§4 "Shared-drive ingest cache").

    Applies only when this install already has chunks for the file and EVERY
    one of them, and every artifact row, carries the same Drive `modified` —
    the proof the source is unchanged and only its chunking/extraction moved.
    Then the artifact rows are planned against the local ones and applied with
    Store.apply_reflow, so local enrichment state survives on covered text and
    every provenance reference (relations, observations, actions, ...) is
    remapped instead of left pointing at a replaced or deleted positional id.

    The artifact carries rendered text only, so each row's single span is its
    whole text (coverage proven on whole-chunk text — conservative). When the
    artifact's own enrichment clears the version gates (`mark_enriched`) AND
    at least one covered row was not enriched locally, the covered rows are
    marked enriched at the artifact's logic version and the extraction is
    applied (the second element of the result); when every covered row was
    already enriched locally, the plan's carried state stands and nothing is
    re-applied. An uncovered row is text this install never extracted and
    always stays enriched=0.

    While the reflow is halted (a wrong remap was caught), raise
    ImportDeferred: carry-over waits for the attended resume like every
    other reflow path.

    Spec §3 guard: if a pending or claimed enrichment unit names this file
    (its `gdrive-<fid>` thread, the file id, or any of its current doc_ids),
    raise ImportDeferred -- drain applying that unit after the re-chunk
    would mark never-extracted text enriched.

    Returns None when the carry-over does not apply (caller takes the plain
    replace path), else (written, apply_extraction). A plan that would drop a
    whole lineage describes a genuinely different document and takes the
    plain path.
    """
    old = store.owner_chunks([f"gdrive-{art.file_id}-"])
    if not old or not rows:
        return None
    modified = (rows[0]["metadata"] or {}).get("modified")
    if not modified or any((r["metadata"] or {}).get("modified") != modified
                           for r in list(old) + rows):
        return None
    from mcpbrain import reflow
    from mcpbrain.store import REFLOW_HALT_CURSOR, ReflowOrphanError
    from mcpbrain.sync.normalise import Chunk
    halted = store.get_cursor(REFLOW_HALT_CURSOR)
    if halted:
        # A wrong remap stopped the reflow; every carry-over path waits for
        # the attended `bin/reflow.py resume` (spec §3), this one included.
        raise ImportDeferred(f"ingest_cache: {art.file_id} carry-over deferred: "
                             f"reflow halted ({halted[:120]})")
    refs = reflow.pending_unit_refs(home)
    if refs and ({f"gdrive-{art.file_id}", art.file_id} | {r["doc_id"] for r in old}) & refs:
        raise ImportDeferred(
            f"ingest_cache: {art.file_id} has an in-flight enrichment unit; "
            "carry-over import deferred")
    new = [Chunk(r["doc_id"], r["text"], r["content_hash"], r["metadata"], [r["text"]])
           for r in rows]
    try:
        plan = reflow.plan(old, new)
    except ValueError:
        return None
    if any(why == "lineage_gone" for why in plan.reasons.values()):
        log.info("ingest_cache: %s reflow plan drops a whole lineage; plain replace",
                 art.file_id)
        return None
    apply_extraction = mark_enriched and any(
        nr.covered and not nr.enriched for nr in plan.rows)
    if apply_extraction:
        for nr in plan.rows:
            if nr.covered:
                nr.enriched, nr.enriched_version = 1, logic_v
    vectors = {r["doc_id"]: r["vector"] for r in rows}
    try:
        out = store.apply_reflow(art.file_id, "drive_import", plan,
                                 [vectors[nr.chunk.doc_id] for nr in plan.rows])
    except ReflowOrphanError:
        log.error("ingest_cache: reflow import of %s would orphan references; "
                  "rolled back", art.file_id)
        raise
    except Exception as exc:  # noqa: BLE001 — same contract as the plain write
        log.warning(
            "ingest_cache: reflow write failed importing artifact for %s "
            "(NOT a cache-corruption signal): %s", art.file_id, exc)
        return False, False
    log.info("ingest_cache: %s imported by carry-over: %s", art.file_id, out)
    return True, apply_extraction


def _replace_rows(store, art: CacheArtifact, rows: list[dict]) -> bool:
    """Plain replace: write every row and sweep the file's orphaned tail, in
    ONE transaction. False = store write failed."""
    try:
        # B5, cache-import half. Spec 2 closed orphan-on-shrink for locally
        # extracted files (drive.upsert_file_chunks) but skipped this path to
        # avoid racing this function's own transaction. That transaction, it
        # turns out, is `Store.import_cached_chunks` — which opens and commits
        # its OWN `_connect(write=True)` block and returns; a sweep run after
        # that call would be a SEPARATE transaction, not atomic with it. So
        # instead of delegating to it, the row-write (`_write_cached_chunk_row`,
        # the same per-row helper import_cached_chunks itself calls) and the
        # orphan sweep both run against ONE connection opened here — genuinely
        # one transaction: both land or neither does.
        #
        # Without it, a shared-drive file that shrank and is served from cache
        # keeps indices n..m-1 searchable indefinitely, and expansion re-feeds
        # them as current content.
        written = {row["doc_id"] for row in rows}
        with store._connect(write=True) as db:
            for row in rows:
                store._write_cached_chunk_row(
                    db, row["doc_id"], row["text"], row["content_hash"],
                    row["metadata"], row["vector"],
                    enriched=row.get("enriched", False),
                    enriched_version=row.get("enriched_version", 0))
            # Exact metadata.file_id match (index-backed by idx_chunks_fileid),
            # not a doc_id LIKE or RANGE predicate: `LIKE ... ESCAPE` disables
            # SQLite's LIKE-to-index optimisation and silently turns a doc_id
            # prefix match into a full `SCAN chunks` (the 0.7.105
            # chunks_for_file incident), and a RANGE bound built from
            # f"gdrive-{art.file_id}" has the same failure mode
            # store.doc_ids_for_file's docstring describes: Drive file ids use
            # the base64url alphabet (embed '-'), so one file's id can be a
            # '-'-delimited prefix of another's and a range/prefix match can't
            # tell them apart. Equality on the metadata field can.
            existing = [r["doc_id"] for r in db.execute(
                f"SELECT doc_id FROM chunks WHERE {_meta_extract('$.file_id')}=?",
                (art.file_id,)).fetchall()]
            stale = [d for d in existing if d not in written]
            if stale:
                log.info("ingest_cache: %s shrank; deleting %d orphaned chunk(s)",
                         art.file_id, len(stale))
                # NOT the payload: this file shrank, it did not go away, and
                # its cached extraction still describes it. (Before the re-key
                # this deleted the stale chunks' own rows while the file's
                # others survived, so the payload effectively stayed anyway.)
                qs = ",".join("?" * len(stale))
                stale_rowids = [r["rowid"] for r in db.execute(
                    f"SELECT rowid FROM chunks WHERE doc_id IN ({qs})", stale).fetchall()]
                if stale_rowids:
                    ph = ",".join("?" * len(stale_rowids))
                    db.execute(f"DELETE FROM vec_chunks WHERE rowid IN ({ph})", stale_rowids)
                    db.execute(f"DELETE FROM fts_chunks WHERE rowid IN ({ph})", stale_rowids)
                    db.execute(f"DELETE FROM chunks WHERE rowid IN ({ph})", stale_rowids)
    except Exception as exc:  # noqa: BLE001 — real infra failure, not cache corruption
        log.warning(
            "ingest_cache: store write failed importing artifact for %s "
            "(NOT a cache-corruption signal): %s", art.file_id, exc)
        return False
    return True


def _import_artifact(store, drive_id: str, art: CacheArtifact, pin,
                     contextual_retrieval: bool | None = None, mime: str = "",
                     home=None) -> bool:
    """Import a validated artifact's chunks into the store, atomically.

    All chunk vectors are decoded/validated UP FRONT, before anything is
    written; if any chunk is corrupt this returns False having written
    NOTHING (never a partial import). Once every chunk validates, all rows
    are written AND the file's now-orphaned tail chunks (B5, cache-import
    half — see the inline comment below) are swept in one single transaction
    (one `store._connect(write=True)` block, using the same per-row helper
    `Store.import_cached_chunks` itself calls) so the artifact lands
    completely, with no stale chunks left behind, or not at all. Callers
    treat False as a cache miss (fall back to local extraction). When this
    install already holds the same file content (same Drive `modified`), the
    write goes through the reflow carry-over instead (see _reflow_rows), which
    may raise store.ReflowOrphanError (rolled back, nothing written) or
    ImportDeferred (an in-flight enrichment unit names the file; nothing
    written). `home` locates the enrichment queue (default config.app_dir()).

    `contextual_retrieval`, when not None, must match the artifact's stamped
    enrich["contextual_retrieval"] flag (when present) — see try_import.

    `mime`, when passed, must match the pipeline fingerprint the artifact was
    published under (see effective_chunker_version)."""
    if (art.embed_model != pin.embed_model or int(art.dim) != int(pin.dim)
            or art.chunker_version != effective_chunker_version(pin, mime)
            or int(art.dim) != int(store.dim)):
        return False
    try:
        # Guard enrich field access; if malformed (e.g. string instead of dict),
        # fall back rather than raise.
        logic_v = int(art.enrich.get("logic_version", 0)) if art.enrich else 0
        # The Q6 contextual-retrieval prefix materially changes the embedding
        # vector and is NOT part of pipeline_fingerprint (embed_model/dim/
        # chunker_version only). Two installs sharing an org_pin but differing
        # on this LOCAL config flag could otherwise import an artifact carrying
        # a semantically different vector under the "cache hit is bit-identical
        # to local embedding" guarantee. contextual_retrieval=None (the default)
        # means "don't check, accept as before" for backward compatibility.
        if contextual_retrieval is not None and art.enrich:
            art_cr = art.enrich.get("contextual_retrieval")
            if art_cr is not None and bool(art_cr) != bool(contextual_retrieval):
                return False
        # Skip local re-enrichment only when the cached enrichment is at least as new
        # as BOTH the fleet floor and this install's own logic version.
        mark_enriched = bool(art.enrich) and logic_v >= max(int(pin.enrich_logic_floor),
                                                            int(ENRICH_LOGIC_VERSION))
        rows = []
        for cc in art.chunks:
            try:
                vector = _decode_vec(cc.embedding_b64, int(art.dim))
            except Exception:
                log.info("ingest_cache: corrupt vector in %s chunk %s (fallback)", art.file_id, cc.idx)
                return False
            meta = dict(cc.metadata or {})
            meta[DRIVE_ID_META_KEY] = drive_id
            doc_id = f"gdrive-{art.file_id}-{int(cc.idx)}"
            rows.append({
                "doc_id": doc_id, "text": cc.text, "content_hash": _text_hash(cc.text),
                "metadata": meta, "vector": vector, "enriched": mark_enriched,
                "enriched_version": logic_v if mark_enriched else 0,
            })
    except Exception:
        log.info("ingest_cache: corrupt artifact %s (fallback to local)", art.file_id)
        return False
    if home is None:
        from mcpbrain import config as _cfg
        home = _cfg.app_dir()
    written, apply_extraction = _write_rows(store, art, rows, mark_enriched, logic_v, home)
    if not written:
        return False
    # A#4: apply the cached enrichment so the importer's graph gets this doc's
    # entities/relations without re-running Haiku. Validate through the SAME
    # guards drain uses before apply — never apply a peer's payload raw.
    extraction = (art.enrich or {}).get("extraction") if apply_extraction else None
    if extraction:
        try:
            from mcpbrain import contract, graph_write, config as _config
            clean, _ = contract.sanitize_batch({"extractions": [extraction]})
            cand = (clean.get("extractions") or [extraction])[0]
            if not contract.validate_extraction(cand):      # [] == valid
                if _config.schema_grounding_enabled(str(_config.app_dir())):
                    from mcpbrain.drain import _grounding_filter
                    cand, _ = _grounding_filter(cand)
                doc_ids = [f"gdrive-{art.file_id}-{int(c.idx)}" for c in art.chunks]
                graph_write.apply(store, cand, doc_ids=doc_ids)   # self-resolves owner/home
        except Exception as exc:  # noqa: BLE001 — apply failure must not fail the import
            log.info("ingest_cache: cached-enrichment apply skipped for %s: %s",
                     art.file_id, exc, exc_info=True)
    return True


def _load(fleet_storage, path) -> CacheArtifact | None:
    try:
        data = fleet_storage.get_bytes(path)
    except Exception as exc:  # noqa: BLE001 — a real storage I/O error (not a
        # None/corrupt-bytes cache miss) must still fail safe: this module's
        # contract is "never raise into the sync loop" for every fetch, and an
        # unguarded exception here would propagate out of try_import and skip
        # the whole remaining file list for the drive that cycle.
        log.info("ingest_cache: get_bytes failed for %s (fallback to local): %s", path, exc)
        return None
    if data is None:
        return None
    try:
        return CacheArtifact.from_dict(json.loads(gzip.decompress(data).decode("utf-8")))
    except Exception:
        log.info("ingest_cache: corrupt artifact %s (fallback to local)", path)
        return None


def try_import(store, fleet_storage, drive_id, file_id, content_hash, pin,
               *, contextual_retrieval: bool | None = None, mime: str = "",
               home=None) -> bool:
    """Cache-first import for one shared-drive file version. Returns True iff the
    artifact was found, validated, and imported; False => caller extracts locally.

    Two exceptions escape, both with NOTHING written, and neither may be
    treated as False (a local re-extract has the same exposure):
    - ImportDeferred: this install already holds the file's content and a
      pending/claimed enrichment unit names it, so the carry-over import must
      wait. Retry later (work_queue backoff).
    - store.ReflowOrphanError: the carry-over's orphan guard fired and the
      transaction rolled back. A wrong remap must stop, not be papered over
      by a plain replace. Log loudly; do not abort other files over it.

    `home` locates the enrichment queue for the in-flight-unit guard
    (default config.app_dir()).

    `content_hash` is the Drive file-version id (NOT the text hash).

    `contextual_retrieval`, when passed, must match the artifact's stamped
    contextual-retrieval flag (see publish); a mismatch is treated as a
    pipeline mismatch and falls back to False. Default None = don't check
    (backward compatible with callers unaware of the flag).

    `mime`, when passed, selects the +x<N>-suffixed fingerprint for a
    block-extracted MIME (see effective_chunker_version); default "" preserves
    today's behaviour for callers unaware of the flag."""
    if not pin.is_pinned:
        return False
    art = _load(fleet_storage, _artifact_path(file_id, content_hash, pin, mime))
    if art is None:
        return False
    if art.file_id != file_id or art.content_hash != content_hash:
        return False
    return _import_artifact(store, drive_id, art, pin,
                            contextual_retrieval=contextual_retrieval, mime=mime,
                            home=home)


def collect_chunks(store, file_id) -> list[CacheChunk]:
    """Build drive-neutral CacheChunks from a locally-embedded file. Strips the
    drive_id key so two publishers of the same file version emit byte-identical
    artifacts (content-hash keying then makes races harmless)."""
    out = []
    for row in store.chunks_for_file(file_id):
        vec = store.embedding_for_doc(row["doc_id"])
        if vec is None:
            continue
        meta = dict(row["metadata"])
        meta.pop(DRIVE_ID_META_KEY, None)
        out.append(CacheChunk(idx=int(row["idx"]), text=row["text"],
                              embedding_b64=_encode_vec(vec), metadata=meta))
    return out


# -- write path -------------------------------------------------------------

def publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin,
            *, enrich=None, published_by="", contextual_retrieval: bool = False,
            skip_gc: bool = False, mime: str = "") -> None:
    """Write the gzip-JSON CacheArtifact for `chunks` (a sequence of CacheChunk),
    then best-effort GC older/stale artifacts for this file. No-op when unpinned
    or chunks is empty. Content-hash keying makes concurrent publishers idempotent
    (byte-equivalent artifacts, last-write-wins is harmless).

    `contextual_retrieval` reflects whether the Q6 contextual-retrieval prefix
    was enabled for the install doing the publishing. It is stamped into the
    artifact's free-form `enrich` dict (not part of the frozen CacheArtifact
    fields or pipeline_fingerprint) so _import_artifact can detect a pipeline
    mismatch on the read side. Defaults False so existing callers are
    unaffected; real wiring of the actual per-install value happens
    elsewhere.

    `skip_gc`, when True, skips the internal single-file `gc_superseded` call
    after the artifact is written. Default False preserves today's exact
    behaviour for every existing caller. Set True when the caller is about to
    (or already has) run `gc_superseded_batch` itself over the whole set of
    files being published this cycle — the per-file listing this call would
    otherwise do is then pure redundant O(n) work on top of that O(1) batch.

    `mime`, when passed, selects the +x<N>-suffixed fingerprint for a
    block-extracted MIME (see effective_chunker_version); default "" preserves
    today's fingerprint for callers unaware of the flag."""
    if not pin.is_pinned or not chunks:
        return
    chunks = tuple(chunks)
    extraction_method = (chunks[0].metadata or {}).get("extraction_method", "")
    enrich_block = dict(enrich or {})
    enrich_block["contextual_retrieval"] = contextual_retrieval
    art = CacheArtifact(
        file_id=file_id, content_hash=content_hash,
        extraction_method=extraction_method,
        chunker_version=effective_chunker_version(pin, mime),
        embed_model=pin.embed_model, dim=int(pin.dim), chunks=chunks,
        enrich=enrich_block, published_by=published_by,
        published_at=datetime.now(timezone.utc).isoformat())
    # sort_keys makes the "byte-identical artifacts" guarantee actually true:
    # plain json.dumps preserves dict insertion order, so two publishers whose
    # extractors happen to build metadata keys in different orders would
    # otherwise emit different bytes for logically-identical content.
    data = gzip.compress(
        json.dumps(art.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    fleet_storage.put_bytes(_artifact_path(file_id, content_hash, pin, mime), data)
    if not skip_gc:
        try:
            gc_superseded(fleet_storage, drive_id, file_id, content_hash, pin)
        except Exception as exc:  # noqa: BLE001 — GC failure must not fail the publish
            log.info("ingest_cache: gc_superseded skipped for %s: %s", file_id, exc)


def publish_file(store, fleet_storage, drive_id, file_id, content_hash, pin,
                 *, enrich=None, published_by="", contextual_retrieval: bool = False,
                 skip_gc: bool = False) -> bool:
    """Collect a locally-embedded file's chunks from the store and publish them.
    Returns True if an artifact was written.

    When `enrich` is not explicitly passed, looks up the file's validated
    extraction payload (`store.get_enrich_payload(file_id)`); if it exists at
    or above the fleet floor (max of `pin.enrich_logic_floor` and this
    install's `ENRICH_LOGIC_VERSION`), it's attached as
    `{"logic_version": N, "extraction": <dict>}` so importers can skip
    re-enrichment. One row per file — chunks share the unit's extraction.
    Falls back to unchanged behaviour (no payload) when nothing qualifies.

    `skip_gc` is forwarded to `publish` — see its docstring.

    `mime` is derived from the first collected chunk's `metadata["mime_type"]`
    (all chunks of one file share the same source mime) and forwarded to
    `publish`, so this file publishes under the MIME-appropriate fingerprint
    with no caller change required."""
    if not pin.is_pinned:
        return False
    chunks = collect_chunks(store, file_id)
    if not chunks:
        return False
    if (enrich is None and store.file_fully_enriched(file_id)
            and store.enrich_payload_covers_file(file_id)):
        # Only a file whose every (non-cold) chunk is enriched AND whose payload
        # was made from every one of those chunks: a plain-path importer marks
        # every row enriched from it. After a reflow, re-enrichment extracts
        # only the uncovered rows and drain's payload describes just those, so
        # "fully enriched" alone would publish a partial extraction.
        floor = max(int(pin.enrich_logic_floor), int(ENRICH_LOGIC_VERSION))
        row = store.get_enrich_payload(file_id)
        if row and int(row["logic_version"]) >= floor:
            enrich = {"logic_version": int(row["logic_version"]),
                      "extraction": json.loads(row["payload"])}
    mime = (chunks[0].metadata or {}).get("mime_type", "")
    publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin,
            enrich=enrich, published_by=published_by,
            contextual_retrieval=contextual_retrieval, skip_gc=skip_gc, mime=mime)
    return True


# -- GC / lifecycle ---------------------------------------------------------

def _safe_delete(fleet_storage, path) -> bool:
    """Delete `path`, swallowing any exception (fail-safe: a delete failure
    must never abort the caller's GC/sweep/removal loop). Returns True if the
    delete succeeded, False (and logs at info) if it raised."""
    try:
        fleet_storage.delete(path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("ingest_cache: failed to delete %s: %s", path, exc)
        return False


def _cache_names(fleet_storage):
    for path in fleet_storage.list_paths(CACHE_DIR + "/"):
        name = path.rsplit("/", 1)[-1]
        parsed = _parse_name(name)
        if parsed:
            yield path, parsed
        else:
            log.info("ingest_cache: skipping unparseable cache filename %s", name)


def gc_superseded(fleet_storage, drive_id, file_id, keep_content_hash, pin) -> int:
    """Delete artifacts for `file_id` with the current pipeline fingerprint whose
    content hash differs from keep_content_hash. Artifacts from other pipelines
    coexist (never GC'd — see spec A2 version-skew guarantee). Returns count.

    Single-file signature — frozen, other subsystems call this directly. Lists
    the whole cache folder once per call; a publish loop over many files should
    prefer gc_superseded_batch to avoid an O(n^2) Drive-API listing cost."""
    keep12 = keep_content_hash[:12]
    cur_pf8s = _current_pipeline_fingerprints(pin)
    removed = 0
    for path, (fid, h12, pf8) in _cache_names(fleet_storage):
        if fid != file_id:
            continue
        # Only GC same-pipeline artifacts with stale content hashes;
        # leave artifacts from other pipelines alone (they coexist). "Same
        # pipeline" includes every block-MIME's +x<N>-suffixed fingerprint,
        # not just the unsuffixed base — otherwise a superseded PDF/DOCX/
        # PPTX/RTF/Google-Doc artifact would never be collected (spec A2
        # says other pipelines coexist; a stale version of THIS pipeline
        # must not).
        if pf8 in cur_pf8s and h12 != keep12:
            if _safe_delete(fleet_storage, path):
                removed += 1
    return removed


def gc_superseded_batch(fleet_storage, drive_id, keep_map: dict[str, str], pin) -> int:
    """Batch form of gc_superseded: GC stale same-pipeline artifacts for MANY
    files in one cache-folder listing instead of one listing per file.

    `keep_map` is {file_id: keep_content_hash}. Applies the identical
    per-file "same pipeline + stale content hash -> delete" rule as
    gc_superseded to every (file_id, keep_hash) pair, but calls
    _cache_names/list_paths exactly ONCE regardless of how many files are in
    the batch. Returns the total count removed across the whole batch.
    """
    keep12_by_fid = {fid: h[:12] for fid, h in keep_map.items()}
    cur_pf8s = _current_pipeline_fingerprints(pin)
    removed = 0
    for path, (fid, h12, pf8) in _cache_names(fleet_storage):
        keep12 = keep12_by_fid.get(fid)
        if keep12 is None:
            continue
        if pf8 in cur_pf8s and h12 != keep12:
            if _safe_delete(fleet_storage, path):
                removed += 1
    return removed


def sweep_drive(fleet_storage, live_file_ids) -> int:
    """Opportunistically delete artifacts whose file id is not in live_file_ids
    (the set of files currently present in the drive). Returns count deleted."""
    live = set(live_file_ids)
    removed = 0
    for path, (fid, _h12, _pf8) in _cache_names(fleet_storage):
        if fid not in live:
            if _safe_delete(fleet_storage, path):
                removed += 1
    return removed


def remove_file_artifacts(fleet_storage, file_id) -> int:
    """Delete every artifact (all content hashes / pipelines) for one file —
    used when a file is deleted (changes.list removal event). Returns count."""
    removed = 0
    for path, (fid, _h12, _pf8) in _cache_names(fleet_storage):
        if fid == file_id:
            if _safe_delete(fleet_storage, path):
                removed += 1
    return removed


# -- bulk import (onboarding) + revocation ----------------------------------

def bootstrap_drive(store, fleet_storage, drive_id, pin, *, home=None) -> dict:
    """Bulk-import all cache artifacts for a drive whose pipeline matches `pin`.
    For a file with several content versions, the newest published_at wins.
    Returns {'imported','chunks','skipped','cache_hits'} where cache_hits ==
    imported (files served from cache). Subsystem C sums cache_hits across drives
    for its onboarding summary (spec §C.2).

    A file whose carry-over import raises ImportDeferred or
    store.ReflowOrphanError is logged, counted as skipped, and the loop moves
    on; nothing was written for it. `home` is forwarded to _import_artifact."""
    summary = {"imported": 0, "chunks": 0, "skipped": 0, "cache_hits": 0}
    if not pin.is_pinned:
        return summary
    cur_pf8s = _current_pipeline_fingerprints(pin)
    best: dict[str, tuple[str, CacheArtifact]] = {}   # file_id -> (published_at, art)
    for path, (fid, _h12, pf8) in _cache_names(fleet_storage):
        # A pf8 outside the current set is a genuinely different pipeline
        # (embed_model/dim/chunker_version) and coexists untouched — never
        # counted as skipped. A pf8 IN the set but for a block MIME whose
        # artifact turns out to be a stale pre-suffix one is handled below,
        # by _import_artifact's own version check (mime derived from the
        # artifact), and DOES count as skipped: it was a real candidate that
        # was correctly rejected, not silently filtered out here.
        if pf8 not in cur_pf8s:
            continue
        art = _load(fleet_storage, path)
        if art is None:
            continue
        prev = best.get(fid)
        if prev is None or (art.published_at or "") > prev[0]:
            best[fid] = (art.published_at or "", art)
    from mcpbrain.store import ReflowOrphanError
    for _fid, (_pa, art) in best.items():
        # One file's refusal must not abort the rest of the drive's bootstrap
        # (onboarding would otherwise re-fail on the same file forever).
        try:
            ok = _import_artifact(store, drive_id, art, pin, mime=_artifact_mime(art),
                                  home=home)
        except ImportDeferred as exc:
            log.info("ingest_cache: bootstrap skipped %s: %s", art.file_id, exc)
            ok = False
        except ReflowOrphanError as exc:
            log.error("ingest_cache: bootstrap skipped %s, reflow orphan guard: %s",
                      art.file_id, exc)
            ok = False
        if ok:
            summary["imported"] += 1
            summary["chunks"] += len(art.chunks)
        else:
            summary["skipped"] += 1
    summary["cache_hits"] = summary["imported"]
    return summary


def purge_drive(store, drive_id) -> dict:
    """Access-revocation purge (spec §A3): bitemporally invalidate local relations
    sourced from this drive's docs, then delete its chunks/vectors/FTS. org rows
    are untouched. Returns a summary dict."""
    doc_ids = store.doc_ids_for_drive(drive_id)
    invalidated = store.invalidate_local_relations_for_docs(doc_ids)
    deleted = store.delete_chunks(doc_ids)
    # warning, not info: purging a drive's entire cached content because access
    # was revoked is exactly the kind of event an operator needs to see without
    # hunting through info-level noise.
    log.warning("ingest_cache: purged drive %s — %d chunks, %d relations invalidated",
                drive_id, deleted, invalidated)
    return {"drive_id": drive_id, "docs": len(doc_ids),
            "chunks_deleted": deleted, "relations_invalidated": invalidated}


# -- consecutive-absence revocation counter (spec §A3) ----------------------

_KNOWN_DRIVES_META = "ingest_cache.known_drives"


def _absence_key(drive_id: str) -> str:
    return f"ingest_cache.absent:{drive_id}"


def note_drive_presence(store, present_ids, *, threshold: int = 3) -> dict:
    """Track per-drive consecutive absence and auto-purge after `threshold`
    consecutive missing cycles (spec §A3: guard against a transient Drive glitch
    reading as revocation). State lives in the meta table — no schema, no cadence.

    A present drive resets its counter and is remembered; a known drive absent for
    `threshold` cycles is purge_drive'd and forgotten. Returns {'purged','tracked'}.
    """
    try:
        known = set(json.loads(store.get_meta(_KNOWN_DRIVES_META) or "[]"))
    except (ValueError, TypeError):
        known = set()
    present = set(present_ids)

    # Data-safety guard: a TOTAL disappearance (we knew >=1 drive, now enumerate
    # zero) is far more likely a transient Drive-API hiccup / scope blip / 200-with-
    # empty during an incident than every drive being revoked at once. list_shared_
    # drives returning [] does not raise, so without this it would sail into the
    # counter and, after `threshold` such glitchy cycles, purge ALL cached content.
    # Skip counting entirely this cycle: never advance absence toward a destructive
    # purge on a blanket-empty enumeration. A genuine single-drive revocation still
    # shows the drive absent while OTHERS remain present, which is counted normally.
    if not present and known:
        log.warning("ingest_cache: shared-drive enumeration returned nothing while "
                    "%d drive(s) were known — treating as a transient glitch, not "
                    "revocation; absence counters unchanged", len(known))
        return {"purged": [], "tracked": len(known)}

    known |= present
    purged = []
    for d in sorted(known):
        if d in present:
            store.set_meta(_absence_key(d), "0")
            continue
        try:
            n = int(store.get_meta(_absence_key(d)) or "0") + 1
        except (ValueError, TypeError):
            n = 1
        if n >= threshold:
            log.warning(
                "ingest_cache: drive %s absent for %d consecutive cycles "
                "(threshold %d) — purging as revoked", d, n, threshold)
            purge_drive(store, d)
            purged.append(d)
            # The drive is being forgotten (removed from `known` below), so its
            # absence counter must be deleted, not reset to "0" — otherwise it
            # accumulates forever as an orphan meta row.
            store.delete_meta(_absence_key(d))
        else:
            store.set_meta(_absence_key(d), str(n))
    known -= set(purged)
    store.set_meta(_KNOWN_DRIVES_META, json.dumps(sorted(known)))
    return {"purged": purged, "tracked": len(known)}
