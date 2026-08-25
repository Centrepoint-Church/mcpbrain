"""mcpbrain doctor — diagnose every health dimension and auto-fix the local,
idempotent failures, pointing at the exact next step for anything only
Claude/Cowork/the user can fix.

Reuses probes.all_connections (so CLI, wizard, monitor and doctor never
disagree) and adds a repair layer. Each probe maps to one of three
dispositions:

  auto    — a local idempotent fix exists: attempt it, re-probe, report fixed/❌
  guided  — only Claude/Cowork/the user can fix it: print the exact remedy
  ok/—    — healthy or deliberately unconfigured: report, do nothing

The repair calls are INJECTED (default dispatch wraps agents.* and the
records bootstrap) so the logic is unit-testable with stubs — no real
launchd/git/agent side effects in tests.

Scheduled-task health is INFERRED from probe_enrichment: the daemon cannot read
the Cowork app DB, so doctor cannot verify the four scheduled tasks directly.
It states this honestly. Recreating tasks is therefore always a guided step
(/mcpbrain-fix), never auto.
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone

from mcpbrain.mcp_server import live_version_records

# Probe key -> disposition. "auto" keys carry the repair-dispatch key to call;
# "guided" keys carry the remedy string to print. Keys absent here are reported
# verbatim with no action.
#
# Note: probe keys are google/claude/backup/records/enrichment. The
# report adds a synthetic "scheduled_tasks" line inferred from enrichment.
_DISPOSITIONS: dict[str, dict] = {
    "claude":     {"kind": "auto", "repair": "daemon",
                   "label": "Daemon",
                   "guided": "Install the mcpbrain plugin and run /reload-plugins"},
    "records":    {"kind": "auto", "repair": "records", "label": "Records"},
    "google":     {"kind": "guided", "label": "Google",
                   "guided": "Run: mcpbrain auth"},
    "enrichment": {"kind": "guided", "label": "Enrichment",
                   "guided": "Open Claude or run /mcpbrain-fix in Cowork"},
    "backup":     {"kind": "guided", "label": "Backup",
                   "guided": "Re-run a backup from the mcpbrain wizard"},
}

# States that mean "needs attention". not_started is deliberately healthy for
# the optional connections (backup/enrichment): an unconfigured feature
# is not a fault. claude not_started (plugin never connected) and records
# not_started (repo never created) ARE actionable, so they are handled per-key.
_FAIL_STATES = {"needs_action"}


def _platform() -> str:
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p in ("win32", "cygwin"):
        return "win32"
    return p


def _mcpbrain_bin() -> str:
    return shutil.which("mcpbrain") or sys.argv[0] or "mcpbrain"


def _default_repairs(home: str, platform: str, mcpbrain_bin: str) -> dict:
    """The real repair dispatch: idempotent local fixes only."""
    from mcpbrain import agents, config, records

    def _repair_daemon():
        agents.restart_agent(platform)

    def _repair_agent():
        agents.install_agent(platform, mcpbrain_bin=mcpbrain_bin, home=home)

    def _repair_records():
        # Pass profile so ensure_records_repo renders the CLAUDE.md + context/
        # reference templates, not just the git scaffold anchors.
        records.ensure_records_repo(
            config.records_dir(home),
            git_name=config.owner_full_name(home) or "mcpbrain",
            git_email=config.owner_email(home) or "mcpbrain@localhost",
            profile=config.read_config(home),
        )

    def _repair_embedder():
        # Warming the embedder forces fastembed to (re-)download the weights into
        # the persistent cache dir and verifies onnxruntime can actually load them.
        # Idempotent: a no-op when the weights are already present. Needs network.
        #
        # On Windows-ARM, x64 onnxruntime/sqlite-vec run under emulation and need
        # the x64 VC++ runtime. The primary fix is the clean x64 vc_redist
        # installed by install.ps1; this is the last-resort safety net — copy the
        # required DLLs from an MS-signed x64 copy on the machine into
        # app_dir()/vcruntime (a location that survives package reinstalls), then
        # add that dir to THIS process's DLL search path (daemon.py does the same
        # on its own startup via the shared helper) before retrying the warm-up —
        # otherwise the freshly-copied DLLs aren't visible until the next daemon
        # restart.
        if sys.platform == "win32":
            from mcpbrain import vcruntime
            vcruntime.ensure_vcruntime_dlls(str(home))
            vcruntime.add_search_dir(str(home))
        from mcpbrain.embed import get_embedder
        get_embedder().embed_query("warm")

    def _repair_baseline():
        # Re-run the baseline bootstrap via the running daemon (which owns the
        # store + Google services). Degrades if the daemon is down.
        from mcpbrain.control_client import ControlClient, DaemonUnavailable
        try:
            return ControlClient(home, timeout=600).bootstrap_baseline()
        except DaemonUnavailable:
            return {"status": "skipped", "reason": "daemon not running"}

    def _repair_ocr():
        # Install the tesseract CLI. Scanned, image-only PDFs have no text layer,
        # so OCR is the only way to read them, and extractors.py degrades to an
        # empty text layer without it — silently, which is why this needs to be
        # both reported and repairable rather than left to a log line during
        # ingestion. Best-effort: returns why, never raises.
        from mcpbrain import ocr
        ok, msg = ocr.install_tesseract(platform)
        return {"status": "ok" if ok else "skipped", "reason": msg}

    return {"daemon": _repair_daemon, "agent": _repair_agent,
            "records": _repair_records, "embedder": _repair_embedder,
            "baseline": _repair_baseline, "ocr": _repair_ocr}


def _is_problem(key: str, state: str) -> bool:
    """True when this probe state is an actionable problem for doctor."""
    if key in ("claude", "records"):
        return state in _FAIL_STATES or state == "not_started"
    return state in _FAIL_STATES


def _reprobe(home, key: str, fallback: dict) -> dict:
    """Re-run the live probes and return this key's fresh result."""
    from mcpbrain import probes
    return probes.all_connections(home).get(key, fallback)


