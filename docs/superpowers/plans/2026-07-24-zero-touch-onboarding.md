# Zero-touch onboarding + Windows install fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix every packaging bug from the 2026-07-24 Windows ARM64 install and automate all post-wizard onboarding so a fresh install needs only Google sign-in + name/timezone from the human.

**Architecture:** Point fixes across the daemon (`daemon.py`), control API (`control_api.py`), sync (`sync/`), config (`config.py`), setup CLI (`setup.py`), the wizard HTML/JS, the Windows installer (`install.ps1`), and a small new cross-platform `desktop.py` helper for the Claude Desktop relaunch. No architectural change; each fix is independently testable.

**Tech Stack:** Python 3.12 (x64 under emulation on Windows-ARM64), stdlib `http.server` control API, fastembed/onnxruntime, networkx, PowerShell installer, plain HTML/JS wizard.

## Execution Grouping (6 dispatch units)

Run sequentially (shared worktree; `daemon.py` touched by several) with a review gate between each:

- **G1** — Task 1 + Task 2 + Task 3 (pure source bug fixes)
- **G2** — Task 4 + Task 6 (daemon/sync robustness)
- **G3** — Task 5 + Task 7 (zero-touch enrichment + tray)
- **G4** — Task 8 + Task 9 (Windows install path)
- **G5** — Task 10 + Task 11 (one-tap Claude Desktop connect)
- **G6** — Task 12 + Task 13 (communities fallback + local release prep; must run last — T13 depends on all)

## Global Constraints

- Target version **0.7.108**; bump the **five** version files + `uv.lock` in the release task, keep them equal: `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, `plugin/mcpb/manifest.json`.
- Local reinstall uses `uv tool install --force ".[daemon]"` — the `[daemon]` extra is required or the embedder breaks.
- **Do NOT push or release** (source/dist/plugin) without an explicit instruction — shipping is an all-users action. This plan ends at a local, verified, committed state.
- TDD every fix. Cross-platform bugs get a guard test that fails on the buggy code path even when run on macOS/Linux (via monkeypatch).
- Josh runs the full `pytest tests/` himself; scope test runs here to the edited + directly-impacted files.
- `ruff check` clean before each commit.
- The **Windows HARDWARE QA GATE stays OPEN**: 0.7.108 must be validated on the real ARM64/x64-emulated box before onboarding Windows users.

---

### Task 1: `os.fchmod` guard (config + backup writes)

Unguarded `os.fchmod` raises `AttributeError` on Windows → atomic writes leave an orphaned temp and never write the target. Silently dropped every wizard config save.

**Files:**
- Modify: `mcpbrain/config.py:997` (inside `write_config`)
- Modify: `mcpbrain/backup.py:593` (inside the restore-temp setup)
- Test: `tests/test_config.py` (or `tests/test_config_timezone.py` if that's the config-write home)

**Interfaces:**
- Produces: no signature change; `config.write_config(home, updates)` still returns the merged dict.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_write_config_without_fchmod(tmp_path, monkeypatch):
    """On platforms lacking os.fchmod (Windows), write_config must still persist."""
    import os
    from mcpbrain import config
    monkeypatch.delattr(os, "fchmod", raising=False)
    config.write_config(str(tmp_path), {"owner_full_name": "Nakia Busby"})
    got = config.read_config(str(tmp_path))
    assert got["owner_full_name"] == "Nakia Busby"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_write_config_without_fchmod -v`
Expected: FAIL with `AttributeError: module 'os' has no attribute 'fchmod'`

- [ ] **Step 3: Guard both call sites**

In `mcpbrain/config.py`, replace the line at ~998:

```python
        os.fchmod(fd, 0o600)  # explicit: don't rely on mkstemp's default
```

with:

```python
        if hasattr(os, "fchmod"):  # POSIX-only; mkstemp is already owner-only on Windows
            os.fchmod(fd, 0o600)   # explicit: don't rely on mkstemp's default
```

In `mcpbrain/backup.py`, replace the line at ~593:

```python
    os.fchmod(fd, 0o600)
```

with:

```python
    if hasattr(os, "fchmod"):  # POSIX-only; mkstemp is already owner-only on Windows
        os.fchmod(fd, 0o600)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py::test_write_config_without_fchmod -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/config.py mcpbrain/backup.py tests/test_config.py
git commit -m "fix(win): guard os.fchmod in config + backup atomic writes"
```

---

### Task 2: UTF-8 on served wizard/dashboard/graph HTML

`p.read_text()` with no encoding decodes as cp1252 on Windows → mojibake (the `✅/⚠️` glyphs).

**Files:**
- Modify: `mcpbrain/control_api.py:254` (`_serve_wizard`), `:265` (`_serve_dashboard`), `:285` (`_serve_graph`)
- Test: `tests/test_control_api_post.py` or `tests/test_daemon_control_wiring.py`

**Interfaces:**
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

The three methods differ only by filename; test the shared behavior by asserting the served bytes decode as UTF-8 for a file containing non-ASCII. Add to `tests/test_control_api_post.py`:

