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

# Canonical agent file locations by platform.
_LAUNCHD_PATH = Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"
_SYSTEMD_PATH = Path.home() / ".config" / "systemd" / "user" / "mcpbrain.service"
_TASK_NAME = "mcpbrain"


# ---------------------------------------------------------------------------
# Pure generators
# ---------------------------------------------------------------------------

def launchd_plist(*, mcpbrain_bin: str, home: str) -> str:
    """Return a macOS launchd plist that runs ``mcpbrain daemon`` at login."""
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{mcpbrain_bin}</string>
        <string>daemon</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MCPBRAIN_HOME</key>
        <string>{home}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
"""


def systemd_unit(*, mcpbrain_bin: str, home: str) -> str:
    """Return a systemd user unit that runs ``mcpbrain daemon`` at login."""
    return f"""\
[Unit]
Description=mcpbrain background daemon
After=network.target

[Service]
ExecStart={mcpbrain_bin} daemon
Environment=MCPBRAIN_HOME={home}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return the schtasks.exe argument list that registers ``mcpbrain daemon`` at logon.

    The /tr value uses conditional quoting: if ``mcpbrain_bin`` contains
    whitespace the path is wrapped in double-quotes so Task Scheduler handles
    it correctly; paths without whitespace are left bare.
    """
    quoted_bin = f'"{mcpbrain_bin}"' if any(c.isspace() for c in mcpbrain_bin) else mcpbrain_bin
    task_run = f"{quoted_bin} daemon"
    return [
        "schtasks",
        "/create",
        "/tn", _TASK_NAME,
        "/sc", "onlogon",
        "/tr", task_run,
        "/f",
    ]


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