def _live_daemon_status(home) -> dict | None:
    """The running daemon's /api/status, or None when it isn't reachable.

    A down daemon is already reported by the "claude" probe, so this degrades
    silently rather than adding a second failure line for the same cause.
    """
    from mcpbrain.control_client import ControlClient
    try:
        return ControlClient(str(home), timeout=5).status()
    except Exception:  # noqa: BLE001 — diagnostics must never fail on a probe
        return None


def run_doctor(home, *, conns=None, repairs=None, reprobe=None, platform=None,
               mcpbrain_bin=None, agent_installed=None, model_present=None,
               daemon_status=None, offline: bool = False) -> tuple[int, str]:
    """Diagnose, auto-fix the idempotent local failures, report, return (code, msg).

    Pure-ish: probes and repairs are injectable. With nothing injected it reads
    the live probes and builds the real repair dispatch. Exit code is 0 when
    nothing needs user action after auto-fix, else 1.

    offline: when True and daemon_status was not explicitly injected, skip the
    live /api/status probe (_live_daemon_status) entirely instead of attempting
    it and degrading to None on failure. That probe is a real HTTP call against
    the local daemon's control API, added for the watchdog-restart-limiter
    line — every other live check here (probes.all_connections) predates it and
    is doctor's actual job, but this one is new and skippable, so offline mode
    restores a way to run doctor's other checks with zero network/socket
    attempts (e.g. from a sandboxed test, or a "just show me the rest" run).
    Defaults to False so the CLI's existing behaviour (always live) is
    unchanged; wired to `--offline` in run_doctor_main.
    """
    from mcpbrain import probes

    platform = platform or _platform()
    mcpbrain_bin = mcpbrain_bin or _mcpbrain_bin()
    if reprobe is None:
        reprobe = _reprobe
    if agent_installed is None:
        agent_installed = _agent_installed
    if model_present is None:
        from mcpbrain.embed import model_weights_cached
        model_present = lambda _home: model_weights_cached()  # noqa: E731
    if conns is None:
        conns = probes.all_connections(home)
    if repairs is None:
        repairs = _default_repairs(str(home), platform, mcpbrain_bin)
    if daemon_status is None:
        daemon_status = {} if offline else _live_daemon_status(home)

    lines: list[str] = []
    fixed = 0
    need_action = 0

    for key, disp in _DISPOSITIONS.items():
        probe = conns.get(key, {"state": "not_started", "detail": ""})
        state = probe.get("state", "not_started")
        label = disp["label"]

        if not _is_problem(key, state):
            # Distinguish "configured + healthy" (✅) from "deliberately not set
            # up" (➖). A green ✅ next to "Not connected" / "Backup off" reads as
            # working to a non-technical user, which it is not.
            if state == "not_started":
                lines.append(f"➖ {label:<16} {probe.get('detail') or 'Not set up'} "
                             f"(optional — not configured)")
            else:
                lines.append(f"✅ {label:<16} {probe.get('detail') or 'OK'}")
            continue

        if disp["kind"] == "auto" and state in _FAIL_STATES:
            # For claude needs_action: choose install vs restart based on agent presence
            if key == "claude":
                if not agent_installed(home, platform):
                    repair_key = "agent"
                else:
                    repair_key = "daemon"
            else:
                repair_key = disp["repair"]
            repair = repairs.get(repair_key)
            if repair is None:
                lines.append(f"❌ {label:<16} no repair registered for '{repair_key}'")
                need_action += 1
                continue
            try:
                repair()
                new_probe = reprobe(home, key, probe)
                new_state = new_probe.get("state", state)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ {label:<16} {probe.get('detail')} → repair failed: {exc}")
                need_action += 1
                continue
            if not _is_problem(key, new_state):
                action = "re-registering agent" if repair_key == "agent" else "restarting"
                lines.append(f"❌ {label:<16} {probe.get('detail')} → {action}... ✅ fixed")
                fixed += 1
            else:
                lines.append(f"❌ {label:<16} {probe.get('detail')} → repair did not fix it; "
                             f"run {disp.get('guided', 'mcpbrain setup')}")
                need_action += 1
            continue

        if key == "records" and state == "not_started":
            repair = repairs.get("records")
            if repair is None:
                lines.append(f"❌ {label:<16} no repair registered for 'records'")
                need_action += 1
                continue
            try:
                repair()
                new_probe = reprobe(home, "records", probe)
                if not _is_problem("records", new_probe.get("state", state)):
                    lines.append(f"❌ {label:<16} not created → creating... ✅ fixed")
                    fixed += 1
                else:
                    lines.append(f"❌ {label:<16} could not create records repo")
                    need_action += 1
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ {label:<16} records repo create failed: {exc}")
                need_action += 1
            continue

        # guided (incl. claude not_started = plugin not connected)
        remedy = disp.get("guided", "see the mcpbrain wizard")
        lines.append(f"⚠️  {label:<16} {probe.get('detail')} → {remedy}")
        need_action += 1

    # Embedder weights: the local bge-small model must be cached on disk or
    # `mcpbrain mcp-server` dies at startup with onnxruntime NO_SUCHFILE — which
    # the user only ever sees as "unable to connect to the MCP server". Cheap
    # offline presence check; auto-repair warms the embedder (re-downloads +
    # verifies it loads). Needs network only when the weights are actually gone.
    if model_present(home):
        lines.append(f"✅ {'Embedder':<16} model weights cached")
    else:
        repair = repairs.get("embedder")
        try:
            if repair is not None:
                repair()
            healed = model_present(home)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"❌ {'Embedder':<16} weights missing → re-download failed: "
                         f"{exc} (needs network; rerun mcpbrain doctor when online)")
            need_action += 1
        else:
            if healed:
                lines.append(f"❌ {'Embedder':<16} weights missing → downloading... ✅ fixed")
                fixed += 1
            else:
                lines.append(f"❌ {'Embedder':<16} weights missing → re-download did not "
                             f"land (needs network; rerun mcpbrain doctor when online)")
                need_action += 1

    # Baseline bootstrap: re-runnable import of the org snapshot + shared-drive
    # ingest caches. Injected so tests don't hit the network; a down daemon or an
    # unreachable fleet is a graceful skip (➖), never an actionable fault.
    baseline = repairs.get("baseline")
    if baseline is None:
        lines.append(f"➖ {'Baseline':<16} not checked")
    else:
        try:
            res = baseline() or {}
            st = res.get("status", "unknown")
            # done/skipped -> ✅. degraded (no transport yet) and pending (curator
            # hasn't published / fleet_secret not distributed) are expected waiting
            # states on a fresh install -> ➖, not an actionable fault. Only a hard
            # error is ❌.
            glyph = ("✅" if st in ("done", "skipped")
                     else "❌" if st == "error"
                     else "➖")
            lines.append(f"{glyph} {'Baseline':<16} bootstrap {st}"
                         + (f" ({res['reason']})" if res.get("reason") else ""))
        except Exception as exc:  # noqa: BLE001 — never fatal
            lines.append(f"➖ {'Baseline':<16} skipped ({exc})")

    # Watchdog restart limiter. The daemon self-restarts on a 30-min stall but
    # gives up after 3 restarts in 6 hours and stays up "visibly stuck" — which
    # is only visible if something says so. Skipped entirely when the daemon
    # isn't reachable (the Daemon line already covers that).
    if daemon_status:
        wd_exits = int(daemon_status.get("watchdog_exits") or 0)
        if daemon_status.get("watchdog_limit_reached"):
            lines.append(f"❌ {'Watchdog':<16} {wd_exits} self-restarts in the last 6 h — "
                         f"restart limit reached, daemon left running for diagnosis; "
                         f"check the daemon log")
            need_action += 1
        elif wd_exits:
            lines.append(f"✅ {'Watchdog':<16} {wd_exits} self-restart(s) in the last 6 h "
                         f"(recovered)")
        else:
            lines.append(f"✅ {'Watchdog':<16} no stall restarts")

    lines.append(arch_line())

    drift_line = version_drift_line(home)
    if drift_line is not None:
        lines.append(drift_line)

    # OCR availability. Without tesseract, scanned/image-only PDFs are indexed
    # with an empty text layer and nothing says so outside a per-file log line
    # during ingestion — which is how this stayed off on every install for
    # months. A warning, not an error: everything except scanned-PDF text works
    # without it, and `mcpbrain doctor --repair` installs it.
    try:
        from mcpbrain import ocr
        if ocr.tesseract_available():
            lines.append(f"✅ {'OCR':<16} tesseract available (scanned PDFs readable)")
        else:
            lines.append(f"⚠️  {'OCR':<16} tesseract missing — scanned PDFs index "
                         f"with no text; run 'mcpbrain doctor --repair'")
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not break doctor
        lines.append(f"⚠️  {'OCR':<16} could not check tesseract ({exc})")

    # Chunks that exceed the embedder window (512 tokens ≈ 2,000 chars). Their
    # tails are silently truncated at embed time and become unsearchable.
    from pathlib import Path
    from mcpbrain.store import Store
    # "brain.sqlite3" — the same filename config.store_path() builds under
    # app_dir(); spelled relative to the injected `home` because run_doctor is
    # home-parameterised (tests pass a tmp_path). It said "b.sqlite3" for a
    # while, which exists nowhere: the read-only handle raised OperationalError,
    # the `except Exception` below swallowed it into "skipped", and the chunk
    # window + repair-state lines never printed on any real install.
    store = Store(Path(home) / "brain.sqlite3", dim=4, read_only=True)
    try:
        oversize = store.count_chunks_longer_than(2000)
        glyph = "✅" if not oversize else "⚠️"
        lines.append(f"{glyph} {'Chunk window':<16} {oversize} chunk(s) over the "
                     f"embedder window")
    except Exception as exc:  # noqa: BLE001 — never fatal
        lines.append(f"➖ {'Chunk window':<16} skipped ({exc})")

    # Repair progress (spec 3, bin/repair.py): content-free chunks and Drive
    # files whose chunks predate the current chunker. Surfaced here so "is the
    # repair done?" is answerable without running the CLI.
    try:
        from mcpbrain.chunking import CHUNKER_VERSION, PRIOR_CHUNKER_VERSION
        empty = store.count_content_free()
        stale = len(store.stale_chunker_ids(table_version=CHUNKER_VERSION,
                                            other_version=PRIOR_CHUNKER_VERSION,
                                            limit=100_000))
        lines.append(f"{'✅' if not empty else '⚠️'} content-free chunks: {empty}")
        lines.append(f"{'✅' if not stale else '⚠️'} Items awaiting re-chunk: {stale}")
    except Exception as exc:  # noqa: BLE001 — never fatal
        lines.append(f"➖ {'Repair state':<16} skipped ({exc})")

    # Bi-temporal back-pointers whose target row is gone. Surfaced because
    # foreign_key_check is STRUCTURALLY BLIND to any of these columns that does
    # not declare a REFERENCES clause -- which is every store that has not been
    # rebuilt. Residue predates FK enforcement and cannot be produced by
    # current code (tests/test_dangling_invalidators.py pins that), so a
    # NON-ZERO count here on an already-rebuilt store means something
    # reintroduced writes with foreign_keys OFF, and is worth investigating
    # rather than just sweeping. Repair: `bin/optimise_store.py --nullify-dangling`.
    try:
        dangling = store.count_dangling_invalidators()
        lines.append(f"{'✅' if not dangling else '⚠️'} dangling invalidator "
                     f"pointers: {dangling}")
    except Exception as exc:  # noqa: BLE001 — never fatal
        lines.append(f"➖ {'Invalidators':<16} skipped ({exc})")

    # Scheduled tasks: inferred from enrichment, never auto. Stated honestly.
    enr = conns.get("enrichment", {}).get("state", "not_started")
    enr_already_counted = enr in _FAIL_STATES  # already counted in the loop above
    if enr == "ok":
        lines.append("✅ Scheduled tasks  enrichment fresh ⇒ enrich task firing")
    else:
        lines.append("⚠️  Scheduled tasks  not directly checkable → "
                     "run /mcpbrain-fix in Cowork to recreate the enrich/gardener/"
                     "meeting-packs/reference-gardener tasks")
        if not enr_already_counted:
            need_action += 1

    header = (f"mcpbrain doctor — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC   "
              f"(home: {home})")
    summary = f"{fixed} fixed automatically, {need_action} need your action (see ↑)."
    message = "\n".join([header, "", *lines, "", summary])
    return (1 if need_action else 0), message


