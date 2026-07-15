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

    return {"daemon": _repair_daemon, "agent": _repair_agent,
            "records": _repair_records, "embedder": _repair_embedder,
            "baseline": _repair_baseline}


def _is_problem(key: str, state: str) -> bool:
    """True when this probe state is an actionable problem for doctor."""
    if key in ("claude", "records"):
        return state in _FAIL_STATES or state == "not_started"
    return state in _FAIL_STATES


def _reprobe(home, key: str, fallback: dict) -> dict:
    """Re-run the live probes and return this key's fresh result."""
    from mcpbrain import probes
    return probes.all_connections(home).get(key, fallback)


def run_doctor(home, *, conns=None, repairs=None, reprobe=None, platform=None,
               mcpbrain_bin=None, agent_installed=None, model_present=None) -> tuple[int, str]:
    """Diagnose, auto-fix the idempotent local failures, report, return (code, msg).

    Pure-ish: probes and repairs are injectable. With nothing injected it reads
    the live probes and builds the real repair dispatch. Exit code is 0 when
    nothing needs user action after auto-fix, else 1.
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

    lines.append(arch_line())

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


def arch_line(os_arch: str | None = None) -> str:
    """One doctor line: OS arch vs interpreter arch. os_arch defaults to the
    interpreter's own (they always match off-Windows); on Windows the installer
    passes the true OSArchitecture so an emulated x64 interpreter is flagged."""
    import platform
    machine = platform.machine()          # 'ARM64' / 'AMD64'
    os_arch = os_arch or machine
    norm = {"arm64": "ARM64", "amd64": "X64", "x64": "X64"}
    agree = norm.get(os_arch.lower(), os_arch) == norm.get(machine.lower(), machine)
    state = "ok" if agree else "MISMATCH (emulated interpreter?)"
    return f"{'ok' if agree else '⚠️'} {'Architecture':<16} OS={os_arch} interpreter={machine} → {state}"


def _agent_installed(home, platform) -> bool:
    """True when the OS login agent is registered. Best-effort; defaults True
    on platforms without a cheap check so doctor prefers a restart over a
    redundant install."""
    if platform == "darwin":
        from mcpbrain import agents
        return agents._LAUNCHD_PATH.exists()
    return True


def run_doctor_main(argv=None) -> None:
    from mcpbrain import config
    code, msg = run_doctor(str(config.app_dir()))
    print(msg)
    sys.exit(code)
