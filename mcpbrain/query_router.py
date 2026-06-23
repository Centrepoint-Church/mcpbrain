"""Query router for retrieval polish — Q6.

On top of the existing RRF hybrid retrieval this module provides four
independently flag-gated sub-features:

  retrieval_routing   — intent classification + entity graph-seed expansion
                        + community-augmented hybrid for thematic queries
  retrieval_crag      — CRAG low-confidence query rewrite: when the top hit
                        score is below crag_min_score, a reformulated query
                        is generated and the two result sets are merged
  retrieval_rerank    — lightweight BM25-token-overlap rerank of the fused
                        top-k (no new model; pure-Python)
  contextual_retrieval — context prefix prepended to chunk text at embed time
                         (already deployed; see embed.contextual_prefix which
                         is always-on in index.py — no additional flag needed)

Each feature is a no-op when its flag is off, so adding this module does not
change existing behaviour without explicit opt-in.

Public entry points:
    route(store, embedder, query, limit, *, home, query_vec, **search_kwargs)
        → list[dict]   (replacement for daemon.search's hybrid_search call)

All LLM calls (CRAG rewrite) go through the claude CLI (config.find_claude).
"""
from __future__ import annotations

import logging
import re
import subprocess

from mcpbrain.retrieval import hybrid_search  # module-level so tests can patch it

log = logging.getLogger("mcpbrain.query_router")

# ---------------------------------------------------------------------------
# Intent classification (heuristic, no LLM)
# ---------------------------------------------------------------------------

_INTENT_ENTITY = "entity"    # query names a specific entity in the brain
_INTENT_THEMATIC = "thematic"  # broad topic; no specific entity anchor
_INTENT_GENERAL = "general"  # default

# Patterns that suggest a thematic / topic-level query rather than entity lookup
_THEMATIC_STARTERS = re.compile(
    r"^(what|how|why|when|who is|tell me about|summarise|summary of|list|show me|"
    r"find all|give me|any|are there|do we have|latest)\b",
    re.IGNORECASE,
)

# Maximum length (words) for entity-name matching in the query
_ENTITY_NAME_MAX_TOKENS = 5


def _classify_intent(query: str, store) -> tuple[str, str | None]:
    """Return (intent, matched_entity_id|None).

    Heuristic:
    1. If the query starts with a thematic-starter verb → THEMATIC.
    2. Otherwise: try to find a known entity whose name appears in the query →
       ENTITY (with the best-matching entity id).
    3. Else: GENERAL.
    """
    q = query.strip()
    if _THEMATIC_STARTERS.match(q):
        return _INTENT_THEMATIC, None

    # Entity scan: tokenise query into consecutive token windows and look each
    # up in the graph. Short names (≤5 tokens) are tried.
    # Strip possessives and trailing punctuation so "Joel's" matches "Joel".
    raw_tokens = q.split()
    tokens = [re.sub(r"['’]s$|[^\w\s-]", "", t).strip() for t in raw_tokens]
    tokens = [t for t in tokens if t]  # drop empty after stripping
    best_ent = None
    best_len = 0
    for start in range(len(tokens)):
        for end in range(start + 1, min(start + _ENTITY_NAME_MAX_TOKENS + 1, len(tokens) + 1)):
            candidate = " ".join(tokens[start:end])
            try:
                ent = store.find_entity(candidate)
            except Exception:
                ent = None
            if ent and (end - start) > best_len:
                best_ent = ent["id"]
                best_len = end - start

    if best_ent:
        return _INTENT_ENTITY, best_ent
    return _INTENT_GENERAL, None


# ---------------------------------------------------------------------------
# Entity graph seeding
# ---------------------------------------------------------------------------

