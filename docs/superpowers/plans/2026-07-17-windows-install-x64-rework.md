# Windows install rework (use-the-platform) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Windows install use the platform instead of fighting it — let uv provision x64 Python, ensure the x64 VC++ runtime, run the same `uv tool install` everywhere — and fix the downstream install bugs a real ARM64 install exposed.

**Architecture:** Slim `install.ps1` (uv → x64 VC++ redist → x64-pinned `uv tool install "mcpbrain[daemon]"` → `mcpbrain setup`), a durable VC++ safety net in the daemon/doctor, an absolute-trampoline autostart shim, and Windows-safe CLI output.

**Tech Stack:** PowerShell + Pester, Python 3.12, uv, Windows-on-ARM x64 emulation (Prism), onnxruntime/sqlite-vec (x64 wheels under emulation).

**Spec:** `docs/superpowers/specs/2026-07-17-windows-install-x64-rework-design.md`

## Global Constraints

- **x64 everywhere on Windows.** uv installs x86_64 CPython by default on ARM64; pin x64 explicitly (`--python cpython-3.12-windows-x86_64`, fall back to `--python 3.12` if uv rejects the qualified form). Never provision ARM64 Python; never install the ARM64 VC++ redist.
- **The one system change is the x64 VC++ Redistributable** (`vc_redist.x64.exe`); installing the ARM64 redist is what poisoned `MSVCP140_1.dll` — do not.
- **DLL scavenging is a fallback only** (doctor, if the embedder still won't load), never the primary path. The durable dir is `app_dir()/vcruntime` (survives reinstalls).
- **Autostart shim runs the absolute `mcpbrain.exe`** — never a bare name, never `pythonw` resolution (drop the Task-6 signed-pythonw approach; signing is out of scope).
- **Org pin unchanged** (bge-small/384); store/sqlite-vec unchanged (x64 wheel runs under emulation).
- **Version → 0.7.97** in all FIVE files + uv.lock at release.
- **Test scope:** edited + directly-impacted files only; full suite + ruff at release (§Task 7).
- **Commit hooks are slow; `git commit --no-verify` is acceptable.**
- **MANDATORY hardware QA before shipping the Windows path** (Task 7) — no exceptions this time.

---

### Task 1: Slim `install.ps1` (use-the-platform) + Pester

**Files:**
- Rewrite: `plugin/scripts/install.ps1`
- Rewrite: `plugin/scripts/install.tests.ps1`

**Interfaces produced:** `Get-InstallPlan([hashtable]$probe) -> [array]`; probe keys `UvOk`, `VcRedistX64Ok`, `SchedulerOk` (no Python/arch keys).

- [ ] **Step 1: Rewrite the Pester tests (RED)**

```powershell
# plugin/scripts/install.tests.ps1
BeforeAll { . "$PSScriptRoot/install.ps1" -DotSourceOnly }

Describe "Get-InstallPlan" {
  It "installs uv + x64 redist when both missing, always installs mcpbrain" {
    $p = Get-InstallPlan @{ UvOk=$false; VcRedistX64Ok=$false; SchedulerOk=$true }
    $p | Should -Contain 'install-uv'
    $p | Should -Contain 'install-vcredist-x64'
    $p | Should -Contain 'install-mcpbrain'
    $p | Should -Contain 'persistence-schtasks'
  }
  It "is near-noop when uv + redist already present (still installs mcpbrain --force)" {
    $p = Get-InstallPlan @{ UvOk=$true; VcRedistX64Ok=$true; SchedulerOk=$true }
    $p | Should -Not -Contain 'install-uv'
    $p | Should -Not -Contain 'install-vcredist-x64'
    $p | Should -Contain 'install-mcpbrain'
  }
  It "never plans an ARM64 redist" {
    $p = Get-InstallPlan @{ UvOk=$true; VcRedistX64Ok=$false; SchedulerOk=$true }
    ($p -join ' ') | Should -Not -Match 'arm64'
    $p | Should -Contain 'install-vcredist-x64'
  }
  It "chooses the startup mechanism when the scheduler is blocked" {
    $p = Get-InstallPlan @{ UvOk=$true; VcRedistX64Ok=$true; SchedulerOk=$false }
    $p | Should -Contain 'persistence-startup'
    $p | Should -Not -Contain 'persistence-schtasks'
  }
}
```

Run: `pwsh -NoProfile -Command "Invoke-Pester plugin/scripts/install.tests.ps1"` → FAIL (rewritten `Get-InstallPlan` not present yet).

- [ ] **Step 2: Rewrite `install.ps1`**

```powershell
# plugin/scripts/install.ps1
param([switch]$DotSourceOnly)

$INDEX = "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/"
# Force an x64 CPython so uv pulls the x64 wheels (all deps ship x64; several ship
# NO win_arm64). x64 runs natively on x64 and under Prism emulation on ARM64.
$PY_REQUEST = "cpython-3.12-windows-x86_64"

function Get-OsArch { [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }

function Test-VcRedistX64 {
  # x64 VC++ runtime present? (never checks/installs the arm64 redist — installing
  # arm64 first poisons the x64 MSVCP140_1.dll via the installer's version-skip.)
  try {
    return ((Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction Stop).Installed -eq 1)
  } catch { return $false }
}

function Test-Scheduler {
  try {
    schtasks /create /tn "mcpbrain-probe" /sc onlogon /tr "cmd /c exit" /f 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    schtasks /delete /tn "mcpbrain-probe" /f 2>&1 | Out-Null
    return $true
  } catch { return $false }
}

function Probe-Machine {
  return @{
    OsArch         = (Get-OsArch)                                   # informational
    UvOk           = [bool](Get-Command uv -ErrorAction SilentlyContinue)
    VcRedistX64Ok  = (Test-VcRedistX64)
    SchedulerOk    = (Test-Scheduler)
  }
}

function Get-InstallPlan {
  # PURE: probe hashtable -> ordered action list. No side effects, no arch branching.
  param([hashtable]$probe)
  $plan = @()
  if (-not $probe.UvOk)          { $plan += "install-uv" }
  if (-not $probe.VcRedistX64Ok) { $plan += "install-vcredist-x64" }
  $plan += "install-mcpbrain"                       # always, with --force
  $plan += if ($probe.SchedulerOk) { "persistence-schtasks" } else { "persistence-startup" }
  return $plan
}

function Invoke-InstallPlan {
  param([array]$plan)
  foreach ($action in $plan) {
    switch ($action) {
      "install-uv"            { Install-Uv }
      "install-vcredist-x64"  { Install-VcRedistX64 }
      "install-mcpbrain"      { Install-Mcpbrain }
      default { }   # persistence-* handled by `mcpbrain setup` (agents.py mechanism probe)
    }
  }
}

function Install-Uv {
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
  $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

function Install-VcRedistX64 {
  $f = "$env:TEMP\vc_redist.x64.exe"
  Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $f
  Start-Process $f -ArgumentList '/install','/quiet','/norestart' -Wait
}

function Install-Mcpbrain {
  # uv provisions the x64 CPython (its default on ARM64; pinned here for future-proofing).
  # If uv rejects the qualified request, fall back to the bare version (x64 on ARM64 today).
  $ok = $false
  try { uv tool install --python $PY_REQUEST --index $INDEX "mcpbrain[daemon]" --force; $ok = ($LASTEXITCODE -eq 0) } catch {}
  if (-not $ok) { uv tool install --python 3.12 --index $INDEX "mcpbrain[daemon]" --force }
}

if (-not $DotSourceOnly) {
  $probe = Probe-Machine
  Write-Host "Machine review: $($probe | Out-String)"
  $plan = Get-InstallPlan $probe
  Write-Host "Plan: $($plan -join ', ')"
  Invoke-InstallPlan -plan $plan
  mcpbrain setup
}
```

- [ ] **Step 3: Run Pester (GREEN)**

Run: `pwsh -NoProfile -Command "Invoke-Pester plugin/scripts/install.tests.ps1 -Output Detailed"` → 4 passed. (Side-effecting installers validated in Task 7 hardware QA.)

- [ ] **Step 4: Commit**

```bash
git add plugin/scripts/install.ps1 plugin/scripts/install.tests.ps1
git commit --no-verify -m "feat(install): slim x64-everywhere installer (uv x64 python + x64 VC++ redist)"
```

---

### Task 2: `agents.py` — absolute-trampoline shim; tray Startup fallback

**Files:**
- Modify: `mcpbrain/agents.py`
- Test: `tests/test_agents_windows_xplat.py`, `tests/test_agents_windows_mechanism.py`, `tests/test_schtasks_home_embed.py`

**Interfaces:** `_win_shim_content(*, mcpbrain_bin, home, subcommand)` (drop `python_bin`); `_win_pythonw_for` deleted; `install_tray_agent(win32)` picks mechanism via `win_persistence_mechanism()`.

- [ ] **Step 1: Update the shim tests (RED)**

In `tests/test_agents_windows_xplat.py` (and `test_schtasks_home_embed.py`), revert the shim assertions to the absolute-`mcpbrain.exe` form and drop `python_bin`:

```python
def test_win_shim_content_runs_subcommand_hidden_and_sets_home():
    vbs = agents._win_shim_content(
        mcpbrain_bin=r"C:\Users\j\.local\bin\mcpbrain.exe",
        home=r"C:\Users\j\AppData\Roaming\mcpbrain", subcommand="daemon")
    assert '""C:\\Users\\j\\.local\\bin\\mcpbrain.exe"" daemon' in vbs
    assert "-m mcpbrain" not in vbs          # no pythonw module form
    assert "sh.Run" in vbs and ", 0, False" in vbs   # hidden window
```

Add a tray-fallback test in `tests/test_agents_windows_mechanism.py`:

```python
def test_tray_uses_startup_when_scheduler_blocked(monkeypatch):
    calls = {}
    monkeypatch.setattr(agents, "win_persistence_mechanism", lambda probe=None: "startup")
    monkeypatch.setattr(agents, "_install_startup_shortcut",
                        lambda task, **kw: calls.setdefault("startup", []).append(task))
    monkeypatch.setattr(agents, "_win_shim_path", lambda home, task: __import__("pathlib").Path(home) / f"{task}.vbs")
    monkeypatch.setattr("pathlib.Path.write_text", lambda self, *a, **k: None)
    monkeypatch.setattr("pathlib.Path.mkdir", lambda self, *a, **k: None)
    agents._install_schtasks_tray(mcpbrain_bin=r"C:\bin\mcpbrain.exe", home=r"C:\home")
    assert "mcpbrain-tray" in calls.get("startup", [])
```

Run the three files → the shim/`python_bin` tests FAIL.

- [ ] **Step 2: Revert the shim + delete `_win_pythonw_for`**

```python
def _win_shim_content(*, mcpbrain_bin: str, home: str, subcommand: str) -> str:
    """A .vbs that runs `mcpbrain <subcommand>` with a hidden console via the
    ABSOLUTE installed launcher path (resolved by setup). Window style 0 hides the
    console; VBScript escapes a double-quote by doubling it."""
    bin_q = '""' + mcpbrain_bin + '""'
    home_esc = home.replace('"', '""')
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Environment("PROCESS")("MCPBRAIN_HOME") = "{home_esc}"\r\n'
        f'sh.Run "{bin_q} {subcommand}", 0, False\r\n'
    )
```

Delete `_win_pythonw_for` entirely. Remove the `python_bin=` argument from **every**
`_win_shim_content(...)` call site (`_install_schtasks`, `_install_schtasks_tray`,
`_install_cadences_schtasks`) — grep `_win_shim_content(` and `_win_pythonw_for` to
confirm none remain.

- [ ] **Step 3: Give the tray the mechanism selection**

In `_install_schtasks_tray`, mirror `_install_schtasks`: write the shim, then
`if win_persistence_mechanism() == "schtasks": subprocess.run(schtasks_tray_args(...), check=True)`
`else: _install_startup_shortcut(_TRAY_TASK_NAME, python_bin=None, shim_path=shim_path)`.
(`_install_startup_shortcut`/`startup_shortcut_target` already exist from the prior
feature — they only need the shim path; drop their `python_bin` use since the shim
now targets `mcpbrain.exe` directly. Update `startup_shortcut_target` signature to
`(*, shim_path)` and its one caller.)

- [ ] **Step 4: Run tests (GREEN) + commit**

Run: `pytest tests/test_agents_windows_xplat.py tests/test_agents_windows_mechanism.py tests/test_schtasks_home_embed.py -v` → all pass.

```bash
git add mcpbrain/agents.py tests/test_agents_windows_xplat.py tests/test_agents_windows_mechanism.py tests/test_schtasks_home_embed.py
git commit --no-verify -m "fix(agents): autostart shim runs absolute mcpbrain.exe; tray gets Startup fallback"
```

---

### Task 3: Windows-safe CLI output (UTF-8)

**Files:**
- Modify: `mcpbrain/cli.py`
- Test: `tests/test_cli_utf8.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_utf8.py
import io, sys
from mcpbrain import doctor

def test_doctor_report_encodes_under_cp1252(monkeypatch):
    # A doctor report full of ✅/⚠️/➖ must not crash on a legacy-codepage console.
    buf = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    lines = ["✅ Daemon           OK", "⚠️  Backup           off", "➖ Fleet            not set up"]
    monkeypatch.setattr(sys, "stdout", buf)
    from mcpbrain.cli import _ensure_utf8_stdio
    _ensure_utf8_stdio()                       # what cli.main() calls on Windows
    for ln in lines:
        print(ln)                              # must not raise UnicodeEncodeError
    buf.flush()
```

Run: `pytest tests/test_cli_utf8.py -v` → FAIL (`_ensure_utf8_stdio` missing).

- [ ] **Step 2: Add UTF-8 reconfig to the CLI**

```python
# mcpbrain/cli.py — add and call at the top of main()
def _ensure_utf8_stdio() -> None:
    """Windows consoles default to a legacy codepage (cp1252) that can't encode the
    doctor report's ✅/⚠️ glyphs → UnicodeEncodeError. Reconfigure stdio to UTF-8
    (errors='replace' so it degrades instead of crashing). No-op where unsupported."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

def main(argv=None):
    _ensure_utf8_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    ...
```

(Call it unconditionally — it's a harmless no-op on macOS/Linux where stdout is
already UTF-8.)

- [ ] **Step 3: Run test (GREEN) + commit**

Run: `pytest tests/test_cli_utf8.py -v` → pass.

```bash
git add mcpbrain/cli.py tests/test_cli_utf8.py
git commit --no-verify -m "fix(cli): force UTF-8 stdio so doctor's emoji don't crash Windows consoles"
```

---

### Task 4: VC++ safety net + doctor arch reframe

**Files:**
- Create: `mcpbrain/vcruntime.py`
- Modify: `mcpbrain/daemon.py` (add_dll_directory), `mcpbrain/doctor.py` (arch_line + embedder repair)
- Test: `tests/test_vcruntime.py` (new), `tests/test_doctor_arch.py`

**Interfaces:** `vcruntime.ensure_vcruntime_dlls(app_dir) -> list[str]` (names copied); `vcruntime.is_x64_pe(path) -> bool`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_vcruntime.py
from mcpbrain import vcruntime

def test_is_x64_pe_detects_machine(tmp_path):
    # Minimal PE: 'MZ' at 0, e_lfanew at 0x3C -> 'PE\0\0' + machine word 0x8664 (x64).
    p = tmp_path / "fake.dll"
    buf = bytearray(0x100)
    buf[0:2] = b"MZ"
    buf[0x3C:0x40] = (0x80).to_bytes(4, "little")     # e_lfanew
    buf[0x80:0x84] = b"PE\x00\x00"
    buf[0x84:0x86] = (0x8664).to_bytes(2, "little")   # IMAGE_FILE_MACHINE_AMD64
    p.write_bytes(bytes(buf))
    assert vcruntime.is_x64_pe(str(p)) is True
    buf[0x84:0x86] = (0xAA64).to_bytes(2, "little")   # ARM64
    p.write_bytes(bytes(buf))
    assert vcruntime.is_x64_pe(str(p)) is False

def test_ensure_copies_only_x64_from_search(tmp_path, monkeypatch):
    # Given a fake source with an x64 MSVCP140_1.dll, ensure copies it into app_dir/vcruntime.
    src = tmp_path / "sys"; src.mkdir()
    dll = src / "MSVCP140_1.dll"
    dll.write_bytes(b"MZ" + b"\x00"*0x3A + (0x80).to_bytes(4,"little") + b"\x00"*0x40 + b"PE\x00\x00" + (0x8664).to_bytes(2,"little") + b"\x00"*0x80)
    app = tmp_path / "app"; app.mkdir()
    monkeypatch.setattr(vcruntime, "_SEARCH_DIRS", lambda: [src])
    monkeypatch.setattr(vcruntime, "_REQUIRED", ("MSVCP140_1.dll",))
    copied = vcruntime.ensure_vcruntime_dlls(str(app))
    assert "MSVCP140_1.dll" in copied
    assert (app / "vcruntime" / "MSVCP140_1.dll").exists()
```

Run: `pytest tests/test_vcruntime.py -v` → FAIL.

- [ ] **Step 2: Implement `vcruntime.py`**

```python
# mcpbrain/vcruntime.py
"""Fallback: ensure the x64 VC++ runtime DLLs onnxruntime/sqlite-vec need are on
the daemon's DLL search path, in a location that survives package reinstalls.

PRIMARY fix is the x64 vc_redist installed by install.ps1; this is the last-resort
repair the doctor runs only if the embedder still can't load on Windows."""
from __future__ import annotations
import os
import shutil
from pathlib import Path

_REQUIRED = ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "MSVCP140.dll", "MSVCP140_1.dll")


