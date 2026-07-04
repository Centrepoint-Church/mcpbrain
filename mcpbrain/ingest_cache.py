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
import struct
from datetime import datetime, timezone

from mcpbrain.chunking import content_hash as _text_hash
from mcpbrain.org_contracts import (
    CacheArtifact, CacheChunk, DRIVE_ID_META_KEY,
    artifact_filename, pipeline_fingerprint,
)
from mcpbrain.store import ENRICH_LOGIC_VERSION

log = logging.getLogger(__name__)

CACHE_DIR = ".mcpbrain-cache"


# -- filename / path helpers ------------------------------------------------

def _pf8(pin) -> str:
    return pipeline_fingerprint(pin.embed_model, pin.dim, pin.chunker_version)[:8]


def _artifact_path(file_id: str, content_hash: str, pin) -> str:
    return f"{CACHE_DIR}/{artifact_filename(file_id, content_hash, pin.embed_model, pin.dim, pin.chunker_version)}"


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

def _import_artifact(store, drive_id: str, art: CacheArtifact, pin) -> bool:
    """Import a validated artifact's chunks into the store, atomically.

    All chunk vectors are decoded/validated UP FRONT, before anything is
    written; if any chunk is corrupt this returns False having written
    NOTHING (never a partial import). Once every chunk validates, all rows
    are written in a single store transaction (Store.import_cached_chunks)
    so the artifact lands completely or not at all. Callers treat False as
    a cache miss (fall back to local extraction)."""
    if (art.embed_model != pin.embed_model or int(art.dim) != int(pin.dim)
            or art.chunker_version != pin.chunker_version
            or int(art.dim) != int(store.dim)):
        return False
    try:
        # Guard enrich field access; if malformed (e.g. string instead of dict),
        # fall back rather than raise.
        logic_v = int(art.enrich.get("logic_version", 0)) if art.enrich else 0
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
    try:
        store.import_cached_chunks(rows)
    except Exception as exc:  # noqa: BLE001 — real infra failure, not cache corruption
        log.warning(
            "ingest_cache: store write failed importing artifact for %s "
            "(NOT a cache-corruption signal): %s", art.file_id, exc)
        return False
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


def try_import(store, fleet_storage, drive_id, file_id, content_hash, pin) -> bool:
    """Cache-first import for one shared-drive file version. Returns True iff the
    artifact was found, validated, and imported; False => caller extracts locally.

    `content_hash` is the Drive file-version id (NOT the text hash)."""
    if not pin.is_pinned:
        return False
    art = _load(fleet_storage, _artifact_path(file_id, content_hash, pin))
    if art is None:
        return False
    if art.file_id != file_id or art.content_hash != content_hash:
        return False
    return _import_artifact(store, drive_id, art, pin)


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
            *, enrich=None, published_by="") -> None:
    """Write the gzip-JSON CacheArtifact for `chunks` (a sequence of CacheChunk),
    then best-effort GC older/stale artifacts for this file. No-op when unpinned
    or chunks is empty. Content-hash keying makes concurrent publishers idempotent
    (byte-equivalent artifacts, last-write-wins is harmless)."""
    if not pin.is_pinned or not chunks:
        return
    chunks = tuple(chunks)
    extraction_method = (chunks[0].metadata or {}).get("extraction_method", "")
    art = CacheArtifact(
        file_id=file_id, content_hash=content_hash,
        extraction_method=extraction_method, chunker_version=pin.chunker_version,
        embed_model=pin.embed_model, dim=int(pin.dim), chunks=chunks,
        enrich=enrich or {}, published_by=published_by,
        published_at=datetime.now(timezone.utc).isoformat())
    # sort_keys makes the "byte-identical artifacts" guarantee actually true:
    # plain json.dumps preserves dict insertion order, so two publishers whose
    # extractors happen to build metadata keys in different orders would
    # otherwise emit different bytes for logically-identical content.
    data = gzip.compress(
        json.dumps(art.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8"))
    fleet_storage.put_bytes(_artifact_path(file_id, content_hash, pin), data)
    try:
        gc_superseded(fleet_storage, drive_id, file_id, content_hash, pin)
    except Exception as exc:  # noqa: BLE001 — GC failure must not fail the publish
        log.info("ingest_cache: gc_superseded skipped for %s: %s", file_id, exc)


def publish_file(store, fleet_storage, drive_id, file_id, content_hash, pin,
                 *, enrich=None, published_by="") -> bool:
    """Collect a locally-embedded file's chunks from the store and publish them.
    Returns True if an artifact was written."""
    if not pin.is_pinned:
        return False
    chunks = collect_chunks(store, file_id)
    if not chunks:
        return False
    publish(store, fleet_storage, drive_id, file_id, content_hash, chunks, pin,
            enrich=enrich, published_by=published_by)
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
    cur_pf8 = _pf8(pin)
    removed = 0
    for path, (fid, h12, pf8) in _cache_names(fleet_storage):
        if fid != file_id:
            continue
        # Only GC same-pipeline artifacts with stale content hashes;
        # leave artifacts from other pipelines alone (they coexist).
        if pf8 == cur_pf8 and h12 != keep12:
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
    cur_pf8 = _pf8(pin)
    removed = 0
    for path, (fid, h12, pf8) in _cache_names(fleet_storage):
        keep12 = keep12_by_fid.get(fid)
        if keep12 is None:
            continue
        if pf8 == cur_pf8 and h12 != keep12:
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

def bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict:
    """Bulk-import all cache artifacts for a drive whose pipeline matches `pin`.
    For a file with several content versions, the newest published_at wins.
    Returns {'imported','chunks','skipped','cache_hits'} where cache_hits ==
    imported (files served from cache). Subsystem C sums cache_hits across drives
    for its onboarding summary (spec §C.2)."""
    summary = {"imported": 0, "chunks": 0, "skipped": 0, "cache_hits": 0}
    if not pin.is_pinned:
        return summary
    cur_pf8 = _pf8(pin)
    best: dict[str, tuple[str, CacheArtifact]] = {}   # file_id -> (published_at, art)
    for path, (fid, _h12, pf8) in _cache_names(fleet_storage):
        if pf8 != cur_pf8:
            continue
        art = _load(fleet_storage, path)
        if art is None:
            continue
        prev = best.get(fid)
        if prev is None or (art.published_at or "") > prev[0]:
            best[fid] = (art.published_at or "", art)
    for _fid, (_pa, art) in best.items():
        if _import_artifact(store, drive_id, art, pin):
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
