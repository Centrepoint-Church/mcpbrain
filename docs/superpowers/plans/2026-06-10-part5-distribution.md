# Part 5 — Distribution & Release (GitHub Pages wheel index + silent auto-update) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the git-pull-from-a-clone update model with a real release channel: versioned wheels published to a GitHub Pages PEP 503 index, installed/updated via `uv` (no source checkout), with the daemon silently auto-updating on a cadence.

**Architecture:** A `bin/release.py` builds a wheel and refreshes a PEP 503 "simple" index in a separate **public dist repo** served by GitHub Pages. `update.py` is rewritten to reinstall `mcpbrain` from that index via `uv tool install --index mcpbrain=<url> mcpbrain --upgrade` (the index is marked `explicit` so deps still resolve from PyPI). The daemon gains a `maybe_auto_update()` cadence (mirroring `maybe_backup`) that compares the installed version to the index's newest and reinstalls when behind. The installers become thin: bootstrap `uv`, then `uv tool install` from the index — no `git clone`, no persistent `--repo-dir`.

**Tech Stack:** Python 3.12, pytest, `uv`. Version compare is stdlib (tuple parse). The actual Pages hosting is a one-time maintainer deployment (documented), not code.

This is **Plan 5 of the productization series** — spec **Part 2**. Code-signing/notarization, public PyPI, and native installers remain out of scope ($0).

**Grounding (verified):**
- `update.py` today: `_repo_dir()` (env `MCPBRAIN_REPO` → persisted `repo_dir` → walk up for `pyproject.toml`), `_run(cmd)->(out,rc)`, `_restart_agent()` (calls `agents.restart_agent(sys.platform)`), `main()` (git pull --ff-only → `uv tool install --from repo mcpbrain --force --reinstall-package mcpbrain` → restart).
- `setup.py` persists `repo_dir` via `write_config(home, {"repo_dir": ...})` for update; that becomes unnecessary.
- `pyproject.toml`: `[project] version = "0.1.0"` (static); `[project.scripts] mcpbrain = "mcpbrain.cli:main"`.
- Daemon cadence pattern: `maybe_backup()` snapshots `(cfg, interval)` under `self._config_lock`, returns `None` if OFF (`self._backup is None`) or not due (`self._clock() - self._last_backup < interval`); advances `_last_*` only on success. Intervals are constructor kwargs, OFF when `None`.
- `tests/test_update.py` / `tests/test_setup.py` monkeypatch `update._run` / subprocess to fake `git`/`uv`.

---

## File Structure

- `mcpbrain/__init__.py` — add `__version__`.
- `mcpbrain/update.py` — rewrite around the index (`_index_url`, `_installed_version`, `_latest_version`, `_should_update`, `update_from_index`, `main`).
- `mcpbrain/daemon.py` — `auto_update_interval_s` kwarg + `maybe_auto_update()` + call it in the loop; wire from config in `apply_config`.
- `bin/release.py` — **new**: build wheel + refresh the PEP 503 index.
- `install/setup.sh`, `install/setup.command`, `install/setup.ps1` — install from the index, no clone.
- Tests: `tests/test_update_index.py`, `tests/test_daemon_autoupdate.py` (new).

---

## Task 1: Version source

**Files:**
- Modify: `mcpbrain/__init__.py`, `pyproject.toml`
- Test: `tests/test_version.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_version.py
def test_version_is_semver():
    import mcpbrain
    parts = mcpbrain.__version__.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
```

- [ ] **Step 2: Run → fail** (`AttributeError: __version__`). `pytest tests/test_version.py -v`

- [ ] **Step 3: Implement** — in `mcpbrain/__init__.py`:

```python
__version__ = "0.2.0"
```

and in `pyproject.toml` set `version = "0.2.0"` (keep them in lockstep; `bin/release.py` bumps both).

- [ ] **Step 4: Run → pass.**

- [ ] **Step 5: Commit** — `git commit -am "feat: real semver __version__ (0.2.0)"`

---

## Task 2: Rewrite `update.py` around the index

