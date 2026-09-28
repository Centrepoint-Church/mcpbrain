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


def remap_gold(gold_path: Path, store, *, dry_run: bool = False) -> list[tuple[str, str]]:
    """Point gold expected_chunk_ids that a reflow removed at their new chunk.

    Textual edit of '- <id>' list lines (never a YAML parse/dump), so the
    file's comments and formatting survive untouched. Only ids with NO chunk
    row in `store` are considered; an id that still resolves is left exactly
    as written even if a (stale) reflow target exists for it. `dry_run=True`
    computes and returns the same changes without touching the file — the
    CLI's `--write` flag is what decides whether a run is a preview or a
    real rewrite."""
    import re
    text = gold_path.read_text()
    changes: list[tuple[str, str]] = []

    def sub(m):
        old = m.group(2)
        if store.get_chunk(old) is not None:
            return m.group(0)
        new = store.latest_reflow_target(old)
        if not new or new == old:
            return m.group(0)
        changes.append((old, new))
        return f"{m.group(1)}{new}"

    out = re.sub(r"^(\s*-\s+)((?:gdrive|gmail|cal|anarlog)-\S+)\s*$", sub, text, flags=re.M)
    if changes and not dry_run:
        gold_path.write_text(out)
    return changes


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
    ns = ap.parse_args(argv)
    if ns.cmd == "use":
        for p in use_profile(Path(ns.dir)):
            print(f"installed {p.relative_to(_REPO)}")
        return 0
    if ns.cmd == "remap-gold":
        from mcpbrain import config
        from mcpbrain.embed import get_embedder
        from mcpbrain.store import Store
        store = Store(config.store_path(), dim=get_embedder("bge-small").dim, read_only=True)
        changes = remap_gold(Path(ns.gold), store, dry_run=not ns.write)
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
