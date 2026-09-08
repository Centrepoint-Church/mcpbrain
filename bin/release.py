#!/usr/bin/env python3
"""Build a wheel and refresh the PEP 503 index in the dist repo.

Usage: python bin/release.py --dist /path/to/mcpbrain-dist
Builds mcpbrain (`uv build --wheel`), copies the wheel into <dist>/simple/mcpbrain/,
and regenerates the two index.html files. The maintainer then commits + pushes the
dist repo (GitHub Pages serves it). Bump mcpbrain.__version__ + pyproject before running.
"""
import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# What every shipped wheel MUST carry. tenant.json names the deployment; the OAuth
# client is what lets it authenticate at all. A wheel missing either is a silent
# fleet-wide outage on the next daily auto-update, so this is checked against the
# wheel actually produced by THIS run, not whatever dist/ happens to contain.
_REQUIRED_IN_WHEEL = ("mcpbrain/tenant.json", "mcpbrain/google_oauth_client.json")


def verify_wheel(wheel: Path, repo: Path) -> list[str]:
    """Assert a built wheel carries this tenant's profile and OAuth client."""
    problems: list[str] = []
    with zipfile.ZipFile(wheel) as z:
        names = set(z.namelist())
        for required in _REQUIRED_IN_WHEEL:
            if required not in names:
                problems.append(f"{wheel.name}: missing {required}")
        if "mcpbrain/google_oauth_client.json" in names:
            packed = json.loads(z.read("mcpbrain/google_oauth_client.json"))
            source = json.loads((Path(repo) / "mcpbrain" /
                                 "google_oauth_client.json").read_text())
            if packed.get("installed", {}).get("client_id") != \
                    source.get("installed", {}).get("client_id"):
                problems.append(
                    f"{wheel.name}: client_id does not match the source tree — this "
                    f"is a STALE wheel from a previous build, not this one")
    return problems


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
    from mcpbrain import tenant
    problems = tenant.check_offline(repo)
    if problems:
        print("release aborted — tenant profile invalid:", file=sys.stderr)
        for p in problems:
            print(f"  ✗ {p}", file=sys.stderr)
        print("Run `python bin/tenant.py use <tenant-dir>` and fix the above.",
              file=sys.stderr)
        return 2
    shutil.rmtree(repo / "build", ignore_errors=True)
    for egg in repo.glob("*.egg-info"):
        shutil.rmtree(egg, ignore_errors=True)
    out = subprocess.run(["uv", "build", "--wheel", "--out-dir", f"{ns.repo}/dist", ns.repo],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stdout + out.stderr, file=sys.stderr); return out.returncode
    from mcpbrain import __version__ as _ver
    built = Path(f"{ns.repo}/dist") / f"mcpbrain-{_ver}-py3-none-any.whl"
    if not built.is_file():
        print(f"release aborted — expected {built.name} in dist/ after build",
              file=sys.stderr)
        return 2
    wheel_problems = verify_wheel(built, repo)
    if wheel_problems:
        print("release aborted — built wheel is incomplete:", file=sys.stderr)
        for p in wheel_problems:
            print(f"  ✗ {p}", file=sys.stderr)
        return 2
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
    # A missing installer must not read as a clean release: this whole function
    # exists to replace a hand-copy step that had nothing verifying it, and a
    # `return 0` here would reproduce exactly that failure mode, just moved
    # from "forgot to run the cp command" to "the warning scrolled past in
    # scripted/CI output."
    return 0 if installer is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
