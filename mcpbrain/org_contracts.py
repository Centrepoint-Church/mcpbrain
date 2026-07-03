"""Frozen shared vocabulary for the org-baseline feature (Phase 0).

Everything subsystems A (ingest cache), B (org graph), and C (onboarding) must
agree on lives here: the on-the-wire dataclasses, the pure fingerprint/HMAC
helpers, the config-view of the fleet pin, and the transport Protocol. Frozen
by design — changing a shape is a fleet-wide contract change, not a local edit.

House style: stdlib @dataclass only (no pydantic/TypedDict).
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

CACHE_ARTIFACT_SCHEMA = 1
CONTRIBUTION_SCHEMA = 1
SNAPSHOT_SCHEMA = 1

# Chunk-metadata JSON key naming the Shared Drive a chunk came from (nullable;
# absent/None => My Drive or shared-with-me). NOT a store column — see plan.
DRIVE_ID_META_KEY = "drive_id"

# Built-in relation allowlist floor; org-config may narrow/extend it via the pin.
DEFAULT_RELATION_ALLOWLIST = ("works_at", "member_of", "mentioned_with")


# -- wire dataclasses -------------------------------------------------------

@dataclass(frozen=True)
class CacheChunk:
    idx: int
    text: str
    embedding_b64: str
    metadata: dict


@dataclass(frozen=True)
class CacheArtifact:
    file_id: str
    content_hash: str
    extraction_method: str
    chunker_version: str
    embed_model: str
    dim: int
    chunks: tuple[CacheChunk, ...]
    enrich: dict
    published_by: str
    published_at: str
    schema: int = CACHE_ARTIFACT_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CacheArtifact":
        chunks = tuple(CacheChunk(**c) for c in d.get("chunks", []))
        return cls(
            file_id=d["file_id"], content_hash=d["content_hash"],
            extraction_method=d.get("extraction_method", ""),
            chunker_version=d.get("chunker_version", ""),
            embed_model=d["embed_model"], dim=int(d["dim"]),
            chunks=chunks, enrich=d.get("enrich") or {},
            published_by=d.get("published_by", ""),
            published_at=d.get("published_at", ""),
            schema=int(d.get("schema", CACHE_ARTIFACT_SCHEMA)))


@dataclass(frozen=True)
class ContributionRecord:
    claim: dict
    confidence: float
    valid_from: str
    contributor_email: str
    source_kind: str
    source_ref: str
    valid_to: str = ""
    schema: int = CONTRIBUTION_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ContributionRecord":
        return cls(
            claim=d["claim"], confidence=float(d.get("confidence", 1.0)),
            valid_from=d.get("valid_from", ""),
            contributor_email=d["contributor_email"],
            source_kind=d.get("source_kind", ""), source_ref=d["source_ref"],
            valid_to=d.get("valid_to", ""),
            schema=int(d.get("schema", CONTRIBUTION_SCHEMA)))


@dataclass(frozen=True)
class Tombstone:
    entity_id: str
    merged_into: str = ""   # "" => hard delete; else re-point target

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Tombstone":
        return cls(entity_id=d["entity_id"], merged_into=d.get("merged_into", ""))


@dataclass(frozen=True)
class SnapshotManifest:
    version: int
    created_at: str
    entity_count: int
    relation_count: int
    tombstone_count: int
    snapshot_sha256: str
    schema: int = SNAPSHOT_SCHEMA

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotManifest":
        return cls(
            version=int(d["version"]), created_at=d.get("created_at", ""),
            entity_count=int(d.get("entity_count", 0)),
            relation_count=int(d.get("relation_count", 0)),
            tombstone_count=int(d.get("tombstone_count", 0)),
            snapshot_sha256=d.get("snapshot_sha256", ""),
            schema=int(d.get("schema", SNAPSHOT_SCHEMA)))


@dataclass(frozen=True)
class FleetPin:
    """Config-view of the fleet-wide pin (org-config.json 'org_pin' block)."""
    embed_model: str = ""
    dim: int = 0
    chunker_version: str = ""
    enrich_logic_floor: int = 0
    relation_allowlist: tuple[str, ...] = DEFAULT_RELATION_ALLOWLIST
    fleet_secret: str = ""

    @property
    def is_pinned(self) -> bool:
        # A secret is required before anything is contributed; its presence is
        # the "org has configured the fleet" signal (spec Phase D ordering).
        return bool(self.fleet_secret)


# -- pure helpers -----------------------------------------------------------

def pipeline_fingerprint(embed_model: str, dim: int, chunker_version: str) -> str:
    """64-hex sha256 of the embedding pipeline identity. Cache artifacts from
    different pipelines never collide (spec A2)."""
    raw = f"{embed_model}|{int(dim)}|{chunker_version}".encode()
    return hashlib.sha256(raw).hexdigest()


def source_ref(fleet_secret: str, doc_id: str) -> str:
    """HMAC-SHA256(fleet_secret, doc_id) as 64-hex. Stable across users (so the
    curator dedupes echoes) yet reveals nothing about the source doc (spec B2)."""
    return hmac.new(fleet_secret.encode(), doc_id.encode(), hashlib.sha256).hexdigest()


def artifact_filename(file_id: str, content_hash: str, embed_model: str,
                      dim: int, chunker_version: str) -> str:
    """`<file_id>.<hash[:12]>.<pipeline_fp[:8]>.mbc.gz` (spec A2)."""
    pf8 = pipeline_fingerprint(embed_model, dim, chunker_version)[:8]
    return f"{file_id}.{content_hash[:12]}.{pf8}.mbc.gz"


# -- transport contract -----------------------------------------------------

@runtime_checkable
class FleetStorage(Protocol):
    """Blob transport over the fleet folder / in-drive cache folders. Prod
    implements this over Google Drive (built in Phase A); tests implement it
    over a temp dir (LocalDirFleetStorage). Paths are '/'-separated relatives."""

    def put_bytes(self, path: str, data: bytes) -> None: ...
    def get_bytes(self, path: str) -> bytes | None: ...   # None when absent
    def list_paths(self, prefix: str) -> list[str]: ...
    def delete(self, path: str) -> None: ...
