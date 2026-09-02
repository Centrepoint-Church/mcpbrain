"""Guards on the setup wizard's HTML: no dead duplicates, no hardcoded org IDs,
and a step numbering that matches the number of things the user actually does."""
import re
from pathlib import Path

from mcpbrain import org_defaults

_HTML = (Path(__file__).parent.parent / "mcpbrain" / "wizard" / "index.html").read_text()


def test_no_duplicate_function_definitions():
    # saveFleet was defined twice, byte-identically; the first copy was dead.
    for name in ("saveFleet", "connectDesktop", "prefillFromConfig",
                 "saveProfile", "startAuth"):
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
    # Renumbering must not delete controls. The fleet block and the status
    # panel still exist. (The model-download step was removed: the daemon's
    # lazy embedder already downloads the weights on its first sync/enrich/
    # search cycle, so there was nothing left for the wizard to trigger.)
    for token in ('id="fleet_folder_id"', 'id="fleet_escrow_folder_id"',
                  'onclick="saveFleet()"', 'id="st-daemon"', 'id="st-count"',
                  'id="backup-status"'):
        assert token in _HTML, token


def test_model_download_step_removed():
    # The wizard used to have an explicit "Search model" step (a button plus
    # an auto-firing background trigger) instructing the user to download the
    # embedding model. That is redundant with the daemon's lazy embedder,
    # which downloads the same weights automatically on its first sync/
    # enrich/search cycle once installed — no wizard involvement needed.
    for token in ('id="step-model"', 'id="model-btn"', "ensureModel",
                  "autoEnsureModel", "refreshModel", "/api/model/status",
                  "/api/model/ensure"):
        assert token not in _HTML, token


def test_fleet_save_disabled_until_prefill_completes():
    # Save posts whatever the inputs currently hold; before /api/config
    # resolves, that is empty strings, and an empty string clears the key
    # server-side rather than leaving it untouched.
    assert re.search(r'id="fleet-save-btn"[^>]*\bdisabled\b', _HTML)
    assert '$("fleet-save-btn").disabled = false' in _HTML


def test_fleet_copy_does_not_claim_a_broken_opt_out():
    # Clearing the field does NOT opt out — config.fleet_defaults (and
    # restore.py/backup_setup.py's identical fallback) treat an empty string
    # as unset and silently re-serve the org default on the next load.
    assert "clear it if you're not part of the org fleet" not in _HTML
