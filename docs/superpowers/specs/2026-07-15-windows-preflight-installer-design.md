# Windows preflight installer — design

**Date:** 2026-07-15
**Status:** approved (design), pending implementation plan
**Author:** Josh + Claude

## Problem

The Windows install path (`/mcpbrain:install`) installs `uv`, lets uv pick a
Python, `uv tool install`s the wheel, and runs `mcpbrain setup`. It has no model
of the machine it lands on, so it fails on real hardware in ways that surface as
in-session "try, fail, tell the user to do something" loops:

1. **uv's bundled Python is x64-only.** On Windows-on-ARM (Snapdragon / Copilot+)
   uv installs an emulated x64 interpreter, which pulls the x64 `onnxruntime`
   wheel, which crashes under Prism emulation
   (`ImportError: DLL load failed … onnxruntime_pybind11_state`).
2. **onnxruntime needs the arch-matched VC++ runtime.** Missing on a clean box
   (x64 *and* ARM) → `The specified module could not be found`. An x64 redist
   present on an ARM box does **not** satisfy a native ARM64 onnxruntime.
3. **Task Scheduler can be policy-blocked** on managed machines
   (`schtasks … Access is denied`), so login-agent registration fails.
4. **Model weights (~130 MB, bge-small, HuggingFace) download at daemon
   startup**, *before* the control server that serves the wizard starts
   (`daemon.py:2407` loads the embedder; `:2468` starts the control server). A
   blocked/slow download crashes the daemon before the wizard is ever reachable.

## Goal

**One Windows install pathway that works on all Windows machines**, with no
in-session try-fail-tell-user loops. The script **reviews the machine first**,
determines exactly what is needed, and installs the **correct arch-native
version of each missing component** — never reusing a wrong-arch / wrong-version
artifact that happens to be present ("none carried over from another").

Anything that legitimately belongs after the daemon exists (model download) moves
into the **wizard**, which is made to always start.

### Non-goals

- No frozen/bundled native installer (PyInstaller/Nuitka/MSI) — explicitly
  rejected in favour of the hardened script.
- No code-signing certificate. We do **not** ship or author a compiled
  executable — our code runs as Python source under the signed PSF interpreter,
  so there is no mcpbrain-authored unsigned binary. (This is a reason the script
  route beats the bundled-installer route, which *would* have introduced an
  unsigned frozen daemon `.exe`.) The `pythonw -m mcpbrain` shim (above) keeps
  the persistent daemon on the signed interpreter, removing the last unsigned exe
  from the run-at-logon path.
- macOS **install** path is unchanged (it is not the problem); it stays inline in
  the command. (The `.mcpb` connect step is cross-platform and *does* improve the
  macOS connect flow — that's intentional; only the daemon-install script is
  macOS-untouched.)

## Architecture

Three-stage design, mirroring the existing `doctor` probe/repair split so the
decision logic is testable without touching a real machine:

- **Probe** — read machine state, no side effects.
- **Plan** — *pure* function `(probe results) → ordered action list`. Unit-testable.
- **Apply** — execute actions, then **re-probe each** to confirm it is now correct.

Five artifacts:

1. **`plugin/scripts/install.ps1`** — the preflight installer (probe/plan/apply).
   Source of truth in this repo; published to the dist GitHub Pages at release.
2. **`/mcpbrain:install` command** (`plugin/commands/install.md`) — thin
   orchestrator on Windows: run `install.ps1`, then `mcpbrain doctor` to validate,
   install the `.mcpb` extension (one-click connect, replacing the manual
   config-write + quit/reopen dance), then guide the four Local tasks +
   run-on-startup. macOS block unchanged.
3. **`mcpbrain/agents.py`** — Startup-folder-shortcut persistence becomes a
   **first-class mechanism**, selected by a Task Scheduler availability probe
   (not a pasted fallback).
4. **`mcpbrain/daemon.py` + `control_api.py` + `wizard/index.html`** —
   **lazy embedder**: control server starts before the model loads; the wizard
   owns the weights download with progress + clear errors.
5. **`mcpbrain/mcp_server.py` (thin client) + `.mcpb` bridge** — the MCP server
   stops owning a store/embedder and becomes a pure control-API shim with **no
   native deps**, packaged as a one-click Claude Desktop Extension.

### install.ps1 — review → plan → action matrix

Master key: `[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture`
(→ `Arm64` / `X64`). This is process-emulation-proof, unlike
`$env:PROCESSOR_ARCHITECTURE`. Every row below is judged against it.

