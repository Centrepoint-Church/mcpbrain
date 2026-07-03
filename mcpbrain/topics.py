"""Deterministic topic-tag normalization.

Converges morphological/synonym variants of a topic onto one canonical tag so
the topic entity (id 'topic-<canonical>') doesn't fragment. Deterministic and
reversible — NO LLM merge — so 'prayer' can never silently absorb 'prayer
meeting'; only an explicit curated synonym entry joins two topics.

Mirrors mcpbrain.orgs: the store stays decoupled from this logic; callers
(graph_write.apply) normalize before deriving the entity id.
"""

import re

from mcpbrain import config
from mcpbrain.text_norm import singularize

# Leading throat-clearing words that don't change a topic's identity. Stripped
# only from the FRONT and only when at least one real token remains. Kept small
# and conservative — this is not a general stopword list.
_LEADING_QUALIFIERS = {"the", "a", "an", "our", "annual", "monthly", "weekly"}


def _synonyms(home) -> dict:
    raw = config.read_config(home).get("topic_synonyms") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip().lower()
            for k, v in raw.items() if str(k).strip() and str(v).strip()}


def normalize_topic(tag: str, home=None) -> str:
    """Canonical lowercased topic tag, or '' when it collapses to empty."""
    if home is None:
        home = str(config.app_dir())
    t = re.sub(r"\s+", " ", (tag or "").strip().lower())
    if not t:
        return ""

    syn = _synonyms(home)

    # Synonym lookup runs at the RAW surface form, the qualifier-stripped form,
    # and the singularized form: a curated entry may be authored against any of
    # these three shapes, and all are legitimate ways for a human to write the
    # config (e.g. "the budget" -> "finance" only matches before qualifier
    # stripping removes "the").
    if t in syn:
        return syn[t]

    # Strip leading qualifiers while a real token remains.
    words = t.split(" ")
    while len(words) > 1 and words[0] in _LEADING_QUALIFIERS:
        words = words[1:]
    t = " ".join(words)

    if t in syn:
        return syn[t]

    # Singularize the LAST token only (the head noun); leaves 'youth services'
    # -> 'youth service' but never mangles a leading modifier.
    if words:
        words[-1] = singularize(words[-1])
        t = " ".join(w for w in words if w)

    # Curated synonym map has the final say.
    return syn.get(t, t)
