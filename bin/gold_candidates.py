#!/usr/bin/env python3
"""Draft gold-retrieval-set candidates for the owner to review.

mcpbrain measures retrieval quality against hand-curated "gold" cases: a query
plus the chunk ids that should come back. This tool proposes new candidates
from documents that already have several chunks, so the owner can write a
query that spans two parts of the same document -- it never invents a query
itself, and it never scores anything.

    python bin/gold_candidates.py draft --store <path> --out <yaml> --n 20
    python bin/gold_candidates.py verify --store <path> <yaml>

This file is PUBLIC and holds no data -- it only reads a store at a path it is
given and writes/reads a YAML file at a path it is given. Gold files are
private tenant data (see tests/eval/run_eval.py::load_gold_cases), so `draft`
refuses to write anywhere inside this repository: the one thing it must never
do is leave a copy of someone's content where a public clone would pick it up.

`--store` is opened read-only; this tool never writes to the brain store. The
live store is normally daemon-owned -- do not run this against it while the
daemon is also writing (see CLAUDE.md's store-corruption incident).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain.store import Store, store_dim_from_path  # noqa: E402
from mcpbrain.sync.blocks import EXTRACTION_VERSIONS  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DIM = 384

# A chunk's doc_id is <owner>-<...>-<i>; stripping the trailing index groups
# chunks back to their parent document. Mirrors tests/eval/run_eval.py's
# gold_eval._doc_key, which relies on the same convention.
_TRAILING_INDEX_RE = re.compile(r"-(\d+)$")


def _doc_root(doc_id: str) -> str:
    return _TRAILING_INDEX_RE.sub("", doc_id)


def _suffix_index(doc_id: str) -> int:
    m = _TRAILING_INDEX_RE.search(doc_id)
    return int(m.group(1)) if m else 0


def _ordered_group(doc_ids: list[tuple[str, dict]]) -> list[str]:
    """Deterministic order within one document's chunks, so a re-run picks the
    same two ids: metadata.chunk_index when a chunk carries one (the
    authoritative logical order), else the doc_id's own trailing index."""
    def key(item: tuple[str, dict]):
        doc_id, meta = item
        idx = meta.get("chunk_index")
        if idx is None:
            idx = _suffix_index(doc_id)
        return (int(idx), doc_id)
    return [doc_id for doc_id, _meta in sorted(doc_ids, key=key)]


def _snippet(text: str | None) -> str:
    return (text or "")[:120]


def _nearest_existing_ancestor(path: Path) -> Path:
    """path itself if it exists, else the first ancestor that does. --out
    names a file that normally doesn't exist yet, so this is almost always a
    directory -- but the walk is content-free either way, just filesystem
    lookups."""
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:  # reached the filesystem root
            break
        candidate = parent
    return candidate


def _is_within_repo(ancestor: Path) -> bool:
    """True when `ancestor`, or any of its parents, IS the repo root -- by
    device+inode, not by string/Path comparison.

    Path.resolve() on POSIX does not case-fold: on a case-insensitive
    filesystem (e.g. this Mac's default APFS), a path spelled with different
    case than the repo's own (GitHub/MCPBRAIN vs GitHub/mcpbrain) normalizes
    to a DIFFERENT string that nonetheless names the SAME file -- so a plain
    `==`/`in .parents` check misses it. os.stat() resolves a path through the
    filesystem's own case-folding, so stat'ing the case-varied spelling still
    returns the real inode; comparing (st_dev, st_ino) catches that case AND
    a symlink that points into the repo from outside it, since `ancestor` is
    already the resolved (symlink-followed) path by the time it gets here.
    """
    try:
        repo_stat = os.stat(_REPO_ROOT)
    except OSError:
        return False  # the repo root always exists in practice; defensive only
    current = ancestor
    while True:
        try:
            st = os.stat(current)
        except OSError:
            st = None
        if st is not None and (st.st_dev, st.st_ino) == (repo_stat.st_dev, repo_stat.st_ino):
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _refuse_if_in_repo(out_path: Path) -> None:
    """Gold candidates are private tenant data; never let one land where a
    public clone of this repo would pick it up."""
    resolved = out_path.resolve()
    ancestor = _nearest_existing_ancestor(resolved)
    if _is_within_repo(ancestor):
        print(f"refusing to write {resolved}: it is inside this repo ({_REPO_ROOT}); "
              "gold candidates are private tenant data and must be written elsewhere "
              "(e.g. ../mcpbrain-tenant/eval/)", file=sys.stderr)
        raise SystemExit(2)


