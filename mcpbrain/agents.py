"""OS-native login-agent generators for the mcpbrain daemon.

Pure generator functions (launchd_plist, systemd_unit, schtasks_args) produce
the text or argument list needed to register ``mcpbrain daemon`` as a login
agent on macOS, Linux, or Windows. They are fully unit-tested and have no
side effects.

The install/uninstall/restart helpers write those definitions to the canonical
OS path and invoke the system loader. Their subprocess/loader bodies are marked
``# pragma: no cover`` because they require a real OS environment.
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
_SYSTEMD_PATH = Path.home() / ".config" / "systemd" / "user" / "mcpbrain.service"
_TRAY_SYSTEMD_PATH = Path.home() / ".config" / "systemd" / "user" / "mcpbrain-tray.service"
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


def _systemd_unit(*, description: str, subcommand: str, mcpbrain_bin: str, home: str,
                  restart_on_failure: bool) -> str:
    restart = "Restart=on-failure\nRestartSec=5\n" if restart_on_failure else "Restart=no\n"
    return f"""\
[Unit]
Description={description}
After=network.target

[Service]
ExecStart={mcpbrain_bin} {subcommand}
Environment=MCPBRAIN_HOME={home}
{restart}
[Install]
WantedBy=default.target
"""


def systemd_unit(*, mcpbrain_bin: str, home: str) -> str:
    """Return a systemd user unit that runs ``mcpbrain daemon`` at login."""
    return _systemd_unit(description="mcpbrain background daemon", subcommand="daemon",
                         mcpbrain_bin=mcpbrain_bin, home=home, restart_on_failure=True)


def systemd_tray_unit(*, mcpbrain_bin: str, home: str) -> str:
    """Return a systemd user unit that runs ``mcpbrain tray`` at login."""
    return _systemd_unit(description="mcpbrain menu-bar tray", subcommand="tray",
                         mcpbrain_bin=mcpbrain_bin, home=home, restart_on_failure=False)


def _schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str) -> list[str]:
    # Conditional quoting: wrap a whitespace-containing path so Task Scheduler
    # parses it correctly; leave bare paths unquoted.
    quoted_bin = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    return [
        "schtasks",
        "/create",
        "/tn", task_name,
        "/sc", "onlogon",
        "/tr", f"{quoted_bin} {subcommand}",
        "/f",
    ]


def schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return the schtasks.exe argument list that registers ``mcpbrain daemon`` at logon."""
    return _schtasks_args(task_name=_TASK_NAME, subcommand="daemon", mcpbrain_bin=mcpbrain_bin)