def is_x64_pe(path: str) -> bool:
    """True iff the PE file's IMAGE_FILE_HEADER.Machine is IMAGE_FILE_MACHINE_AMD64 (0x8664)."""
    try:
        with open(path, "rb") as f:
            data = f.read(0x400)
        if data[:2] != b"MZ":
            return False
        e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
        if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            return False
        machine = int.from_bytes(data[e_lfanew + 4:e_lfanew + 6], "little")
        return machine == 0x8664
    except OSError:
        return False


def _SEARCH_DIRS():  # pragma: no cover — real system dirs, monkeypatched in tests
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return [Path(root) / "System32", Path(root) / "WinSxS"]


def ensure_vcruntime_dlls(app_dir: str) -> list[str]:
    """Copy any missing required x64 runtime DLL from an MS-signed x64 copy on the
    machine into <app_dir>/vcruntime. Returns the names copied. Best-effort."""
    dest = Path(app_dir) / "vcruntime"
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in _REQUIRED:
        if (dest / name).exists():
            continue
        for d in _SEARCH_DIRS():
            found = _find_x64(d, name)
            if found:
                shutil.copy2(found, dest / name)
                copied.append(name)
                break
    return copied


def _find_x64(root: Path, name: str):  # pragma: no cover — filesystem walk
    if not root.is_dir():
        return None
    direct = root / name
    if direct.is_file() and is_x64_pe(str(direct)):
        return direct
    try:
        for cand in root.rglob(name):
            if cand.is_file() and is_x64_pe(str(cand)):
                return cand
    except OSError:
        pass
    return None
