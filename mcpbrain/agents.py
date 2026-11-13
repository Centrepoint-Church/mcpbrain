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
_WIN_SHIM_DIR = "agents"  # app_dir()/agents/<task>.vbs

# The waiting shim propagates the daemon's exit code via WScript.Quit; only that
# variant lets Task Scheduler's RestartOnFailure ever see a failure. Used by
# win_supervised() to tell a real XML registration from the /TR fallback.
_WAITING_SHIM_MARKER = "WScript.Quit"


# ---------------------------------------------------------------------------
# Pure generators
# ---------------------------------------------------------------------------

def _launchd_plist(*, label: str, subcommand: str, mcpbrain_bin: str, home: str, keep_alive,
                   run_at_load: bool = True) -> str:
    # keep_alive: True -> always relaunch; False -> never; "crashonly" -> relaunch
    # only on an abnormal (non-zero) exit. "crashonly" is for the tray: a clean
    # Quit (exit 0) must stay quit, but a crash should bring the icon back.
    # run_at_load=False only means "not at login" when keep_alive is False too:
    # launchd.plist(5) says any KeepAlive (including a SuccessfulExit dict)
    # implies RunAtLoad.
    if keep_alive == "crashonly":
        keep_xml = ("<dict>\n        <key>SuccessfulExit</key>\n"
                    "        <false/>\n    </dict>")
    else:
        keep_xml = "<true/>" if keep_alive else "<false/>"
    run_xml = "<true/>" if run_at_load else "<false/>"
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
    {run_xml}
    <key>KeepAlive</key>
    {keep_xml}
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{err_path}</string>
</dict>
</plist>
"""


def launchd_plist(*, mcpbrain_bin: str, home: str, start_at_login: bool = True) -> str:
    """Return a macOS launchd plist that runs ``mcpbrain daemon``.

    start_at_login=True (the default): RunAtLoad and KeepAlive true, so the
    daemon starts at login and launchd relaunches it on any exit.

    start_at_login=False: RunAtLoad and KeepAlive both false. KeepAlive has to
    go too, because launchd treats any KeepAlive, even the crash-only
    {SuccessfulExit: false} dict, as implying RunAtLoad (launchd.plist(5)).
    The job stays loaded, so the tray's "Start mcpbrain" (launchctl kickstart)
    still starts it on demand; nothing relaunches it after an exit.
    """
    return _launchd_plist(label=_LABEL, subcommand="daemon", mcpbrain_bin=mcpbrain_bin,
                          home=home, keep_alive=bool(start_at_login),
                          run_at_load=bool(start_at_login))


def launchd_tray_plist(*, mcpbrain_bin: str, home: str) -> str:
    """Return a macOS launchd plist that runs ``mcpbrain tray`` at login.

    KeepAlive is "crashonly" (KeepAlive={SuccessfulExit: false}): a clean Quit
    (exit 0) from the menu-bar icon stays quit until the next login, but a crash
    (non-zero exit) relaunches the icon so it doesn't silently disappear. The
    daemon agent (separate) keeps KeepAlive true.
    """
    return _launchd_plist(label=_TRAY_LABEL, subcommand="tray", mcpbrain_bin=mcpbrain_bin,
                          home=home, keep_alive="crashonly")


def _win_shim_content(*, mcpbrain_bin: str, home: str, subcommand: str, wait: bool = False) -> str:
    """A .vbs that runs `mcpbrain <subcommand>` with a hidden console via the
    ABSOLUTE installed launcher path (resolved by setup). Window style 0 hides the
    console; VBScript escapes a double-quote by doubling it.

    wait=False (default) is the original fire-and-forget form: `sh.Run "...", 0,
    False` returns immediately with wscript exiting 0 regardless of the child's
    outcome. This is what every caller EXCEPT the schtasks-XML daemon
    registration wants: the Startup-folder "start now" call
    (_install_startup_shortcut) launches a process that never returns
    (`mcpbrain daemon`/`tray`), so a waiting shim there would hang that
    subprocess.run call forever; the tray's and cadences' plain-CLI schtasks
    registrations have no RestartOnFailure to feed anyway, and (for the tray/
    daemon-CLI-fallback, both long-lived) waiting would additionally expose
    them to Task Scheduler's un-set-by-us defaults (StopIfGoingOnBatteries=true,
    ExecutionTimeLimit=PT72H) for no benefit.

    wait=True is for the ONE caller that both needs it and is safe with it: the
    daemon's schtasks-XML on-logon registration, whose <Settings> explicitly
    turns those two defaults off and which sets RestartOnFailure -- a setting
    that can only ever fire if Task Scheduler sees the daemon's REAL exit code.
    `sh.Run(...)` (function-call form, not the fire-and-forget statement form)
    returns the child's exit code, which is then propagated as wscript's own
    exit code via `WScript.Quit` -- the statement form discards that return
    value entirely, so capturing it is what actually makes this work, not the
    wait flag by itself.
    """
    bin_q = '""' + mcpbrain_bin + '""'
    home_esc = home.replace('"', '""')
    run_line = (
        f'rc = sh.Run("{bin_q} {subcommand}", 0, True)\r\n'
        'WScript.Quit rc\r\n'
        if wait else
        f'sh.Run "{bin_q} {subcommand}", 0, False\r\n'
    )
    return (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Environment("PROCESS")("MCPBRAIN_HOME") = "{home_esc}"\r\n'
        f'{run_line}'
    )


def win_persistence_mechanism(probe: bool | None = None) -> str:
    """Return the run-at-logon mechanism for Windows: 'schtasks' if Task
    Scheduler is usable, else 'startup' (Startup-folder shortcut).

    probe=True/False injects a known scheduler-availability result (tests /
    preflight). probe=None probes now via _scheduler_available()."""
    available = _scheduler_available() if probe is None else probe
    return "schtasks" if available else "startup"


def _scheduler_available() -> bool:  # pragma: no cover — touches schtasks
    """True if we can create+delete a task. Access-denied (policy block) → False."""
    probe_name = "mcpbrain-probe"
    try:
        r = subprocess.run(["schtasks", "/create", "/tn", probe_name, "/sc", "onlogon",
                            "/tr", "cmd /c exit", "/f"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return False
        subprocess.run(["schtasks", "/delete", "/tn", probe_name, "/f"],
                       capture_output=True, text=True)
        return True
    except OSError:
        return False


def startup_shortcut_target(*, shim_path) -> tuple[str, str]:
    """(-> wscript.exe path, arguments) for a Startup-folder .lnk that runs the
    hidden-console shim. Pure so it is unit-testable."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    wscript = str(Path(system_root) / "System32" / "wscript.exe")
    return wscript, f'"{shim_path}"'


