from mcpbrain import config
from mcpbrain.org_contracts import FleetPin, DEFAULT_RELATION_ALLOWLIST


def test_role_defaults_to_member(tmp_path):
    assert config.install_role(str(tmp_path)) == "member"
    assert config.is_org_curator(str(tmp_path)) is False


def test_role_curator(tmp_path):
    config.write_config(str(tmp_path), {"role": "org_curator"})
    assert config.install_role(str(tmp_path)) == "org_curator"
    assert config.is_org_curator(str(tmp_path)) is True


def test_org_flags_default_true(tmp_path):
    h = str(tmp_path)
    assert config.org_contrib_enabled(h) is True
    assert config.org_import_enabled(h) is True
    assert config.ingest_cache_enabled(h) is True


def test_org_contrib_can_be_disabled(tmp_path):
    config.write_config(str(tmp_path), {"org_contrib_enabled": False})
    assert config.org_contrib_enabled(str(tmp_path)) is False


def test_fleet_pin_empty_when_no_org_config(tmp_path):
    pin = config.fleet_pin(str(tmp_path))
    assert isinstance(pin, FleetPin)
    assert pin.is_pinned is False
    assert pin.relation_allowlist == DEFAULT_RELATION_ALLOWLIST


def test_fleet_pin_reads_org_config_block(tmp_path):
    config.write_config(str(tmp_path), {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 384, "chunker_version": "v1",
        "enrich_logic_floor": 2, "fleet_secret": "s3cret",
        "relation_allowlist": ["works_at", "member_of"]}}})
    pin = config.fleet_pin(str(tmp_path))
    assert pin.embed_model == "bge-small" and pin.dim == 384
    assert pin.enrich_logic_floor == 2 and pin.is_pinned is True
    assert pin.relation_allowlist == ("works_at", "member_of")


def test_fleet_pin_ignores_non_dict_org_config(tmp_path):
    config.write_config(str(tmp_path), {"org_config": "not-a-dict"})
    pin = config.fleet_pin(str(tmp_path))
    assert pin == FleetPin()


def test_fleet_pin_ignores_non_dict_org_pin(tmp_path):
    config.write_config(str(tmp_path), {"org_config": {"org_pin": ["not", "a", "dict"]}})
    pin = config.fleet_pin(str(tmp_path))
    assert pin == FleetPin()


def test_fleet_pin_ignores_non_string_iterable_relation_allowlist(tmp_path):
    config.write_config(str(tmp_path), {"org_config": {"org_pin": {
        "fleet_secret": "s3cret", "relation_allowlist": "works_at"}}})
    pin = config.fleet_pin(str(tmp_path))
    # A bare string must not be silently exploded into a char-tuple.
    assert pin.relation_allowlist == DEFAULT_RELATION_ALLOWLIST
    assert pin.is_pinned is True