def _true_os_arch() -> str:
    """Best-effort native OS architecture, even when the running interpreter is
    itself emulated (e.g. an x64 Python launched via WOW64 on an ARM64 Windows
    box reports AMD64 from platform.machine() same as the OS would report).

    Windows: PROCESSOR_ARCHITEW6432 is set by WOW64 ONLY when the current
    process is emulated, and holds the true native arch (e.g. "ARM64") in
    that case; PROCESSOR_ARCHITECTURE is the native arch when not emulated.
    macOS: an x86_64 interpreter under Rosetta on Apple Silicon reports
    "x86_64" from platform.machine(); `sysctl.proc_translated == 1` means the
    process is translated, so the true hardware arch is arm64.
    Other: platform.machine().
    """
    import os
    import platform
    import sys

    if os.name == "nt":
        return (os.environ.get("PROCESSOR_ARCHITEW6432")
                or os.environ.get("PROCESSOR_ARCHITECTURE")
                or platform.machine())
    if sys.platform == "darwin" and _is_rosetta_translated():
        return "arm64"
    return platform.machine()


def _is_rosetta_translated() -> bool:
    """True iff this process runs under Rosetta 2 (translated x86_64 on Apple
    Silicon). `sysctl.proc_translated` is 1 when translated, 0 native, absent on
    Intel Macs. Best-effort — any error means 'not translated'."""
    import subprocess
    try:
        out = subprocess.run(["sysctl", "-n", "sysctl.proc_translated"],
                             capture_output=True, text=True, timeout=2)
        return out.returncode == 0 and out.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