def _startup_shortcut_path(task_name: str) -> Path:
    """Path to a task's Startup-folder `.lnk` (the scheduler-blocked fallback).
    Pure — unit-testable by setting APPDATA. Used by both install and uninstall so
    the shortcut is created and removed at the same location."""
    appdata = os.environ.get("APPDATA", "")
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup" / f"{task_name}.lnk")


def _win_shim_path(home: str, task_name: str) -> Path:
    return Path(home) / _WIN_SHIM_DIR / f"{task_name}.vbs"


def win_supervised(home: str) -> bool:
    """True when a watchdog exit will actually be restarted by Windows.

    Only the XML-registered task carries `RestartOnFailure`, and only that path
    installs the WAITING shim (`sh.Run ..., True`) that keeps the task instance
    alive for the daemon's lifetime so its exit code reaches Task Scheduler at
    all. If XML registration fails, install_agent falls back to the plain /TR
    form and reverts the shim to the non-waiting variant — the daemon still
    starts at logon but nothing restarts it.

    Deriving supervision from the installed shim rather than a separate marker
    means there is no second piece of state to drift out of sync with reality:
    the artefact that determines the behaviour is the artefact we inspect.
    A missing shim, or the Startup-folder mechanism, is never supervised.
    """
    if win_persistence_mechanism() != "schtasks":
        return False
    try:
        content = _win_shim_path(home, _TASK_NAME).read_text()
    except OSError:
        return False
    return _WAITING_SHIM_MARKER in content


