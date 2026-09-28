"""Content-preserving reflow: re-chunk an owner without discarding enrichment.

`plan` is pure: given an owner's OLD chunk rows (with their enrichment state)
and its NEW chunks (with source spans), it decides per new chunk whether its
source text is provably covered by already-enriched old text, and maps every
old doc_id to the new chunk now holding its text. Store.apply_reflow applies
the plan in one transaction. Spec: 2026-09-24 extraction-fidelity §3.

CONTRACT (Stage 0): dataclasses, `norm`, `lineage_key` are final; `stitch` and
`plan` are implemented by unit 1d.
"""
from dataclasses import dataclass, field

from mcpbrain.sync.normalise import Chunk


def norm(s: str) -> str:
    return " ".join((s or "").split())


def lineage_key(doc_id: str, metadata: dict) -> str:
    if (metadata or {}).get("source_type") == "calendar":
        return f"cal-{metadata.get('event_id', '')}"
    return doc_id.rsplit("-", 1)[0]


@dataclass
class NewRow:
    chunk: Chunk
    covered: bool
    enriched: int = 0
    enriched_version: int = 0
    enrich_state: str | None = None
    salience: object = None
    memory_tier: str | None = None
    memory_type: str | None = None


@dataclass
class ReflowPlan:
    rows: list[NewRow] = field(default_factory=list)
    remap: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    deletes: list[str] = field(default_factory=list)
    content_equal: bool = False
    # Lineage keys whose stitched old and new text differ.
    unequal: list[str] = field(default_factory=list)


def stitch(texts: list[str]) -> tuple[str, list[tuple[int, int]]]:
    raise NotImplementedError("reflow.stitch: unit 1d")


def plan(old: list[dict], new: list[Chunk]) -> ReflowPlan:
    raise NotImplementedError("reflow.plan: unit 1d")
