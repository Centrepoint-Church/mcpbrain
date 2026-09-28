"""Content-preserving reflow: re-chunk an owner without discarding enrichment.

`plan` is pure: given an owner's OLD chunk rows (with their enrichment state)
and its NEW chunks (with source spans), it decides per new chunk whether its
source text is provably covered by already-enriched old text, and maps every
old doc_id to the new chunk now holding its text. Store.apply_reflow applies
the plan in one transaction. Spec: 2026-09-24 extraction-fidelity §3.

Offsets in the stitched text are a PARTITION: each old chunk owns the text it
contributed after its seam overlap with the previous chunk was dropped, so
every character of the stitched text belongs to exactly one old chunk. That is
what makes "the new chunk containing an old chunk's start offset" well defined
when consecutive chunks overlap by up to 50 words -- an inclusive start would
sit inside the PREVIOUS chunk's tail and remap chunk i onto i-1.
"""
from bisect import bisect_right
from dataclasses import dataclass, field

from mcpbrain.sync.normalise import Chunk

_MAX_OVERLAP_WORDS = 60     # chunk_text overlaps by 50 words; allow slack


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
    """Join chunk texts into one normalised text, removing each seam's word
    overlap (longest suffix/prefix match, at most _MAX_OVERLAP_WORDS words).

    Returns the text and, per input, the (start, end) offsets of the text it
    CONTRIBUTED: ``o[start:end] == norm(text)`` minus its dropped overlap
    prefix. Consecutive spans therefore partition the stitched text (separated
    by the single joining space); an input wholly consumed by the overlap gets
    an empty span at the current end."""
    words: list[str] = []
    spans: list[tuple[int, int]] = []
    char_len = 0                    # == len(" ".join(words)), kept incrementally
    for t in texts:
        tw = norm(t).split()
        k = 0
        for n in range(min(_MAX_OVERLAP_WORDS, len(words), len(tw)), 0, -1):
            if words[-n:] == tw[:n]:
                k = n
                break
        added = tw[k:]
        if not added:
            spans.append((char_len, char_len))
            continue
        start = char_len + (1 if words else 0)
        words.extend(added)
        char_len = start + sum(len(w) for w in added) + len(added) - 1
        spans.append((start, char_len))
    return " ".join(words), spans


def _find_word(o: str, ns: str, pos: int) -> int:
    """First index >= pos where `ns` occurs in `o` on word boundaries (an
    alphanumeric edge of `ns` may not continue an alphanumeric run in `o`),
    or -1. Stops "pha bet" matching inside "alpha beta" while still letting a
    table cell "Chairs" match "Chairs;" in a row sentence."""
    lo_edge, hi_edge = ns[0].isalnum(), ns[-1].isalnum()
    at = o.find(ns, pos)
    while at >= 0:
        end = at + len(ns)
        if (not (lo_edge and at > 0 and o[at - 1].isalnum())
                and not (hi_edge and end < len(o) and o[end].isalnum())):
            return at
        at = o.find(ns, at + 1)
    return -1