```

- [ ] **Step 3: Daemon adds the dir to the DLL search path**

In `daemon.py` `main()` (near the top, before the embedder factory is set):

```python
    if sys.platform == "win32":
        _vc = config.app_dir() / "vcruntime"
        if _vc.is_dir():
            try:
                os.add_dll_directory(str(_vc))   # harmless no-op if empty
            except OSError:
                pass
```

(Ensure `os` and `sys` are imported in daemon.py — they are.)

- [ ] **Step 4: Doctor — embedder-repair fallback + arch reframe**

In `doctor.py` `_repair_embedder` (which warms the embedder), on Windows, **before**
re-warming, call the runtime repair so a retry can succeed:

```python
    def _repair_embedder():
        import sys as _sys
        if _sys.platform == "win32":
            from mcpbrain import vcruntime
            vcruntime.ensure_vcruntime_dlls(str(home))
        # ... existing re-download/warm logic ...
```

Reframe `arch_line` so x64-on-ARM64 is **ok (emulated — expected)**, using the
interpreter's wheel platform:

```python
def arch_line(os_arch: str | None = None) -> str:
    import platform, sysconfig
    os_arch = os_arch if os_arch is not None else _true_os_arch()
    interp = sysconfig.get_platform()          # e.g. 'win-amd64'
    on_arm = os_arch.lower() in ("arm64", "aarch64")
    emulated = on_arm and interp == "win-amd64"
    state = "emulated — expected" if emulated else "ok"
    glyph = "✅"
    return f"{glyph} {'Architecture':<16} OS={os_arch} interpreter={interp} → {state}"
