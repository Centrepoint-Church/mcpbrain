"""Guard against silently reintroducing the retry gap this PR closed.

Every plain (non-resumable) googleapiclient `.execute()` call in these
modules got `num_retries=` added (backup.py, fleet.py, dashboard.py, auth.py,
sync/drive.py, sync/calendar.py -- sync/gmail.py and sync/attachments.py
already had it via their own `_NUM_RETRIES`). Nothing in the language or a
linter enforces that a NEW call added later also gets it -- only copy-paste
discipline does. This scans the AST (not source text, so it can't be fooled
by a comment/docstring mentioning ".execute()") for any `.execute(...)` call
with ZERO arguments -- a bare call is exactly the shape the original bug had,
and every sqlite3 `db.execute(sql, ...)` call in this codebase always passes
at least the SQL string, so this can't false-positive on a database call.
"""
import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

_MODULES = [
    "mcpbrain/backup.py",
    "mcpbrain/fleet.py",
    "mcpbrain/dashboard.py",
    "mcpbrain/auth.py",
    "mcpbrain/sync/drive.py",
    "mcpbrain/sync/calendar.py",
    "mcpbrain/sync/gmail.py",
    "mcpbrain/sync/attachments.py",
]

# The one deliberate exception: a retried chunk of the resumable media
# upload can't re-seek a non-seekable stream. It already passes
# num_retries=_MEDIA_NUM_RETRIES (not a bare call), so it's not caught by
# this scan at all -- nothing to exempt here.


def _bare_execute_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and not node.args and not node.keywords):
            lines.append(node.lineno)
    return lines


def test_detector_actually_catches_a_bare_execute_call(tmp_path):
    """Prove the AST scan itself works, not just that current code is
    clean -- a guard test that can't fail is not a guard."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(service):\n"
        "    return service.files().list(q='x').execute()\n"
    )
    assert _bare_execute_calls(bad) == [2]

    good = tmp_path / "good.py"
    good.write_text(
        "def f(service):\n"
        "    return service.files().list(q='x').execute(num_retries=5)\n"
    )
    assert _bare_execute_calls(good) == []


@pytest.mark.parametrize("rel_path", _MODULES)
def test_no_bare_execute_calls(rel_path):
    path = _REPO_ROOT / rel_path
    bare = _bare_execute_calls(path)
    assert bare == [], (
        f"{rel_path} has bare .execute() call(s) with no num_retries at "
        f"line(s) {bare} -- googleapiclient defaults num_retries=0 (no "
        f"retry) when omitted, exactly the gap this test exists to catch. "
        f"Add num_retries=_NUM_RETRIES (or the module's local retry "
        f"constant) unless this is a genuinely new, deliberate exception "
        f"like backup.py's resumable media upload.")
