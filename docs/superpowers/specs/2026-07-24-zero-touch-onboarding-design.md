# Zero-touch onboarding + Windows install fixes — design

**Date:** 2026-07-24
**Target release:** 0.7.108
**Status:** approved (brainstorming), pending spec review

## Goal

A fresh install must require the person to do exactly **two** inherently-human
things and nothing else:

1. Sign into Google (OAuth — only the human can consent).
2. Enter their name + timezone in the wizard.

Every packaging bug found during the 2026-07-24 Windows-on-ARM64 install is
fixed at the source (the repo — the live patches on that box do not ship), and
every *post-wizard* step (connect Claude Desktop, enable enrichment, start the
tray) is automated or reduced to a single labelled click.

This design was driven by a real ARM64/x64-emulated Windows install. Bugs were
patched live on that machine's installed copy only; the repo (source of truth)
still contains every one of them. All fixes below are verified as still-present
in the repo at design time.

## Context: what actually connects the brain

- `mcpbrain setup` already writes the `brain_*` MCP server into Claude Desktop's
  `claude_desktop_config.json` (`setup.py:_register_desktop_mcp`, 68). The
  `.mcpb` extension (`plugin/mcpb/manifest.json`) wraps the *same*
  `mcpbrain mcp-server` — so "download and double-click the `.mcpb`" is
  **redundant** with what setup already does. It is dropped from the flow.
- The one unavoidable step is that **Claude Desktop only loads MCP servers at
  launch and overwrites its config while running**. So the connector setup wrote
  won't load until Desktop is quit + reopened. That is what "Plugin not
  connected" in `doctor` means.
- Because setup runs *inside* a Claude Desktop code session, setup cannot kill
  Desktop out from under itself. So the reload is a **user-initiated one-tap
  button** in the wizard, done last.

## Context: embedding vs enrichment (drives section D)

- Sync/index is identity-agnostic and runs **every cycle automatically**
  (`_gated_enrich_mode`, `daemon.py:305`). History **embeds** with no action.
- `enrich_mode=spool` makes `run_cycle` → `prepare_units` →
  `_group_unenriched_threads` (`prepare.py:749`) spool **all** un-enriched
  threads up to `thread_cap` (default 2000) **per cycle** — the backlog is pulled
  in progressively, not just new items.
- The daemon's spool step only **queues** units and **applies** pushed results.
  Actual Haiku extraction is done by drainer subagents the **hourly enrichment
  scheduled task** spawns.
- Consequence: history enrichment needs only `enrich_mode=spool` + the scheduled
  task draining over cycles. **No install-time backfill blast.** (An earlier
  install-session claim that "spool is going-forward only, run a backfill" was
  incorrect and is retracted.)

## Scope

### A. Source fixes for the packaging bugs

All confirmed present in the repo at design time.

1. **`os.fchmod` guard** — `config.py:998`, `backup.py:593` call the POSIX-only
   `os.fchmod` unguarded → `AttributeError` on Windows → the atomic write's temp
   file is left orphaned and the target is never written. On Windows this
   silently dropped every wizard config save and would break the backup keyfile
   write. Wrap both in the `if hasattr(os, "fchmod")` guard already used in
   `control_api.py:38` and `restore.py:48/248`. (`mkstemp` is already owner-only
   on Windows, so skipping `fchmod` there is correct.)

2. **UTF-8 on served HTML** — `control_api.py:254/265/285` call `p.read_text()`
   with no `encoding=`, so on Windows the wizard/dashboard/graph HTML is decoded
   as cp1252 → mojibake. Add `encoding="utf-8"` to all three.

3. **Invalid `corpora` on the Drive Changes API** — `sync/drive.py:390` passes
   `corpora="drive"` to `changes().list()`, which does not accept it →
   `TypeError: Got an unexpected keyword argument corpora` → **every** shared
   drive is skipped. `corpora` is a `files.list`-only param; the `files.list`
   use at `drive.py:560` is legitimate and stays. Remove the one line at 390.
   (The docstring mention at 351 is corrected too.)