**Files:**
- Modify: `mcpbrain/update.py` (replace the git-pull body)
- Test: `tests/test_update_index.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_update_index.py
from mcpbrain import update


def test_latest_version_parses_pep503_index(monkeypatch):
    html = ('<!DOCTYPE html><html><body>'
            '<a href="mcpbrain-0.2.0-py3-none-any.whl">mcpbrain-0.2.0-py3-none-any.whl</a>'
            '<a href="mcpbrain-0.10.1-py3-none-any.whl">x</a>'
            '<a href="mcpbrain-0.9.0-py3-none-any.whl">x</a>'
            '</body></html>')
    monkeypatch.setattr(update, "_fetch", lambda url: html)
    assert update._latest_version("https://x/simple/") == "0.10.1"  # numeric, not lexical


def test_should_update_true_when_behind():
    assert update._should_update("0.2.0", "0.10.1") is True
    assert update._should_update("0.10.1", "0.10.1") is False
    assert update._should_update("0.11.0", "0.10.1") is False


def test_update_from_index_runs_uv_then_restart(monkeypatch):
    calls = {"run": [], "restart": 0}
    monkeypatch.setattr(update, "_run", lambda cmd: (calls["run"].append(cmd), ("", 0))[1])
    monkeypatch.setattr(update, "_restart_agent", lambda: calls.__setitem__("restart", 1))
    rc = update.update_from_index("https://org.github.io/mcpbrain-dist/simple/")
    assert rc == 0
    uv_cmd = calls["run"][0]
    assert uv_cmd[0] == "uv" and "tool" in uv_cmd and "install" in uv_cmd
    assert any("mcpbrain=" in c for c in uv_cmd)        # --index mcpbrain=<url>
    assert "mcpbrain" in uv_cmd and "--upgrade" in uv_cmd
    assert calls["restart"] == 1
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_update_index.py -v`

- [ ] **Step 3: Implement** — rewrite `mcpbrain/update.py` (keep `_run`/`_restart_agent` as they are; replace `_repo_dir`/`main`):

```python
"""mcpbrain update — reinstall from the wheel index, then restart.

Resolves the index URL (env → config → default), asks it for the newest
mcpbrain wheel, and if we're behind reinstalls via uv (the index is marked
explicit, so deps still come from PyPI), then restarts the daemon + tray.
"""
import os
import re
import subprocess
import sys
import urllib.request
from importlib.metadata import version, PackageNotFoundError

# Maintainer sets this to the published Pages index (the dist repo's /simple/).
DEFAULT_INDEX_URL = "https://CHANGE-ME.github.io/mcpbrain-dist/simple/"

_WHEEL_RE = re.compile(r"mcpbrain-(\d+\.\d+\.\d+)-")


def _index_url() -> str:
    env = os.environ.get("MCPBRAIN_INDEX_URL")
    if env:
        return env
    try:
        from mcpbrain.config import read_config, app_dir
        cfg = read_config(str(app_dir()))
        if cfg.get("update_index_url"):
            return cfg["update_index_url"]
    except Exception:  # noqa: BLE001 — config read must never break update
        pass
    return DEFAULT_INDEX_URL


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _parse(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def _installed_version() -> str:
    try:
        return version("mcpbrain")
    except PackageNotFoundError:
        return "0.0.0"


def _latest_version(index_url: str) -> str | None:
    """Newest mcpbrain version on the PEP 503 index, or None if unreachable."""
    try:
        html = _fetch(index_url.rstrip("/") + "/mcpbrain/")
    except Exception:  # noqa: BLE001 — offline / index down: no update
        return None
    versions = _WHEEL_RE.findall(html)
    if not versions:
        return None
    return max(versions, key=_parse)


def _should_update(installed: str, latest: str | None) -> bool:
    return bool(latest) and _parse(latest) > _parse(installed)


def _run(cmd: list) -> tuple[str, int]:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.stdout or "", result.returncode


def _restart_agent() -> None:
    from mcpbrain import agents
    agents.restart_agent(sys.platform)


def update_from_index(index_url: str) -> int:
    """Reinstall mcpbrain from the index via uv, then restart. Returns 0 on success."""
    out, rc = _run([
        "uv", "tool", "install",
        "--index", f"mcpbrain={index_url}",
        "mcpbrain", "--upgrade", "--reinstall-package", "mcpbrain",
    ])
    if rc != 0:
        print("Update failed (uv tool install):\n" + out.strip(), file=sys.stderr)
        return rc
    _restart_agent()
    return 0


def main(argv: list) -> int:
    index_url = _index_url()
    installed = _installed_version()
    latest = _latest_version(index_url)
    if not _should_update(installed, latest):
        print(f"Already up to date (v{installed}).")
        return 0
    print(f"Updating mcpbrain {installed} → {latest} …")
    return update_from_index(index_url)
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_update_index.py -v`

