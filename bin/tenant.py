#!/usr/bin/env python3
"""mcpbrain tenant — install and validate this build's tenant profile.

`use <dir>` copies a private tenant directory (the mcpbrain-tenant repo) into the
source tree, where every downstream consumer already looks: `uv build`,
`uv tool install --force .`, and the test suite. It is a one-time step per
checkout, not per build.

`check` validates the installed profile. Run it before any release —
bin/release.py runs the offline half itself and refuses to build on failure.

Runnable from a bare source checkout (before anything is installed), which is why
it lives in bin/ and is also wired into the `mcpbrain tenant` CLI.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# (source path in the tenant repo, destination path in the source tree).
#
# The OAuth client is REQUIRED — it is the only file a build cannot be made without.
# tenant.json is optional because it is committed in the source repo; a tenant repo
# may keep a reference copy, and if it does, it wins.
#
# The gold eval sets are optional and are TENANT DATA, not product: their chunk ids
# point into one organisation's own store, so a fork cannot use another's. They used
# to be committed under tests/, where the tenant-literal guard deliberately does not
# look ("fixtures and history") — which is right for a slugify assertion and wrong
# for a curated corpus describing real people. A fork with no curated cases simply
# has none: load_gold_cases() returns [] and the gold floor test skips honestly.
#
# tenant_people.json is the same idea applied to PERSON names rather than org
# identifiers: a real person's surname leaked into this PUBLIC repo's tests/ for
# months (see CLAUDE.md for the incident) because the tenant-literal guard only ever checked org/
# infrastructure identifiers, never staff names — and a guard that enumerated real
# names to check for their own absence would just reintroduce the leak it exists to
# prevent. So the forbidden-names list itself lives in the private tenant repo, is
# gitignored here, and test_no_tenant_literals.py skips its person-name check
# honestly when the file is absent (a fork gets a clean skip, not a failure).
_REQUIRED = (("google_oauth_client.json", "mcpbrain/google_oauth_client.json"),)
_OPTIONAL = (
    ("tenant.json", "mcpbrain/tenant.json"),
    ("tenant_people.json", "mcpbrain/tenant_people.json"),
    ("eval/golden_retrieval_set.yaml", "tests/eval/golden_retrieval_set.yaml"),
    ("eval/golden_retrieval_set_mcpbrain_candidate.yaml",
     "tests/eval/golden_retrieval_set_mcpbrain_candidate.yaml"),
)


def use_profile(src: Path, repo: Path = _REPO) -> list[Path]:
    """Copy a tenant directory's files into <repo>/mcpbrain/. Returns what it wrote."""
    src, repo = Path(src), Path(repo)
    written: list[Path] = []
    for name, _dest in _REQUIRED:
        if not (src / name).is_file():
            raise FileNotFoundError(f"{src} has no {name} — is this a tenant repo?")
    for name, rel_dest in (*_REQUIRED, *_OPTIONAL):
        origin = src / name
        if not origin.is_file():
            continue
        dest = repo / rel_dest
        # A fresh clone has no tests/eval/ until pytest creates it, and copy2 into
        # a missing parent raises.
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, dest)
        written.append(dest)
    return written


_GOLD_WATERMARK = "# remap-gold: reflow_map applied through id "


def _reflow_batches(rows):
    """Group reflow_map rows (ascending id) into apply_reflow batches: one
    owner + one timestamp. A batch's rows are SIMULTANEOUS (i->j and j->i in
    one reflow), so they are applied as one mapping, never chained."""
    batches: list[dict[str, str]] = []
    key = None
    for r in rows:
        k = (r["owner"], r["at"])
        if k != key:
            batches.append({})
            key = k
        batches[-1][r["old_doc_id"]] = r["new_doc_id"]
    return batches