4. **Tray tooltip length** — the playbook lists a 127-char Windows tooltip
   overflow, but the repo uses a static `title="mcpbrain"` (`tray.py:270`, 8
   chars). **Verify during implementation**: only patch (truncate to ≤127) if a
   *dynamic* hover/status string that can exceed 127 chars actually exists. If
   the tooltip is only ever the static string, record "not applicable in
   0.7.107" and make no change.

5. **Embedder DLL, made self-healing** — onnxruntime's x64 build fails on ARM64
   with `DLL load failed importing onnxruntime_pybind11_state` because the x64
   VC++ redist does not place `MSVCP140_1.dll` where the emulated module looks.
   `vcruntime.py` + a `doctor` repair already exist to populate
   `app_dir()/vcruntime` from an MS-signed x64 copy. Make this repair **run
   automatically** on Windows when `doctor`/`setup` detects onnxruntime cannot
   import — locating the DLL from the installed x64 redist or a trusted
   system-signed source (e.g. Office ClickToRun) — so the person never runs a
   repair script. Keep the standalone repair as a fallback.

### B. Installer robustness — `plugin/scripts/install.ps1`

6. **uv "minor version link" glitch** — on ARM64, `uv tool install` can fail with
   *"Missing expected target directory for Python minor version link"* even
   though the x64 CPython is fully extracted. Add a fallback: on that failure,
   resolve the concrete installed x64 `python.exe` and retry
   `uv tool install --python <path> "mcpbrain[daemon]"`. The `[daemon]` extra is
   required (a bare `.` install breaks the embedder — missing fastembed).

### C. Zero-touch connect

7. **Wizard "Connect Claude Desktop" step + one-tap button.**
   - New wizard step (final) with a button **"Connect & restart Claude
     Desktop"** and a status badge fed by the existing connected-probe.
   - New authed endpoint `POST /api/connect-desktop` that (a) re-writes the
     connector via the existing `setup._register_desktop_mcp` logic (handles the
     case where Desktop overwrote the file), then (b) quits + relaunches Claude
     Desktop cross-platform:
     - Windows: `taskkill /IM Claude.exe`, then start the resolved Claude
       executable.
     - macOS: quit via `osascript`/`killall Claude`, then `open -a Claude`.
     - Linux: best-effort; if the executable can't be resolved, return a clear
       "restart Claude Desktop manually" status rather than failing hard.
   - The button is clearly labelled "do this last — restarts Claude Desktop."
     The wizard is served by the daemon (a separate process), so it survives the
     Desktop restart and can show "Connected ✅" afterward.
   - **Remove the "download & double-click the `.mcpb`" instruction** from setup
     output and docs; it is redundant with the connector.

### D. Zero-touch enrichment

8. **Auto-enable spool on wizard completion** — when the wizard saves identity +
   org (`is_configured` becomes true), set `enrich_mode=spool` via the same live
   config path the control API uses (`apply_config` updates `self._enrich_mode`
   with no restart). This is the only switch needed; the daemon then
   progressively spools the un-enriched backlog + new items each cycle.

9. **(Removed)** No install-time enrichment backfill. History is enriched by the
   hourly enrichment scheduled task draining the spool over cycles.

