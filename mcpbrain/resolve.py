"""Entity resolution: deterministic auto-merge + fuzzy candidate generation
+ write-time dedup cascade.

Step 1 (deterministic) merges same-type entities whose canonical keys match —
honorific-stripped, accent-folded, slugified. It is LLM-free and always safe to
run. Step 2 (blocking + scoring) surfaces near-duplicate candidate pairs for the
spool merge_review block in prepare; nothing is merged here.

Step 3 (write-time dedup, Q3): before inserting a new entity, check the
current in-memory index for a same-type near-duplicate above the high-confidence
threshold (_WRITE_TIME_MERGE_THRESHOLD). If found, redirect to the existing
entity instead of creating a duplicate. Behind config flag
`write_time_dedup_enabled` (default False). The cascade:
  exact canonical key → high-confidence token similarity → create new.
  Ambiguous band [_CANDIDATE_GATE, _WRITE_TIME_MERGE_THRESHOLD): still queued
  for LLM review via the existing spool merge_review mechanism.

Note: embedding-based semantic blocking (cosine similarity on entity vectors)
would give better recall for non-overlapping names (e.g. "Joel" vs "J. Chelliah")
but requires entity-specific vector indices that don't yet exist. The token-
similarity cascade handles the common fragmentation patterns. Deferred.

The LLM-adjudication tier (_adjudicate / _pick_winner) has been removed in §9A.
Fuzzy candidate generation (_candidate_pairs) is preserved for
prepare._merge_review_block.
"""

import logging

from mcpbrain.chunking import slugify, _canonical_name

log = logging.getLogger(__name__)


def canonical_key(name: str) -> str:
    """Normalised dedup key: honorific-stripped + accent-folded + slugified.

    'Ps Joel' and 'Joel' share a key; 'Chané' and 'Chane' share a key.
    """
    return slugify(_canonical_name(name))


def _deterministic_merges(store) -> int:
    """Merge same-type, canonical-key-identical entities into the highest-mentions
    survivor. Returns the number of merges applied. Safe (no LLM)."""
    ents = store.entities_for_resolution()
    groups = {}   # (type, canonical_key) -> [entity dicts]
    for e in ents:
        key = canonical_key(e["name"])
        if not key:
            continue
        groups.setdefault((e["type"], key), []).append(e)
    merged = 0
    for (_type, _key), members in groups.items():
        if len(members) < 2:
            continue
        # id is the final tiebreaker so equal-mentions, equal-name-length groups
        # pick a deterministic survivor. entities_for_resolution() ORDERs BY id, so
        # group membership order is stable too, making the whole merge reproducible.
        survivor = max(members, key=lambda m: (m.get("mentions", 0), len(m["name"]), m["id"]))
        for m in members:
            if m["id"] != survivor["id"]:
                store.merge_entities(m["id"], survivor["id"], method="deterministic")
                merged += 1
    return merged


# --- R6: blocking + fuzzy candidate scoring -------------------------------

_STOPWORDS = {"the", "a", "an", "of", "and", "for", "to", "in", "at", "on"}
# Jaccard floor for a fuzzy candidate: ~at least 1 shared token out of 3 distinct.
# Below this, pairs aren't worth LLM adjudication; above, they go to the
# adjudicator (R7), never auto-merge.
_CANDIDATE_GATE = 0.3


def _tokens(name) -> set:
    """Lowercased, accent-folded, honorific-stripped alphanumeric tokens; drop
    stopwords and 1-char tokens."""
    key = canonical_key(name)            # 'joel-chelliah'
    toks = {t for t in key.split("-") if len(t) > 1 and t not in _STOPWORDS}
    return toks


def _token_set_ratio(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _candidate_pairs(entities) -> list:
    """Return (a, b) entity-dict pairs that are: same type, share a significant
    token, token-set similarity >= gate, and NOT canonical-key-identical (those
    are handled deterministically). No cross-type pairs, no singletons."""
    by_type = {}
    for e in entities:
        by_type.setdefault(e["type"], []).append(e)
    pairs = []
    seen = set()
    for _type, members in by_type.items():
        # index by token for blocking
        index = {}
        toks_cache = {}
        for e in members:
            toks_cache[e["id"]] = _tokens(e["name"])
            for t in toks_cache[e["id"]]:
                index.setdefault(t, []).append(e)
        for _tok, bucket in index.items():
            for i in range(len(bucket)):
                for j in range(i + 1, len(bucket)):
                    a, b = bucket[i], bucket[j]
                    if a["id"] == b["id"]:
                        continue
                    pair_key = tuple(sorted((a["id"], b["id"])))
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)   # dedup on first encounter, regardless of outcome
                    if canonical_key(a["name"]) == canonical_key(b["name"]):
                        continue   # deterministic handles these
                    if _token_set_ratio(toks_cache[a["id"]], toks_cache[b["id"]]) >= _CANDIDATE_GATE:
                        pairs.append((a, b))
    return pairs