def _locate(o: str, spans: list[str], cursor: int) -> tuple[list[tuple[int, int]], bool, int | None]:
    """Where a new chunk's source spans sit inside `o`.

    Returns (intervals, complete, advance):
      - intervals: one [start, end) per span that WAS found, merged and
        sorted. A chunk's position is the UNION of these, never their hull: a
        chunk holding text from far-apart places in the old text (a table the
        old extractor appended at the end, now back in place beside its prose)
        must not claim every old chunk in between.
      - complete: every non-empty span was found; only a complete chunk can be
        covered. Position and coverage are separate answers -- a new chunk that
        is an old slide plus its new speaker notes still sits where the slide's
        text is and stays the remap target for the slide's old chunk.
      - advance: where the forward cursor moves to -- the end of the chunk's
        FIRST non-empty span, when that span was found searching forward from
        `cursor`; otherwise None (the cursor stays). Advancing on any other
        span lets a coincidental later match (a short word, a moved table's
        cell) push the cursor past repeated text, so later identical chunks
        would map out of order.
    Each span is searched forward from the previous forward hit first, so
    repeated text maps monotonically, then from 0 (moved content)."""
    found: list[tuple[int, int]] = []
    complete = True
    advance = None
    first = True
    pos = cursor
    for s in spans:
        ns = norm(s)
        if not ns:
            continue
        at = _find_word(o, ns, pos)
        forward = at >= 0
        if at < 0:
            at = _find_word(o, ns, 0)
        if at < 0:
            complete = False
            first = False
            continue
        if first and forward:
            advance = at + len(ns)
        first = False
        if forward:
            pos = at + len(ns)
        found.append((at, at + len(ns)))
    found.sort()
    merged: list[tuple[int, int]] = []
    for a, b in found:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged, complete, advance


def _overlaps(intervals: list[tuple[int, int]], offs: list[tuple[int, int]],
              starts: list[int]) -> dict[int, int]:
    """{old index: characters of that old chunk's contributed span covered by
    `intervals`} (merged, disjoint intervals; `offs` a partition)."""
    out: dict[int, int] = {}
    for a, b in intervals:
        i = max(bisect_right(starts, a) - 1, 0)
        while i < len(offs) and offs[i][0] < b:
            s, e = offs[i]
            w = min(b, e) - max(a, s)
            if w > 0:
                out[i] = out.get(i, 0) + w
            i += 1
    return out


def _majority(overlapped: list[tuple[int, int]], old: list[dict], key: str):
    """Overlap-weighted majority of old[i][key]; ties go to the value of the
    old chunk with the largest overlap (``overlapped`` is sorted that way)."""
    totals: dict = {}
    for i, w in overlapped:
        v = old[i][key]
        if key == "enrich_state":
            v = v or None           # '' (column default) and NULL both mean hot
        totals[v] = totals.get(v, 0) + w
    return max(totals, key=totals.get)


def _plan_lineage(key: str, old: list[dict], new: list[Chunk], plan_: ReflowPlan) -> None:
    old = sorted(old, key=lambda r: int((r["metadata"] or {}).get("chunk_index", 0)))
    o, offs = stitch([r["text"] for r in old])
    n_text, _ = stitch([c.text for c in new])
    starts = [s for s, _e in offs]
    # per old chunk: {new index: overlap chars}
    old_hits: list[dict[int, int]] = [{} for _ in old]
    placed: list[tuple[int, list[tuple[int, int]]]] = []
    cursor = 0
    for j, c in enumerate(new):
        intervals, complete, advance = _locate(o, c.spans or [c.text], cursor)
        if advance is not None:
            cursor = max(cursor, advance)
        hits = _overlaps(intervals, offs, starts) if intervals else {}
        if intervals:
            placed.append((j, intervals))
        for i, w in hits.items():
            old_hits[i][j] = w
        overlapped = sorted(hits.items(), key=lambda p: -p[1]) if complete else []
        row = NewRow(chunk=c, covered=bool(overlapped))
        if row.covered:
            row.enriched = 1 if all(old[i]["enriched"] == 1 for i, _ in overlapped) else 0
            # The oldest extraction logic that touched this text: a later
            # enrich_logic_floor bump must still re-enrich it.
            row.enriched_version = (min(int(old[i]["enriched_version"] or 0)
                                        for i, _ in overlapped) if row.enriched else 0)
            for f in ("enrich_state", "salience", "memory_tier", "memory_type"):
                setattr(row, f, _majority(overlapped, old, f))
        plan_.rows.append(row)
    for i, (r, (s, e)) in enumerate(zip(old, offs)):
        hits = old_hits[i]
        if hits:
            # the new chunk now holding MOST of this old chunk's text; ties go
            # to the one containing its start, then to reading order
            def contains_start(j):
                return any(a <= s < b for jj, iv in placed if jj == j for a, b in iv)
            j = max(hits, key=lambda j: (hits[j], contains_start(j), -j))
            target, reason = new[j].doc_id, "exact"
        elif placed:
            def dist(item):
                return min(max(a - s, s - b + 1, 0) for a, b in item[1])
            j, _iv = min(placed, key=dist)
            target, reason = new[j].doc_id, "nearest"
        else:
            target, reason = new[0].doc_id, "fallback"
        plan_.remap[r["doc_id"]] = target
        plan_.reasons[r["doc_id"]] = reason
    if o != n_text:
        plan_.content_equal = False
        plan_.unequal.append(key)


