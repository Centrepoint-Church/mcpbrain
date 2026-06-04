"""migrate_claude_memories: ~/.claude memory .md files -> capture envelopes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import migrate_claude_memories as mig


def _mem(dirpath, name, mtype, body):
    (dirpath / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: a {mtype} memory\n"
        f"metadata:\n  type: {mtype}\n---\n\n{body}\n")


def test_converts_files_to_ingest_envelopes(tmp_path):
    src = tmp_path / "memory"
    src.mkdir()
    _mem(src, "feedback_x.md", "feedback", "Always do X because Y.")
    _mem(src, "reference_y.md", "reference", "Dashboard at http://example.")
    (src / "MEMORY.md").write_text("# index — must be skipped")
    out = tmp_path / "out"
    n = mig.migrate(src, out)
    assert n == 2
    envs = [json.loads(p.read_text()) for p in sorted(out.glob("cap-*.json"))]
    kinds = {e["kind"] for e in envs}
    assert kinds == {"ingest"}
    by_type = {e["observation_type"] for e in envs}
    assert by_type == {"memory", "reference"}    # feedback -> memory
    assert all(e["source"] == "migration" for e in envs)


def test_envelopes_validate_against_contract(tmp_path):
    from mcpbrain.contract import validate_capture
    src = tmp_path / "memory"
    src.mkdir()
    _mem(src, "feedback_x.md", "feedback", "Body.")
    out = tmp_path / "out"
    mig.migrate(src, out)
    env = json.loads(next(out.glob("cap-*.json")).read_text())
    assert validate_capture(env) == []
