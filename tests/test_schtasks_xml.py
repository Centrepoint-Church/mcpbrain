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
