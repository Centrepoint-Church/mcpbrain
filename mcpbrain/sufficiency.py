"""Sufficiency / NLI gate: check whether recalled memory helps answer the query.

Called after the vector-distance off-topic gate in daemon.search().  The
distance gate asks "is ANYTHING in the brain close to this query?"  This gate
asks the stronger question: "does this specific chunk actually contain
information that helps answer the query?" (the Google study found insufficient
context raised hallucination 10 %→66 % — a weak recall is worse than none).

Design:
  - Batch all candidate hits in ONE LLM call (efficient; < 2 s on bge-small
    results).
  - Fail-open: any error (CLI absent, timeout, parse failure) returns all hits
    unchanged.  Recall must never block a prompt.
  - Prompt is NLI-style: RELEVANT vs IRRELEVANT, binary, no explanation required.
  - Tied to the `sufficiency_gate` config flag (default False — safe rollout).

Public API:
    from mcpbrain.sufficiency import filter_by_sufficiency
    hits = filter_by_sufficiency(query, hits, home=home)
"""
from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger("mcpbrain.sufficiency")

_TIMEOUT = 6    # seconds — generous for a short batch prompt
_SNIPPET = 300  # chars per chunk shown to the LLM

_PROMPT_TMPL = """\
You are a relevance classifier.  For each numbered memory chunk below decide
whether it HELPS ANSWER the query.  Return JSON only — no other text.

QUERY: {query}

CHUNKS:
{chunks}

JSON format:
{{"results": [{{"idx": 1, "relevant": true}}, {{"idx": 2, "relevant": false}}, ...]}}

Rules:
- relevant=true  : the chunk contains specific information that helps answer the query.
- relevant=false : the chunk is topically similar but does NOT contain a useful answer
                   (e.g. mentions the same keywords but is about a different question).
- When uncertain, prefer true (err on the side of injecting).
"""


def _build_prompt(query: str, hits: list[dict]) -> str:
    chunk_lines: list[str] = []
    for i, h in enumerate(hits, 1):
        snippet = " ".join((h.get("text") or "").split())[:_SNIPPET]
        chunk_lines.append(f"[{i}] {snippet}")
    return _PROMPT_TMPL.format(query=query[:400], chunks="\n".join(chunk_lines))


def _call_claude(prompt: str) -> str:
    """Call claude CLI with prompt; return stdout or '' on any failure."""
    from mcpbrain import config
    try:
        claude = config.find_claude()
    except RuntimeError:
        return ""
    try:
        result = subprocess.run(
            [claude, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        if result.returncode != 0:
            log.debug("sufficiency: claude returned %d: %s",
                      result.returncode, (result.stderr or "")[:200])
            return ""
        return (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        log.debug("sufficiency: timed out after %ds", _TIMEOUT)
        return ""
    except Exception as exc:  # noqa: BLE001
        log.debug("sufficiency: claude call failed: %s", exc)
        return ""


def _parse_result(raw: str, n: int) -> list[bool] | None:
    """Parse the LLM JSON response; return a list of bools (index-aligned to hits).

    Returns None on any parse failure so the caller can fail-open.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except (ValueError, KeyError):
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None
    # Build index-keyed map; default to True so missing entries pass through.
    idx_map: dict[int, bool] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item["idx"])
            relevant = bool(item.get("relevant", True))
        except (KeyError, TypeError, ValueError):
            continue
        idx_map[idx] = relevant
    return [idx_map.get(i, True) for i in range(1, n + 1)]


def filter_by_sufficiency(query: str, hits: list[dict], *, home: str) -> list[dict]:
    """Return the subset of `hits` that the sufficiency gate passes.

    Fail-open: on any error the full `hits` list is returned unchanged.
    Only runs when `sufficiency_gate` is enabled in config.json; if the flag is
    off the function is a no-op (fast path, no subprocess).

    The gate is intentionally permissive (err toward true) — it targets
    high-similarity-but-off-topic misses, not edge cases where the model is
    unsure.  A hit is kept unless the LLM explicitly marks it irrelevant=false.
    """
    if not hits:
        return hits
    from mcpbrain import config
    if not config.sufficiency_gate_enabled(home):
        return hits

    prompt = _build_prompt(query, hits)
    raw = _call_claude(prompt)
    if not raw:
        return hits   # fail-open: CLI absent or timed out

    judgements = _parse_result(raw, len(hits))
    if judgements is None:
        log.debug("sufficiency: could not parse response — fail-open")
        return hits   # fail-open: bad JSON

    filtered = [h for h, keep in zip(hits, judgements) if keep]
    kept = len(filtered)
    dropped = len(hits) - kept
    if dropped:
        log.info("sufficiency gate: kept %d/%d hits (dropped %d)", kept, len(hits), dropped)
    return filtered if filtered else hits  # never return empty (fail-open on all-dropped)
