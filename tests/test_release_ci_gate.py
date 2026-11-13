"""bin/release.py refuses to ship a commit CI has not passed.

CI sat red from 2026-09-08 to 2026-11-13 (344 runs) across releases
0.7.125-0.7.149 and nothing noticed: a release must not depend on someone
remembering to look."""
import importlib.util
import json
import subprocess
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("_release_ci", _ROOT / "bin" / "release.py")
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

HEAD = "a" * 40


def _fake(runs, head=HEAD, gh_rc=0):
    def run(cmd, *a, **k):
        if cmd[:2] == ["git", "-C"] or cmd[:1] == ["git"]:
            return subprocess.CompletedProcess(cmd, 0, head + "\n", "")
        return subprocess.CompletedProcess(cmd, gh_rc, json.dumps(runs), "boom")
    return run


def test_passes_when_the_latest_run_is_head_and_green(monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", _fake(
        [{"headSha": HEAD, "status": "completed", "conclusion": "success"}]))
    assert release.ci_problems(Path(".")) == []


def test_refuses_a_red_run(monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", _fake(
        [{"headSha": HEAD, "status": "completed", "conclusion": "failure"}]))
    assert "failure" in release.ci_problems(Path("."))[0]


def test_refuses_when_ci_has_not_run_on_head(monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", _fake(
        [{"headSha": "b" * 40, "status": "completed", "conclusion": "success"}]))
    assert "not for HEAD" in release.ci_problems(Path("."))[0]


def test_refuses_a_run_still_in_progress(monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", _fake(
        [{"headSha": HEAD, "status": "in_progress", "conclusion": ""}]))
    assert "still running" in release.ci_problems(Path("."))[0]


def test_fails_closed_when_gh_cannot_answer(monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", _fake([], gh_rc=1))
    assert release.ci_problems(Path("."))


def test_main_stops_before_building_on_a_ci_problem(monkeypatch, tmp_path):
    built = []
    monkeypatch.setattr(release, "ci_problems", lambda repo: ["CI red"])
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: built.append(a))
    assert release.main(["--dist", str(tmp_path), "--repo", str(tmp_path)]) == 2
    assert built == []