def remap_gold(gold_path: Path, store, *, dry_run: bool = False, from_start: bool = False,
               info: dict | None = None) -> list[tuple[str, str]]:
    """Repoint gold expected_chunk_ids through reflow_map (spec §5).

    doc_ids are positional and REUSED by a reflow: after one, gdrive-F-4
    usually still exists but holds other text, so whether an id still has a
    chunk row says nothing. Every reflow batch recorded after the file's
    watermark (a `# remap-gold: reflow_map applied through id N` line this
    writes) is applied in order, each batch as one simultaneous mapping, and
    the watermark is advanced -- so a re-run is a no-op and a later reflow is
    composed onto the earlier one, never re-applied.

    Textual edit of '- <id>' list lines (never a YAML parse/dump): single- or
    double-quoted ids and trailing '# comments' are kept exactly, and so is
    every other line. `dry_run=True` returns the same changes without
    touching the file (the CLI's `--write` decides).

    The watermark is persisted whenever it advances, even when no id changed:
    otherwise a later edit that adds a CURRENT id would have already-seen
    batches replayed onto it. A file with NO watermark is ambiguous (written
    before any reflow, or after one), so by default it is treated as current:
    it adopts the store's max reflow_map id and nothing is remapped.
    `from_start=True` (the CLI's `--from-start`) replays from id 0 instead --
    the right call exactly once, for a gold file that predates the reflow; on
    a file that already carries a watermark it raises ValueError rather than
    being silently ignored.
    `info`, when given, is filled with {after, watermark, adopted}."""
    import re
    text = gold_path.read_text()
    m = re.search(r"^" + re.escape(_GOLD_WATERMARK) + r"(\d+)[ \t]*$", text, flags=re.M)
    adopted = False
    if m and from_start:
        # The file was already remapped through id N: replaying from id 0 would
        # apply those batches a second time and move ids that name current text.
        raise ValueError(
            f"{gold_path} already carries a remap-gold watermark (id {m.group(1)}); "
            f"--from-start is only for a gold file with NO watermark. Re-run without "
            f"--from-start to apply only the batches after id {m.group(1)}.")
    if m:
        after = int(m.group(1))
        rows = list(store.reflow_map_rows(after_id=after))
    elif from_start:
        after = 0
        rows = list(store.reflow_map_rows(after_id=0))
    else:
        seen = list(store.reflow_map_rows(after_id=0))
        after = seen[-1]["id"] if seen else 0
        adopted = bool(seen)
        rows = []
    watermark = rows[-1]["id"] if rows else after
    if info is not None:
        info.update({"after": after, "watermark": watermark, "adopted": adopted})
    batches = _reflow_batches(rows)
    changes: list[tuple[str, str]] = []

    def resolve(doc_id: str) -> str:
        for b in batches:
            doc_id = b.get(doc_id, doc_id)
        return doc_id

    def sub(mm):
        indent, dq, sq, bare, tail = mm.groups()
        old = dq if dq is not None else sq if sq is not None else bare
        new = resolve(old)
        if new == old:
            return mm.group(0)
        changes.append((old, new))
        q = '"' if dq is not None else "'" if sq is not None else ""
        return f"{indent}{q}{new}{q}{tail}"

    # Gmail attachment ids embed the attachment's FILENAME (spaces, brackets,
    # dashes), so an id is everything up to its closing quote, or -- unquoted
    # -- up to a ' #' comment or the end of the line. [ \t], never \s: \s*$
    # under re.M swallows the newline and a following blank line.
    ident = r"(?:gdrive|gmail|cal|anarlog)-"
    out = re.sub(r"^([ \t]*-[ \t]+)"
                 rf"""(?:"({ident}[^"\n]*)"|'({ident}[^'\n]*)'|({ident}[^\n]*?))"""
                 r"([ \t]+#.*|[ \t]*)$", sub, text, flags=re.M)
    advanced = watermark > (int(m.group(1)) if m else 0) or (adopted and not m)
    if advanced:
        mark = f"{_GOLD_WATERMARK}{watermark}"
        if m:
            out = out.replace(m.group(0), mark, 1)
        else:
            out = f"{mark}\n{out}"
    if (changes or advanced) and not dry_run:
        gold_path.write_text(out)
    return changes


def _open_gold_store():
    """The live store, read-only (remap-gold never writes it)."""
    from mcpbrain import config
    from mcpbrain.embed import get_embedder
    from mcpbrain.store import Store
    return Store(config.store_path(), dim=get_embedder("bge-small").dim, read_only=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcpbrain tenant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_use = sub.add_parser("use", help="copy a private tenant directory into the tree")
    p_use.add_argument("dir", help="path to the mcpbrain-tenant checkout")
    p_check = sub.add_parser("check", help="validate the installed tenant profile")
    p_check.add_argument("--online", action="store_true",
                          help="also check Drive folders, the wheel index and the "
                               "marketplace repo")
    p_gold = sub.add_parser("remap-gold", help="repoint gold chunk ids moved by a reflow")
    p_gold.add_argument("gold", help="path to a gold YAML (in the tenant checkout)")
    p_gold.add_argument("--write", action="store_true")
    p_gold.add_argument("--from-start", action="store_true",
                        help="replay reflow_map from id 0 for a gold file with no "
                             "watermark (one written BEFORE the reflow). Without it, "
                             "a watermark-less file adopts the current max id.")
    ns = ap.parse_args(argv)
    if ns.cmd == "use":
        for p in use_profile(Path(ns.dir)):
            print(f"installed {p.relative_to(_REPO)}")
        return 0
    if ns.cmd == "remap-gold":
        info: dict = {}
        try:
            changes = remap_gold(Path(ns.gold), _open_gold_store(), dry_run=not ns.write,
                                 from_start=ns.from_start, info=info)
        except ValueError as exc:
            print(f"remap-gold refused: {exc}", file=sys.stderr)
            return 2
        if info.get("adopted"):
            print(f"no watermark: started at the current max reflow_map id "
                  f"{info['watermark']}; nothing replayed. If "
                  f"this gold file predates the reflow, remove the watermark line and "
                  f"re-run with --from-start.")
        for o, n in changes:
            print(f"{o} -> {n}")
        print(f"{len(changes)} id(s) {'rewritten' if ns.write else 'would change'}")
        return 0
    from mcpbrain import tenant as _tenant
    problems = _tenant.check_offline(_REPO)
    if problems:
        print("tenant check FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    prof = _tenant.load(_REPO / "mcpbrain" / "tenant.json")

    # The pass line is printed LAST, after every requested check. Printing it
    # straight after the offline half meant `check --online` could report
    # "✓ passed" on stdout, "✗" on stderr and exit 1 — three different answers
    # to one question.
    if getattr(ns, "online", False):
        from mcpbrain import auth
        try:
            creds = auth.load_credentials()
            drive = auth.build_service("drive", "v3", creds)
        except Exception as exc:  # noqa: BLE001
            print(f"  ➖ Drive checks skipped: {exc}")
            drive = None
        online, notes = _tenant.check_online(prof, drive=drive)
        for n in notes:
            print(f"  ➖ {n}")
        if online:
            print("tenant check FAILED:", file=sys.stderr)
            for p in online:
                print(f"  ✗ {p}", file=sys.stderr)
            return 1

    print(f"✓ tenant check passed — {prof.display_name} ({prof.tenant_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
