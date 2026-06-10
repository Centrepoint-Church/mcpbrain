from pathlib import Path

from mcpbrain import records


def test_scaffold_stamps_claude_md_with_profile(tmp_path):
    repo = str(tmp_path / "records")
    profile = {"owner_full_name": "Dana Lee", "owner_role": "Ops Lead",
               "orgs": [{"name": "Acme"}, {"name": "Globex"}]}
    records._ENSURED.clear()
    records.ensure_records_repo(repo, profile=profile)
    claude = (Path(repo) / "CLAUDE.md").read_text()
    assert "Acme" in claude and "Globex" in claude
    ident = (Path(repo) / "context" / "identity.md").read_text()
    assert "Dana Lee" in ident and "Ops Lead" in ident
    assert "{{OWNER_FULL_NAME}}" not in ident  # token fully replaced


def test_scaffold_never_clobbers_user_edits(tmp_path):
    repo = str(tmp_path / "records")
    records._ENSURED.clear()
    records.ensure_records_repo(repo, profile={"owner_full_name": "A", "owner_role": "R", "orgs": []})
    edited = Path(repo) / "CLAUDE.md"
    edited.write_text("MY EDITS")
    records._ENSURED.clear()
    records.ensure_records_repo(repo, profile={"owner_full_name": "B", "owner_role": "R2", "orgs": []})
    assert edited.read_text() == "MY EDITS"  # write-if-absent


def test_scaffold_records_degrades(tmp_path, monkeypatch):
    # ensure_records_repo raising -> scaffold_records returns [] (no raise)
    monkeypatch.setattr(records, "ensure_records_repo",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git missing")))
    assert records.scaffold_records(str(tmp_path / "home")) == []
