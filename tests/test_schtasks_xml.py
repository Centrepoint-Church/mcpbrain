"""The on-logon task must actually start, and actually restart on failure."""
import xml.etree.ElementTree as ET

from mcpbrain import agents


def _xml(tmp_path):
    return agents.schtasks_xml(shim_path=tmp_path / "agents" / "com.mcpbrain.vbs")


def test_xml_is_wellformed_and_has_required_sections(tmp_path):
    root = ET.fromstring(_xml(tmp_path))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:RegistrationInfo", ns) is not None
    assert root.find(".//t:Principals", ns) is not None, \
        'Actions Context="Author" requires a matching Principal id'


def test_exec_launches_wscript_not_the_vbs_directly(tmp_path):
    """Task Scheduler's Exec is CreateProcess; it cannot run a .vbs."""
    root = ET.fromstring(_xml(tmp_path))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    cmd = root.find(".//t:Exec/t:Command", ns).text
    args = root.find(".//t:Exec/t:Arguments", ns).text
    assert cmd.lower().endswith("wscript.exe"), f"Command was {cmd!r}"
    assert ".vbs" in args


def test_restart_on_failure_present(tmp_path):
    root = ET.fromstring(_xml(tmp_path))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:RestartOnFailure/t:Count", ns) is not None


# --- supervision must reflect what was ACTUALLY installed --------------------

def test_win_supervised_true_only_for_the_waiting_shim(tmp_path, monkeypatch):
    """The XML path installs a waiting shim; the /TR fallback reverts it.

    Without this, agents.py's fallback loses RestartOnFailure but
    _recover_from_stall still treats schtasks as supervised and takes the bare
    os._exit branch — leaving the daemon dead until next logon, which the spec
    calls strictly worse than the stall it is recovering from.
    """
    from mcpbrain import agents
    monkeypatch.setattr(agents, "win_persistence_mechanism", lambda: "schtasks")
    shim = agents._win_shim_path(str(tmp_path), agents._TASK_NAME)
    shim.parent.mkdir(parents=True, exist_ok=True)

    shim.write_text(agents._win_shim_content(
        mcpbrain_bin="C:\\x\\mcpbrain.exe", home=str(tmp_path),
        subcommand="daemon", wait=True))
    assert agents.win_supervised(str(tmp_path)) is True

    shim.write_text(agents._win_shim_content(
        mcpbrain_bin="C:\\x\\mcpbrain.exe", home=str(tmp_path),
        subcommand="daemon", wait=False))
    assert agents.win_supervised(str(tmp_path)) is False, \
        "the /TR fallback shim has no RestartOnFailure behind it"


def test_win_supervised_false_for_startup_folder(tmp_path, monkeypatch):
    from mcpbrain import agents
    monkeypatch.setattr(agents, "win_persistence_mechanism", lambda: "startup")
    assert agents.win_supervised(str(tmp_path)) is False


def test_win_supervised_false_when_shim_missing(tmp_path, monkeypatch):
    from mcpbrain import agents
    monkeypatch.setattr(agents, "win_persistence_mechanism", lambda: "schtasks")
    assert agents.win_supervised(str(tmp_path)) is False
