from pathlib import Path
_INSTALL = Path(__file__).parent.parent / "install"

def test_setup_sh_deleted():       assert not (_INSTALL / "setup.sh").exists()
def test_setup_command_deleted():  assert not (_INSTALL / "setup.command").exists()
def test_setup_ps1_deleted():      assert not (_INSTALL / "setup.ps1").exists()
