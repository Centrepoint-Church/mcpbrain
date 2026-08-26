"""Guards on the setup wizard's HTML: no dead duplicates, no hardcoded org IDs,
and a step numbering that matches the number of things the user actually does."""
import re
from pathlib import Path

from mcpbrain import org_defaults

_HTML = (Path(__file__).parent.parent / "mcpbrain" / "wizard" / "index.html").read_text()


def test_no_duplicate_function_definitions():
    # saveFleet was defined twice, byte-identically; the first copy was dead.
    for name in ("saveFleet", "connectDesktop", "ensureModel", "prefillFromConfig",
                 "saveProfile", "startAuth", "refreshModel"):
        count = len(re.findall(rf"function {name}\(", _HTML))
        assert count == 1, f"{name} defined {count} times"


def test_no_hardcoded_org_folder_ids():
    assert org_defaults.FLEET_FOLDER_ID not in _HTML
    assert org_defaults.ESCROW_FOLDER_ID not in _HTML


def test_exactly_three_numbered_steps():
    nums = re.findall(r'<span class="num">(\d+)</span>', _HTML)
    assert nums == ["1", "2", "3"], f"expected three numbered steps, got {nums}"


def test_the_three_steps_are_the_three_actions():
    assert "Connect Google" in _HTML and "About you" in _HTML
    assert "Connect Claude Desktop" in _HTML


def test_retains_every_functional_panel():
    # Renumbering must not delete controls. The fleet block, the status panel and
    # the model button all still exist.
    for token in ('id="fleet_folder_id"', 'id="fleet_escrow_folder_id"',
                  'onclick="saveFleet()"', 'id="st-daemon"', 'id="st-count"',
                  'id="model-btn"', 'id="backup-status"'):
        assert token in _HTML, token


def test_model_download_auto_fires():
    assert "autoEnsureModel" in _HTML