def _resolve_dim(path: Path, dim: int | None) -> int:
    if dim is not None:
        return dim
    return store_dim_from_path(path) or _DEFAULT_DIM


def _open_store(store_path: str, dim: int | None) -> Store:
    path = Path(store_path)
    return Store(path, dim=_resolve_dim(path, dim), read_only=True)


def _chunk_groups(store: Store) -> dict[str, list[tuple[str, dict]]]:
    """{doc_root: [(doc_id, metadata), ...]} for every chunk in the store."""
    groups: dict[str, list[tuple[str, dict]]] = {}
    with store._connect() as db:
        for row in db.execute("SELECT doc_id, metadata FROM chunks"):
            meta = json.loads(row["metadata"]) if row["metadata"] else {}
            groups.setdefault(_doc_root(row["doc_id"]), []).append((row["doc_id"], meta))
    return groups


def _doc_kind(root: str) -> str | None:
    """"drive" | "gmail" | None (not a document kind this tool drafts from).

    Only an ORIGINAL Drive file or Gmail message makes a useful gold case --
    a query the owner writes should name real content the owner recognises.
    Everything else a live store also chunks (`enriched-<thread>` synthesized
    digests, `anarlog-` meeting notes, `cal-` calendar events, `note-`
    captures) is explicitly excluded, never just left to fail some other
    filter -- a live run that drafted 20 enriched-/anarlog-/cal- candidates
    and zero real documents is exactly the failure this guards against.

    A Gmail message's root must end in `-body`: attachment chunks
    (`gmail-<id>-att-<idx>-<i>`) strip to `gmail-<id>-att-<idx>`, which does
    not match, so they are excluded the same way regardless of chunk count.
    """
    if root.startswith("gdrive-"):
        return "drive"
    if root.startswith("gmail-") and root.endswith("-body"):
        return "gmail"
    return None


def _qualifying_documents(store: Store) -> list[dict]:
    """[{root, kind, members}] for every Drive file in a block MIME (per
    mcpbrain.sync.blocks.EXTRACTION_VERSIONS) with >= 3 chunks, or Gmail
    message with >= 2 body chunks. members is the group's (doc_id, metadata)
    pairs, as returned by _chunk_groups."""
    out: list[dict] = []
    for root, members in _chunk_groups(store).items():
        kind = _doc_kind(root)
        if kind is None:
            continue
        if kind == "drive":
            if len(members) < 3:
                continue
            mime = next((meta.get("mime_type") for _doc_id, meta in members
                         if meta.get("mime_type")), None)
            if mime not in EXTRACTION_VERSIONS:
                continue
        elif len(members) < 2:  # kind == "gmail"
            continue
        out.append({"root": root, "kind": kind, "members": members})
    return out


def _hash_key(root: str) -> str:
    """sha1 of the doc root -- the ordering key for both picking and listing
    candidates, so a re-run against an unchanged store is byte-identical and
    the spread across the corpus doesn't just track insertion/alphabetical
    order."""
    return hashlib.sha1(root.encode("utf-8")).hexdigest()


