"""Entity resolution: deterministic auto-merge + fuzzy candidate generation.

Step 1 (deterministic) merges same-type entities whose canonical keys match —
honorific-stripped, accent-folded, slugified. It is LLM-free and always safe to
run. Step 2 (blocking + scoring) surfaces near-duplicate candidate pairs for an
LLM adjudicator added in R7; nothing is merged here.
"""

import json
import logging

from mcpbrain.chunking import slugify, _canonical_name
from mcpbrain.enrich import _parse_first_json_object, _DEFAULT_MODEL

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


# --- R7: LLM adjudication -------------------------------------------------


def _adjudicate(client, a, b, model=None) -> dict:
    """Ask the model if entities a and b are the SAME real-world entity.
    Returns {"same": bool, "canonical": str}. The generate_content CALL may
    raise (API error) — the caller handles that. Unparseable output -> {"same": False}."""
    model = model or _DEFAULT_MODEL
    prompt = (
        "You are deduplicating a knowledge graph for an operations manager. "
        "Are these two entries the SAME real-world entity?\n"
        f"A: name={json.dumps(a['name'])} type={a['type']}\n"
        f"B: name={json.dumps(b['name'])} type={b['type']}\n"
        "Consider that initials/short forms can match a full name (\"Joel\" = \"Joel Chelliah\"), "
        "but different surnames or different initials are DIFFERENT people "
        "(\"Daniel P\" != \"Daniel F\"). When unsure, answer false.\n"
        'Respond with STRICT JSON only: {"same": true|false, "canonical": "<the single best full name if same, else empty>"}'
    )
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config={"response_mime_type": "application/json"},
    )
    try:
        data = _parse_first_json_object(resp.text or "")
    except Exception:
        return {"same": False, "canonical": ""}
    return {"same": bool(data.get("same")), "canonical": (data.get("canonical") or "").strip()}


def resolve_entities(store, client=None, *, max_adjudications: int = 200) -> dict:
    """Resolve duplicate entities in two tiers.

    Step 1 (deterministic, always) merges canonical-key-identical same-type
    entities. Step 2 (only when a client is given) asks the LLM to adjudicate
    fuzzy candidate pairs; a merge happens ONLY when the model says same=true.
    API errors per pair are caught and never crash the run. Returns a summary.
    """
    auto = _deterministic_merges(store)
    if client is None:
        return {"mode": "deterministic", "auto_merges": auto, "llm_merges": 0,
                "llm_calls": 0, "kept_distinct": 0}

    ents = store.entities_for_resolution()
    pairs = _candidate_pairs(ents)

    gone = set()         # ids merged away this run — don't adjudicate stale members
    attempts = 0         # pairs adjudicated (incl. failed calls) — bounds the cap
    llm_calls = 0        # successful adjudication calls (for the summary)
    llm_merges = 0
    kept_distinct = 0

    for a, b in pairs:
        if a["id"] in gone or b["id"] in gone:
            continue
        # Cap on ATTEMPTS, not successes: a persistently-failing API must still
        # stop at the cap rather than walk every candidate pair.
        if attempts >= max_adjudications:
            log.info("resolve_entities: adjudication cap (%d) hit; stopping",
                     max_adjudications)
            break
        attempts += 1
        try:
            verdict = _adjudicate(client, a, b)
        except Exception as exc:
            log.warning("resolve_entities: adjudication failed for (%s, %s): %s",
                        a["id"], b["id"], exc)
            continue
        llm_calls += 1
        if verdict["same"]:
            winner, loser = _pick_winner(a, b)
            try:
                store.merge_entities(loser["id"], winner["id"],
                                     canonical_name=verdict["canonical"] or None,
                                     method="llm")
            except Exception as exc:
                log.error("merge failed for %s <- %s: %s", winner["id"], loser["id"], exc)
                continue
            gone.add(loser["id"])
            winner["mentions"] = winner.get("mentions", 0) + loser.get("mentions", 0)
            llm_merges += 1
        else:
            kept_distinct += 1

    return {"mode": "live", "auto_merges": auto, "llm_merges": llm_merges,
            "llm_calls": llm_calls, "kept_distinct": kept_distinct}


def _pick_winner(a, b):
    """Survivor is the higher-mentions entity; tiebreak longer name, then id.
    Returns (winner, loser)."""
    winner = max((a, b), key=lambda m: (m.get("mentions", 0), len(m["name"]), m["id"]))
    loser = b if winner is a else a
    return winner, loser
