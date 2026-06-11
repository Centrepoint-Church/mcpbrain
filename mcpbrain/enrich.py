"""Enrichment constants and helpers; Gemini extraction removed (§9B).

The spool/drain path (extractor_driver + drain.py) is the sole live enrichment
path. Constants used by that path live in chunking.py and are re-exported here
for backward-compat (callers that `from mcpbrain.enrich import …`).
"""
from mcpbrain.chunking import (  # noqa: F401 — public re-exports
    slugify,
    _canonical_name,
    _VALID_CONTENT_TYPES,
    _VALID_TYPES,
    _is_junk_entity,
    _parse_first_json_object,
)

from mcpbrain import orgs as _orgs

_VALID_ORGS = set(_orgs.DEFAULT_TAXONOMY.valid_orgs)