def schtasks_tray_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return the schtasks.exe argument list that registers ``mcpbrain tray`` at logon."""
    return _schtasks_args(task_name=_TRAY_TASK_NAME, subcommand="tray", mcpbrain_bin=mcpbrain_bin)


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
    elif platform == "linux":
        _install_systemd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_schtasks(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def uninstall_agent(platform: str) -> None:
    """Remove the agent definition and deregister it from the OS loader."""
    if platform == "darwin":
        _uninstall_launchd()
    elif platform == "linux":
        _uninstall_systemd()
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
    elif platform == "linux":
        _restart_systemd()
        _restart_systemd_tray()
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
# Linux helpers
# ---------------------------------------------------------------------------

def _install_systemd(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    unit = systemd_unit(mcpbrain_bin=mcpbrain_bin, home=home)
    _SYSTEMD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SYSTEMD_PATH.write_text(unit)
    log.info("wrote %s", _SYSTEMD_PATH)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "mcpbrain.service"], check=True)
    log.info("systemd user service enabled and started")


def _uninstall_systemd() -> None:  # pragma: no cover
    subprocess.run(["systemctl", "--user", "disable", "--now", "mcpbrain.service"], check=False)
    _SYSTEMD_PATH.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    log.info("systemd user service removed")


def _restart_systemd() -> None:  # pragma: no cover
    subprocess.run(["systemctl", "--user", "restart", "mcpbrain.service"], check=True)
    log.info("systemd user service restarted")


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def _install_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    args = schtasks_args(mcpbrain_bin=mcpbrain_bin, home=home)
    # Pass MCPBRAIN_HOME via the environment of the spawning process; the task
    # inherits the user environment at logon, so set it as a persistent user env var.
    subprocess.run(
        ["setx", "MCPBRAIN_HOME", home],
        check=True,
    )
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
    elif platform == "linux":
        _install_systemd_tray(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_schtasks_tray(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def uninstall_tray_agent(platform: str) -> None:
    """Remove the tray login agent and deregister it from the OS loader."""
    if platform == "darwin":
        _uninstall_launchd_tray()
    elif platform == "linux":
        _uninstall_systemd_tray()
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


def _install_systemd_tray(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    unit = systemd_tray_unit(mcpbrain_bin=mcpbrain_bin, home=home)
    _TRAY_SYSTEMD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _TRAY_SYSTEMD_PATH.write_text(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "mcpbrain-tray.service"], check=True)
    log.info("systemd tray user service enabled and started")


def _uninstall_systemd_tray() -> None:  # pragma: no cover
    subprocess.run(["systemctl", "--user", "disable", "--now", "mcpbrain-tray.service"], check=False)
    _TRAY_SYSTEMD_PATH.unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    log.info("systemd tray user service removed")


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


def _restart_systemd_tray() -> None:  # pragma: no cover
    if not _TRAY_SYSTEMD_PATH.exists():
        return
    subprocess.run(["systemctl", "--user", "restart", "mcpbrain-tray.service"], check=False)
    log.info("systemd tray user service restarted")


def _restart_schtasks_tray() -> None:  # pragma: no cover
    subprocess.run(["schtasks", "/end", "/tn", _TRAY_TASK_NAME], check=False)
    subprocess.run(["schtasks", "/run", "/tn", _TRAY_TASK_NAME], check=False)
    log.info("Windows scheduled task '%s' restarted", _TRAY_TASK_NAME)


# ---------------------------------------------------------------------------
# records-repo calendar agents (prune_hot_md daily, context_health weekly)
# ---------------------------------------------------------------------------

_PRUNE_LABEL = "com.mcpbrain.records.prune"
_HEALTH_LABEL = "com.mcpbrain.records.context-health"
_MEETING_PACKS_LABEL = "com.mcpbrain.records.meeting-packs"
_GARDENER_LABEL = "com.mcpbrain.records.gardener"


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


def meeting_packs_plist(*, home: str, mcpbrain_bin: str) -> str:
    """Return a launchd plist: `mcpbrain meeting-packs` twice daily (07:45 + 12:00)."""
    home_x = _xml_escape(home)
    bin_x = _xml_escape(mcpbrain_bin)
    label = _MEETING_PACKS_LABEL
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
        <string>{bin_x}</string>
        <string>meeting-packs</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MCPBRAIN_HOME</key>
        <string>{home_x}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>StartCalendarInterval</key>
    <array>
        <dict>
            <key>Hour</key>
            <integer>7</integer>
            <key>Minute</key>
            <integer>45</integer>
        </dict>
        <dict>
            <key>Hour</key>
            <integer>12</integer>
            <key>Minute</key>
            <integer>0</integer>
        </dict>
    </array>
    <key>StandardOutPath</key>
    <string>{home_x}/{label}.log</string>
    <key>StandardErrorPath</key>
    <string>{home_x}/{label}.err</string>
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


def _timer_units(*, label_desc: str, subcommand: str, mcpbrain_bin: str, home: str,
                 on_calendar: str) -> tuple[str, str]:
    service = (f"[Unit]\nDescription={label_desc}\n\n[Service]\nType=oneshot\n"
               f"ExecStart={mcpbrain_bin} {subcommand}\nEnvironment=MCPBRAIN_HOME={home}\n")
    timer = (f"[Unit]\nDescription={label_desc} timer\n\n[Timer]\n"
             f"OnCalendar={on_calendar}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    return service, timer


def prune_timer_units(*, mcpbrain_bin: str, home: str) -> tuple[str, str]:
    """Return (service, timer) systemd unit strings for `mcpbrain records-prune` daily 06:00."""
    return _timer_units(label_desc="mcpbrain records prune", subcommand="records-prune",
                        mcpbrain_bin=mcpbrain_bin, home=home, on_calendar="*-*-* 06:00:00")


def health_timer_units(*, mcpbrain_bin: str, home: str) -> tuple[str, str]:
    """Return (service, timer) systemd unit strings for `mcpbrain records-health` Mon 07:00."""
    return _timer_units(label_desc="mcpbrain records health", subcommand="records-health",
                        mcpbrain_bin=mcpbrain_bin, home=home, on_calendar="Mon *-*-* 07:00:00")


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


def records_gardener_plist(*, mcpbrain_bin: str, mcpbrain_home: str) -> str:
    """Return a launchd plist: `mcpbrain records-gardener` weekly Monday 08:00 (no RunAtLoad)."""
    return _calendar_plist(
        label=_GARDENER_LABEL,
        program_args=[mcpbrain_bin, "records-gardener"],
        mcpbrain_home=mcpbrain_home,
        hour=8,
        minute=0,
        weekday=1,
        run_at_load=False,
        env_vars={"MCPBRAIN_HOME": mcpbrain_home},
    )


# ---------------------------------------------------------------------------
# Cross-platform cadence install dispatcher
# ---------------------------------------------------------------------------

def install_cadences(platform: str, *, mcpbrain_bin: str, home: str) -> None:
    """Schedule records-prune (daily) and records-health (weekly) for the given OS."""
    if platform == "darwin":
        _install_cadences_launchd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "linux":
        _install_cadences_systemd(mcpbrain_bin=mcpbrain_bin, home=home)
    elif platform == "win32":
        _install_cadences_schtasks(mcpbrain_bin=mcpbrain_bin, home=home)
    else:
        raise ValueError(f"Unsupported platform: {platform!r}")


def _install_cadences_launchd(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    from pathlib import Path
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for label, plist_fn in [
        (_PRUNE_LABEL, lambda: records_prune_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
        (_HEALTH_LABEL, lambda: records_context_health_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
        (_GARDENER_LABEL, lambda: records_gardener_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
        (_MEETING_PACKS_LABEL, lambda: meeting_packs_plist(mcpbrain_bin=mcpbrain_bin, home=home)),
    ]:
        path = agents_dir / f"{label}.plist"
        path.write_text(plist_fn())
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True)


def _install_cadences_systemd(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    from pathlib import Path
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, fn in [
        ("mcpbrain-records-prune", lambda: prune_timer_units(mcpbrain_bin=mcpbrain_bin, home=home)),
        ("mcpbrain-records-health", lambda: health_timer_units(mcpbrain_bin=mcpbrain_bin, home=home)),
        ("mcpbrain-records-gardener", lambda: gardener_timer_units(mcpbrain_bin=mcpbrain_bin, home=home)),
        ("mcpbrain-meeting-packs", lambda: meeting_packs_timer_units(mcpbrain_bin=mcpbrain_bin, home=home)),
    ]:
        service, timer = fn()
        (unit_dir / f"{name}.service").write_text(service)
        (unit_dir / f"{name}.timer").write_text(timer)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"], check=True)


def _install_cadences_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    for args_fn in [
        lambda: prune_schtasks_args(mcpbrain_bin=mcpbrain_bin),
        lambda: health_schtasks_args(mcpbrain_bin=mcpbrain_bin),
        lambda: gardener_schtasks_args(mcpbrain_bin=mcpbrain_bin),
    ]:
        subprocess.run(args_fn(), check=True)
    for task_args in meeting_packs_schtasks_args(mcpbrain_bin=mcpbrain_bin):
        subprocess.run(task_args, check=True)


# ---------------------------------------------------------------------------
# Cowork cadence generators (gardener + meeting-packs)
# ---------------------------------------------------------------------------

def gardener_timer_units(*, mcpbrain_bin: str, home: str) -> tuple[str, str]:
    """Return (service, timer) systemd unit strings for `mcpbrain records-gardener` Mon 08:00."""
    return _timer_units(label_desc="mcpbrain records gardener", subcommand="records-gardener",
                        mcpbrain_bin=mcpbrain_bin, home=home, on_calendar="Mon *-*-* 08:00:00")


def meeting_packs_timer_units(*, mcpbrain_bin: str, home: str) -> tuple[str, str]:
    """Return (service, timer) systemd unit strings for meeting-packs (07:45 + 12:00)."""
    service = (f"[Unit]\nDescription=mcpbrain meeting packs\n\n[Service]\nType=oneshot\n"
               f"ExecStart={mcpbrain_bin} meeting-packs\nEnvironment=MCPBRAIN_HOME={home}\n")
    timer = ("[Unit]\nDescription=mcpbrain meeting packs timer\n\n[Timer]\n"
             "OnCalendar=*-*-* 07:45:00\nOnCalendar=*-*-* 12:00:00\nPersistent=true\n\n"
             "[Install]\nWantedBy=timers.target\n")
    return service, timer


def gardener_schtasks_args(*, mcpbrain_bin: str) -> list[str]:
    """Return schtasks args to schedule `mcpbrain records-gardener` weekly Monday at 08:00."""
    return _cadence_schtasks_args(task_name="mcpbrain-records-gardener",
                                  subcommand="records-gardener", mcpbrain_bin=mcpbrain_bin,
                                  schedule=["/sc", "weekly", "/d", "MON", "/st", "08:00"])


def meeting_packs_schtasks_args(*, mcpbrain_bin: str) -> list[list[str]]:
    """Return two schtasks arg-lists for meeting-packs (07:45 am + 12:00 pm)."""
    return [
        _cadence_schtasks_args(task_name="mcpbrain-meeting-packs-am", subcommand="meeting-packs",
                               mcpbrain_bin=mcpbrain_bin, schedule=["/sc", "daily", "/st", "07:45"]),
        _cadence_schtasks_args(task_name="mcpbrain-meeting-packs-pm", subcommand="meeting-packs",
                               mcpbrain_bin=mcpbrain_bin, schedule=["/sc", "daily", "/st", "12:00"]),
    ]