def _select_candidates(documents: list[dict], n: int) -> list[dict]:
    """Up to `n` documents, ordered by sha1(doc_root). When both Drive and
    Gmail documents are available, aims for about half of each (n may be
    odd -- the extra slot, and any shortfall in one kind, goes to whichever
    kind still has supply) rather than letting one kind crowd out the other."""
    by_kind: dict[str, list[dict]] = {"drive": [], "gmail": []}
    for doc in documents:
        by_kind[doc["kind"]].append(doc)
    for pool in by_kind.values():
        pool.sort(key=lambda d: _hash_key(d["root"]))

    take = {"drive": min(n - n // 2, len(by_kind["drive"])),
            "gmail": min(n // 2, len(by_kind["gmail"]))}
    remaining = n - take["drive"] - take["gmail"]
    for kind in sorted(by_kind, key=lambda k: len(by_kind[k]) - take[k], reverse=True):
        if remaining <= 0:
            break
        extra = min(remaining, len(by_kind[kind]) - take[kind])
        take[kind] += extra
        remaining -= extra

    selected = by_kind["drive"][:take["drive"]] + by_kind["gmail"][:take["gmail"]]
    selected.sort(key=lambda d: _hash_key(d["root"]))
    return selected


def draft(store_path: str, out_path: str, n: int, *, dim: int | None = None) -> list[dict]:
    """Write up to `n` candidate stubs to out_path (YAML list): one per
    qualifying Drive file or Gmail message (see _qualifying_documents) --
    never an enriched-/anarlog-/cal-/note- document or a non-block-MIME Drive
    file. Each stub has an EMPTY query for the owner to write.

    Deterministic: candidates are selected and ordered by sha1(doc_root) (see
    _select_candidates), and the two expected_chunk_ids are the first and
    last chunk of the document in its own logical order -- a re-run against
    an unchanged store yields identical output.
    """
    out = Path(out_path)
    _refuse_if_in_repo(out)

    store = _open_store(store_path, dim)
    selected = _select_candidates(_qualifying_documents(store), n)

    candidates: list[dict] = []
    for doc in selected:
        ordered = _ordered_group(doc["members"])
        first_id, last_id = ordered[0], ordered[-1]
        first_chunk = store.get_chunk(first_id)
        last_chunk = store.get_chunk(last_id)
        candidates.append({
            "id": f"cand_{doc['root']}",
            "query": "",
            "expected_chunk_ids": [first_id, last_id],
            "notes": f"{_snippet(first_chunk['text'] if first_chunk else '')} || "
                     f"{_snippet(last_chunk['text'] if last_chunk else '')}",
        })

    import yaml  # pyyaml -- dev dependency, same as tests/eval/run_eval.py
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(candidates, sort_keys=False, allow_unicode=True))
    return candidates


def verify(store_path: str, yaml_path: str, *, dim: int | None = None) -> tuple[int, int]:
    """(present, total) -- how many expected_chunk_ids across every case in
    yaml_path still have a chunk row in the store. Read-only, no writes."""
    import yaml  # pyyaml -- dev dependency

    store = _open_store(store_path, dim)
    cases = yaml.safe_load(Path(yaml_path).read_text()) or []
    present = total = 0
    for case in cases:
        for doc_id in case.get("expected_chunk_ids") or []:
            total += 1
            if store.get_chunk(doc_id) is not None:
                present += 1
    return present, total


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="gold_candidates",
        description="Draft gold-retrieval-set candidates for owner review, "
                     "or verify an existing candidates/gold YAML against a store.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft", help="propose candidates from multi-chunk documents")
    d.add_argument("--store", required=True, help="path to the sqlite store (opened read-only)")
    d.add_argument("--out", required=True,
                   help="output YAML path -- must be OUTSIDE this repo (private tenant data)")
    d.add_argument("--n", type=int, default=20, help="max candidates to draft (default: 20)")
    d.add_argument("--dim", type=int, default=None,
                   help="embedding dim; default: read from the store, else 384")

    v = sub.add_parser("verify", help="report how many expected_chunk_ids still exist")
    v.add_argument("--store", required=True, help="path to the sqlite store (opened read-only)")
    v.add_argument("yaml_path", help="candidates/gold YAML file to check")
    v.add_argument("--dim", type=int, default=None,
                   help="embedding dim; default: read from the store, else 384")

    ns = ap.parse_args(argv)

    if ns.cmd == "draft":
        candidates = draft(ns.store, ns.out, ns.n, dim=ns.dim)
        print(f"wrote {len(candidates)} candidate(s) to {ns.out}")
        return 0

    present, total = verify(ns.store, ns.yaml_path, dim=ns.dim)
    print(f"{present}/{total} expected_chunk_ids present in {ns.store}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