| # | Component | "Correct for this machine" | Probe | Action if missing/wrong |
|---|---|---|---|---|
| 1 | OS arch | — | `RuntimeInformation.OSArchitecture` | — (drives all rows) |
| 2 | Python 3.12 | a 3.12 whose `platform.machine()` matches OS arch; x64 Python on ARM is **rejected** | enumerate candidate interpreters (py launcher, `%LOCALAPPDATA%\Programs\Python\*`), run `-c "import platform;print(platform.machine())"` | winget `--architecture <arch>`; else pinned python.org arch-specific `.exe` (`/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1` — per-user, no UAC); re-probe `platform.machine()` |
| 3 | uv | present; **only ever** used with `--python <native-python>` | `Get-Command uv` | install uv (`install.ps1` from astral, arch-native); never let uv fetch its own managed Python |
| 4 | VC++ runtime | **arch-matched** runtime present | registry `HKLM:\SOFTWARE\...\VisualStudio\14.0\VC\Runtimes\<arch>` `Installed=1` (preflight signal); onnxruntime-load in `doctor` = definitive post-install truth | install `vc_redist.<arch>.exe /install /quiet /norestart` |
| 5 | mcpbrain + onnxruntime | wheel installed **and** onnxruntime actually loads | after install, `mcpbrain doctor` embedder check | `uv tool install --python <native> --index <dist> mcpbrain --force` — pip auto-resolves the arch-matched `win_*` onnxruntime wheel because the interpreter is native |
| 6 | Login persistence | a working run-at-logon mechanism | `_scheduler_available()` probe: create + delete a benign no-op task; access-denied → blocked | permitted → `schtasks`; blocked → Startup-folder shortcut to the hidden-console VBS shim, **chosen by detection** |
| 7 | Claude Desktop connector | `mcpbrain` entry in `claude_desktop_config.json` | path exists / entry present | `mcpbrain connect` (quit/reopen dance) |

**Correctness-not-carry-over rule (explicit):** rows 2 and 4 must *reject* a
present-but-wrong-arch artifact and install the right one. e.g. an x64 Python or
x64 VC++ redist on an ARM box is ignored; the ARM64 build is installed.

**Pinned versions:** Python patch version and the python.org download URL are
constants at the top of `install.ps1`, updatable in one place (testable, no
"latest" surprises). VC++ redist uses the evergreen `https://aka.ms/vs/17/release/vc_redist.<arch>.exe`.

**Preflight network check (review-first):** reach the dist index and HuggingFace
host; if unreachable, report clearly *up front* rather than failing mid-install.

### agents.py — Startup-shortcut as a first-class persistence mechanism

Today `install_agent(win32)` always calls `schtasks`. Change:

- Add `_scheduler_available()` (create + delete a throwaway no-op task under a
  temp name; returns False on access-denied).
- `install_agent`/`install_tray_agent`/`install_cadences` on win32 pick the
  mechanism from the probe: `schtasks` when permitted, else a Startup-folder
  `.lnk` → `wscript "<home>\agents\<task>.vbs"` (reusing the existing
  hidden-console shim generator, so no console flash).
- The VBS shim is already written before the schtasks call, so the shortcut
  reuses it. Cadence tasks (prune/health) that have no logon trigger degrade to
  "skipped, logged" under the Startup mechanism (documented), since a Startup
  shortcut only fires the daemon, not scheduled cadences — acceptable: those
  cadences also run opportunistically inside the daemon loop.
- `doctor` repair and `mcpbrain setup` both inherit this automatically.

