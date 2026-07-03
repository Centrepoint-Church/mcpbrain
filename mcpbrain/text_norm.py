"""Shared lexical normalization helpers.

singularize() wraps `inflect` so callers get a safe, lowercased singular form
with a fall-back to the input. Deliberately NOT wired into person/org name
resolution (resolve.canonical_key): surnames and many org names pluralize
legitimately, so singularizing them would over-merge distinct entities. It is
used only for topic-tag normalization (mcpbrain.topics.normalize_topic), where
plural/singular variants of the same concept ('budget'/'budgets') are genuinely
one thing.
"""

import inflect

_ENGINE = inflect.engine()


def singularize(word: str) -> str:
    """Lowercased singular of a simple plural; unchanged if already singular.

    inflect.singular_noun returns False for words it considers already singular
    (or can't analyse); fall back to the lowercased input in that case.
    """
    w = (word or "").strip().lower()
    if not w:
        return ""
    result = _ENGINE.singular_noun(w)
    return result if result else w