_ARCH_NORM = {"arm64": "ARM64", "aarch64": "ARM64", "amd64": "X64", "x64": "X64", "x86_64": "X64"}


def arch_line(os_arch: str | None = None) -> str:
    """One doctor line: OS arch vs interpreter wheel platform. os_arch defaults
    to the TRUE OS architecture (via _true_os_arch, which sees through WOW64
    emulation on Windows).

    An x64 interpreter running on an ARM64 OS is EXPECTED (that's exactly the
    emulated path Task 1/4 harden on Windows via WOW64, not a fault) —
    reported as ok/"emulated — expected" rather than a MISMATCH. (macOS
    Rosetta emulation is not covered — _true_os_arch has no macOS-specific
    detection.) Any other disagreement between OS arch and interpreter arch
    (e.g. a genuinely broken pairing) is still flagged, preserving the
    original mismatch-detection this function existed for.

    Uses sysconfig.get_platform() rather than platform.machine() for the
    interpreter side: platform.machine() reflects the OS's reported machine
    type (which WOW64/Rosetta can mask), while sysconfig.get_platform()
    reflects the actual wheel/ABI the running interpreter was built for
    (e.g. 'win-amd64', 'macosx-14.0-arm64', 'linux-x86_64') — the thing that
    actually determines whether emulation is in play."""
    import sysconfig

    os_arch = os_arch if os_arch is not None else _true_os_arch()
    interp = sysconfig.get_platform()          # e.g. 'win-amd64', 'macosx-14.0-arm64'
    interp_arch = interp.rsplit("-", 1)[-1]
    os_n = _ARCH_NORM.get(os_arch.lower(), os_arch.upper())
    interp_n = _ARCH_NORM.get(interp_arch.lower(), interp_arch.upper())
    emulated = os_n == "ARM64" and interp_n == "X64"
    agree = os_n == interp_n
    if emulated:
        glyph, state = "✅", "emulated — expected"
    elif agree:
        glyph, state = "✅", "ok"
    else:
        glyph, state = "⚠️", "MISMATCH (emulated interpreter?)"
    return f"{glyph} {'Architecture':<16} OS={os_arch} interpreter={interp} → {state}"