10. **Auto-start the tray after setup** — currently the tray is only registered
    as a login shortcut and appears "next login." Launch it immediately at the
    end of `setup` (best-effort, never fatal — matches the existing tray
    install's failure policy).

Note: the hourly enrichment scheduled task is a Claude Code cron routine the
daemon cannot create for itself. It stays **assistant-created** during onboarding
(not a human manual step). Documented, not automated in code.

### E. Robustness

11. **Drive errors non-fatal** — a single Drive/TLS error (e.g. a transient
    `[SSL] record layer failure`, or the pre-fix `corpora` `TypeError`) currently
    aborts the whole sync/bootstrap cycle, stalling the heartbeat and tripping
    "Baseline: bootstrap error." Wrap the Drive sync + baseline-bootstrap steps so
    a Drive failure is logged and skipped, letting Gmail/Calendar proceed and
    Drive retry next cycle. Gmail/Calendar failures keep their current behavior.

12. **Wizard model-step self-updates** — two defects:
    - `_model_downloading` is only set by `ensure_model()` (the button path), so an
      automatic embedder warm (via sync/enrich/doctor) is invisible to the wizard,
      which shows a stale "Not downloaded / Download model" prompt for a model that
      is already downloading or on disk. Set the flag around *any* embedder warm,
      or have `model_status()` report "building" when the embedder is mid-construction.
    - `refreshModel()` (`wizard/index.html:318`) only re-polls in the `downloading`
      branch; from `"Not downloaded"`/`error` it never polls again, so it can't
      reach "Ready" without a manual reload. Make it poll on a light interval until
      `cached` is true.
    Result: the step flips to "Ready" on its own and never shows a redundant
    download prompt.

### F. Communities under x64 emulation

13. `communities.py:71-74` imports `igraph`/`leidenalg` and already degrades
    gracefully (`{"skipped": "leiden unavailable"}`) — no crash. Under Prism x64
    emulation on ARM64 the native libs won't load, so communities are always empty.
    Investigate, in priority order:
    1. An x64 `igraph`/`leidenalg` wheel that loads under emulation (pin/provision).
    2. If (1) fails, a **pure-Python fallback** community algorithm (e.g. networkx
       greedy modularity, or `python-louvain`) invoked in the existing
       `leiden unavailable` branch, so communities populate on ARM64.
    Keep the graceful-skip as the last resort if both fail. This item is
    time-boxed: if neither path is viable within the plan's budget, ship the
    graceful-skip + a documented known-limitation and file a follow-up.

## Testing

Each fix TDD'd with cross-platform guards where the bug is platform-specific:

- **fchmod (#1):** `monkeypatch.delattr(os, "fchmod")` then assert the config /
  backup write completes and the file is present + correct.
- **UTF-8 (#2):** serve HTML containing non-ASCII (e.g. `✅`) and assert the bytes
  round-trip as UTF-8.
- **corpora (#3):** assert `changes().list` is called without `corpora`; guard
  test mirrors the empirical kwarg check done live.
- **connect endpoint (#7):** mock the process kill/relaunch and the config write;
  assert the connector is (re)written and the relaunch is invoked per-platform;
  assert graceful status on an unresolvable executable.
- **spool auto-enable (#8):** assert saving identity+org flips `enrich_mode` to
  `spool` on the live daemon state.
- **Drive non-fatal (#11):** inject a Drive error and assert the cycle completes,
  the heartbeat updates, and Gmail/Calendar work still ran.
- **model-step (#12):** assert `model_status()` reports building during an
  automatic warm; assert `refreshModel` re-polls from the idle state (JS logic
  covered by the existing wizard test harness if present, else a focused unit
  test of `model_status`).
- **communities (#13):** assert the fallback produces a non-empty membership on a
  small graph when leiden is unavailable.

`mcpbrain doctor` remains green on a clean install. Full suite + ruff before
release. Author runs the full-repo `pytest`; Claude scopes runs to edited +
directly-impacted files.

## Release

Ship as **0.7.108** following `docs/RELEASE-RUNBOOK.md`:

- Bump the **five** version files + `uv.lock`.
- If extraction rules change (they don't here), run `bin/sync_agents.py`.
- Push `mcpbrain` source → build dist wheel into `../mcpbrain-dist` (mind the
  stale-wheel gotcha) → sync `plugin/` into `mcpbrain-plugin`. All three repos end
  at 0.7.108.

**The Windows HARDWARE QA GATE (runbook §5) stays OPEN** until 0.7.108 is
validated on the real ARM64/x64-emulated Windows box. Publishing the wheel is
safe (existing installs auto-update only the daemon); `install.ps1`/`.mcpb` are
opt-in. Do NOT onboard new Windows users until QA passes.

## Out of scope / follow-ups

- Auto-creating the Claude Code enrichment scheduled task in code (stays
  assistant-driven).
- The escrow-key lockdown on the all-members-readable fleet folder (pre-existing,
  tracked separately).
- Any change to what enrichment extracts (no extraction-rule changes here).
