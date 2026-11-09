"""Guards on the setup page's HTML: no dead duplicates, no hardcoded org IDs,
a step numbering that matches the number of things the user actually does, and
nothing of the old configured-home view (that is the Overview and Settings now).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from mcpbrain import tenant

_ROOT = Path(__file__).parent.parent
_HTML = (_ROOT / "mcpbrain" / "wizard" / "index.html").read_text()


def test_no_duplicate_function_definitions():
    for name in ("connectDesktop", "prefillFromConfig", "saveProfile", "startAuth",
                 "autoBackup", "reflectStepBadges"):
        count = len(re.findall(rf"function {name}\(", _HTML))
        assert count == 1, f"{name} defined {count} times"


def test_no_hardcoded_org_folder_ids():
    prof = tenant.profile()
    assert prof is not None
    assert prof.fleet_folder_id not in _HTML
    assert prof.escrow_folder_id not in _HTML


def test_exactly_three_numbered_steps():
    nums = re.findall(r'<span class="num"[^>]*>(\d+)</span>', _HTML)
    assert nums == ["1", "2", "3"], f"expected three numbered steps, got {nums}"


def test_the_three_steps_are_the_three_actions():
    assert "Connect Google" in _HTML and "About you" in _HTML
    assert "Connect Claude Desktop" in _HTML
    order = [_HTML.index(f'id="{s}"') for s in ("step-google", "step-profile", "step-connect",
                                                 "step-done")]
    assert order == sorted(order)


def test_backup_is_a_status_line_in_step_one():
    google = _HTML[_HTML.index('id="step-google"'):_HTML.index('id="step-profile"')]
    assert 'id="backup-status"' in google
    assert 'id="step-backup"' not in _HTML


def test_done_screen_links_to_the_console():
    done = _HTML[_HTML.index('id="step-done"'):]
    assert 'href="/dashboard"' in done


def test_setup_only_no_configured_home_or_status_card():
    for token in ('id="home-status"', 'id="conn-cards"', 'id="backfill-progress"',
                  'id="self-heal-banners"', "HIDE_WHEN_CONFIGURED", "renderHome",
                  'id="step-status"', 'id="st-daemon"', 'id="st-count"'):
        assert token not in _HTML, token


def test_fleet_folders_moved_to_settings():
    assert 'id="fleet_folder_id"' not in _HTML
    assert "saveFleet" not in _HTML
    settings = (_ROOT / "mcpbrain" / "wizard" / "settings.html").read_text()
    assert 'id="fleet_folder_id"' in settings and "function saveFleet(" in settings


def test_model_download_step_removed():
    # The daemon's lazy embedder downloads the search model on its first
    # cycle; the setup page has nothing to trigger (Settings can re-download).
    for token in ('id="step-model"', 'id="model-btn"', "ensureModel",
                  "autoEnsureModel", "refreshModel", "/api/model/status",
                  "/api/model/ensure"):
        assert token not in _HTML, token


def test_wizard_does_not_hardcode_a_tenant_name():
    prof = tenant.load(_ROOT / "mcpbrain" / "tenant.json")
    assert prof.display_name not in _HTML
    assert "Josh Kemp" not in _HTML


def test_partial_restore_has_its_own_message():
    # /api/backup/auto answers {"action": "restored", "partial": true} when the
    # store came back but the records repo / settings did not: the page must
    # not show plain success, and must say what to do.
    assert "j.partial" in _HTML
    assert "mcpbrain restore --auto --force" in _HTML


def test_shared_shell_tokens_only():
    assert '<link rel="stylesheet" href="/static/console.css">' in _HTML
    assert '<script src="/static/console.js"></script>' in _HTML
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", _HTML)
    assert "oklch(" not in _HTML and "rgb" not in _HTML
    assert "innerHTML" not in _HTML
    assert "—" not in _HTML and "·" not in _HTML


def test_one_filled_primary_at_a_time():
    assert _HTML.count('class="btn btn--primary"') == 1


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_scripts_parse(tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", _HTML, flags=re.S)
    assert scripts
    for i, src in enumerate(scripts):
        p = tmp_path / f"s{i}.js"
        p.write_text(src)
        r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