def version_drift_line(home, installed: str | None = None) -> str | None:
    """One doctor line naming any live MCP server(s) running superseded code,
    or None when there is nothing to say.

    A live MCP server executes the code it started with for its whole life —
    nothing signals it on update — so a shipped fix reaches a user only on
    their next client restart, not when the wheel lands. doctor previously
    reported only the installed version and looked green while a connected
    server ran stale code underneath it.

    Silent (None) both when no MCP server is currently running (that is not a
    drift problem — doctor's other checks already cover connectivity) and
    when every live server matches `installed`. `installed` defaults to the
    installed package version.
    """
    if installed is None:
        import importlib.metadata
        installed = importlib.metadata.version("mcpbrain")
    recs = live_version_records(home)
    if not recs:
        return None
    stale_versions = sorted({r.get("version") for r in recs if r.get("version") != installed})
    if not stale_versions:
        return None
    stale_count = sum(1 for r in recs if r.get("version") != installed)
    return (f"⚠️  {'MCP version':<16} {stale_count} live server(s) on "
            f"{', '.join(stale_versions)}, installed is {installed} — "
            f"restart Claude Desktop to pick up {installed}")


def _agent_installed(home, platform) -> bool:
    """True when the OS login agent is registered. Best-effort; defaults True
    on platforms without a cheap check so doctor prefers a restart over a
    redundant install."""
    if platform == "darwin":
        from mcpbrain import agents
        return agents._LAUNCHD_PATH.exists()
    return True


def run_doctor_main(argv=None) -> None:
    import argparse
    from mcpbrain import config

    ap = argparse.ArgumentParser(prog="mcpbrain doctor")
    ap.add_argument("--offline", action="store_true",
                    help="skip the live daemon /api/status check (see run_doctor's "
                         "offline= docstring); the rest of doctor's checks still run")
    args = ap.parse_args(argv)

    code, msg = run_doctor(str(config.app_dir()), offline=args.offline)
    print(msg)
    sys.exit(code)