def _graph_seed_query(store, entity_id: str, query: str, max_neighbors: int = 5) -> str:
    """Expand query with 1-hop neighbour names to improve recall for entity queries.

    Example: "Joel budget" + entity 'joel-chelliah' neighbours → append
    "Taryn Hamilton Centrepoint Maddington" so the expanded query surfaces
    chunks that mention Joel's teammates.
    """
    try:
        relations = store.relations_for(entity_id)
    except Exception:
        return query
    seen: set[str] = set()
    names: list[str] = []
    for rel in relations:
        for field in ("entity_a", "entity_b"):
            eid = rel.get(field)
            if not eid or eid == entity_id or eid in seen:
                continue
            seen.add(eid)
            try:
                ent = store.get_entity(eid)
            except Exception:
                continue
            if ent and ent.get("name"):
                names.append(ent["name"])
            if len(names) >= max_neighbors:
                break
        if len(names) >= max_neighbors:
            break
    if not names:
        return query
    expansion = " ".join(names[:max_neighbors])
    log.debug("router: graph-seed expansion for entity %s: +%d names", entity_id, len(names))
    return f"{query} {expansion}"


# ---------------------------------------------------------------------------
# Community-augmented hybrid (thematic queries)
# ---------------------------------------------------------------------------

def _community_augment(store, query: str, results: list[dict], limit: int) -> list[dict]:
    """For thematic queries, surface community summaries as additional results.

    Looks up community summaries, scores them by simple keyword overlap with
    the query tokens, and appends the top matches as synthetic result dicts.
    These carry provenance='community_summary' so callers can identify them.
    Max augmentation: 2 community entries (they are broad, not granular).
    """
    try:
        communities = store.list_communities()
    except Exception:
        return results
    if not communities:
        return results

    q_tokens = set(re.findall(r"\b\w{3,}\b", query.lower()))
    scored: list[tuple[float, dict]] = []
    for comm in communities:
        summary = (comm.get("summary") or "").strip()
        if not summary:
            continue
        c_tokens = set(re.findall(r"\b\w{3,}\b", summary.lower()))
        overlap = len(q_tokens & c_tokens) / (len(q_tokens) + 1)
        if overlap > 0:
            scored.append((overlap, comm))
    scored.sort(key=lambda x: -x[0])
    augmented = list(results)
    for _, comm in scored[:2]:
        text = (
            f"[Community {comm.get('community_id', '?')}] "
            f"{comm.get('summary', '').strip()}"
        )
        augmented.append({
            "doc_id": f"community:{comm.get('community_id', '?')}",
            "score": round(float(scored[0][0]) * 0.5, 4),
            "distance": 1.0,
            "text": text[:400],
            "provenance": "community_summary",
        })
    return augmented[:limit]


# ---------------------------------------------------------------------------
# CRAG low-confidence rewrite
# ---------------------------------------------------------------------------

_CRAG_TIMEOUT = 5   # seconds — short; CRAG is a best-effort improvement


def _crag_rewrite(query: str) -> str:
    """Ask the claude CLI to rewrite a low-confidence query for better recall.

    Keeps it cheap: single short prompt, returns the rewritten query or ''
    on any failure.
    """
    from mcpbrain import config
    prompt = (
        f"Rewrite this search query to improve recall from a personal email/document "
        f"brain. Use alternative phrasings, synonyms, or more specific terms. "
        f"Return only the rewritten query — no explanation, no quotes.\n\nQuery: {query}"
    )
    try:
        claude = config.find_claude()
    except RuntimeError:
        return ""
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=_CRAG_TIMEOUT,
        )
        if result.returncode != 0:
            return ""
        rewritten = (result.stdout or "").strip()
        # Sanity check: must be non-empty and not a multi-paragraph essay
        if rewritten and len(rewritten) < 300 and "\n\n" not in rewritten:
            return rewritten
        return ""
    except (subprocess.TimeoutExpired, Exception):  # noqa: BLE001
        return ""


def _merge_crag(primary: list[dict], secondary: list[dict], limit: int) -> list[dict]:
    """Merge CRAG secondary results into primary; deduplicate by doc_id, keep highest score."""
    seen: dict[str, dict] = {r["doc_id"]: r for r in primary if r.get("doc_id")}
    for r in secondary:
        doc_id = r.get("doc_id")
        if not doc_id:
            continue
        existing = seen.get(doc_id)
        if existing is None or float(r.get("score") or 0) > float(existing.get("score") or 0):
            seen[doc_id] = {**r, "provenance": r.get("provenance", "crag_rewrite")}
    merged = sorted(seen.values(), key=lambda x: -float(x.get("score") or 0))
    return merged[:limit]