- [ ] **Step 5: Fallout** — `grep -rn "_repo_dir\|repo_dir\|MCPBRAIN_REPO" mcpbrain/ tests/`; remove `repo_dir` persistence from `setup.py` (the `write_config(home, {"repo_dir": ...})` call and the `--repo-dir` arg's persistence) and delete/replace `tests/test_update.py` assertions that exercised the git-pull path. Run `pytest tests/ -q -k update`.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(update): reinstall from the wheel index, not git pull"`

---

## Task 3: Daemon silent auto-update cadence

**Files:**
- Modify: `mcpbrain/daemon.py` (constructor kwarg `auto_update_interval_s`, `maybe_auto_update()`, call in the loop, wire in `apply_config` from config `auto_update_interval_s`)
- Test: `tests/test_daemon_autoupdate.py`

- [ ] **Step 1: Failing test** (mirror the `maybe_backup` tiering/clock pattern)

```python
# tests/test_daemon_autoupdate.py
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock


class _Emb:
    dim = 4
    def embed_passages(self, texts): return [[0.0] * 4 for _ in texts]


def _daemon(tmp_path, **kw):
    s = Store(tmp_path / "b.sqlite3", dim=4, read_only=False); s.init()
    clock = kw.pop("clock", lambda: 0.0)
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path / "d.lock"),
                  clock=clock, **kw)


def test_auto_update_off_by_default(tmp_path):
    d = _daemon(tmp_path)
    assert d.maybe_auto_update() is None  # OFF unless interval set


def test_auto_update_runs_when_due_and_behind(tmp_path, monkeypatch):
    import mcpbrain.update as upd
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    ran = {"n": 0}
    monkeypatch.setattr(upd, "update_from_index", lambda url: ran.__setitem__("n", 1) or 0)
    d = _daemon(tmp_path, auto_update_interval_s=3600.0)
    out = d.maybe_auto_update()  # first call: due (last is None)
    assert ran["n"] == 1 and out is not None and out.get("updated") is True


def test_auto_update_skips_when_current(tmp_path, monkeypatch):
    import mcpbrain.update as upd
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.3.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index", lambda url: (_ for _ in ()).throw(AssertionError("must not update")))
    d = _daemon(tmp_path, auto_update_interval_s=3600.0)
    out = d.maybe_auto_update()
    assert out == {"updated": False}
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_daemon_autoupdate.py -v`

- [ ] **Step 3: Implement** — in `mcpbrain/daemon.py`:

1. Constructor: add `auto_update_interval_s: float | None = None` kwarg; store `self._auto_update_interval_s = auto_update_interval_s`; init `self._last_auto_update = None`.

2. Add the method (mirror `maybe_backup`):

```python
    def maybe_auto_update(self) -> dict | None:
        """Silently reinstall from the wheel index if a newer version is published.

        OFF unless auto_update_interval_s is set. Time-gated via self._clock. On a
        due tick: compare installed vs the index's newest; reinstall + restart only
        when behind. Failures are swallowed (logged) so the loop keeps running."""
        with self._config_lock:
            interval = self._auto_update_interval_s
        if interval is None:
            return None
        if self._last_auto_update is not None and (self._clock() - self._last_auto_update) < interval:
            return None
        self._last_auto_update = self._clock()
        try:
            from mcpbrain import update as upd
            latest = upd._latest_version(upd._index_url())
            if not upd._should_update(upd._installed_version(), latest):
                return {"updated": False}
            upd.update_from_index(upd._index_url())
            return {"updated": True, "version": latest}
        except Exception as exc:  # noqa: BLE001 — auto-update must never crash the loop
            log.warning("auto-update failed (loop continues): %s", exc)
            return {"updated": False, "error": str(exc)}
```

3. Call `self.maybe_auto_update()` in the run loop next to the other `maybe_*` cadence calls (find where `self.maybe_backup()` is invoked and add it adjacent).

4. In `apply_config`, read `auto_update_interval_s` from config (default e.g. `21600` = 6h when the install is configured; `None`/off otherwise) and assign under `self._config_lock`, mirroring the other cadence re-wires.

- [ ] **Step 4: Run → pass.** `pytest tests/test_daemon_autoupdate.py -v`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(daemon): silent auto-update cadence (maybe_auto_update)"`

---

## Task 4: `bin/release.py` — build wheel + refresh the PEP 503 index

**Files:**
- Create: `bin/release.py`
- Test: `tests/test_release_index.py` (the index-generation function is pure/testable; the build step is invoked via subprocess and not unit-tested)

- [ ] **Step 1: Failing test**

```python
# tests/test_release_index.py
from pathlib import Path
from importlib import import_module
release = import_module("bin.release") if False else None  # see note

def test_render_simple_index_lists_wheels(tmp_path):
    import sys; sys.path.insert(0, "bin")
    import release
    wheels = ["mcpbrain-0.2.0-py3-none-any.whl", "mcpbrain-0.3.0-py3-none-any.whl"]
    html = release.render_package_index(wheels)
    assert all(w in html for w in wheels)
    assert "<a href=" in html and "mcpbrain" in html
```

- [ ] **Step 2: Run → fail.** `pytest tests/test_release_index.py -v`

- [ ] **Step 3: Implement** — create `bin/release.py`:

```python
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
```

- [ ] **Step 4: Run → pass.** `pytest tests/test_release_index.py -v`

- [ ] **Step 5: Commit** — `git add bin/release.py tests/test_release_index.py && git commit -m "feat(release): build wheel + PEP 503 index generator"`

---

## Task 5: Installers install from the index (no clone)

**Files:**
- Modify: `install/setup.sh`, `install/setup.command`, `install/setup.ps1`
- Test: none (shell); verified by inspection + dry-run

- [ ] **Step 1: Rewrite the install body**

In each installer, replace `uv tool install --from . "mcpbrain[tray]" --force` with an index install, and drop the `cd`-to-repo / `--repo-dir` logic. macOS/Linux (`setup.sh`/`setup.command`):

```bash
INDEX_URL="${MCPBRAIN_INDEX_URL:-https://CHANGE-ME.github.io/mcpbrain-dist/simple/}"
command -v uv >/dev/null 2>&1 || run sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
run uv tool install --index "mcpbrain=$INDEX_URL" mcpbrain --force
BIN="$(command -v mcpbrain || echo "$HOME/.local/bin/mcpbrain")"
run "$BIN" register || true
run "$BIN" daemon --once || true
run "$BIN" setup
```

PowerShell (`setup.ps1`): the analogous `uv tool install --index "mcpbrain=$IndexUrl" mcpbrain --force`, dropping `--repo-dir`. Remove the `Set-Location (repo)` and `$Repo` capture.

- [ ] **Step 2: Dry-run check** — `bash install/setup.sh --dry-run` prints the index install line and no `git`/`--from .`. Confirm `grep -n "from \.\|--repo-dir\|git clone" install/*` returns nothing.

- [ ] **Step 3: Commit** — `git add install/ && git commit -m "feat(install): install from the wheel index, drop clone + repo-dir"`

---

## Final: deployment doc + suite

- [ ] **Step 1:** `pytest -q` green; `ruff check` clean.
- [ ] **Step 2: Document the one-time maintainer deployment** (README + a `docs/DISTRIBUTION.md`): create the public `mcpbrain-dist` repo, enable GitHub Pages on it, set `DEFAULT_INDEX_URL`/`MCPBRAIN_INDEX_URL` and the installers' `CHANGE-ME` to the Pages URL, and host `install.sh`/`install.ps1` (copies of the `install/` scripts) at the Pages root so the one-line bootstrap works. Note: the index is consumed as `--index mcpbrain=<url>` so it is `explicit` (only `mcpbrain` resolves there; deps from PyPI). Commit the doc.

---

## Self-Review

**Spec coverage (Part 2):** versioned wheels (Task 1, 4) on a PEP 503 Pages index (Task 4), install/update via uv from the index with no clone (Tasks 2, 5), silent daemon auto-update (Task 3). Signing/PyPI/native installers stay out of scope.

**Placeholder honesty:** all code is concrete. The Pages **URL** is a deliberate `CHANGE-ME`/env-overridable constant (the maintainer's deployment value, not knowable in-repo) — flagged in `DEFAULT_INDEX_URL`, the installers, and the deployment doc, not hidden. The wheel *build* runs via `uv build` (subprocess), so only the pure index-render function is unit-tested (Task 4).

**Type consistency:** `_index_url()->str`, `_installed_version()->str`, `_latest_version(url)->str|None`, `_should_update(installed,latest)->bool`, `update_from_index(url)->int`, `Daemon.maybe_auto_update()->dict|None` (mirrors `maybe_backup`), `render_package_index(list)->str`. The `--index mcpbrain=<url>` form is consistent across update.py, the installers, and the deployment doc.