def plan(old: list[dict], new: list[Chunk]) -> ReflowPlan:
    """Plan a reflow of one owner (spec §3). Every old doc_id is remapped to a
    new chunk's doc_id; the caller deletes ``deletes`` (old ids with no new
    chunk at that position) after applying the remap."""
    if not new:
        raise ValueError("reflow.plan: no new chunks -- never plan a deletion")
    p = ReflowPlan(content_equal=True)
    by_old: dict[str, list[dict]] = {}
    for r in old:
        by_old.setdefault(lineage_key(r["doc_id"], r["metadata"] or {}), []).append(r)
    by_new: dict[str, list[Chunk]] = {}
    for c in new:
        by_new.setdefault(lineage_key(c.doc_id, c.metadata), []).append(c)
    for key, chunks in by_new.items():
        if key in by_old:
            _plan_lineage(key, by_old[key], chunks, p)
        else:
            p.content_equal = False
            p.unequal.append(key)
            p.rows.extend(NewRow(chunk=c, covered=False) for c in chunks)
    for key, rows in by_old.items():
        if key not in by_new:
            p.content_equal = False
            p.unequal.append(key)
            for r in rows:
                p.remap[r["doc_id"]] = new[0].doc_id
                p.reasons[r["doc_id"]] = "lineage_gone"
    order = {c.doc_id: i for i, c in enumerate(new)}
    p.rows.sort(key=lambda r: order[r.chunk.doc_id])
    p.deletes = sorted(r["doc_id"] for r in old if r["doc_id"] not in order)
    return p


_UNIT_REF_KEYS = ("thread_id", "message_id")
_UNIT_REF_LISTS = ("part_doc_ids", "chunk_doc_ids")


def _collect_unit_refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _UNIT_REF_KEYS and isinstance(v, (str, int)) and v != "":
                out.add(str(v))
            elif k in _UNIT_REF_LISTS and isinstance(v, list):
                out.update(str(d) for d in v if isinstance(d, (str, int)) and d != "")
            else:
                _collect_unit_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_unit_refs(v, out)


def pending_unit_refs(home) -> set[str]:
    """Every thread_id, message_id, part_doc_ids and chunk_doc_ids entry named by
    an enrichment unit still pending or claimed under <home>/enrich_queue (spec §3
    guard: an owner with an in-flight unit must not be reflowed, or drain would
    later mark re-chunked, never-extracted text enriched). Unreadable or
    malformed unit files are ignored. A claim's unit is read from units/<uid>.json;
    a claim with no readable unit file contributes nothing."""
    import json
    from pathlib import Path
    queue = Path(home) / "enrich_queue"
    paths: set[Path] = set()
    try:
        paths.update((queue / "units").glob("*.json"))
    except OSError:
        pass
    try:
        for claim in (queue / "claims").iterdir():
            uid = claim.name[:-5] if claim.name.endswith(".json") else claim.name
            paths.add(queue / "units" / f"{uid}.json")
    except OSError:
        pass
    out: set[str] = set()
    for p in paths:
        try:
            _collect_unit_refs(json.loads(p.read_text(encoding="utf-8")), out)
        except (OSError, ValueError):
            continue
    return out