```

Update `tests/test_doctor_arch.py`: assert an x64 interpreter on an ARM64 OS reports
`emulated`/ok (not a mismatch); keep the encoding-safe expectation.

- [ ] **Step 5: Run tests + commit**

Run: `pytest tests/test_vcruntime.py tests/test_doctor_arch.py -v` and `python -c "import mcpbrain.daemon"` → pass.

```bash
git add mcpbrain/vcruntime.py mcpbrain/daemon.py mcpbrain/doctor.py tests/test_vcruntime.py tests/test_doctor_arch.py
git commit --no-verify -m "feat(vcruntime): durable x64 VC++ DLL fallback + doctor repair; arch_line = emulation-expected"
```

---

### Task 5: Silence maintenance warning (M4) + verify wizard timezone (M5)

**Files:**
- Modify: the `mcpbrain.maintenance` import site (grep to find); `mcpbrain/config.py` or wizard/`control_api` for timezone (investigate)
- Test: `tests/test_config_timezone.py` (new, if a bug is found)

- [ ] **Step 1: Make the `mcpbrain.maintenance` import optional (M4)**

Run: `grep -rn "import.*maintenance\|mcpbrain.maintenance" mcpbrain/*.py` to find the
site that logs a startup *warning* (it's excluded from the wheel per pyproject). Wrap:

```python
try:
    from mcpbrain import maintenance   # noqa: F401  (dev-only; excluded from the wheel)
except ImportError:
    log.debug("maintenance module not installed (expected in a wheel install)")
```

Confirm no remaining `log.warning(...)` for the missing module.

- [ ] **Step 2: Verify timezone persistence (M5)**

Trace the wizard timezone path: `wizard/index.html` (`#timezone` dropdown) → POST
`/api/config` → `daemon.apply_config(body)` → `config.write_config`. Add/confirm a
test that a posted `timezone` is stored and read back:

```python
# tests/test_config_timezone.py
def test_timezone_persists_through_apply_config(tmp_path, monkeypatch):
    from mcpbrain import config
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    config.write_config(str(tmp_path), {**config.read_config(str(tmp_path)), "timezone": "Australia/Brisbane"})
    assert config.read_config(str(tmp_path)).get("timezone") == "Australia/Brisbane"
```

If the wizard's `save()` omits `timezone` from the POST body (the likely bug — trace
the JS `body` object in `index.html`), fix the JS to include `$("timezone").value`.
If it already persists, note "verified, no bug" in the report and skip the JS change.

- [ ] **Step 3: Run tests + commit**

Run: `pytest tests/test_config_timezone.py -v` (+ any impacted config test) → pass.

```bash
git add -A
git commit --no-verify -m "fix: optional maintenance import (no wheel-install warning); verify wizard timezone persistence"
```

---

### Task 6: `install.md` + stable `.mcpb` URL + runbook

**Files:**
- Modify: `plugin/commands/install.md`, `docs/RELEASE-RUNBOOK.md`

- [ ] **Step 1: `install.md` — download-then-run + stable `.mcpb` URL**

Replace the Windows step-1 block with the download-then-run form (managed machines
block `irm|iex`), keeping the one-liner as a note:

```powershell
irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 -OutFile "$env:TEMP\mcpbrain-install.ps1"
& "$env:TEMP\mcpbrain-install.ps1"
mcpbrain doctor
```

Add one sentence: the installer makes system changes (uv, the x64 VC++ runtime,
autostart, Claude Desktop config), so it needs approval / non-restricted execution.
In step 3, change the `.mcpb` link to the **stable unversioned** URL:
`https://centrepoint-church.github.io/mcpbrain-dist/mcpbrain.mcpb`.

- [ ] **Step 2: Runbook — publish unversioned `.mcpb`; x64 note; mandatory QA**

In `docs/RELEASE-RUNBOOK.md` §1b.1, publish **both** the versioned and an
**unversioned** copy:
```bash
npx @anthropic-ai/mcpb pack plugin/mcpb ~/GitHub/mcpbrain-dist/mcpbrain-<version>.mcpb
cp ~/GitHub/mcpbrain-dist/mcpbrain-<version>.mcpb ~/GitHub/mcpbrain-dist/mcpbrain.mcpb
```
Add a note that Windows uses **x64 Python under emulation on ARM64** (native ARM64 is
blocked by sqlite-vec/cryptography/pymupdf/leidenalg), and change the Windows QA
section header to state the gate is **MANDATORY — do not ship the Windows path
without it.**

- [ ] **Step 3: Commit**

```bash
git add plugin/commands/install.md docs/RELEASE-RUNBOOK.md
git commit --no-verify -m "docs(install): download-then-run + stable .mcpb URL; runbook x64 note + mandatory QA"
```

---

### Task 7: Release 0.7.97 + MANDATORY hardware QA

**Files:** the five version files + `uv.lock`; `CLAUDE.md` current-state.

- [ ] **Step 1: Gates**

Run: `uv run pytest -q` (full suite green) and `uv run ruff check mcpbrain/` (clean).

- [ ] **Step 2: Bump to 0.7.97** in `pyproject.toml`, `mcpbrain/__init__.py`,
`plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`,
`plugin/mcpb/manifest.json`, and `uv.lock` (one occurrence each — verify).
Run: `uv run pytest tests/test_version.py tests/test_plugin_manifest.py -q` → pass.

- [ ] **Step 3: HARDWARE QA — the gate (do NOT skip)**

On a real **ARM64** box (ideally the colleague's) **and** an **x64** box, from clean:
- `install.ps1` runs end-to-end with **no manual steps**: uv installs x64 Python, the
  x64 VC++ redist installs, `uv tool install "mcpbrain[daemon]"` succeeds.
- onnxruntime loads with **no DLL copying** (embedder ✅ in `mcpbrain doctor`, which
  runs without crashing).
- daemon autostarts at login (reboot/relogin; confirm a live daemon under the uv venv).
- `.mcpb` installs in Claude Desktop; `brain_search` returns results.
Record results in the runbook's Windows QA table. **If onnxruntime still fails after a
clean x64-only redist, confirm the doctor `vcruntime` fallback repairs it** (that
validates the safety net).

- [ ] **Step 4: Release (only after QA passes)**

Per `docs/RELEASE-RUNBOOK.md` §1a–1d: push source; publish wheel + `install.ps1` +
`mcpbrain-0.7.97.mcpb` + unversioned `mcpbrain.mcpb` to dist (purge 0.7.96); sync
plugin. Update `CLAUDE.md` current-state to 0.7.97 (use-the-platform Windows install).

---

## Self-Review

**Spec coverage:** slim installer (Task 1); absolute-trampoline shim + tray fallback (Task 2, spec C3/M2); UTF-8 CLI (Task 3, H2); vcruntime safety net + daemon add_dll_directory + arch reframe (Task 4, C2/H3); M4/M5 (Task 5); install.md + stable `.mcpb` + runbook (Task 6, M1/H1); release + mandatory QA (Task 7). ✓ Native-ARM64 and python.org provisioning explicitly removed (not just skipped). ✓

**Placeholder scan:** every code step has real code; the two investigate-items (M5 timezone JS, the maintenance import site) name the exact grep/trace and the conditional fix. ✓

**Type consistency:** `Get-InstallPlan` probe keys (`UvOk`/`VcRedistX64Ok`/`SchedulerOk`) match between `install.ps1` and its Pester tests; `_win_shim_content(*, mcpbrain_bin, home, subcommand)` matches its tests and all call sites; `vcruntime.ensure_vcruntime_dlls`/`is_x64_pe`/`_SEARCH_DIRS`/`_REQUIRED` are consistent across module + tests + doctor caller. ✓

**Open items flagged for the plan executor:** confirm uv's `--python cpython-3.12-windows-x86_64` request form (fallback coded); the whole point is Task 7's hardware QA — it is a gate, not optional.
