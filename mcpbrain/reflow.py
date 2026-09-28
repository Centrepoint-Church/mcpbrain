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


def _holder(text: str, n_text: str, n_offs: list[tuple[int, int]],
            n_starts: list[int]) -> int | None:
    """Index of the new chunk holding most of `text` inside the stitched new
    text (word-boundary match, first occurrence), or None when `text` is empty
    or not there."""
    if not text:
        return None
    at = _find_word(n_text, text, 0)
    if at < 0:
        return None
    hits = _overlaps([(at, at + len(text))], n_offs, n_starts)
    if not hits:
        return None
    return max(hits, key=lambda j: (hits[j], -j))


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
    n_text, n_offs = stitch([c.text for c in new])
    n_starts = [a for a, _b in n_offs]
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
        elif (j := _holder(norm(r["text"]), n_text, n_offs, n_starts)) is not None:
            # No new chunk was placed on this old chunk's region -- its text is
            # a DUPLICATE of text another old chunk holds (a legacy positional
            # tail, a pre-split row kept beside its split), so the new chunks
            # were placed on the other copy. Its text still exists: map it to
            # the new chunk holding it, not to whatever is positionally near.
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


def contained_both_ways(old: list[dict], new: list[Chunk]) -> bool:
    """One lineage's old rows and new chunks hold the same text, up to
    duplication and re-splitting: every new chunk's spans (its text when it
    has none) occur in the stitched old text, and every old chunk's text
    occurs in the stitched new text or in the plain concatenation of the new
    texts. Normalised, word-boundary matches (the calendar/anarlog "source
    unchanged" test, dry run #2 D2)."""
    old = sorted(old, key=lambda r: int((r["metadata"] or {}).get("chunk_index", 0)))
    o, _ = stitch([r["text"] for r in old])
    for c in new:
        for sp in (c.spans or [c.text]):
            ns = norm(sp)
            if ns and _find_word(o, ns, 0) < 0:
                return False
    n_text, _ = stitch([c.text for c in new])
    joined = norm(" ".join(c.text for c in new))
    for r in old:
        t = norm(r["text"])
        if t and _find_word(n_text, t, 0) < 0 and _find_word(joined, t, 0) < 0:
            return False
    return True


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


# Unit-file keys naming chunk owners / chunks (see prepare.write_units and
# thread_enrich.reassemble_thread). Anything that names an id is collected; a
# false positive only defers an item, which is the safe direction.
_SPLIT_SOURCES = ("gdrive", "gmail", "anarlog", "calendar")


def needs_reflow(rows: list[dict]) -> bool:
    """True when an owner's stored chunk rows (Store.owner_chunks) are what
    Store.reflow_candidates selects: a block-extracted MIME below its
    EXTRACTION_VERSIONS entry, or a multi-chunk, non-table lineage below
    SPLIT_VERSION. The ORDINARY ingest paths use it to hand an unchanged owner
    to the reflow instead of re-chunking it destructively (final review I2).
    A lineage with no chunk_total counts its own rows (legacy chunks)."""
    from mcpbrain.chunking import SPLIT_VERSION
    from mcpbrain.sync.blocks import extraction_version
    counts: dict[str, int] = {}
    for r in rows:
        key = lineage_key(r["doc_id"], r["metadata"] or {})
        counts[key] = counts.get(key, 0) + 1
    for r in rows:
        md = r["metadata"] or {}
        st = md.get("source_type")
        mime = md.get("mime_type") if st == "gdrive" else md.get("attachment_mime")
        want = extraction_version(mime or "")
        if st in ("gdrive", "gmail") and want and int(md.get("extraction_version") or 0) < want:
            return True
        if (st in _SPLIT_SOURCES and int(md.get("split_version") or 0) < SPLIT_VERSION
                and md.get("content_subtype") != "table"):
            total = md.get("chunk_total")
            if total is None:
                total = counts[lineage_key(r["doc_id"], md)]
            if int(total or 1) > 1:
                return True
    return False


_UNIT_REF_KEYS = frozenset({"thread_id", "message_id", "doc_id", "file_id",
                            "event_id", "session_id"})
_UNIT_REF_LISTS = frozenset({"part_doc_ids", "chunk_doc_ids", "doc_ids"})


