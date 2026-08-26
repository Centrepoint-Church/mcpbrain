#!/usr/bin/env python3
"""Build a wheel and refresh the PEP 503 index in the dist repo.

Usage: python bin/release.py --dist /path/to/mcpbrain-dist
Builds mcpbrain (`uv build --wheel`), copies the wheel into <dist>/simple/mcpbrain/,
and regenerates the two index.html files. The maintainer then commits + pushes the
dist repo (GitHub Pages serves it). Bump mcpbrain.__version__ + pyproject before running.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def render_package_index(wheel_names: list[str]) -> str:
    links = "\n".join(f'    <a href="{w}">{w}</a><br>' for w in sorted(wheel_names))
    return ("<!DOCTYPE html><html><head><meta name=\"pypi:repository-version\" "
            "content=\"1.0\"></head><body>\n" + links + "\n</body></html>\n")


def render_root_index() -> str:
    return ('<!DOCTYPE html><html><body>\n    <a href="mcpbrain/">mcpbrain</a><br>\n'
            '</body></html>\n')


def copy_installer(repo: Path, dist: Path) -> Path | None:
    """Publish plugin/scripts/install.ps1 to the dist repo root.

    Windows installs fetch this from GitHub Pages
    (…/mcpbrain-dist/install.ps1), but the source of truth is this repo. It used
    to be hand-copied at release time with nothing verifying it, so a fixed
    installer could sit unpublished for releases at a time. Returns the written
    path, or None if the source is missing.
    """
    src = Path(repo) / "plugin" / "scripts" / "install.ps1"
    if not src.is_file():
        return None
    dest = Path(dist) / "install.ps1"
    shutil.copy2(src, dest)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, help="path to the public dist repo checkout")
    ap.add_argument("--repo", default=".", help="path to the mcpbrain source repo")
    ns = ap.parse_args(argv)
    # Wipe stale build intermediates first. setuptools reuses build/lib, so a file
    # deleted from the source can otherwise reship in the wheel (it happened once:
    # a removed module rode along in a release build). Clean build/ + *.egg-info so
    # the wheel reflects exactly the current source tree.
    repo = Path(ns.repo)
    shutil.rmtree(repo / "build", ignore_errors=True)
    for egg in repo.glob("*.egg-info"):
        shutil.rmtree(egg, ignore_errors=True)
    out = subprocess.run(["uv", "build", "--wheel", "--out-dir", f"{ns.repo}/dist", ns.repo],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stdout + out.stderr, file=sys.stderr); return out.returncode
    pkg_dir = Path(ns.dist) / "simple" / "mcpbrain"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    for whl in Path(f"{ns.repo}/dist").glob("mcpbrain-*.whl"):
        shutil.copy2(whl, pkg_dir / whl.name)
    wheels = [p.name for p in pkg_dir.glob("mcpbrain-*.whl")]
    (pkg_dir / "index.html").write_text(render_package_index(wheels))
    (Path(ns.dist) / "simple" / "index.html").write_text(render_root_index())
    installer = copy_installer(Path(ns.repo), Path(ns.dist))
    if installer is None:
        print("WARNING: plugin/scripts/install.ps1 not found — Windows installer "
              "NOT published.", file=sys.stderr)
    print(f"Index refreshed at {ns.dist}/simple/ ({len(wheels)} wheels)"
          f"{'; install.ps1 published' if installer else ''}. "
          f"Commit + push the dist repo to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
