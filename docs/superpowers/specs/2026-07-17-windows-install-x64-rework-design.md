# Windows install rework (use-the-platform) — design

**Date:** 2026-07-17
**Status:** draft (design), pending review
**Author:** Josh + Claude
**Supersedes the Windows-ARM64 strategy in:** `2026-07-15-windows-preflight-installer-design.md`

## Problem

The 0.7.95/0.7.96 Windows installer was built on a misdiagnosis — that
onnxruntime's x64 build can't run under ARM64 emulation, so the installer must
provision **native ARM64 Python**. A real Windows-on-ARM install (colleague's
Snapdragon box, 2026-07-17) disproved this and exposed a chain of failures:

1. **Native ARM64 is not achievable.** Four core deps have **no `win_arm64`
   wheel or binary through any channel**: `sqlite-vec` (confirmed against its
   GitHub release matrix — windows x86_64 only), `cryptography`, `pymupdf`,
   `leidenalg`. So `uv tool install` against ARM64 Python fails, and there is no
   realistic pure-Python substitute for these.
2. **x64-under-emulation is the supported path — and the platform already does
   it.** `uv` **installs x86_64 CPython by default on ARM64 Windows** (Astral's
   deliberate choice: x64 wheels cover more, and Windows runs them transparently
   under Prism). So a *plain* `uv tool install` already produces a working x64
   install on ARM64 — our native-ARM64 provisioning was fighting the platform.
3. **The one genuine gap:** emulated x64 native modules (onnxruntime, sqlite-vec)
   need the **x64 Visual C++ runtime**. The colleague's box was missing
   `MSVCP140_1.dll` — **very likely because our installer installed the *ARM64*
   redist first**, and the VC++ installer skips a DLL when a same/newer version
   is already registered, so the later x64 redist skipped it.
4. Downstream install bugs (all still real): autostart shim pointed at a bare
   `pythonw.exe` (Task-6 regression → daemon never starts at login); `mcpbrain
   doctor` crashes emitting emoji on a cp1252 console; the `.mcpb` URL 404s
   (`install.md` links unversioned, we publish versioned); `irm|iex` is blocked
   on managed machines.

## Goal

Make the Windows install **use the platform instead of fighting it**: let uv
provision x64 Python, ensure the **x64 VC++ runtime** (the single real system
change), and otherwise run the *same* `uv tool install` as everywhere else.
Validate on real ARM64 + x64 hardware before shipping.

### Non-goals

