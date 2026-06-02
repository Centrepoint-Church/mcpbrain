from pathlib import Path


def test_export_script_excludes_internal_paths():
    # tests/ -> mcp-ops-brain/ -> products/ -> ops-brain root, then bin/.
    script = Path(__file__).resolve().parents[3] / "bin" / "export_mcpbrain_repo.sh"
    s = script.read_text()
    for internal in ("state/", "docs/superpowers/", ".claude/", "outputs/",
                     "google_oauth_client.json", "google_token.json", "config.json", "control_token"):
        assert internal in s, f"export must exclude {internal}"
