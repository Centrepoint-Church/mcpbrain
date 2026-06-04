"""OS-native login-agent generators for the mcpbrain daemon.

Pure generator functions (launchd_plist, systemd_unit, schtasks_args) produce
the text or argument list needed to register ``mcpbrain daemon`` as a login
agent on macOS, Linux, or Windows. They are fully unit-tested and have no
side effects.

The install/uninstall/restart helpers write those definitions to the canonical
OS path and invoke the system loader. Their subprocess/loader bodies are marked
``# pragma: no cover`` because they require a real OS environment.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# Bundle identifier used as the launchd label and scheduled-task name.
_LABEL = "church.centrepoint.mcpbrain"
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

def _launchd_plist(*, label: str, subcommand: str, mcpbrain_bin: str, home: str, keep_alive: bool) -> str:
    keep = "true" if keep_alive else "false"
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
    <{keep}/>
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

    KeepAlive is false: quitting the menu-bar icon must not respawn it; it
    returns at the next login. The daemon agent (separate) keeps KeepAlive true.
    """
    return _launchd_plist(label=_TRAY_LABEL, subcommand="tray", mcpbrain_bin=mcpbrain_bin,
                          home=home, keep_alive=False)


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
    """Restart the running agent via the OS loader."""
    if platform == "darwin":
        _restart_launchd()
    elif platform == "linux":
        _restart_systemd()
    elif platform == "win32":
        _restart_schtasks()
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
