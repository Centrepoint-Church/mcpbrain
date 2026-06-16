"""OS-native login-agent generators for the mcpbrain daemon.

Pure generator functions (launchd_plist, schtasks_args) produce the text or
argument list needed to register ``mcpbrain daemon`` as a login agent on macOS
or Windows. They are fully unit-tested and have no side effects.

The install/uninstall/restart helpers write those definitions to the canonical
OS path and invoke the system loader. Their subprocess/loader bodies are marked
``# pragma: no cover`` because they require a real OS environment.

Supported platforms: darwin (launchd), win32 (schtasks). All other platforms
raise ``ValueError``.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

log = logging.getLogger(__name__)

# Bundle identifier used as the launchd label and scheduled-task name.
_LABEL = "com.mcpbrain"
_TRAY_LABEL = f"{_LABEL}.tray"

# Canonical agent file locations by platform.
_LAUNCHD_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
_TRAY_LAUNCHD_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_TRAY_LABEL}.plist"
_TASK_NAME = "mcpbrain"
_TRAY_TASK_NAME = "mcpbrain-tray"


# ---------------------------------------------------------------------------
# Pure generators
# ---------------------------------------------------------------------------

def _launchd_plist(*, label: str, subcommand: str, mcpbrain_bin: str, home: str, keep_alive) -> str:
    # keep_alive: True -> always relaunch; False -> never; "crashonly" -> relaunch
    # only on an abnormal (non-zero) exit. "crashonly" is for the tray: a clean
    # Quit (exit 0) must stay quit, but a crash should bring the icon back.
    if keep_alive == "crashonly":
        keep_xml = ("<dict>\n        <key>SuccessfulExit</key>\n"
                    "        <false/>\n    </dict>")
    else:
        keep_xml = "<true/>" if keep_alive else "<false/>"
    # Log paths under MCPBRAIN_HOME so crashes are debuggable. Without these,
    # launchd discards stdout/stderr and a daemon that exits non-zero leaves
    # no trace beyond `last exit code` in `launchctl print`.
    log_path = f"{home}/{label}.log"
    err_path = f"{home}/{label}.err"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{mcpbrain_bin}</string>
        <string>{subcommand}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MCPBRAIN_HOME</key>
        <string>{home}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    {keep_xml}
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
</dict>
</plist>
"""


def launchd_plist(*, mcpbrain_bin: str, home: str) -> str:
    """Return a macOS launchd plist that runs ``mcpbrain daemon`` at login."""
    return _launchd_plist(label=_LABEL, subcommand="daemon", mcpbrain_bin=mcpbrain_bin,
                          home=home, keep_alive=True)


def launchd_tray_plist(*, mcpbrain_bin: str, home: str) -> str:
    """Return a macOS launchd plist that runs ``mcpbrain tray`` at login.

    KeepAlive is "crashonly" (KeepAlive={SuccessfulExit: false}): a clean Quit
    (exit 0) from the menu-bar icon stays quit until the next login, but a crash
    (non-zero exit) relaunches the icon so it doesn't silently disappear. The
    daemon agent (separate) keeps KeepAlive true.
    """
    return _launchd_plist(label=_TRAY_LABEL, subcommand="tray", mcpbrain_bin=mcpbrain_bin,
                          home=home, keep_alive="crashonly")


def _schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str, home: str) -> list[str]:
    """schtasks args registering an on-logon task whose action embeds MCPBRAIN_HOME
    so the daemon starts correctly even if the env var is cleared."""
    quoted_bin = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    quoted_home = f'"{home}"' if any(c.isspace() for c in home) else home
    action = f'cmd /c "set MCPBRAIN_HOME={quoted_home} && {quoted_bin} {subcommand}"'
    return ["schtasks", "/create", "/tn", task_name, "/sc", "onlogon", "/tr", action, "/f"]


def schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return the schtasks.exe argument list that registers ``mcpbrain daemon`` at logon."""
    return _schtasks_args(task_name=_TASK_NAME, subcommand="daemon", mcpbrain_bin=mcpbrain_bin, home=home)


def schtasks_tray_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return the schtasks.exe argument list that registers ``mcpbrain tray`` at logon."""
    return _schtasks_args(task_name=_TRAY_TASK_NAME, subcommand="tray", mcpbrain_bin=mcpbrain_bin, home=home)


# ---------------------------------------------------------------------------
# Install / uninstall / restart helpers (thin; loaders are pragma: no cover)
# ---------------------------------------------------------------------------

