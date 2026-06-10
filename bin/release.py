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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", required=True, help="path to the public dist repo checkout")
    ap.add_argument("--repo", default=".", help="path to the mcpbrain source repo")
    ns = ap.parse_args(argv)
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
    print(f"Index refreshed at {ns.dist}/simple/ ({len(wheels)} wheels). "
          f"Commit + push the dist repo to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