**Shim runs under the signed interpreter (no unsigned exe on the persistent
path).** The hidden-console VBS shim currently launches `mcpbrain.exe <sub>` —
the `uv`/`pip`-generated launcher trampoline, which is effectively unsigned.
Change `_win_shim_content` to launch **`pythonw.exe -m mcpbrain <sub>`** using
the tool venv's `pythonw.exe` — a signature-preserving copy of the signed base
interpreter — resolved next to the installed interpreter (extend the
`_mcpbrain_bin`-style resolution to also locate `pythonw.exe`). The persistent
run-at-logon daemon then never invokes an unsigned executable, so AppLocker's
default "allow signed binaries" rules can permit it without a code-signing cert.
The `schtasks` action string gets the same treatment. In-session one-shots
(`setup`/`connect`) still use the `mcpbrain` launcher — attended, not the
persistent surface. (QA item: confirm the uv tool-venv `pythonw.exe` retains the
PSF Authenticode signature; if uv trampolines it instead of copying, resolve the
base interpreter's `pythonw.exe` directly.)

### Lazy embedder + wizard-owned model download

**daemon.py:**
- Do not call `get_embedder("bge-small")` eagerly at startup. `Store` needs only
  `dim` — expose a cheap `embedder_dim("bge-small") → 384` in `embed.py` that
  does **not** construct onnxruntime.
- `Daemon._embedder` becomes lazy: constructed on first access via
  `get_embedder`. Control server (`ctrl.start()`) runs first, so the wizard is
  always reachable even before weights exist.
- Guard embedder-first-use inside periodic passes / sync so a not-yet-downloaded
  or network-blocked model **logs and retries next cycle** instead of crashing
  the daemon.

**control_api.py + wizard:**
- `GET /api/model/status` → `{cached: bool, downloading: bool, error: str|null}`
  built from `model_weights_cached()` + an in-daemon download-state flag.
- `POST /api/model/ensure` → kicks a background thread that constructs the
  embedder (triggering fastembed's download) and records progress/errors.
- New wizard step "Search model" showing cached / downloading / error, with a
  Retry button. Setup is not "done" until the model is cached, but the daemon
  and wizard are alive throughout.

### MCP bridge (thin client) + `.mcpb` Desktop Extension

**Why.** Today `mcp_server.py` opens its own `Store` and constructs the
`embedder` (onnxruntime) in-process — `brain_search` embeds locally, the other
read tools query SQLite directly, and only writes go through the daemon (via
`write_capture`'s file queue). That means (a) the Claude Desktop connection
depends on onnxruntime loading, and (b) there are two consumers of the store
schema. Both block a clean one-click connect.

**A — thin client (best-implementation, not a half-route).** Make the daemon the
**single owner** of the store + embedder, and reduce `mcp_server.py` to a pure
**control-API shim** that holds no store handle and imports **no native deps**:

- New daemon read endpoints mirroring each read tool (search already exists as
  `/api/recall`, which embeds server-side): `/api/context` (profile / communities),
  `/api/graph`, `/api/actions`, `/api/proactive`, `/api/read`, `/api/draft/context`,
  `/api/draft/save`. Corresponding `control_client.py` methods.
- `brain_*` tools call those endpoints and shape the JSON. Writes keep the
  existing `write_capture` queue (already daemon-mediated, pure-Python).
- **Dependency isolation:** move `fastembed`/`onnxruntime` into an optional
  dependency group used only by the daemon path; `mcp_server.py` and
  `control_client.py` import nothing native. Add a test asserting
  `import mcpbrain.mcp_server` pulls in no onnxruntime.
- Benefit stack: connection no longer waits on the model; single source of truth
  for the schema; composes with the lazy embedder; and the shim becomes small
  enough to bundle.

**B — `.mcpb` Desktop Extension (one-click connect).** Package the thin shim as
an `.mcpb` bundle (`manifest.json` + the pure-Python server) so connecting the
`brain_*` tools to Claude Desktop is **double-click → Install**, cross-platform
and identical on macOS/Windows — replacing the current `mcpbrain connect` +
quit-and-reopen dance (`install.md` step 3, which kills the user's session
mid-install). The bridge locates the daemon exactly as `control_client` does
(control_port/control_token under `app_dir`), so no `user_config` secrets are
needed. Built from `plugin/mcpb/` via the `@anthropic-ai/mcpb` toolchain
(`mcpb pack`) at release; published alongside the wheel.

**Two-artifact model (explicit):** the heavy native **daemon** is still
script-installed (`install.ps1` / macOS block) — `.mcpb` cannot portably bundle
onnxruntime and does not run a persistent syncing process. `.mcpb` covers only
the **Claude Desktop connection**. Install order: daemon first (script), then
`.mcpb`.

### doctor validation additions

- Add an explicit **architecture** line (OS arch + interpreter `platform.machine()`
  agree). The existing embedder/onnxruntime-load check remains the definitive
  functional gate. `/mcpbrain:install` runs `mcpbrain doctor` as its final step so
  the install ends on a single green confirmation, not a trust-me.

### Distribution / release integration

- `install.ps1` source lives at `plugin/scripts/install.ps1`.
- Release runbook gains steps: publish `install.ps1` **and the built `.mcpb`**
  to the dist Pages (`centrepoint-church.github.io/mcpbrain-dist/`) alongside the
  wheel index; run `mcpb pack` on `plugin/mcpb/`.
- `/mcpbrain:install` Windows block becomes:
  `irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 | iex`
  → `mcpbrain doctor` → install `.mcpb` → guide four Local tasks → run-on-startup.
- The `.mcpb` manifest `version` becomes a version source to keep in step with
  the wheel/plugin manifests (add to the release runbook's version-file list).
- Otherwise `install.ps1` is not a version source.

## Testing

- **Plan (pure):** unit-test the `probe-results → action-list` mapping for the
  matrix — ARM-missing-python, x64-python-on-arm (must reject), redist-present-wrong-arch,
  scheduler-blocked, all-present-noop, etc. This is the highest-value coverage and
  needs no Windows host.
- **agents.py:** test mechanism selection given a mocked `_scheduler_available()`;
  test the Startup `.lnk` target string + shim reuse.
- **daemon lazy embedder:** test that the control server starts and `/api/status`
  responds when the embedder cannot load; test `embedder_dim` returns 384 without
  constructing onnxruntime; test periodic-pass guard swallows a model-load error.
- **MCP thin client:** contract test each `brain_*` tool's output against its new
  daemon endpoint (parity with today's in-process behaviour); assert
  `import mcpbrain.mcp_server` imports **no** onnxruntime/fastembed (guards the
  `.mcpb`-bundlable property against regression).
- **install.ps1 apply / real hardware:** cannot be unit-tested from macOS; validate
  manually on an ARM64 box and an x64 box (documented in the plan's manual-QA step).
  Same for the `.mcpb` double-click install on macOS + Windows Claude Desktop.

## Files touched

- `plugin/scripts/install.ps1` (new)
- `plugin/commands/install.md` (Windows block → thin orchestrator)
- `mcpbrain/agents.py` (scheduler probe + Startup-shortcut mechanism)
- `mcpbrain/embed.py` (`embedder_dim`)
- `mcpbrain/daemon.py` (lazy embedder; guarded first-use)
- `mcpbrain/control_api.py` (`/api/model/*` + new read endpoints: context, graph,
  actions, proactive, read, draft/context, draft/save)
- `mcpbrain/control_client.py` (methods for the new read endpoints)
- `mcpbrain/mcp_server.py` (thin control-API client; drop in-process store/embedder)
- `pyproject.toml` (move fastembed/onnxruntime to a daemon-only optional group)
- `plugin/mcpb/` (new: `manifest.json` + bundled thin server for the `.mcpb`)
- `mcpbrain/wizard/index.html` (search-model step)
- `mcpbrain/doctor.py` (arch line)
- `docs/RELEASE-RUNBOOK.md` (publish install.ps1 + `.mcpb`; `.mcpb` version file)

## Open risks

- **Strict exe-allowlisting (AppLocker/WDAC).** We ship no unsigned binary of our
  own, and the `pythonw -m mcpbrain` shim keeps the daemon on the signed
  interpreter. The residual unsigned executables are toolchain-generated —
  `mcpbrain.exe` (the uv/pip launcher, used only for attended one-shots) and
  `uv.exe` — plus the in-session install itself. A machine that allowlists
  individual executables would also block uv, the Python installer, and most dev
  tooling, so this is a locked-endpoint policy, not something our packaging
  introduces. Note: Task-Scheduler blocking (Intune/MDM, as seen on the ARM box)
  does **not** imply exe-allowlisting — they are separate policies.
- **winget absence / manifest gaps on older ARM builds** → python.org `.exe`
  path is the deterministic install (not a user-facing fallback; chosen when the
  probe finds no winget).
- **Startup-shortcut cadences:** prune/health don't fire as scheduled tasks under
  the Startup mechanism; they rely on the in-daemon opportunistic path. Acceptable,
  documented.
- **`.mcpb` vs the org marketplace plugin — coexistence (resolve in plan).** The
  marketplace plugin already provides agents/skills/hooks/commands, and today the
  `brain_*` MCP tools are wired via the desktop-config entry. The `.mcpb` is a
  third registration mechanism; confirm it supersedes the desktop-config write
  cleanly and doesn't double-register the server alongside the marketplace plugin.
  Needs a quick check against current Claude Desktop `.mcpb` docs during planning.
- **`.mcpb` Python support maturity.** Anthropic's guidance favours Node.js and
  flags Python bundling of compiled deps as hard — mitigated here because our
  bridge is pure-Python (`server.type = "uv"`, no native deps). Validate the
  pure-Python bridge packs and runs on both OSes before relying on it; the
  script-based `mcpbrain connect` remains the mechanism until that's confirmed.
