import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).parent.parent
_SCRIPT = _REPO / "bin" / "seed_joshbrain.py"


def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for d in ["context", "reference/examples", "state", "templates", "bin"]:
        (src / d).mkdir(parents=True)
    (src / "context" / "identity.md").write_text("# Identity\n")
    (src / "context" / "voice.md").write_text("# Voice\n")
    (src / "context" / "preferences.md").write_text("# Preferences\n")
    (src / "reference" / "projects.md").write_text("# Projects\n")
    (src / "reference" / "systems.md").write_text("# Systems\n")
    (src / "reference" / "ministry-context.md").write_text("# Ministry\n")
    (src / "reference" / "examples" / "job-description.md").write_text("# JD example\n")
    (src / "state" / "hot.md").write_text("# Hot\n")
    (src / "state" / "decisions.md").write_text("# Decisions\n")
    (src / "state" / "retired.md").write_text("# Retired\n")
    (src / "state" / "compliance.md").write_text("# Compliance\n")
    (src / "templates" / "meeting-minutes.md").write_text("# Template\n")
    (src / "bin" / "prune_hot_md.py").write_text("#!/usr/bin/env python3\n# prune\n")
    return src


def _run(src: Path, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--src", str(src), "--dest", str(dest)],
        capture_output=True,
        text=True,
    )


def test_creates_expected_structure(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    result = _run(src, dest)
    assert result.returncode == 0, result.stderr
    assert (dest / "context" / "identity.md").exists()
    assert (dest / "context" / "voice.md").exists()
    assert (dest / "reference" / "projects.md").exists()
    assert (dest / "state" / "hot.md").exists()
    assert (dest / "templates" / "meeting-minutes.md").exists()
    assert (dest / "bin" / "prune_hot_md.py").exists()
    assert (dest / "CLAUDE.md").exists()
    assert (dest / "BOOTSTRAP.md").exists()
    assert (dest / "cowork" / "context-project.md").exists()
    assert (dest / "cowork" / "memory-gardener.md").exists()
    assert (dest / "bin" / "context_health.py").exists()
    assert (dest / ".gitignore").exists()
    assert (dest / ".git").is_dir()


def test_claude_md_has_gardener_markers(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    _run(src, dest)
    text = (dest / "CLAUDE.md").read_text()
    assert "GARDENER-PROTECTED-START" in text
    assert "GARDENER-PROTECTED-END" in text


def test_git_has_exactly_one_commit(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    _run(src, dest)
    result = subprocess.run(
        ["git", "log", "--oneline"],
        capture_output=True, text=True, cwd=str(dest),
    )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln]
    assert len(lines) == 1
    assert "seed" in lines[0].lower()


def test_refuses_if_dest_exists(tmp_path):
    src = _make_src(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    result = _run(src, dest)
    assert result.returncode != 0
    assert "already exists" in result.stderr


def test_refuses_if_src_missing(tmp_path):
    result = _run(tmp_path / "no-such-src", tmp_path / "dest")
    assert result.returncode != 0
    assert "not found" in result.stderr.lower()


def test_copies_examples_directory(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    _run(src, dest)
    assert (dest / "reference" / "examples" / "job-description.md").exists()


def test_context_health_is_executable(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    _run(src, dest)
    import stat
    mode = (dest / "bin" / "context_health.py").stat().st_mode
    assert mode & stat.S_IXUSR


def test_seed_generates_memory_index_and_gitkeep(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    result = _run(src, dest)
    assert result.returncode == 0, result.stderr
    assert (dest / "MEMORY.md").exists()
    assert (dest / "memory" / ".gitkeep").exists()
    assert "Memory Index" in (dest / "MEMORY.md").read_text()


def _load_context_health(dest: Path):
    """Import the seeded context_health.py as a module bound to dest as JOSHBRAIN."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "seeded_context_health", str(dest / "bin" / "context_health.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_context_health_hot_md_regex_fires_on_stale_bullet(tmp_path):
    # Functional proof the generated regex matches real bullet-style hot.md
    # entries (the prior `\*\*(date):` pattern never matched a real entry).
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    assert _run(src, dest).returncode == 0
    # Stale, bullet-style entry well past the 14-day window.
    (dest / "state" / "hot.md").write_text(
        "# Hot\n\n- **2020-01-01: Old.** stale entry that should be pruned\n"
    )
    mod = _load_context_health(dest)
    # The module computes JOSHBRAIN from __file__ — the seeded dest IS the tree.
    warnings = mod._check_hot_md()
    assert warnings, "expected a stale-entry warning, got none"
    assert "2020-01-01" in warnings[0]


def test_context_health_hot_md_regex_quiet_on_fresh_bullet(tmp_path):
    src, dest = _make_src(tmp_path), tmp_path / "dest"
    assert _run(src, dest).returncode == 0
    from datetime import date
    today = date.today().isoformat()
    (dest / "state" / "hot.md").write_text(
        f"# Hot\n\n- **{today}: Fresh.** recent entry, should not warn\n"
    )
    mod = _load_context_health(dest)
    assert mod._check_hot_md() == []