def _collect_unit_refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _UNIT_REF_KEYS and isinstance(v, (str, int)) and v != "":
                out.add(str(v))
            elif k in _UNIT_REF_LISTS:
                if isinstance(v, list):
                    for d in v:
                        if isinstance(d, (str, int)) and d != "":
                            out.add(str(d))
                        else:
                            _collect_unit_refs(d, out)
                else:
                    _collect_unit_refs(v, out)
            elif isinstance(v, (dict, list)):
                _collect_unit_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_unit_refs(v, out)


# home -> (per-entry stamp, refs): re-scanned whenever an entry of units/ or claims/
# changes, so a unit written mid-cycle is still seen, while repeated calls
# (one per reflow item or import) do not re-read hundreds of unit files.
_REFS_CACHE: dict[str, tuple[tuple, set[str]]] = {}


def _queue_stamp(queue) -> tuple:
    """Every entry's (name, mtime_ns, size) in units/ and claims/: one stat
    per entry, no file read or JSON parse. The directory mtime alone is not
    enough -- on a coarse-mtime filesystem two writes in the same tick leave
    it unchanged and the cache would serve a scan that misses a new unit."""
    import os
    out = []
    for d in (queue / "units", queue / "claims"):
        try:
            with os.scandir(d) as it:
                entries = []
                for e in it:
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    entries.append((e.name, st.st_mtime_ns, st.st_size))
            out.append(tuple(sorted(entries)))
        except OSError:
            out.append(None)
    return tuple(out)


def pending_unit_refs(home) -> set[str]:
    """Every id (thread_id, message_id, doc_id, file_id, event_id, session_id,
    and every part_doc_ids / chunk_doc_ids / doc_ids entry) named by an
    enrichment unit still pending or claimed under <home>/enrich_queue (spec
    §3 guard: an owner with an in-flight unit must not be reflowed, or drain
    would later mark re-chunked, never-extracted text enriched). The single
    scanner for every reflow path (queue handler and ingest-cache import).
    Unreadable or malformed unit files are ignored. A claim's unit is read
    from units/<uid>.json; a claim with no readable unit file contributes
    nothing. The returned set is shared -- do not mutate it."""
    import json
    from pathlib import Path
    queue = Path(home) / "enrich_queue"
    stamp = _queue_stamp(queue)
    hit = _REFS_CACHE.get(str(queue))
    if hit is not None and hit[0] == stamp:
        return hit[1]
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
    _REFS_CACHE[str(queue)] = (stamp, out)
    return out


# -- which reflow sources this install can work --------------------------------

REFLOW_SOURCES = ("reflow:drive", "reflow:gmail", "reflow:calendar", "reflow:anarlog")
REFLOW_SOURCE_SERVICES = {"reflow:drive": "drive_service",
                          "reflow:gmail": "gmail_service",
                          "reflow:calendar": "calendar_service"}


def workable_reflow_sources(home, services: dict | None = None) -> set[str]:
    """Reflow sources this install can work as configured -- the ONE set the
    seed restricts `remaining` to, frees queued rows outside of, and records
    in `reflow:last_seed` for doctor / `bin/reflow.py status` to count against.

    anarlog only while it is enabled (config.anarlog_db_path). A Google source
    is workable when its service is built, or -- when it is not -- unless that
    absence is PERMANENT: no stored token at all (not configured), or a token
    whose stored scopes lack the service's scope (auth.build_google_services
    omits such a service by design). Any other absence (the token could not
    be refreshed, a build failed) is transient: the source stays workable, so
    its queued rows are kept and `remaining` still counts it."""
    from pathlib import Path

    from mcpbrain import config
    out: set[str] = set()
    if config.anarlog_db_path(home):
        out.add("reflow:anarlog")
    services = services or {}
    missing = [s for s, key in REFLOW_SOURCE_SERVICES.items() if services.get(key) is None]
    out |= set(REFLOW_SOURCE_SERVICES) - set(missing)
    if not missing:
        return out
    from mcpbrain import auth
    token = Path(home) / auth.token_path().name
    if not token.exists():
        return out
    try:
        import json
        stored = json.loads(token.read_text()).get("scopes")
    except (OSError, ValueError, AttributeError):
        stored = None
    scope_of = {key: scope for key, _api, _v, scope in auth._SERVICE_SPECS}
    for src in missing:
        if not stored or scope_of[REFLOW_SOURCE_SERVICES[src]] in stored:
            out.add(src)
    return out
