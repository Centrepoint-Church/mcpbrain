import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "dev_reinstall", Path(__file__).resolve().parent.parent / "bin/dev_reinstall.py")
dr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dr)


def test_matches_launchd_roles_only_never_client_mcp_servers():
    ps = [
        "  829 /u/.local/share/uv/tools/mcpbrain/bin/python3 /u/.local/share/uv/tools/mcpbrain/bin/mcpbrain tray",
        "63016 /u/.local/share/uv/tools/mcpbrain/bin/python /u/.local/share/uv/tools/mcpbrain/bin/mcpbrain daemon",
        "60758 /u/.local/share/uv/tools/mcpbrain/bin/python3 /u/.local/bin/mcpbrain mcp-server",
        "  100 /usr/bin/vim notes-about-mcpbrain daemon",
        "garbage",
    ]
    assert dr.mcpbrain_pids(ps) == [829, 63016]


def test_diff_trees_reports_missing_changed_and_stale(tmp_path):
    src, inst = tmp_path / "src", tmp_path / "inst"
    for root in (src, inst):
        (root / "sync").mkdir(parents=True)
        (root / "same.py").write_text("x = 1\n")
    (src / "sync/drive.py").write_text("new\n")
    (inst / "sync/drive.py").write_text("old\n")
    (src / "added.py").write_text("")
    (inst / "removed.py").write_text("")
    (inst / "__pycache__").mkdir()
    (inst / "__pycache__/ignored.py").write_text("")
    problems = dr.diff_trees(dr.tree_digest(src), dr.tree_digest(inst))
    assert problems == ["missing from install: added.py",
                        "differs: sync/drive.py",
                        "stale extra file in install: removed.py"]


def test_identical_trees_report_nothing(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    d = dr.tree_digest(tmp_path)
    assert dr.diff_trees(d, d) == []


def test_wheel_excludes_reads_pyproject(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[tool.setuptools.packages.find]\nexclude = ["mcpbrain.maintenance*", "other*"]\n')
    assert dr.wheel_excludes(p) == ("maintenance",)