def _schtasks_tr_for_shim(shim_path: Path) -> str:
    return f'wscript "{shim_path}"'


def _schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str, home: str) -> list[str]:
    """schtasks args registering an on-logon task that launches via a hidden-console
    .vbs shim — no visible window, no child-process console flashes."""
    shim_path = _win_shim_path(home, task_name)
    tr = _schtasks_tr_for_shim(shim_path)
    return ["schtasks", "/create", "/tn", task_name, "/sc", "onlogon", "/tr", tr, "/f"]


def schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return the schtasks.exe argument list that registers ``mcpbrain daemon`` at logon."""
    return _schtasks_args(task_name=_TASK_NAME, subcommand="daemon", mcpbrain_bin=mcpbrain_bin, home=home)


def schtasks_xml(*, shim_path) -> str:
    """On-logon task XML with RestartOnFailure.

    The CLI's /RI flag cannot express restart-on-failure for an on-logon task,
    so registration goes through /XML instead. Without this, a watchdog exit on
    Windows would kill the daemon until the next logon — strictly worse than the
    stall it is recovering from.

    Two further defects this fixes (verified by reading the previously-generated
    XML): Exec is CreateProcess under the hood and cannot execute a .vbs
    directly, so <Command> must be wscript.exe with the .vbs path passed as an
    <Arguments> element rather than as the command itself; and Actions
    Context="Author" requires a matching <Principal id="Author"> inside a
    <Principals> block (with <RegistrationInfo> alongside it), or the task
    definition is invalid.
    """
    shim_path_xml = _xml_escape(str(shim_path))
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Author>mcpbrain</Author>\n"
        "  </RegistrationInfo>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>\n"
        "  <Settings>\n"
        "    <RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>wscript.exe</Command>\n"
        f'      <Arguments>"{shim_path_xml}"</Arguments>\n'
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


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


def uninstall_agent(platform: str, *, home: str | None = None) -> None:
    """Remove the agent definition and deregister it from the OS loader."""
    if platform == "darwin":
        _uninstall_launchd()
    elif platform == "win32":
        _uninstall_schtasks(home=home)
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


class LoginAgentError(RuntimeError):
    """The login agent could not be changed (the reason is the message)."""


def _rc(res) -> int:
    return int(getattr(res, "returncode", 1))


def _win_task_exists(run) -> bool:
    return _rc(run(["schtasks", "/query", "/tn", _TASK_NAME],
                   check=False, capture_output=True, text=True)) == 0


def set_login_start(platform: str, *, home: str, on: bool, run=None,
                    plist_path: Path | None = None) -> bool:
    """Point the daemon's login agent at the owner's "Start when I log in" choice.

    Takes effect at the next login and leaves the running daemon alone:
    - darwin rewrites the installed plist (RunAtLoad/KeepAlive, see
      launchd_plist) without reloading it; launchd reads it again at login.
      The installed binary and MCPBRAIN_HOME are carried over from the plist.
    - win32 with a scheduled task runs `schtasks /change /tn mcpbrain
      /enable|/disable`; with the Startup-folder fallback (shim, no task) it
      removes the shortcut for off and recreates it for on.

    Returns True when an installed agent was changed, False when no agent is
    installed (nothing to change; the caller still records the choice).
    Raises LoginAgentError when the change failed. `run` and `plist_path` are
    injectable for tests.
    """
    run = run or subprocess.run
    if platform == "darwin":
        import plistlib
        path = Path(plist_path) if plist_path is not None else _LAUNCHD_PATH
        if not path.exists():
            log.info("start-at-login: no daemon plist at %s; config only", path)
            return False
        try:
            cur = plistlib.loads(path.read_bytes())
            mcpbrain_bin = cur["ProgramArguments"][0]
            agent_home = (cur.get("EnvironmentVariables") or {}).get("MCPBRAIN_HOME") or str(home)
        except Exception as exc:  # noqa: BLE001 — unreadable plist: rebuild from what we know
            log.warning("start-at-login: could not parse %s (%s); rebuilding", path, exc)
            from mcpbrain import setup as _setup
            mcpbrain_bin, agent_home = _setup._mcpbrain_bin(), str(home)
        try:
            path.write_text(launchd_plist(mcpbrain_bin=mcpbrain_bin, home=agent_home,
                                          start_at_login=on))
        except OSError as exc:
            raise LoginAgentError(f"could not write {path.name}: {exc}") from exc
        log.info("start-at-login %s: rewrote %s (applies at next login)",
                 "on" if on else "off", path)
        return True
    if platform == "win32":
        if _win_task_exists(run):
            res = run(["schtasks", "/change", "/tn", _TASK_NAME,
                       "/enable" if on else "/disable"],
                      check=False, capture_output=True, text=True)
            if _rc(res) != 0:
                why = (getattr(res, "stderr", "") or getattr(res, "stdout", "") or "").strip()
                raise LoginAgentError(f"schtasks /change failed: {why or 'exit ' + str(_rc(res))}")
            return True
        shim = _win_shim_path(str(home), _TASK_NAME)
        if not shim.exists():
            log.info("start-at-login: no scheduled task or shim; config only")
            return False
        lnk = _startup_shortcut_path(_TASK_NAME)
        if on:
            _create_startup_shortcut(_TASK_NAME, shim_path=shim, run=run)
        else:
            try:
                lnk.unlink(missing_ok=True)
            except OSError as exc:
                raise LoginAgentError(f"could not remove the Startup shortcut: {exc}") from exc
        return True
    raise ValueError(f"Unsupported platform: {platform!r}")


def start_agent(platform: str, *, home: str | None = None, run=None) -> bool:
    """Start the daemon through its login agent. Daemon only, and never kills.

    For the tray's "Start mcpbrain" and the Windows restart helper; unlike
    restart_agent it never touches the tray and never runs taskkill (which on
    Windows would kill the calling mcpbrain.exe too).
    - darwin: `launchctl kickstart gui/<uid>/com.mcpbrain` (no -k). If the job
      is not loaded, load the plist and try once more.
    - win32: `schtasks /run` the daemon task. If login start is off the task
      is disabled, so enable it, run it, and disable it again to keep the
      owner's choice. With the Startup-folder install (no task) run the shim.
    Returns True when the start was issued successfully.
    """
    run = run or subprocess.run
    from mcpbrain import config
    home = str(home) if home is not None else str(config.app_dir())
    if platform == "darwin":
        uid = getattr(os, "getuid", lambda: 0)()
        kick = ["launchctl", "kickstart", f"gui/{uid}/{_LABEL}"]
        if _rc(run(kick, check=False)) == 0:
            return True
        if _LAUNCHD_PATH.exists():
            run(["launchctl", "load", str(_LAUNCHD_PATH)], check=False)
            return _rc(run(kick, check=False)) == 0
        return False
    if platform == "win32":
        if _win_task_exists(run):
            disabled = not config.start_at_login(home)
            if disabled:
                run(["schtasks", "/change", "/tn", _TASK_NAME, "/enable"], check=False)
            try:
                ok = _rc(run(["schtasks", "/run", "/tn", _TASK_NAME], check=False)) == 0
            finally:
                if disabled:
                    run(["schtasks", "/change", "/tn", _TASK_NAME, "/disable"], check=False)
            return ok
        shim = _win_shim_path(home, _TASK_NAME)
        if shim.exists():
            wscript, _args = startup_shortcut_target(shim_path=shim)
            return _rc(run([wscript, str(shim)], check=False)) == 0
        return False
    raise ValueError(f"Unsupported platform: {platform!r}")


# ---------------------------------------------------------------------------
# macOS helpers
# ---------------------------------------------------------------------------

def _install_launchd(*, mcpbrain_bin: str, home: str) -> None:
    # A reinstall or update keeps the owner's "Start when I log in" choice.
    from mcpbrain import config
    plist = launchd_plist(mcpbrain_bin=mcpbrain_bin, home=home,
                          start_at_login=config.start_at_login(home))
    _LAUNCHD_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LAUNCHD_PATH.write_text(plist)
    log.info("wrote %s", _LAUNCHD_PATH)
    subprocess.run(["launchctl", "load", "-w", str(_LAUNCHD_PATH)], check=True)
    log.info("launchd agent loaded")
    if not config.start_at_login(home):
        # RunAtLoad is false, so loading does not start it; setup and doctor's
        # agent repair wait for the daemon to come up, so start it now.
        start_agent("darwin", home=home)


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

def _register_schtasks_xml(*, task_name: str, shim_path: Path) -> None:  # pragma: no cover
    """Register an on-logon task via /XML instead of /TR + /SC ONLOGON.

    The CLI flags cannot express RestartOnFailure for an on-logon trigger, so the
    task is created from the schtasks_xml() document via a temp file instead. See
    schtasks_xml for why this matters to the watchdog (Task 5).
    """
    import tempfile
    xml = schtasks_xml(shim_path=shim_path)
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        subprocess.run(["schtasks", "/create", "/tn", task_name, "/xml", xml_path, "/f"],
                       check=True)
    finally:
        Path(xml_path).unlink(missing_ok=True)


def _install_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    shim_path = _win_shim_path(home, _TASK_NAME)
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    # Non-waiting by default: this is the shim _install_startup_shortcut's
    # "start now" call fires directly (see below), and also what the CLI
    # fallback below ends up using if XML registration fails -- both need
    # wscript to return immediately, not block for the daemon's lifetime.
    shim_path.write_text(_win_shim_content(mcpbrain_bin=mcpbrain_bin, home=home,
                                           subcommand="daemon", wait=False))
    log.info("wrote Windows shim %s", shim_path)
    if win_persistence_mechanism() == "schtasks":
        try:
            # Only the XML-registered task carries RestartOnFailure, so only
            # this path gets the waiting/exit-code-propagating shim variant --
            # switch the shim to it before registering.
            shim_path.write_text(_win_shim_content(mcpbrain_bin=mcpbrain_bin, home=home,
                                                   subcommand="daemon", wait=True))
            _register_schtasks_xml(task_name=_TASK_NAME, shim_path=shim_path)
            log.info("Windows scheduled task '%s' created (RestartOnFailure)", _TASK_NAME)
        except Exception:
            # Malformed/rejected XML must not abort the whole install (the previous
            # behaviour, via check=True) -- fall back to the plain /TR registration
            # so the daemon still starts at logon, and log loudly since this
            # fallback silently loses RestartOnFailure supervision. Revert the
            # shim to non-waiting first: without RestartOnFailure to feed, a
            # waiting shim here would only expose this long-lived on-logon task
            # to Task Scheduler's un-set-by-us defaults (StopIfGoingOnBatteries,
            # a 72h ExecutionTimeLimit) for no benefit.
            log.exception(
                "schtasks /xml registration of '%s' failed; falling back to the "
                "plain /TR form -- the on-logon task will start the daemon but "
                "will NOT restart it on a watchdog exit until this is fixed",
                _TASK_NAME)
            shim_path.write_text(_win_shim_content(mcpbrain_bin=mcpbrain_bin, home=home,
                                                   subcommand="daemon", wait=False))
            subprocess.run(schtasks_args(mcpbrain_bin=mcpbrain_bin, home=home), check=True)
            log.info("Windows scheduled task '%s' created via CLI fallback", _TASK_NAME)
    else:
        _install_startup_shortcut(_TASK_NAME, shim_path=shim_path)
        log.info("Task Scheduler blocked; installed Startup-folder shortcut for '%s'", _TASK_NAME)
    _apply_login_choice_after_install(home)


def _apply_login_choice_after_install(home: str, run=None) -> None:
    """After a Windows (re)install, keep the owner's "Start when I log in" choice.

    The install registers the agent enabled (and the Startup-folder path starts
    the daemon itself). With login start off, switch the agent off again and,
    for the scheduled-task path, start the daemon now (start_agent briefly
    re-enables the task to run it), since setup and doctor wait for it.
    """
    from mcpbrain import config
    if config.start_at_login(home):
        return
    run = run or subprocess.run
    task = _win_task_exists(run)
    try:
        set_login_start("win32", home=home, on=False, run=run)
    except LoginAgentError:
        log.exception("could not switch the login agent off after install")
    if task:
        start_agent("win32", home=home, run=run)


def _create_startup_shortcut(task_name, *, shim_path, run=None) -> None:
    """Write the Startup-folder .lnk that runs the shim at logon (no start)."""
    run = run or subprocess.run
    wscript, args = startup_shortcut_target(shim_path=shim_path)
    lnk = _startup_shortcut_path(task_name)
    ps = (f"$s=New-Object -ComObject WScript.Shell;"
          f"$sc=$s.CreateShortcut('{lnk}');$sc.TargetPath='{wscript}';"
          f"$sc.Arguments='{args}';$sc.Save()")
    res = run(["powershell", "-NoProfile", "-Command", ps], check=False)
    if _rc(res) != 0:
        raise LoginAgentError(f"could not create the Startup shortcut (exit {_rc(res)})")


def _install_startup_shortcut(task_name, *, shim_path) -> None:  # pragma: no cover
    try:
        _create_startup_shortcut(task_name, shim_path=shim_path)
    except LoginAgentError as exc:
        raise subprocess.CalledProcessError(1, "powershell") from exc
    wscript, _args = startup_shortcut_target(shim_path=shim_path)
    subprocess.run([wscript, str(shim_path)], check=False)   # start now


def _uninstall_schtasks(home: str | None = None) -> None:  # pragma: no cover
    subprocess.run(["schtasks", "/delete", "/tn", _TASK_NAME, "/f"], check=False)
    log.info("Windows scheduled task '%s' deleted", _TASK_NAME)
    # Also remove the Startup-folder .lnk (the scheduler-blocked fallback), so an
    # uninstall doesn't leave a dangling shortcut pointing at a deleted shim.
    _startup_shortcut_path(_TASK_NAME).unlink(missing_ok=True)
    if home is not None:
        _win_shim_path(home, _TASK_NAME).unlink(missing_ok=True)


def _restart_schtasks() -> None:  # pragma: no cover
    # schtasks /end cannot reach a detached hidden-console process; taskkill the exe first.
    subprocess.run(["taskkill", "/F", "/IM", "mcpbrain.exe"], check=False)
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
    shim_path = _win_shim_path(home, _TRAY_TASK_NAME)
    shim_path.parent.mkdir(parents=True, exist_ok=True)
    shim_path.write_text(_win_shim_content(mcpbrain_bin=mcpbrain_bin, home=home,
                                           subcommand="tray"))
    if win_persistence_mechanism() == "schtasks":
        subprocess.run(schtasks_tray_args(mcpbrain_bin=mcpbrain_bin, home=home), check=True)
        log.info("Windows scheduled task '%s' created", _TRAY_TASK_NAME)
    else:
        _install_startup_shortcut(_TRAY_TASK_NAME, shim_path=shim_path)
        log.info("Task Scheduler blocked; installed Startup-folder shortcut for '%s'", _TRAY_TASK_NAME)


def _uninstall_schtasks_tray() -> None:  # pragma: no cover
    subprocess.run(["schtasks", "/delete", "/tn", _TRAY_TASK_NAME, "/f"], check=False)
    log.info("Windows scheduled task '%s' deleted", _TRAY_TASK_NAME)
    _startup_shortcut_path(_TRAY_TASK_NAME).unlink(missing_ok=True)   # clean the Startup fallback .lnk


# -- best-effort tray restarts (called by restart_agent; never fatal) --------

def _restart_launchd_tray() -> None:  # pragma: no cover
    if not _TRAY_LAUNCHD_PATH.exists():
        return  # tray not registered on this machine — nothing to restart
    uid = getattr(os, "getuid", lambda: 0)()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{_TRAY_LABEL}"], check=False)
    log.info("launchd tray agent restarted")


def _restart_schtasks_tray() -> None:  # pragma: no cover
    subprocess.run(["taskkill", "/F", "/IM", "mcpbrain.exe"], check=False)
    subprocess.run(["schtasks", "/run", "/tn", _TRAY_TASK_NAME], check=False)
    log.info("Windows scheduled task '%s' restarted", _TRAY_TASK_NAME)


# ---------------------------------------------------------------------------
# records-repo calendar agents (prune_hot_md daily, context_health weekly)
# ---------------------------------------------------------------------------

_PRUNE_LABEL = "com.mcpbrain.records.prune"
_HEALTH_LABEL = "com.mcpbrain.records.context-health"


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


def _cadence_schtasks_args(*, task_name: str, subcommand: str, mcpbrain_bin: str, home: str,
                           schedule: list[str]) -> list[str]:
    shim_path = _win_shim_path(home, task_name)
    tr = _schtasks_tr_for_shim(shim_path)
    return ["schtasks", "/create", "/tn", task_name, *schedule, "/tr", tr, "/f"]


def prune_schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return schtasks args to schedule `mcpbrain records-prune` daily at 06:00."""
    return _cadence_schtasks_args(task_name="mcpbrain-records-prune", subcommand="records-prune",
                                  mcpbrain_bin=mcpbrain_bin, home=home,
                                  schedule=["/sc", "daily", "/st", "06:00"])


