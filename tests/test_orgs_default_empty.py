"""DEFAULT_TAXONOMY is empty; configured orgs come only from config."""
import json

from mcpbrain.orgs import DEFAULT_TAXONOMY, taxonomy_from_config


def test_default_taxonomy_is_empty():
    assert DEFAULT_TAXONOMY.names == ()
    assert DEFAULT_TAXONOMY.domain_map == {}
    assert DEFAULT_TAXONOMY.aliases == {}


def test_unconfigured_taxonomy_is_empty(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({}))
    tax = taxonomy_from_config(str(tmp_path))
    assert tax.names == ()


def test_configured_taxonomy_reads_orgs(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({
        "orgs": [{"name": "Acme", "domains": ["acme.com"], "aliases": ["Acme Inc"]}]
    }))
    tax = taxonomy_from_config(str(tmp_path))
    assert tax.names == ("Acme",)
    assert tax.domain_map == {"acme.com": "Acme"}
    assert tax.from_email("a@acme.com") == "Acme"
    assert tax.canonical("acme inc") == "Acme"