def _pick_winner(a, b):
    """Survivor is the higher-mentions entity; tiebreak longer name, then id.
    Returns (winner, loser). Used by drain._apply_merge_answers for spool merges."""
    winner = max((a, b), key=lambda m: (m.get("mentions", 0), len(m["name"]), m["id"]))
    loser = b if winner is a else a
    return winner, loser


# --- Q3: write-time dedup cascade -----------------------------------------

# Token-set similarity above this threshold → auto-merge at write time without
# LLM review. Conservative: 0.8 means ≥4 out of 5 tokens must overlap (or a
# 2-token name where both match). Lower values risk false positives.
_WRITE_TIME_MERGE_THRESHOLD = 0.8


def build_entity_index(entities: list[dict]) -> dict:
    """Build a BLOCKED index for write-time dedup lookups.

    Returns {"ids": {id: {name,type,key,toks}}, "by_key": {(type,key): id},
    "by_tok": {(type,tok): set(ids)}}. by_key gives O(1) exact-canonical matches;
    by_tok lets write_time_dedup_check scan only entities that SHARE A TOKEN with
    the candidate (blocking) instead of all N — important on a 25k+ entity graph.
    add_to_index keeps it current for intra-batch dedup within one apply() run.
    """
    index: dict = {"ids": {}, "by_key": {}, "by_tok": {}}
    for e in entities:
        add_to_index(index, e["id"], e["name"], e["type"])
    return index


def add_to_index(index: dict, eid: str, name: str, entity_type: str) -> None:
    """Insert one entity into a build_entity_index() structure.

    Called as new entities are created during an apply() run so later entities in
    the same batch dedup against them, not only against the store snapshot.
    """
    key = canonical_key(name)
    toks = _tokens(name)
    index["ids"][eid] = {"name": name, "type": entity_type, "key": key, "toks": toks}
    if key:
        index["by_key"].setdefault((entity_type, key), eid)
    for t in toks:
        index["by_tok"].setdefault((entity_type, t), set()).add(eid)


def write_time_dedup_check(name: str, entity_type: str,
                           index: dict) -> str | None:
    """Return the id of an existing entity to redirect this write to, or None.

    Cascade (same type only), using the blocked index:
    1. Exact canonical key → certain duplicate → redirect.
    2. Among candidates SHARING A TOKEN, token-set ratio >=
       _WRITE_TIME_MERGE_THRESHOLD → high-confidence duplicate → redirect.
    3. Otherwise → None (caller creates a new entity).

    The ambiguous band [_CANDIDATE_GATE, threshold) is NOT acted on here; those
    pairs go to the spool merge_review mechanism in prepare. Embedding-based
    blocking (cosine on entity vectors) would additionally catch non-overlapping
    aliases ('Joel' vs 'J. Chelliah') but needs entity vectors that don't exist
    yet — deferred (tracked on #10).
    """
    if not name or not index:
        return None
    key = canonical_key(name)
    if key:
        hit = index["by_key"].get((entity_type, key))
        if hit:
            return hit
    toks = _tokens(name)
    if not toks:
        return None
    candidates: set = set()
    for t in toks:
        candidates |= index["by_tok"].get((entity_type, t), set())
    for eid in candidates:
        ent = index["ids"].get(eid)
        if ent and _token_set_ratio(toks, ent["toks"]) >= _WRITE_TIME_MERGE_THRESHOLD:
            return eid
    return None


def resolve_entities(store, client=None, *, max_adjudications: int = 200) -> dict:
    """Resolve duplicate entities (deterministic tier only; §9A).

    The LLM-adjudication tier is removed — spool merge_review handles it. Fuzzy
    candidate generation (_candidate_pairs) is preserved for prepare._merge_review_block.
    """
    auto = _deterministic_merges(store)
    return {"mode": "deterministic", "auto_merges": auto, "llm_merges": 0,
            "llm_calls": 0, "kept_distinct": 0}