def health_schtasks_args(*, mcpbrain_bin: str, home: str) -> list[str]:
    """Return schtasks args to schedule `mcpbrain records-health` weekly Monday at 07:00."""
    return _cadence_schtasks_args(task_name="mcpbrain-records-health", subcommand="records-health",
                                  mcpbrain_bin=mcpbrain_bin, home=home,
                                  schedule=["/sc", "weekly", "/d", "MON", "/st", "07:00"])


# ---------------------------------------------------------------------------
# Cross-platform cadence install dispatcher
# ---------------------------------------------------------------------------

def _cadence_specs(*, mcpbrain_bin: str, home: str):
    """The (label, plist-thunk) pairs to install on launchd. The fleet beacon is
    not here: the daemon writes it (Daemon._run_fleet_beacon)."""
    return [
        (_PRUNE_LABEL, lambda: records_prune_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
        (_HEALTH_LABEL, lambda: records_context_health_plist(mcpbrain_bin=mcpbrain_bin, mcpbrain_home=home)),
    ]


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
    for label, plist_fn in _cadence_specs(mcpbrain_bin=mcpbrain_bin, home=home):
        path = agents_dir / f"{label}.plist"
        path.write_text(plist_fn())
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", "-w", str(path)], check=True)


def _install_cadences_schtasks(*, mcpbrain_bin: str, home: str) -> None:  # pragma: no cover
    import subprocess
    cadences = [
        ("mcpbrain-records-prune", "records-prune",
         lambda: prune_schtasks_args(mcpbrain_bin=mcpbrain_bin, home=home)),
        ("mcpbrain-records-health", "records-health",
         lambda: health_schtasks_args(mcpbrain_bin=mcpbrain_bin, home=home)),
    ]
    for task_name, subcommand, args_fn in cadences:
        shim_path = _win_shim_path(home, task_name)
        shim_path.parent.mkdir(parents=True, exist_ok=True)
        shim_path.write_text(_win_shim_content(mcpbrain_bin=mcpbrain_bin, home=home,
                                               subcommand=subcommand))
        subprocess.run(args_fn(), check=True)