def install_agent(
    platform: str,
    *,
    mcpbrain_bin: str,
    home: str,
) -> None:
    """Write the agent definition and register it with the OS loader."""
    if platform == "darwin":
        _install_launchd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_schtasks(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def uninstall_agent(platform: str) -> None:
    """Remove the agent definition and deregister it from the OS loader."""
    if platform == "darwin":
        _uninstall_launchd()
    elif platform == "win32":
        _uninstall_schtasks()
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def restart_agent(platform: str) -> None:
    """Restart the running agents via the OS loader.

    The daemon and its menu-bar tray are one system: a restart (e.g. after
    `mcpbrain update` reinstalls the package) brings BOTH up on the new code.
    The daemon restart is required (raises on failure); the tray restart is
    best-effort — a machine without the tray installed (headless, or the tray
    never registered) simply has nothing to kick, and that must not fail an
    update.
    """
    if platform == "darwin":
        _restart_launchd()
        _restart_launchd_tray()
    elif platform == "win32":
        _restart_schtasks()
        _restart_schtasks_tray()
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


# ---------------------------------------------------------------------------
# macOS helpers
# ---------------------------------------------------------------------------

def _install_launchd(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    plist = launchd_plist(mcpbrain_bin=mcpbrain_bin, home=home)
    _LAUNCHD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PATH.write_text(plist)
    log.info("wrote %s", _LAUNCHD_PATH)
    subprocess.run(["launchctl", "load", "-w", str(_LAUNCHD_PATH)], check=True)
    log.info("launchd agent loaded")


def _uninstall_launchd() -> None:  # pragma: no cover
    if _LAUNCHD_PATH.exists():
        result = subprocess.run(["launchctl", "unload", "-w", str(_LAUNCHD_PATH)], check=False)
        _LAUNCHD_PATH.unlink(missing_ok=True)
        if result.returncode == 0:
            log.info("launchd agent removed")
        else:
            log.warning("launchd agent: unload returned rc=%d; plist deleted anyway", result.returncode)
    else:
        log.warning("launchd plist not found: %s", _LAUNCHD_PATH)


def _restart_launchd() -> None:  # pragma: no cover
    uid = getattr(os, "getuid", lambda: 0)()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{_LABEL}"], check=True)
    log.info("launchd agent restarted")


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def _install_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    # MCPBRAIN_HOME is now embedded directly in the task action via
    # `cmd /c "set MCPBRAIN_HOME=... && mcpbrain daemon"`, so no separate setx is needed.
    args = schtasks_args(mcpbrain_bin=mcpbrain_bin, home=home)
    subprocess.run(args, check=True)
    log.info("Windows scheduled task '%s' created", _TASK_NAME)


def _uninstall_schtasks() -> None:  # pragma: no cover
    subprocess.run(
        ["schtasks", "/delete", "/tn", _TASK_NAME, "/f"],
        check=False,
    )
    log.info("Windows scheduled task '%s' deleted", _TASK_NAME)
    # Remove the persistent user environment variable set during install.
    # check=False so a missing variable (clean uninstall) does not raise.
    subprocess.run(
        ["reg", "delete", r"HKCU\Environment", "/v", "MCPBRAIN_HOME", "/f"],
        check=False,
    )


def _restart_schtasks() -> None:  # pragma: no cover
    subprocess.run(["schtasks", "/end", "/tn", _TASK_NAME], check=False)
    subprocess.run(["schtasks", "/run", "/tn", _TASK_NAME], check=True)
    log.info("Windows scheduled task '%s' restarted", _TASK_NAME)


# ---------------------------------------------------------------------------
# Tray login agent (separate from the daemon agent; optional GUI convenience)
# ---------------------------------------------------------------------------

def install_tray_agent(platform: str, *, mcpbrain_bin: str, home: str) -> None:
    """Write the menu-bar tray login agent and register it with the OS loader."""
    if platform == "darwin":
        _install_launchd_tray(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_schtasks_tray(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def uninstall_tray_agent(platform: str) -> None:
    """Remove the tray login agent and deregister it from the OS loader."""
    if platform == "darwin":
        _uninstall_launchd_tray()
    elif platform == "win32":
        _uninstall_schtasks_tray()
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def _install_launchd_tray(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    plist = launchd_tray_plist(mcpbrain_bin=mcpbrain_bin, home=home)
    _TRAY_LAUNCHD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TRAY_LAUNCHD_PATH.write_text(plist)
    subprocess.run(["launchctl", "load", "-w", str(_TRAY_LAUNCHD_PATH)], check=True)
    log.info("launchd tray agent loaded")


def _uninstall_launchd_tray() -> None:  # pragma: no cover
    if _TRAY_LAUNCHD_PATH.exists():
        subprocess.run(["launchctl", "unload", "-w", str(_TRAY_LAUNCHD_PATH)], check=False)
        _TRAY_LAUNCHD_PATH.unlink(missing_ok=True)
        log.info("launchd tray agent removed")


def _install_schtasks_tray(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    subprocess.run(schtasks_tray_args(mcpbrain_bin=mcpbrain_bin, home=home), check=True)
    log.info("Windows scheduled task '%s' created", _TRAY_TASK_NAME)


def _uninstall_schtasks_tray() -> None:  # pragma: no cover
    subprocess.run(["schtasks", "/delete", "/tn", _TRAY_TASK_NAME, "/f"], check=False)
    log.info("Windows scheduled task '%s' deleted", _TRAY_TASK_NAME)


# -- best-effort tray restarts (called by restart_agent; never fatal) --------

def _restart_launchd_tray() -> None:  # pragma: no cover
    if not _TRAY_LAUNCHD_PATH.exists():
        return  # tray not registered on this machine — nothing to restart
    uid = getattr(os, "getuid", lambda: 0)()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{_TRAY_LABEL}"], check=False)
    log.info("launchd tray agent restarted")


def _restart_schtasks_tray() -> None:  # pragma: no cover
    subprocess.run(["schtasks", "/end", "/tn", _TRAY_TASK_NAME], check=False)
    subprocess.run(["schtasks", "/run", "/tn", _TRAY_TASK_NAME], check=False)
    log.info("Windows scheduled task '%s' restarted", _TRAY_TASK_NAME)


# ---------------------------------------------------------------------------
# records-repo calendar agents (prune_hot_md daily, context_health weekly)
# ---------------------------------------------------------------------------

_PRUNE_LABEL = "com.mcpbrain.records.prune"
_HEALTH_LABEL = "com.mcpbrain.records.context-health"
_FLEET_BEACON_LABEL = "com.mcpbrain.fleet.beacon"


def _calendar_plist(
    *,
    label: str,
    program_args: list[str],
    mcpbrain_home: str,
    hour: int,
    minute: int,
    weekday: int | None = None,
    run_at_load: bool = True,
    env_vars: dict[str, str] | None = None,
) -> str:
    """Return a macOS launchd plist that runs on a StartCalendarInterval schedule.

    run_at_load=True (default) emits RunAtLoad so a run missed while powered off is
    caught up at the next login/boot. Set run_at_load=False for expensive jobs that
    must fire only on schedule (e.g. the weekly gardener's headless claude session)."""
    # Escape XML-special chars (&, <, >) in each arg so shell operators like
    # `&&` survive as well-formed `&amp;&amp;` in the plist. Plain paths are
    # unaffected; this can only ever help args that carry markup-significant chars.
    args_xml = "\n".join(f"        <string>{_xml_escape(a)}</string>" for a in program_args)
    day_key = (
        f"        <key>Weekday</key>\n        <integer>{weekday}</integer>\n"
        if weekday is not None
        else ""
    )
    interval_block = (
        f"{day_key}"
        f"        <key>Hour</key>\n        <integer>{hour}</integer>\n"
        f"        <key>Minute</key>\n        <integer>{minute}</integer>"
    )
    log_path = f"{mcpbrain_home}/{label}.log"
    err_path = f"{mcpbrain_home}/{label}.err"
    run_at_load_block = "    <key>RunAtLoad</key>\n    <true/>\n" if run_at_load else ""
    if env_vars is not None and len(env_vars) > 0:
        entries = "\n".join(
            f"        <key>{k}</key>\n        <string>{_xml_escape(v)}</string>"
            for k, v in env_vars.items()
        )
        env_vars_block = f"    <key>EnvironmentVariables</key>\n    <dict>\n{entries}\n    </dict>\n"
    else:
        env_vars_block = ""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
{run_at_load_block}{env_vars_block}    <key>StartCalendarInterval</key>
    <dict>
{interval_block}
    </dict>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
</dict>
</plist>
"""


def records_prune_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """Return a launchd plist: `mcpbrain records-prune` daily at 06:00."""
    return _calendar_plist(
        label=_PRUNE_LABEL,
        program_args=[mcpbrain_bin, "records-prune"],
        mcpbrain_home=mcpbrain_home,
        hour=6,
        minute=0,
        env_vars={"MCPBRAIN_HOME": mcpbrain_home},
    )


def records_context_health_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """Return a launchd plist: `mcpbrain records-health` weekly Monday at 07:00."""
    return _calendar_plist(
        label=_HEALTH_LABEL,
        program_args=[mcpbrain_bin, "records-health"],
        mcpbrain_home=mcpbrain_home,
        hour=7,
        minute=0,
        weekday=1,
        env_vars={"MCPBRAIN_HOME": mcpbrain_home},
    )


def fleet_beacon_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """Return a launchd plist: `mcpbrain fleet-report --beacon` every hour.

    Uses StartInterval (3600s) rather than a calendar time so the beacon fires
    roughly hourly regardless of wall-clock; RunAtLoad catches up a run missed
    while powered off."""
    label = _FLEET_BEACON_LABEL
    log_path = f"{mcpbrain_home}/{label}.log"
    err_path = f"{mcpbrain_home}/{label}.err"
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_xml_escape(mcpbrain_bin)}</string>
        <string>fleet-report</string>
        <string>--beacon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MCPBRAIN_HOME</key>
        <string>{_xml_escape(mcpbrain_home)}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
</dict>
</plist>
"""


def _cadence_schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str,
                           schedule: list[str]) -> list[str]:
    quoted = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    return ["schtasks", "/create", "/tn", task_name, *schedule,
            "/tr", f"{quoted} {subcommand}", "/f"]


def prune_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    """Return schtasks args to schedule `mcpbrain records-prune` daily at 06:00."""
    return _cadence_schtasks_args(task_name="mcpbrain-records-prune", subcommand="records-prune",
                                  mcpbrain_bin=mcpbrain_bin,
                                  schedule=["/sc", "daily", "/st", "06:00"])


def health_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    """Return schtasks args to schedule `mcpbrain records-health` weekly Monday at 07:00."""
    return _cadence_schtasks_args(task_name="mcpbrain-records-health", subcommand="records-health",
                                  mcpbrain_bin=mcpbrain_bin,
                                  schedule=["/sc", "weekly", "/d", "MON", "/st", "07:00"])


def fleet_beacon_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    """Return schtasks args to run `mcpbrain fleet-report --beacon` hourly."""
    quoted = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    return ["schtasks", "/create", "/tn", "mcpbrain-fleet-beacon",
            "/sc", "hourly", "/tr", f"{quoted} fleet-report --beacon", "/f"]


# ---------------------------------------------------------------------------
# Cross-platform cadence install dispatcher
# ---------------------------------------------------------------------------

def _fleet_configured(home: str) -> bool:
    from mcpbrain import config
    return bool((config.read_config(home).get("fleet") or {}).get("folder_id"))


def _cadence_specs(*, home_fleet_configured: bool, mcpbrain_bin: str, home: str):
    """The (label, plist-thunk) pairs to install on launchd. The beacon pair is
    included only when fleet.folder_id is configured."""
    specs = [
        (_PRUNE_LABEL, lambda: records_prune_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
        (_HEALTH_LABEL, lambda: records_context_health_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
    ]
    if home_fleet_configured:
        specs.append(
            (_FLEET_BEACON_LABEL,
             lambda: fleet_beacon_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)))
    return specs


def install_cadences(platform: str, *, mcpbrain_bin: str, home: str) -> None:
    """Schedule cadences for the given OS.

    - records-prune: daily 06:00
    - records-health: weekly Mon 07:00
    """
    if platform == "darwin":
        _install_cadences_launchd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_cadences_schtasks(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def _install_cadences_launchd(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    from pathlib import Path
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for label, plist_fn in _cadence_specs(
            home_fleet_configured=_fleet_configured(home),
            mcpbrain_bin=mcpbrain_bin, home=home):
        path = agents_dir / f"{label}.plist"
        path.write_text(plist_fn())
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True)


def _install_cadences_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    args_fns = [
        lambda: prune_schtasks_args(mcpbrain_bin=mcpbrain_bin),
        lambda: health_schtasks_args(mcpbrain_bin=mcpbrain_bin),
    ]
    if _fleet_configured(home):
        args_fns.append(lambda: fleet_beacon_schtasks_args(mcpbrain_bin=mcpbrain_bin))
    for args_fn in args_fns:
        subprocess.run(args_fn(), check=True)