- Native ARM64 (blocked by 4 core deps; revisit only if the ecosystem ships
  `win_arm64` for all of them — CPython's native-ARM64 default lands ~3.15/Oct
  2026 but the wheels won't).
- Provisioning python.org Python, arch-detection gymnastics, per-app DLL
  scavenging as the *primary* mechanism — all removed; the platform + the x64
  redist cover it.
- Restructuring the MCP server (full read-routing). Not needed: with the system
  x64 redist present, the `.mcpb` bridge's x64 `sqlite-vec` loads under emulation
  like everything else. (Noted as optional future cleanup, not this release.)
- Code signing (unchanged — out of scope).

## Design

### The install path (slim `plugin/scripts/install.ps1`)

Uniform across x64 and ARM64 Windows — no arch branching:

1. **Ensure uv** (native ARM64 uv binary installs fine; else install via astral).
2. **Ensure the x64 VC++ Redistributable** — the one system change. Probe the
   registry (`HKLM:\SOFTWARE\...\VC\Runtimes\x64` `Installed=1` + `Version`); if
   absent/old, download and run `vc_redist.x64.exe /install /quiet /norestart`.
   **Install ONLY the x64 redist — never the arm64 one** (installing arm64 first
   is what poisoned the x64 `MSVCP140_1.dll` on the colleague's box).
3. **Install mcpbrain**, forcing an **x64 interpreter**:
   `uv tool install --python cpython-3.12-windows-x86_64 --index
   "mcpbrain=…/simple/" "mcpbrain[daemon]" --force`. Pinning x64 explicitly (not
   just `--python 3.12`) future-proofs against uv changing its ARM64 default when
   CPython 3.15 switches to native-aarch64. (Verify uv's exact python-request
   spelling during implementation; fall back to `--python 3.12`, which resolves
   to x64 on ARM64 today, if the qualified form isn't accepted.)
4. `mcpbrain setup`.

Structure it as a small **probe → pure `Get-InstallPlan` → apply** (keeps the
decision logic Pester-testable): plan ∈ `[install-uv?, install-vcredist-x64?,
install-mcpbrain, persistence-*]`. No `Test-PythonArch`, no `Get-NativePython`,
no `Get-PythonArchStrings`, no python.org download — deleted.

### VC++ runtime — primary fix + cheap safety net

- **Primary:** the clean **x64** redist (step 2) supplies `MSVCP140.dll`,
  `MSVCP140_1.dll`, `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` for all emulated x64
  apps. Because we never install the arm64 redist, the version-skip that hid
  `MSVCP140_1.dll` should not occur.
- **Safety net (fallback only):** the daemon calls
  `os.add_dll_directory(str(app_dir()/"vcruntime"))` on Windows before the
  embedder loads (harmless no-op when the dir is empty/absent). `mcpbrain doctor`,
  **only if the embedder still fails to load**, (a) re-runs the x64 redist, and
  (b) if it still fails, copies the missing x64 DLL(s) from a Microsoft-signed
  copy found on the machine (PE machine `0x8664`, from System32 / WinSxS / redist
  cache) into `app_dir()/vcruntime`. Living under `app_dir()` survives package
  reinstalls/auto-updates (fixes the "wiped on update" concern). This is a
  bounded last resort, not the main path.

### Autostart — absolute trampoline, drop signed-`pythonw` (`agents.py`)

Revert the Task-6 signed-`pythonw` shim (it broke: `_win_pythonw_for` looked for
`pythonw` beside `~/.local/bin/mcpbrain.exe`, which uv doesn't put there, and
fell back to a bare, PATH-less `pythonw.exe`). The signed benefit only pays off
with code signing, which is out of scope. Instead:
- The hidden-console VBS shim runs the **absolute** `mcpbrain.exe`
  (`""<abs mcpbrain_bin>"" <sub>`) — the pre-Task-6 behavior that worked.
- Delete `_win_pythonw_for` and the pythonw path from `_win_shim_content`
  (restore the `mcpbrain_bin` form). Update the agents tests back to asserting the
  absolute-`mcpbrain.exe` shim.

### Tray Startup-shortcut fallback (`agents.py`) — M2

`_install_schtasks_tray` is currently schtasks-or-skip, so on a
scheduler-blocked box the tray never autostarts. Give the tray the same
`win_persistence_mechanism` selection as the daemon (schtasks → else a
Startup-folder shortcut to the tray shim). Records cadences stay schtasks-or-skip
(they need scheduled triggers; they run opportunistically in the daemon loop).

### `mcpbrain doctor` / CLI Windows-safe output — H2

At the CLI entrypoint (`cli.py:main`, before any output), on Windows reconfigure
stdio to UTF-8: `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`
(and stderr), guarded on `.reconfigure` existing. Fixes the emoji crash for every
Windows user. Add a test that the doctor report encodes under a forced-cp1252
stdout without raising.

### `doctor` arch line — H3 (reframed)

Emulation is now the **expected, supported** state on ARM64, not a fault.
`arch_line` reports OS arch + the interpreter's `sysconfig.get_platform()`
(`win-amd64`), and labels x64-on-ARM64 as **ok (emulated — expected)**. Only flag
a genuinely unusable combo. Update the existing tests.

### Stable `.mcpb` URL — H1

Publish an **unversioned `mcpbrain.mcpb`** at the dist root (copy of the versioned
artifact) so `install.md`'s stable link resolves; keep the versioned
`mcpbrain-<version>.mcpb` too. `install.md` uses the unversioned URL (no hardcoded
version). Runbook §1b.1 copies both.

### `install.md` — M1 + H1

- Lead with **download → run** (managed machines block `irm|iex`):
  ```powershell
  irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 -OutFile "$env:TEMP\mcpbrain-install.ps1"
  & "$env:TEMP\mcpbrain-install.ps1"
  mcpbrain doctor
  ```
  Keep the one-liner as a note; state that the installer makes system changes
  (uv, x64 VC++ runtime, autostart, Claude Desktop config) so it needs approval.
- Fix the `.mcpb` link to the stable unversioned URL.

### Minor — M4, M5

- **M4:** the `mcpbrain.maintenance*` import (excluded from the wheel) logs a
  startup *warning* on installed daemons → make it a clean optional
  (`except ImportError: log.debug(...)`).
- **M5:** verify the wizard persists the timezone (none was found on disk
  post-wizard). Trace dropdown → `/api/config` → config write; fix if the
  selection isn't saved.

## Files touched

- `plugin/scripts/install.ps1` + `install.tests.ps1` (slim rewrite: uv + x64
  redist + x64-pinned `uv tool install`; delete arch/python-provisioning)
- `plugin/commands/install.md` (download-then-run; stable `.mcpb` URL)
- `mcpbrain/agents.py` (revert to absolute-`mcpbrain.exe` shim; drop
  `_win_pythonw_for`; tray Startup fallback)
- `mcpbrain/cli.py` (UTF-8 stdio on Windows)
- `mcpbrain/doctor.py` (arch_line reframe; embedder-repair → vcruntime fallback)
- `mcpbrain/daemon.py` (`os.add_dll_directory(app_dir/vcruntime)` safety net)
- `mcpbrain/vcruntime.py` (new: locate/copy MS-signed x64 VC++ DLLs — used only
  by the doctor fallback)
- `mcpbrain/<maintenance import site>` (M4 optional import)
- wizard timezone path (M5, if a bug is found)
- `docs/RELEASE-RUNBOOK.md` (publish unversioned `.mcpb`; x64-everywhere note;
  Windows QA gate now MANDATORY pre-ship)

## Testing

- **Pester (pure):** `Get-InstallPlan` — needs-uv, needs-x64-redist, all-present
  (still always `install-mcpbrain --force`), scheduler-blocked → startup.
- **Python unit:** `cli`/`doctor` report encodes under forced cp1252 without
  raising; `arch_line` treats x64-on-ARM64 as ok; `agents._win_shim_content`
  emits the absolute `mcpbrain.exe` form (no bare name, no pythonw); tray
  mechanism selection; `vcruntime` DLL-locate (PE machine `0x8664` filter on a
  fake tree); M4 import is silent-optional.
- **MANDATORY hardware QA (gate — no Windows ship without it):** rerun clean on a
  real ARM64 box (ideally the colleague's) and an x64 box. Expected: `install.ps1`
  end-to-end with **no manual steps** — uv installs x64 Python, x64 redist
  installs, `uv tool install "mcpbrain[daemon]"` succeeds, onnxruntime loads with
  **no DLL copying**, `mcpbrain doctor` runs (no crash) and shows embedder ✅,
  daemon autostarts at login, `.mcpb` installs, `brain_search` returns results.
  The whole point: reproduce automatically what had to be done by hand.

## Open risks / to verify in the plan

- **Does a clean x64 redist alone fix `MSVCP140_1.dll`?** Strong hypothesis (the
  arm64-redist-first pollution was the cause), but unproven — hence the doctor
  fallback. The hardware QA is the real test.
- **uv x64 python-request spelling** — confirm `--python cpython-3.12-windows-
  x86_64` (or equivalent) forces x64; fall back to `--python 3.12` (x64 today on
  ARM64) if not.
- **`.mcpb` bridge under emulation** — `uvx --from mcpbrain … mcp-server` uses
  uv's x64 Python and loads x64 `sqlite-vec`; relies on the system x64 redist
  being present (it is, post-install). Confirm in QA.
