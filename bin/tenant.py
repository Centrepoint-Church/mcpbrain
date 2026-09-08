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

# The OAuth client is REQUIRED (it is the only genuinely private file); tenant.json
# is optional here because it is committed in the source repo — a tenant repo may
# keep a reference copy, and if it does, it wins.
_REQUIRED = ("google_oauth_client.json",)
_OPTIONAL = ("tenant.json",)


def use_profile(src: Path, repo: Path = _REPO) -> list[Path]:
    """Copy a tenant directory's files into <repo>/mcpbrain/. Returns what it wrote."""
    src, repo = Path(src), Path(repo)
    written: list[Path] = []
    for name in _REQUIRED:
        origin = src / name
        if not origin.is_file():
            raise FileNotFoundError(f"{src} has no {name} — is this a tenant repo?")
    for name in (*_REQUIRED, *_OPTIONAL):
        origin = src / name
        if not origin.is_file():
            continue
        dest = repo / "mcpbrain" / name
        shutil.copy2(origin, dest)
        written.append(dest)
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mcpbrain tenant")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_use = sub.add_parser("use", help="copy a private tenant directory into the tree")
    p_use.add_argument("dir", help="path to the mcpbrain-tenant checkout")
    sub.add_parser("check", help="validate the installed tenant profile")
    ns = ap.parse_args(argv)
    if ns.cmd == "use":
        for p in use_profile(Path(ns.dir)):
            print(f"installed {p.relative_to(_REPO)}")
        return 0
    from mcpbrain import tenant as _tenant
    problems = _tenant.check_offline(_REPO)
    if problems:
        print("tenant check FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 1
    prof = _tenant.load(_REPO / "mcpbrain" / "tenant.json")
    print(f"✓ tenant check passed — {prof.display_name} ({prof.tenant_id})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
