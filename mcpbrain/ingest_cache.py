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
    """Import a validated artifact's chunks into the store. Returns False on any
    per-chunk corruption (whole-file fallback) — never a partial import commit
    beyond what has already been written; callers treat False as a cache miss."""
    if (art.embed_model != pin.embed_model or int(art.dim) != int(pin.dim)
            or art.chunker_version != pin.chunker_version
            or int(art.dim) != int(store.dim)):
        return False
    logic_v = int(art.enrich.get("logic_version", 0)) if art.enrich else 0
    # Skip local re-enrichment only when the cached enrichment is at least as new
    # as BOTH the fleet floor and this install's own logic version.
    mark_enriched = bool(art.enrich) and logic_v >= max(int(pin.enrich_logic_floor),
                                                        int(ENRICH_LOGIC_VERSION))
    for cc in art.chunks:
        try:
            vector = _decode_vec(cc.embedding_b64, int(art.dim))
        except Exception:
            log.info("ingest_cache: corrupt vector in %s chunk %s (fallback)", art.file_id, cc.idx)
            return False
        meta = dict(cc.metadata or {})
        meta[DRIVE_ID_META_KEY] = drive_id
        doc_id = f"gdrive-{art.file_id}-{int(cc.idx)}"
        store.import_cached_chunk(
            doc_id, cc.text, _text_hash(cc.text), meta, vector,
            enriched=mark_enriched, enriched_version=logic_v if mark_enriched else 0)
    return True


def _load(fleet_storage, path) -> CacheArtifact | None:
    data = fleet_storage.get_bytes(path)
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
