"""The enrichment gate: _gated_enrich_mode forces 'off' until configured."""
import json
from pathlib import Path

from mcpbrain.daemon import _gated_enrich_mode


def _home(tmp_path: Path, data: dict) -> str:
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_blocks_spool_when_unconfigured(tmp_path):
    assert _gated_enrich_mode("spool", _home(tmp_path, {})) == "off"


def test_off_stays_off(tmp_path):
    assert _gated_enrich_mode("off", _home(tmp_path, {})) == "off"


def test_passes_through_when_configured(tmp_path):
    home = _home(tmp_path, {
        "owner_name": "Sam", "owner_email": "sam@x.org",
        "orgs": [{"name": "Org", "domains": ["x.org"]}],
    })
    assert _gated_enrich_mode("spool", home) == "spool"