# ---------------------------------------------------------------------------
# BM25-style token-overlap reranker (no new model required)
# ---------------------------------------------------------------------------

def _token_overlap_rerank(query: str, results: list[dict]) -> list[dict]:
    """Rerank results by token overlap between query and chunk text.

    This is a cheap cross-encoder proxy: BM25-style term frequency overlap.
    Boosts chunks whose text contains more of the query's distinctive tokens.
    The reranked score is a blend: 0.7 * original + 0.3 * overlap_score.
    """
    q_tokens = set(re.findall(r"\b\w{3,}\b", query.lower()))
    if not q_tokens:
        return results
    reranked: list[tuple[float, dict]] = []
    for r in results:
        text = (r.get("text") or "").lower()
        c_tokens = set(re.findall(r"\b\w{3,}\b", text))
        overlap = len(q_tokens & c_tokens) / len(q_tokens) if q_tokens else 0.0
        base = float(r.get("score") or 0.0)
        combined = 0.7 * base + 0.3 * overlap
        reranked.append((combined, {**r, "score": round(combined, 4)}))
    reranked.sort(key=lambda x: -x[0])
    return [r for _, r in reranked]


# ---------------------------------------------------------------------------
# Main route() entry point
# ---------------------------------------------------------------------------

def route(store, embedder, query: str, limit: int, *,
          home: str, query_vec=None, **search_kwargs) -> list[dict]:
    """Route a query through the polished retrieval pipeline.

    Falls back to plain hybrid_search for each disabled sub-feature so callers
    always get valid results.  Never raises — all errors degrade gracefully.
    """
    from mcpbrain import config

    # ---- intent classification ------------------------------------------
    routing_on = config.retrieval_routing_enabled(home)
    intent = _INTENT_GENERAL
    entity_id: str | None = None
    if routing_on:
        try:
            intent, entity_id = _classify_intent(query, store)
        except Exception:  # noqa: BLE001
            pass
        log.debug("router: intent=%s entity=%s", intent, entity_id)

    # ---- build search query (entity graph-seed expansion) ---------------
    search_query = query
    if routing_on and intent == _INTENT_ENTITY and entity_id:
        try:
            search_query = _graph_seed_query(store, entity_id, query)
        except Exception:  # noqa: BLE001
            search_query = query

    # ---- primary hybrid search ------------------------------------------
    qv = query_vec
    try:
        results = hybrid_search(store, embedder, search_query, limit,
                                query_vec=qv, **search_kwargs)
    except Exception:  # noqa: BLE001
        log.warning("router: hybrid_search failed", exc_info=True)
        return []

    # Tag provenance on entity-seeded results
    if routing_on and search_query != query:
        for r in results:
            r.setdefault("provenance", "graph_seeded")

    # ---- CRAG: rewrite on low confidence --------------------------------
    crag_on = config.retrieval_crag_enabled(home)
    if crag_on and results:
        top_score = float(results[0].get("score") or 0.0)
        crag_threshold = config.crag_min_score(home)
        if top_score < crag_threshold:
            log.debug("router: CRAG rewrite triggered (top=%.3f < %.3f)", top_score, crag_threshold)
            try:
                rewritten = _crag_rewrite(query)
                if rewritten and rewritten != query:
                    secondary = hybrid_search(store, embedder, rewritten, limit,
                                              **search_kwargs)
                    for r in secondary:
                        r["provenance"] = "crag_rewrite"
                    results = _merge_crag(results, secondary, limit)
            except Exception:  # noqa: BLE001
                pass

    # ---- community augmentation (thematic intent) -----------------------
    if routing_on and intent == _INTENT_THEMATIC:
        try:
            results = _community_augment(store, query, results, limit)
        except Exception:  # noqa: BLE001
            pass

    # ---- token-overlap rerank ------------------------------------------
    if config.retrieval_rerank_enabled(home):
        try:
            results = _token_overlap_rerank(query, results)
        except Exception:  # noqa: BLE001
            pass

    return results[:limit]
