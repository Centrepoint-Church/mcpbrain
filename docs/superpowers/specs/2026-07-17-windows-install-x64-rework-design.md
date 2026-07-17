# Windows install rework (x64-everywhere) — design

**Date:** 2026-07-17
**Status:** draft (design), pending review
**Author:** Josh + Claude
**Supersedes the Windows-ARM64 strategy in:** `2026-07-15-windows-preflight-installer-design.md`

## Problem

The 0.7.95/0.7.96 Windows preflight installer was built on a misdiagnosis: that
onnxruntime's x64 build fundamentally can't run under ARM64 emulation, so the
installer must provision **native ARM64 Python**. A real Windows-on-ARM install
(colleague's Snapdragon box, 2026-07-17) disproved this and exposed a chain of
failures:

1. **`sqlite-vec` (a core dep) ships no `win_arm64` wheel** → `uv tool install
   "mcpbrain[daemon]"` against native ARM64 Python **fails outright**. The
   installer's central action cannot produce a working install on ARM64.
2. **x64 Python under emulation works** — including onnxruntime — once the x64
   VC++ runtime is complete. The original crash was a missing
   **`MSVCP140_1.dll`**, not an emulation incompatibility.
3. **`MSVCP140_1.dll` (x64) is missing from the emulated System32 view** even
   after installing the x64 `vc_redist`; onnxruntime won't load until an
   MS-signed x64 copy is on its DLL search path.
4. **Autostart shim points at bare `pythonw.exe`** (not on PATH) → the daemon
   silently never starts at login. A Task-6 regression: `_win_pythonw_for`
   resolves `pythonw` beside `~/.local/bin/mcpbrain.exe`, but uv puts the venv
   (and its `pythonw`) in the uv **tools dir**, so it fell through to the bare
   literal.
5. **`mcpbrain doctor` crashes on Windows** — emits `✅/⚠️/➖/❌` to a legacy
   codepage console (cp1252) → `UnicodeEncodeError`, on the install's final
   verification step.
6. **Arch detection uses `platform.machine()`**, which under x64 emulation
   reports the **OS** arch, not the interpreter's. `sysconfig.get_platform()`
   (`win-amd64`/`win-arm64`) is the wheel-compatibility signal.
7. **The `.mcpb` URL 404s** — `install.md` links `…/mcpbrain.mcpb` but we publish
   the versioned `mcpbrain-<version>.mcpb`.

## Goal

A Windows install pathway that actually works, validated on real ARM64 + x64
hardware. Core pivot: **target x64 Python on all Windows machines** (native on
x64, emulated on ARM64) — x64 wheels exist for every dependency and run under
Prism — and make the x64 VC++ runtime (incl. `MSVCP140_1.dll`) reliably present
and durable.

### Non-goals

- Native ARM64 Python (abandoned — blocked by `sqlite-vec`/ecosystem wheels).
  Revisit only if/when `sqlite-vec` + onnxruntime ship `win_arm64` wheels.
- Shipping our own compiled binaries / bundling the whole VC++ runtime in the
  wheel (keep the wheel pure; the installer sources runtime DLLs from the
  official redist).

## Design

### Global decision: x64 everywhere on Windows

`install.ps1` always provisions/uses an **x64** Python 3.12. This removes all
Python arch-branching. Consequences:
- **Arch signal:** decide interpreter suitability with `sysconfig.get_platform()
  == "win-amd64"`, never `platform.machine()`.
- **Python install:** winget refuses an x64 install alongside an existing ARM64
  Python (confirmed on the colleague's box), so install the **python.org
  `amd64`** installer directly (per-user, silent) to a dedicated dir; probe for
  an existing suitable x64 3.12 first.
- **On ARM64**, running x64 under emulation is the *expected, supported* state —
  not a warning.

### C1 — installer targets x64 (`plugin/scripts/install.ps1`)

Rewrite the probe/plan/apply around x64:
- `Get-OsArch` stays (informational + drives VC++/emulation handling).
- Replace `Test-PythonArch` with `Test-X64Python`: a Python 3.12 is suitable iff
  `sysconfig.get_platform()` reports `win-amd64`. Probe candidate paths (existing
  x64 installs, the dedicated dir).
- `Install-Python` always installs the python.org **amd64** 3.12 to a dedicated
  dir (e.g. `%LOCALAPPDATA%\Programs\Python\Python312-x64-mcpbrain`) — never
  winget on ARM64. `Get-PythonArchStrings` collapses (always amd64/x64).
- `Get-NativePython` → `Get-X64Python`: return the interpreter whose
  `sysconfig.get_platform()=="win-amd64"`; throw with a clear message if absent.
- `Install-Mcpbrain` unchanged in shape: `uv tool install --python <x64-python>
  --index <dist> "mcpbrain[daemon]" --force`.
- `Get-InstallPlan` simplifies: `[install-vcredist-x64?, install-python-x64?,
  install-uv?, install-mcpbrain, ensure-vcruntime-dlls, persistence-*]`. Pure,
  Pester-tested (update the existing cases: bare-ARM now plans x64 python + x64
  redist + the DLL-ensure step; x64 box same minus emulation DLL work).

### C2 — durable x64 VC++ runtime for onnxruntime

Two parts:

**(a) Daemon adds a managed native-DLL dir to the search path.** On Windows,
before the embedder first loads, call `os.add_dll_directory(str(app_dir() /
"vcruntime"))` (guarded: dir-exists, Windows-only). Living under `app_dir()` (not
the package dir) means it **survives wheel reinstalls/auto-updates** — fixing the
colleague's "the fix gets wiped on update" concern.

**(b) Installer + doctor populate that dir with the required x64 runtime DLLs.**
onnxruntime needs `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll`, `MSVCP140.dll`,
`MSVCP140_1.dll`. Ensure all four (x64) exist in `app_dir()/vcruntime`. Sourcing,
in order:
1. Copy from an MS-signed x64 copy already on the machine (System32 for a native
   x64 box; WinSxS / VC redist cache) — verified x64 via PE machine `0x8664`.
2. Else download the official `vc_redist.x64.exe` and **extract** the DLLs
   (`vc_redist.x64.exe /extract` → expand the cabs) into the dir.

`install.ps1` does this during apply (`ensure-vcruntime-dlls`). `mcpbrain doctor`
gains a repair that re-ensures it (self-heal if ever missing). We still install
the x64 `vc_redist` normally (it correctly places the DLLs a *native* x64 machine
needs); the `app_dir()/vcruntime` copy is the belt-and-suspenders that fixes the
ARM64-emulation search-path gap and the durability problem.

**Licensing:** the VC++ runtime DLLs are Microsoft-redistributable; sourcing them
from the official redist and placing them beside our app is a supported pattern.

### C3 — autostart runs the real installed interpreter (`agents.py`)

Fix `_win_pythonw_for(mcpbrain_bin)` so it resolves the **uv tool venv**
interpreter, and never falls back to a bare, PATH-dependent name:
- Resolve the venv via `uv tool dir` → `<dir>/mcpbrain/Scripts/pythonw.exe`
  (query `uv tool dir` with subprocess; also try the `Scripts/` beside the
  resolved tool). Return that absolute path if it exists.
- **Fallback is the absolute `mcpbrain_bin`** (the resolved `mcpbrain.exe`), not
  `"pythonw.exe"`. The shim then runs `""<abs mcpbrain.exe>"" <sub>` — the
  pre-Task-6 behavior that actually worked — so autostart is never broken even
  when the venv pythonw can't be located.
- Keep the signed-`pythonw -m mcpbrain` form as the *preferred* path when the
  venv pythonw resolves (AppLocker benefit retained), absolute-trampoline as the
  guaranteed fallback. Unit-test the resolution + that the fallback is the
  absolute path (never a bare name).

### C3-adjacent (M3) — no cross-interpreter child

The colleague saw the venv pythonw spawn a second daemon under **base** amd64
Python (no mcpbrain in site-packages). Investigate the daemon's process model
(re-exec / auto-update relaunch / setup double-start) and ensure every spawned
daemon/child uses the mcpbrain venv interpreter (`sys.executable`), not a bare
`python`/`pythonw`. Add an assertion/log of the running interpreter at daemon
startup. (Likely resolved once the shim points at the venv pythonw, but verify.)

### H2 — `mcpbrain doctor` (and CLI) UTF-8 on Windows

At the CLI entrypoint (`cli.py main`, before any emoji output), on Windows
reconfigure stdio: `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
(and stderr). Guarded (only when `.reconfigure` exists). This fixes the crash for
every Windows user, not just doctor. Add a test that the doctor report encodes
under a cp1252 stdout without raising.

### H3 — `sysconfig.get_platform()` for arch decisions

- `install.ps1`: all interpreter-suitability checks use `sysconfig.get_platform()`
  (`win-amd64`), per C1.
- `doctor.arch_line`: reframe. It should report OS arch + interpreter platform,
  and treat **x64-on-ARM64 as EXPECTED (ok), not a mismatch** — the supported
  emulated config. Only flag a genuinely broken combo (e.g. interpreter platform
  that can't install our wheels). Update the existing tests accordingly.

### H1 — stable `.mcpb` URL

Publish an **unversioned `mcpbrain.mcpb`** at the dist root (a copy of the
versioned artifact) so `install.md`'s stable link resolves; keep the versioned
`mcpbrain-<version>.mcpb` too. Runbook §1b.1 copies both. `install.md` keeps the
unversioned URL (no hardcoded version).

### M1 — install.md leads with download-then-run

Managed machines block `irm | iex` (piped remote execution) — the colleague hit
this. Make the **download → (inspect) → run** form the documented primary:
```powershell
irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 -OutFile "$env:TEMP\mcpbrain-install.ps1"
& "$env:TEMP\mcpbrain-install.ps1"
mcpbrain doctor
```
Keep the one-liner as a note. Also state plainly that the installer makes
system-level changes (installs Python/uv/VC++ runtime, registers autostart,
writes Claude Desktop config) so it needs approval / non-restricted execution.

### M2 — tray gets the Startup-shortcut fallback

`_install_schtasks_tray` currently is schtasks-or-skip, so on a
scheduler-blocked box the tray never autostarts. Give the tray the same
`win_persistence_mechanism` selection as the daemon (schtasks → else Startup-
folder shortcut to the tray shim). Records **cadences stay schtasks-or-skip**
(they need scheduled triggers; they run opportunistically in the daemon loop) —
unchanged, documented.

### M4 — silence the `mcpbrain.maintenance` startup warning

`mcpbrain.maintenance*` is `exclude`d from the wheel, so an installed daemon logs
a missing-module warning at startup. Make that import a clean, silent optional
(try/except ImportError → debug log, not warning), so a normal install has a
clean log.

### M5 — verify the wizard persists timezone

No saved timezone was found on disk post-wizard. Trace the wizard's timezone
dropdown → `/api/config` → config write; confirm it persists, fix if the
selection isn't saved. (May be user-didn't-select; verify with a deliberate
selection.)

## Files touched

- `plugin/scripts/install.ps1` + `install.tests.ps1` (C1, C2b, H3 — x64 rewrite)
- `mcpbrain/daemon.py` (C2a `os.add_dll_directory`; M3 interpreter check)
- `mcpbrain/vcruntime.py` (new: locate/extract/ensure the x64 runtime DLLs;
  shared by installer-invoked repair + doctor)
- `mcpbrain/doctor.py` (H2 UTF-8; H3 arch_line reframe; C2 vcruntime repair)
- `mcpbrain/cli.py` (H2 UTF-8 stdio on Windows)
- `mcpbrain/agents.py` (C3 pythonw resolution + absolute fallback; M2 tray fallback)
- `mcpbrain/setup.py` (invoke vcruntime-ensure on Windows)
- `plugin/commands/install.md` (M1 download-then-run; H1 stable `.mcpb` URL)
- `mcpbrain/__init__.py` + config/module for the `mcpbrain.maintenance` optional (M4)
- wizard timezone path (M5, if a bug is found)
- `docs/RELEASE-RUNBOOK.md` (publish unversioned `.mcpb`; note the x64 strategy;
  the Windows QA gate is now MANDATORY pre-ship)

## Testing

- **Pester (pure):** `Get-InstallPlan` for x64 box + ARM64 box (both now plan x64
  python); `Test-X64Python`/`Get-PythonArchStrings` collapse to amd64; the
  vcruntime-ensure plan step present on Windows.
- **Python unit:** `vcruntime` DLL-ensure logic (locate MS-signed x64 copy in a
  fake tree; PE machine check `0x8664`); `agents._win_pythonw_for` resolves the
  venv path and falls back to the absolute `mcpbrain_bin` (never a bare name);
  `doctor` report encodes under a forced-cp1252 stdout without raising;
  `arch_line` treats x64-on-ARM64 as ok; tray mechanism selection.
- **MANDATORY hardware QA (gate — no Windows ship without it):** rerun on a real
  ARM64 box (ideally the colleague's) and an x64 box: clean `install.ps1` end-to-
  end installs x64 Python + x64 VC++ runtime + the four `vcruntime` DLLs; onnx
  loads with **no** manual DLL copy; `mcpbrain doctor` runs (no crash) and shows
  embedder ✅; daemon autostarts at login under the venv interpreter; `.mcpb`
  installs; `brain_search` returns results. This spec's whole point is that the
  installer reproduces — automatically — what had to be done by hand.

## Open risks / decisions to confirm

- **C2 sourcing:** prefer copy-from-machine, fall back to extract-from-official-
  redist. Confirm we're comfortable extracting DLLs from `vc_redist.x64.exe` at
  install time (vs. requiring the redist be installed). Recommended: extract, so
  a clean machine with no prior redist still works.
- **Emulation performance:** x64-under-emulation embedding on ARM64 is slower
  than native would be. Acceptable (bge-small is small); revisit if it's a
  problem in practice.
- **The `.mcpb`/bridge** runs `uvx --from mcpbrain mcpbrain mcp-server` — on
  ARM64 that uvx resolve must also land on x64 (else it re-hits the sqlite-vec
  wall). Verify uvx uses an x64 interpreter for the bridge on ARM64, or pin it.
  **New risk surfaced by this review — must resolve in the plan.**