```python
def test_serve_wizard_reads_utf8(tmp_path, monkeypatch):
    """Wizard HTML with non-ASCII must be read as UTF-8, not the platform default."""
    import mcpbrain.control_api as ca
    # Simulate a cp1252 default decoder to prove encoding= is explicit.
    real_read_text = ca.Path.read_text
    calls = {}
    def spy(self, *a, **k):
        calls["encoding"] = k.get("encoding")
        return real_read_text(self, *a, **k)
    monkeypatch.setattr(ca.Path, "read_text", spy)
    # Minimal fake handler capturing written bytes
    class H:
        def __init__(self): self.buf = b""
        def send_response(self, *_): pass
        def send_header(self, *_): pass
        def end_headers(self): pass
        class _W:
            def __init__(self, o): self.o = o
            def write(self, b): self.o.buf += b
        @property
        def wfile(self): return H._W(self)
    srv = ca.ControlServer.__new__(ca.ControlServer)
    srv.token = "T"
    srv._serve_wizard(H())
    assert calls["encoding"] == "utf-8"
```

(If `ControlServer.__new__` needs more attributes, set only `token`; `_serve_wizard` uses just `self.token`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_api_post.py::test_serve_wizard_reads_utf8 -v`
Expected: FAIL (`calls["encoding"]` is `None`)

- [ ] **Step 3: Add `encoding="utf-8"` to all three `read_text()` calls**

In `mcpbrain/control_api.py`, change each of the three (lines 254, 265, 285):

```python
        html = p.read_text().replace("__MCPBRAIN_TOKEN__", self.token).encode()
```

to:

```python
        html = p.read_text(encoding="utf-8").replace("__MCPBRAIN_TOKEN__", self.token).encode()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_control_api_post.py::test_serve_wizard_reads_utf8 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/control_api.py tests/test_control_api_post.py
git commit -m "fix(win): read served HTML as UTF-8 (dashboard mojibake)"
```

---

### Task 3: Remove invalid `corpora` from the Drive Changes API call

`changes().list()` rejects `corpora` → `TypeError` → every shared drive skipped.

**Files:**
- Modify: `mcpbrain/sync/drive.py:390` (and the docstring mention at ~351)
- Test: `tests/test_sync_cycle.py` or a new `tests/test_drive_changes.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change.

- [ ] **Step 1: Write the failing test**

Add a test that drives the changes loop with a fake service asserting `corpora` is NOT among the kwargs. Locate the function containing line 390 (the shared-drive changes fetch) and call it with a fake `service`:

```python
def test_changes_list_omits_corpora():
    """changes().list() must not be passed corpora (API rejects it)."""
    from mcpbrain.sync import drive
    seen = {}
    class _Changes:
        def list(self, **kw):
            seen.update(kw)
            class _Ex:
                def execute(self_):
                    return {"changes": [], "newStartPageToken": "tok"}
            return _Ex()
    class _Svc:
        def changes(self): return _Changes()
    # Call the changes-fetch helper directly (name it per the function at drive.py:386).
    drive._collect_change_events(_Svc(), drive_id="D", page_token="p")  # adjust name if different
    assert "corpora" not in seen
    assert seen.get("driveId") == "D"
```

(Confirm the enclosing function/helper name around `drive.py:386-395`; if the loop is inline in a larger function, extract the smallest callable or test via the public sync entry with a fake service. Use the actual name.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_drive_changes.py -v`
Expected: FAIL (`corpora` present in `seen`)

- [ ] **Step 3: Delete the `corpora` line**

In `mcpbrain/sync/drive.py`, remove line 390:

```python
            corpora="drive",
```

from the `service.changes().list(...)` call (keep `driveId`, `includeItemsFromAllDrives`, `supportsAllDrives`, `includeRemoved`, `fields`). Update the docstring at ~351 that documents `corpora='drive'` on `changes.list` to note it's `files.list`-only.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_drive_changes.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/drive.py tests/test_drive_changes.py
git commit -m "fix(drive): drop invalid corpora kwarg from changes().list (shared drives skipped)"
```

---

### Task 4: Make My-Drive sync non-fatal to the cycle

A Drive/TLS blip in `sync_drive` (My-Drive) aborts the whole cycle before the heartbeat is written. The shared-drive block and bootstrap are already wrapped; this closes the last gap.

**Files:**
- Modify: `mcpbrain/sync/__init__.py:64-66`
- Test: `tests/test_sync_cycle.py`

**Interfaces:**
- Consumes: `sync_drive(drive_service, store)` (may raise).
- Produces: `run_sync_cycle(...)` still returns the result dict; `result["drive"]` is 0 on a Drive failure.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_sync_cycle.py`:

```python
def test_drive_failure_does_not_abort_cycle(monkeypatch, tmp_store, fake_embedder):
    """A raising My-Drive sync must be logged and skipped; the cycle completes."""
    from mcpbrain import sync
    monkeypatch.setattr(sync, "sync_gmail", lambda *a, **k: 0, raising=False)
    monkeypatch.setattr(sync, "sync_calendar", lambda *a, **k: 0, raising=False)
    monkeypatch.setattr(sync, "index_pending", lambda *a, **k: 0, raising=False)
    def boom(*a, **k):
        raise RuntimeError("[SSL] record layer failure")
    monkeypatch.setattr("mcpbrain.sync.drive.sync_drive", boom)
    result = sync.run_sync_cycle(tmp_store, fake_embedder,
                                 gmail_service=object(), calendar_service=object(),
                                 drive_service=object(), home=None)
    assert result["drive"] == 0  # skipped, not crashed
```

(Reuse whatever store/embedder fixtures `tests/test_sync_cycle.py` already defines; match their names.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync_cycle.py::test_drive_failure_does_not_abort_cycle -v`
Expected: FAIL (RuntimeError propagates)

- [ ] **Step 3: Wrap the My-Drive sync**

In `mcpbrain/sync/__init__.py`, replace lines 64-66:

```python
    if drive_service is not None:
        result["drive"] = sync_drive(drive_service, store)
        result["embedded"] += index_pending(store, embedder, home=home)
```

with:

```python
    if drive_service is not None:
        try:
            result["drive"] = sync_drive(drive_service, store)
            result["embedded"] += index_pending(store, embedder, home=home)
        except Exception as exc:  # noqa: BLE001 — a Drive/TLS blip must not abort the cycle
            log.warning("sync: My-Drive sync failed (cycle continues, retries next cycle): %s", exc)
```

(`log` is already module-level in this file — used at line ~110.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync_cycle.py::test_drive_failure_does_not_abort_cycle -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/sync/__init__.py tests/test_sync_cycle.py
git commit -m "fix(sync): My-Drive failure is non-fatal to the cycle (heartbeat no longer stalls)"
```

---

### Task 5: Auto-enable `enrich_mode=spool` on first configured save

The wizard leaves enrichment off. The first time an install becomes configured (identity + ≥1 org), turn on spool so the daemon starts spooling the un-enriched backlog. Honor an explicit user choice.

**Files:**
- Modify: `mcpbrain/daemon.py:967` (`apply_config`)
- Test: `tests/test_daemon_control_wiring.py` or `tests/test_daemon.py`

**Interfaces:**
- Consumes: `config.is_configured(home)`, `config.enrich_mode(home)`, `config.write_config(home, updates)`.
- Produces: after `apply_config(body)`, `self._enrich_mode == "spool"` when configured and the caller didn't set `enrich_mode`.

- [ ] **Step 1: Write the failing test**

```python
def test_apply_config_auto_enables_spool_when_configured(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod, config
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._config_lock = __import__("threading").Lock()
    # identity + org make is_configured() true
    d.apply_config({"owner_full_name": "Nakia", "owner_email": "n@centrepoint.church",
                    "orgs": [{"name": "Centrepoint Church", "domain": "centrepoint.church"}]})
    assert config.enrich_mode(str(tmp_path)) == "spool"
    assert d._enrich_mode == "spool"

def test_apply_config_honors_explicit_off(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod, config
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._config_lock = __import__("threading").Lock()
    d.apply_config({"owner_full_name": "Nakia", "owner_email": "n@centrepoint.church",
                    "orgs": [{"name": "Centrepoint Church", "domain": "centrepoint.church"}],
                    "enrich_mode": "off"})
    assert config.enrich_mode(str(tmp_path)) == "off"
```

(`apply_config` sets many `self._*` cadence fields under the lock. If `Daemon.__new__` leaves those unset, the assignments still succeed since they're plain attribute writes. If `_cadences_from_config`/`_backup_from_config` need config keys, they read from the freshly written config — fine. Adjust the required identity keys to whatever `config.is_configured` actually checks — verify `is_configured` before writing the test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_daemon_control_wiring.py -k auto_enables_spool -v`
Expected: FAIL (`enrich_mode` stays `"off"`)

- [ ] **Step 3: Add the auto-enable in `apply_config`**

In `mcpbrain/daemon.py`, immediately after `config.write_config(home, body)` (line ~975) and before `enrich_mode = config.enrich_mode(home)`:

```python
        # Zero-touch enrichment: the first time an install becomes configured
        # (identity + >=1 org saved), turn enrichment on so the daemon starts
        # spooling the un-enriched backlog. Only auto-flip when the caller didn't
        # set enrich_mode itself (an explicit "off" is honored) and it's still the
        # "off" default — so a later save won't re-flip a deliberate choice.
        if ("enrich_mode" not in body
                and config.is_configured(home)
                and config.enrich_mode(home) == "off"):
            config.write_config(home, {"enrich_mode": "spool"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_daemon_control_wiring.py -k "auto_enables_spool or honors_explicit_off" -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/daemon.py tests/test_daemon_control_wiring.py
git commit -m "feat(onboarding): auto-enable enrich_mode=spool on first configured save"
```

---

### Task 6: Wizard model-step self-updates (no stale "download" prompt)

Two defects: (a) an automatic embedder warm doesn't set `_model_downloading`, so the wizard is blind to it; (b) `refreshModel()` never re-polls from the idle state. Fix both so the step reaches "Ready" without a reload.

**Files:**
- Modify: `mcpbrain/daemon.py:437-464` (`model_status` / warm path)
- Modify: `mcpbrain/wizard/index.html:318-334` (`refreshModel`)
- Test: `tests/test_control_api_model.py`

**Interfaces:**
- Produces: `daemon.model_status()` returns `{"cached": bool, "downloading": bool, "error": str|None}` where `downloading` is true during ANY embedder build, not just the button path.

- [ ] **Step 1: Write the failing test (backend)**

```python
def test_model_status_reports_building_during_any_warm(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._model_downloading = False
    d._model_error = None
    d._embedder_obj = None
    d._model_building = True   # NEW flag set around any build
    monkeypatch.setattr("mcpbrain.embed.model_weights_cached", lambda: False)
    st = d.model_status()
    assert st["downloading"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_control_api_model.py::test_model_status_reports_building_during_any_warm -v`
Expected: FAIL (`downloading` is False — `model_status` only reads `_model_downloading`)

- [ ] **Step 3: Report building in `model_status`, set the flag on lazy build**

In `mcpbrain/daemon.py`, change `model_status` (~440) to also reflect a build in progress:

```python
    def model_status(self) -> dict:
        """Search-model state for the wizard: cached on disk / downloading / last error."""
        from mcpbrain.embed import model_weights_cached
        building = bool(getattr(self, "_model_downloading", False)
                        or getattr(self, "_model_building", False))
        return {
            "cached": bool(model_weights_cached()),
            "downloading": building,
            "error": getattr(self, "_model_error", None),
        }
```

Then find the lazy embedder build path (the `_embedder` property / `_embedder_factory` use around line 486-540 — where `embed_query`/`embed_passages` first triggers a fastembed download outside `ensure_model`) and set `self._model_building = True` before the build and `False` in a `finally`. Initialize `self._model_building = False` in `__init__` next to `self._model_downloading = False` (line ~488). Minimal shape at the build site:

```python
        self._model_building = True
        try:
            # ... existing embedder construction / first embed ...
        finally:
            self._model_building = False
```

(If the lazy build is a `@property` returning `_embedder_obj`, wrap the construction inside it. Verify the exact build site during implementation and wrap the narrowest span that performs the download.)

- [ ] **Step 4: Run backend test to verify it passes**

Run: `pytest tests/test_control_api_model.py::test_model_status_reports_building_during_any_warm -v`
Expected: PASS

- [ ] **Step 5: Fix `refreshModel()` to poll from idle**

In `mcpbrain/wizard/index.html`, change `refreshModel` (318-334) so the idle/"not downloaded" branch keeps polling until cached:

```javascript
async function refreshModel(){
  try{
    const j = await (await fetch("/api/model/status", H)).json();
    const btn = $("model-btn");
    if(j.cached){
      badge("model-state", "Ready", "ok"); btn.disabled = true;
    }else if(j.downloading){
      badge("model-state", "Downloading…", "wait"); btn.disabled = true;
      setTimeout(refreshModel, 2000);
    }else if(j.error){
      badge("model-state", "Failed: " + j.error, "wait"); btn.disabled = false;
      setTimeout(refreshModel, 4000);
    }else{
      badge("model-state", "Not downloaded", "idle"); btn.disabled = false;
      setTimeout(refreshModel, 4000);
    }
  }catch(e){ setTimeout(refreshModel, 4000); }
}
```

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/daemon.py mcpbrain/wizard/index.html tests/test_control_api_model.py
git commit -m "fix(wizard): model step self-updates to Ready; report building on any warm"
```

---

### Task 7: Tray — auto-start after setup + tooltip length audit

Start the tray immediately (not deferred to next login). Audit the tooltip: the repo uses a static `title="mcpbrain"` (8 chars), so the 127-char overflow is not applicable in 0.7.107 — add a guard test to keep it that way.

**Files:**
- Modify: `mcpbrain/setup.py` (add `_start_tray_now`, call it in `main`)
- Test: `tests/test_setup.py` (create if absent) and `tests/test_tray.py` (guard test)

**Interfaces:**
- Consumes: `_mcpbrain_bin()` (existing in setup.py).
- Produces: `setup._start_tray_now(home)` → None, best-effort.

- [ ] **Step 1: Write the failing tests**

`tests/test_setup.py`:

```python
def test_start_tray_now_spawns_tray(monkeypatch):
    from mcpbrain import setup
    calls = {}
    monkeypatch.setattr(setup, "_mcpbrain_bin", lambda: "/x/mcpbrain")
    def fake_popen(args, **k):
        calls["args"] = args
        class P: pass
        return P()
    monkeypatch.setattr(setup.subprocess, "Popen", fake_popen, raising=False)
    setup._start_tray_now("/home")
    assert calls["args"][:2] == ["/x/mcpbrain", "tray"]
```

`tests/test_tray.py` (guard):

```python
def test_tray_title_within_windows_tooltip_limit():
    import re, pathlib
    src = pathlib.Path("mcpbrain/tray.py").read_text(encoding="utf-8")
    m = re.search(r'title\s*=\s*"([^"]*)"', src)
    assert m and len(m.group(1)) <= 127
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_setup.py::test_start_tray_now_spawns_tray -v`
Expected: FAIL (`_start_tray_now` doesn't exist)
Run: `pytest tests/test_tray.py::test_tray_title_within_windows_tooltip_limit -v`
Expected: PASS already (documents the invariant; keep it)

- [ ] **Step 3: Add `_start_tray_now` and call it**

In `mcpbrain/setup.py`, add `import subprocess` at the top if not present, then:

```python
def _start_tray_now(home: str) -> None:
    """Launch the tray immediately so it appears without waiting for next login.
    Best-effort — the login agent still starts it at next login regardless."""
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen([_mcpbrain_bin(), "tray"], **kw)
        print("Menu-bar tray started.")
    except Exception as exc:  # noqa: BLE001 — optional; never block onboarding
        print(f"Could not start the tray now ({exc}); it starts at next login.", file=sys.stderr)
```

In `main`, right after `_install_tray_best_effort(home)`:

```python
    _start_tray_now(home)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_setup.py::test_start_tray_now_spawns_tray tests/test_tray.py::test_tray_title_within_windows_tooltip_limit -v`
Expected: PASS (both)

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/setup.py tests/test_setup.py tests/test_tray.py
git commit -m "feat(setup): start tray immediately; guard tray tooltip length"
```

---

### Task 8: Embedder DLL self-heal — broaden search dirs + run at daemon startup

`_SEARCH_DIRS()` only checks System32/WinSxS; on ARM64 the x64 `MSVCP140_1.dll` was absent there (redist version-skip) but present in Office ClickToRun. Also the daemon only calls `add_search_dir` at startup, never `ensure_vcruntime_dlls`, so it doesn't self-heal without a `doctor` run.

**Files:**
- Modify: `mcpbrain/vcruntime.py` (`_SEARCH_DIRS`)
- Modify: `mcpbrain/daemon.py:2511-2512` (startup vcruntime call)
- Test: `tests/test_vcruntime.py` (create if absent)

**Interfaces:**
- Produces: `vcruntime._SEARCH_DIRS()` returns a list including ClickToRun + VC++ install dirs.

- [ ] **Step 1: Write the failing test**

```python
def test_search_dirs_include_clicktorun(monkeypatch):
    from mcpbrain import vcruntime
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    dirs = [str(d) for d in vcruntime._SEARCH_DIRS()]
    assert any("ClickToRun" in d for d in dirs)
    assert any("System32" in d for d in dirs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_vcruntime.py::test_search_dirs_include_clicktorun -v`
Expected: FAIL (no ClickToRun dir)

- [ ] **Step 3: Broaden `_SEARCH_DIRS` + daemon startup call**

In `mcpbrain/vcruntime.py`, replace `_SEARCH_DIRS`:

```python
def _SEARCH_DIRS():  # pragma: no cover — real system dirs, monkeypatched in tests
    root = os.environ.get("SystemRoot", r"C:\Windows")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    # System32/WinSxS first (fast, canonical). On ARM64 the x64 MSVCP140_1.dll
    # can be absent there (redist version-skip); an MS-signed x64 copy commonly
    # ships with Office ClickToRun and the VC++ / Visual Studio install dirs.
    return [
        Path(root) / "System32",
        Path(root) / "WinSxS",
        Path(pf) / "Common Files" / "microsoft shared" / "ClickToRun",
        Path(pf) / "Microsoft Visual Studio",
        Path(pfx86) / "Microsoft Visual Studio",
    ]
```

(Remove the `# pragma: no cover` only if the test now exercises it — keep it; the test monkeypatches env and reads the returned paths without walking them.)

In `mcpbrain/daemon.py`, change the startup block at 2511-2512:

```python
    from mcpbrain import vcruntime
    if sys.platform == "win32":
        vcruntime.ensure_vcruntime_dlls(str(config.app_dir()))
    vcruntime.add_search_dir(str(config.app_dir()))
```

(Confirm `sys` is imported in `daemon.py`; it is used elsewhere.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_vcruntime.py::test_search_dirs_include_clicktorun -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/vcruntime.py mcpbrain/daemon.py tests/test_vcruntime.py
git commit -m "fix(win): broaden vcruntime DLL search (ClickToRun) + self-heal at daemon startup"
```

---

### Task 9: `install.ps1` uv "minor version link" fallback

On ARM64 `uv tool install` can fail with "Missing expected target directory for Python minor version link" though the x64 interpreter is fully extracted. Resolve the concrete `python.exe` and install against it.

**Files:**
- Modify: `plugin/scripts/install.ps1` (`Install-Mcpbrain`)
- Test: `plugin/scripts/install.tests.ps1`

**Interfaces:**
- Produces: `Install-Mcpbrain` retries against a resolved `python.exe` on link failure.

- [ ] **Step 1: Add a Pester test**

In `plugin/scripts/install.tests.ps1`, add a case that dot-sources the script (`$DotSourceOnly = $true`) and asserts the fallback resolves a python.exe when the two `uv tool install` forms fail. Mock `uv` to fail the first two calls and succeed on `--python <path>`; assert `uv python find` (or the glob) is consulted. (Match the file's existing Pester structure and mocking style.)

```powershell
It "falls back to a resolved python.exe on the uv link failure" {
  Mock uv { $global:LASTEXITCODE = 1 } -ParameterFilter { $args -contains 'tool' -and $args -notcontains '--python' -or $args -contains '3.12' }
  Mock uv { "C:\uv\python\cpython-3.12.13-windows-x86_64\python.exe" } -ParameterFilter { $args -contains 'find' }
  Mock uv { $global:LASTEXITCODE = 0 } -ParameterFilter { $args -contains '--python' -and $args -match 'python.exe' }
  Install-Mcpbrain
  Assert-MockCalled uv -ParameterFilter { $args -contains 'find' } -Times 1
}
```

(Adjust mock filters to the harness; the intent is: prove the resolved-exe path is taken.)

- [ ] **Step 2: Run to verify it fails**

Run: `pwsh -File plugin/scripts/install.tests.ps1` (on a box with pwsh; otherwise this task's verification is deferred to the Windows QA gate — note it)
Expected: FAIL (no fallback branch)

- [ ] **Step 3: Add the fallback branch**

In `plugin/scripts/install.ps1`, replace `Install-Mcpbrain`:

```powershell
function Install-Mcpbrain {
  # uv provisions the x64 CPython (its default on ARM64; pinned here for future-proofing).
  $ok = $false
  try { uv tool install --python $PY_REQUEST --index $INDEX "mcpbrain[daemon]" --force; $ok = ($LASTEXITCODE -eq 0) } catch {}
  if (-not $ok) { try { uv tool install --python 3.12 --index $INDEX "mcpbrain[daemon]" --force; $ok = ($LASTEXITCODE -eq 0) } catch {} }
  if (-not $ok) {
    # uv can fail to finalize the minor-version link on ARM64 even though the x64
    # interpreter is fully extracted. Install the interpreter, resolve its concrete
    # python.exe, and install directly against it.
    uv python install $PY_REQUEST
    $py = $null
    try { $py = (uv python find $PY_REQUEST 2>$null) } catch {}
    if (-not $py) {
      $base = (uv python dir).Trim()
      $py = Get-ChildItem "$base\cpython-3.12*x86_64*\python.exe" -ErrorAction SilentlyContinue |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if ($py) { uv tool install --python "$py" --index $INDEX "mcpbrain[daemon]" --force }
    else { throw "Could not resolve an x64 python.exe for the uv-link fallback" }
  }
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `pwsh -File plugin/scripts/install.tests.ps1`
Expected: PASS (or defer to Windows QA if no pwsh locally — record which)

- [ ] **Step 5: Commit**

```bash
git add plugin/scripts/install.ps1 plugin/scripts/install.tests.ps1
git commit -m "fix(install): resolve concrete x64 python.exe on uv minor-version-link failure"
```

---

### Task 10: `desktop.py` relaunch helper + `/api/connect-desktop` endpoint

Backend for the one-tap connect: re-write the connector, then quit + relaunch Claude Desktop cross-platform.

**Files:**
- Create: `mcpbrain/desktop.py`
- Modify: `mcpbrain/control_api.py` (POST block, ~366)
- Test: `tests/test_desktop.py`, `tests/test_control_api_post.py`

**Interfaces:**
- Produces: `desktop.relaunch_claude_desktop() -> dict` → `{"relaunched": bool, "detail": str}`, never raises.
- Produces: `POST /api/connect-desktop` → `{"relaunched": bool, "detail": str}` (200).

- [ ] **Step 1: Write the failing test**

`tests/test_desktop.py`:

```python
def test_relaunch_windows(monkeypatch):
    import mcpbrain.desktop as desktop
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_claude_exe", lambda: r"C:\x\Claude.exe")
    ran = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: ran.append(("run", a[0])))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: ran.append(("popen", a[0])))
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)
    res = desktop.relaunch_claude_desktop()
    assert res["relaunched"] is True
    assert any(kind == "popen" for kind, _ in ran)

def test_relaunch_unresolved_exe_is_graceful(monkeypatch):
    import mcpbrain.desktop as desktop
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_claude_exe", lambda: None)
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)
    res = desktop.relaunch_claude_desktop()
    assert res["relaunched"] is False
    assert "manually" in res["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_desktop.py -v`
Expected: FAIL (no module `mcpbrain.desktop`)

- [ ] **Step 3: Create `mcpbrain/desktop.py`**

```python
"""Quit + relaunch Claude Desktop so it reloads its MCP config (the brain_*
connector setup wrote). Claude Desktop only reads mcpServers at launch and
overwrites the config while running, so a reload is the only way to connect."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _windows_claude_exe() -> str | None:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base) / "Programs" / "claude" / "Claude.exe"
        if p.is_file():
            return str(p)
    return shutil.which("Claude")


def relaunch_claude_desktop() -> dict:
    """Best-effort quit + relaunch of Claude Desktop. Never raises.
    Returns {"relaunched": bool, "detail": str}."""
    manual = "restart Claude Desktop manually to load the brain"
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "Claude.exe", "/F"],
                           capture_output=True, check=False)
            exe = _windows_claude_exe()
            if not exe:
                return {"relaunched": False, "detail": f"Claude.exe not found; {manual}"}
            time.sleep(1.0)
            subprocess.Popen([exe])
            return {"relaunched": True, "detail": "Claude Desktop is restarting"}
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'quit app "Claude"'],
                           capture_output=True, check=False)
            time.sleep(1.0)
            subprocess.run(["open", "-a", "Claude"], capture_output=True, check=False)
            return {"relaunched": True, "detail": "Claude Desktop is restarting"}
        return {"relaunched": False, "detail": f"auto-restart unsupported here; {manual}"}
    except Exception as exc:  # noqa: BLE001 — never propagate to the control API
        return {"relaunched": False, "detail": f"restart failed ({exc}); {manual}"}
```

- [ ] **Step 4: Add the endpoint**

In `mcpbrain/control_api.py` POST block, after the `/api/model/ensure` handler (~331):

```python
            if h.path == "/api/connect-desktop":
                from mcpbrain import setup as _setup, desktop
                _setup._register_desktop_mcp()   # (re)write the connector Desktop may have clobbered
                return h_json(h, 200, desktop.relaunch_claude_desktop())
```

Add an endpoint test to `tests/test_control_api_post.py` that posts to `/api/connect-desktop` with the bearer token and asserts a 200 with a `relaunched` key (monkeypatch `desktop.relaunch_claude_desktop` and `setup._register_desktop_mcp` to no-ops). Follow the file's existing request-helper pattern.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_desktop.py tests/test_control_api_post.py -k "relaunch or connect_desktop" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/desktop.py mcpbrain/control_api.py tests/test_desktop.py tests/test_control_api_post.py
git commit -m "feat(connect): /api/connect-desktop rewrites connector + relaunches Claude Desktop"
```

---

### Task 11: Wizard "Connect Claude Desktop" step + drop the `.mcpb` instruction

Front-end for the one-tap connect, and remove the redundant "download & double-click the `.mcpb`" guidance.

**Files:**
- Modify: `mcpbrain/wizard/index.html` (add a final step + JS)
- Modify: `mcpbrain/setup.py` (the closing print block, ~240-246) — drop `.mcpb` wording, point at the wizard's Connect button
- Test: manual/visual + a string-presence unit check

**Interfaces:**
- Consumes: `POST /api/connect-desktop` (Task 10).

- [ ] **Step 1: Add the wizard step markup**

In `mcpbrain/wizard/index.html`, after the existing model step (`#step-model`, ~142-148), add:

```html
  <section id="step-connect" class="card">
    <h2><span class="num">4</span>Connect Claude Desktop</h2>
    <p class="desc">Do this <strong>last</strong>. It restarts Claude Desktop so it loads your brain — any open Claude Desktop chat will close and reopen.</p>
    <div class="row">
      <button class="primary" id="connect-btn" onclick="connectDesktop()">Connect &amp; restart Claude Desktop</button>
      <span id="connect-state" class="badge idle">Ready when you are</span>
    </div>
  </section>
```

(Renumber any later step numbers if the wizard has steps after model; match the existing `.num` sequence.)

- [ ] **Step 2: Add the JS handler**

Near `ensureModel()` (~336) in `index.html`:

```javascript
async function connectDesktop(){
  const btn = $("connect-btn");
  btn.disabled = true;
  badge("connect-state", "Restarting…", "wait");
  try{
    const r = await P("/api/connect-desktop");
    if(r && r.relaunched){
      badge("connect-state", "Restarting Claude Desktop — reopen it to use your brain", "ok");
    }else{
      badge("connect-state", (r && r.detail) || "Restart Claude Desktop manually", "wait");
      btn.disabled = false;
    }
  }catch(e){
    badge("connect-state", "Restart Claude Desktop manually", "wait");
    btn.disabled = false;
  }
}
```

(Confirm the `P(path)` POST helper exists in this file — it's used by `ensureModel`. Reuse it.)

- [ ] **Step 3: Drop the `.mcpb` wording in setup output**

In `mcpbrain/setup.py`, in `main`'s closing prints (~240-246), remove any "download mcpbrain.mcpb / double-click / install the extension" guidance and replace the connect guidance with:

```python
    print("Finish setup in the wizard (Google sign-in, your details), then click "
          "'Connect & restart Claude Desktop' as the LAST step — that loads the brain_* "
          "tools. Backup and recovery happen automatically.")
```

Search the repo for other `.mcpb`/"double-click"/"install the extension" onboarding copy and remove/redirect it (e.g. any README or docs onboarding section that tells the user to install the extension by hand):

```bash
grep -rniE "double-click|\.mcpb|install the extension" --include="*.py" --include="*.md" mcpbrain/ docs/ plugin/ | grep -iv "marketplace\|manifest"
```

Update user-facing onboarding copy hits; leave build/packaging references (manifest, marketplace) alone.

- [ ] **Step 4: Add a guard test**

`tests/test_setup.py`:

```python
def test_setup_output_has_no_manual_extension_step(capsys, monkeypatch):
    import mcpbrain.setup as setup
    src = __import__("pathlib").Path("mcpbrain/setup.py").read_text(encoding="utf-8")
    assert ".mcpb" not in src
    assert "double-click" not in src.lower()
```

Run: `pytest tests/test_setup.py::test_setup_output_has_no_manual_extension_step -v`
Expected: PASS after Step 3

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/wizard/index.html mcpbrain/setup.py tests/test_setup.py
git commit -m "feat(wizard): one-tap Connect Claude Desktop step; drop manual .mcpb instruction"
```

---

### Task 12: Communities pure-Python fallback (populate under emulation)

Under x64 emulation, igraph/leidenalg won't load, so communities are always empty. Add a networkx greedy-modularity fallback (networkx is already a dependency). Time-box the igraph-wheel investigation; ship the fallback regardless.

**Files:**
- Modify: `mcpbrain/communities.py` (the `except ImportError` branch + a new helper)
- Test: `tests/test_consolidation.py` or a new `tests/test_communities.py`

**Interfaces:**
- Produces: `communities._greedy_modularity_partition(G) -> dict[node, int]`, same contract as the leiden path (`{entity_id: community_id}`).

- [ ] **Step 1: Investigation note (time-boxed, no code)**

Check whether an x64 `igraph`/`leidenalg` wheel loads under Prism emulation on the ARM64 box: `python -c "import igraph, leidenalg"` in the tool venv during Windows QA. If it loads, prefer it (no code change needed — the existing path runs). If it fails (expected), the fallback below is the shipped behavior. Record the finding in the QA notes. Proceed to implement the fallback either way (it only runs when the import fails).

- [ ] **Step 2: Write the failing test**

```python
def test_greedy_modularity_fallback_populates(monkeypatch):
    import networkx as nx
    from mcpbrain import communities
    G = nx.Graph()
    G.add_edge("a", "b", weight=1); G.add_edge("b", "c", weight=1)
    G.add_edge("x", "y", weight=1); G.add_edge("y", "z", weight=1)
    # Force the leiden import to fail so the fallback runs.
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name in ("igraph", "leidenalg"):
            raise ImportError(name)
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    part = communities.detect(G)   # use the real public entry that wraps the import branch
    assert isinstance(part, dict)
    assert "skipped" not in part
    assert len(part) >= 1
```

(Use the actual public function name that contains the `except ImportError` block — the function starting at `communities.py:54`. If it's named `detect`/`run`, match it.)

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_communities.py::test_greedy_modularity_fallback_populates -v`
Expected: FAIL (returns `{"skipped": "leiden unavailable"}`)

- [ ] **Step 4: Implement the fallback**

In `mcpbrain/communities.py`, change the `except ImportError` branch (~72-77):

```python
    except ImportError:
        log.warning("leiden stack unavailable; using networkx greedy-modularity fallback")
        return _greedy_modularity_partition(G)
```

Add the helper (module level):

```python
def _greedy_modularity_partition(G) -> dict:
    """Pure-Python community fallback when igraph/leidenalg can't load (e.g. x64
    emulation on ARM64). Runs networkx greedy modularity on the largest connected
    component — a real community algorithm, NOT raw connected-components — and
    returns the same {node: community_id} contract as the leiden path."""
    from networkx.algorithms.community import greedy_modularity_communities
    largest_cc = max(nx.connected_components(G), key=len)
    sub = G.subgraph(largest_cc)
    try:
        comms = greedy_modularity_communities(sub, weight="weight")
    except Exception as exc:  # noqa: BLE001 — never crash the cadence
        log.warning("greedy-modularity fallback failed: %s", exc)
        return {"skipped": "leiden unavailable"}
    return {node: cid for cid, comm in enumerate(comms) for node in comm}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_communities.py::test_greedy_modularity_fallback_populates -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add mcpbrain/communities.py tests/test_communities.py
git commit -m "feat(communities): networkx greedy-modularity fallback when leiden unavailable"
```

---

### Task 13: Prepare the 0.7.108 release (local only — no push)

Bump versions and run local verification. **Do not push or release** — that's a separate explicit instruction (Windows QA gate is still open).

**Files:**
- Modify: `pyproject.toml`, `mcpbrain/__init__.py`, `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`, `plugin/mcpb/manifest.json`, `uv.lock` (mcpbrain entry)

- [ ] **Step 1: Confirm all five files currently read 0.7.107**

Run:
```bash
grep -RnE "0\.7\.107" pyproject.toml mcpbrain/__init__.py plugin/.claude-plugin/plugin.json plugin/.claude-plugin/marketplace.json plugin/mcpb/manifest.json
```
Expected: one match in each of the five files.

- [ ] **Step 2: Bump all five to 0.7.108 + uv.lock**

Edit each file's version string `0.7.107` → `0.7.108`. Then refresh the lock:
```bash
uv lock
grep -nE "0\.7\.108" pyproject.toml mcpbrain/__init__.py plugin/.claude-plugin/plugin.json plugin/.claude-plugin/marketplace.json plugin/mcpb/manifest.json
```
Expected: 0.7.108 in all five.

- [ ] **Step 3: Reinstall locally with the daemon extra + run doctor**

```bash
uv tool install --force ".[daemon]"
mcpbrain doctor
```
Expected: doctor green on this (macOS) box; embedder cached.

- [ ] **Step 4: Run the impacted test files + ruff**

```bash
ruff check mcpbrain/ tests/
pytest tests/test_config.py tests/test_control_api_post.py tests/test_control_api_model.py \
       tests/test_sync_cycle.py tests/test_daemon_control_wiring.py tests/test_desktop.py \
       tests/test_vcruntime.py tests/test_setup.py tests/test_communities.py tests/test_drive_changes.py -q
```
Expected: all pass, ruff clean. (Josh runs the full suite separately.)

- [ ] **Step 5: Commit the version bump**

```bash
git add pyproject.toml mcpbrain/__init__.py plugin/.claude-plugin/*.json plugin/mcpb/manifest.json uv.lock
git commit -m "chore(release): bump to 0.7.108 — zero-touch onboarding + Windows install fixes"
```

- [ ] **Step 6: Report — do NOT push**

Summarize what's committed locally and that release (source push → dist wheel → plugin sync per `docs/RELEASE-RUNBOOK.md`) and Windows hardware QA are pending explicit go-ahead.

---

## Self-Review

**Spec coverage:**
- A1 fchmod → Task 1 ✓; A2 UTF-8 → Task 2 ✓; A3 corpora → Task 3 ✓; A4 tray tooltip → Task 7 (audit + guard) ✓; A5 embedder DLL → Task 8 ✓
- B6 uv fallback → Task 9 ✓
- C7 connect endpoint → Task 10 ✓; wizard step + drop .mcpb → Task 11 ✓
- D8 spool → Task 5 ✓; D9 removed (no task, by design) ✓; D10 tray auto-start → Task 7 ✓
- E11 Drive non-fatal → Task 4 ✓; E12 model-step → Task 6 ✓
- F13 communities → Task 12 ✓
- Release/version files → Task 13 ✓

**Placeholder scan:** Tasks 3, 6, 9, 12 contain "confirm the exact name/site during implementation" notes where a symbol name must be verified against the live code (the changes-loop helper name in `drive.py`, the lazy-build span in `daemon.py`, the Pester mock shape, the public communities entry). These are verification instructions with the surrounding real code shown, not missing content — acceptable, but the implementer must read the cited lines first.

**Type consistency:** `relaunch_claude_desktop() -> {"relaunched","detail"}` used identically in Task 10 (backend) and Task 11 (JS reads `r.relaunched`/`r.detail`). `model_status() -> {"cached","downloading","error"}` consistent across Task 6 backend and JS. `_greedy_modularity_partition` name consistent within Task 12.

**Known implementer prerequisites (read before coding):**
- Task 3: the enclosing function name at `drive.py:386-395`.
- Task 5: what `config.is_configured` actually requires (identity keys / org shape) — set the test fixture to match.
- Task 6: the exact lazy-embedder build site in `daemon.py` (property vs factory) to wrap the narrowest download span.
- Task 12: the public function name wrapping the `except ImportError` block at `communities.py:54`.
