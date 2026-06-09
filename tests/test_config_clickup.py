"""Tests for clickup_api_key() and clickup_list_id() config helpers."""
import json
from pathlib import Path


from mcpbrain.config import clickup_api_key, clickup_list_id


def _write_config(tmp_path: Path, data: dict) -> str:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(data))
    return str(tmp_path)


class TestClickupApiKey:
    def test_returns_empty_when_key_absent(self, tmp_path):
        home = _write_config(tmp_path, {})
        assert clickup_api_key(home) == ""

    def test_returns_value_when_present(self, tmp_path):
        home = _write_config(tmp_path, {"clickup_api_key": "pk_abc123"})
        assert clickup_api_key(home) == "pk_abc123"

    def test_returns_empty_for_blank_string_value(self, tmp_path):
        home = _write_config(tmp_path, {"clickup_api_key": ""})
        assert clickup_api_key(home) == ""


class TestClickupListId:
    def test_returns_empty_when_key_absent(self, tmp_path):
        home = _write_config(tmp_path, {})
        assert clickup_list_id(home) == ""

    def test_returns_value_when_present(self, tmp_path):
        home = _write_config(tmp_path, {"clickup_list_id": "901610549962"})
        assert clickup_list_id(home) == "901610549962"

    def test_returns_empty_for_blank_string_value(self, tmp_path):
        home = _write_config(tmp_path, {"clickup_list_id": ""})
        assert clickup_list_id(home) == ""


class TestOwnerName:
    def test_defaults_to_empty_when_absent(self, tmp_path):
        from mcpbrain.config import owner_name
        home = _write_config(tmp_path, {})
        assert owner_name(home) == ""

    def test_returns_configured_value(self, tmp_path):
        from mcpbrain.config import owner_name
        home = _write_config(tmp_path, {"owner_name": "Taryn"})
        assert owner_name(home) == "Taryn"

    def test_blank_string_falls_back_to_empty(self, tmp_path):
        from mcpbrain.config import owner_name
        home = _write_config(tmp_path, {"owner_name": ""})
        assert owner_name(home) == ""


class TestClickupUserId:
    def test_returns_none_when_absent(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        assert clickup_user_id(_write_config(tmp_path, {})) is None

    def test_returns_int_when_present(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        home = _write_config(tmp_path, {"clickup_user_id": 72748441})
        assert clickup_user_id(home) == 72748441

    def test_parses_numeric_string(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        home = _write_config(tmp_path, {"clickup_user_id": "555"})
        assert clickup_user_id(home) == 555

    def test_none_on_garbage(self, tmp_path):
        from mcpbrain.config import clickup_user_id
        home = _write_config(tmp_path, {"clickup_user_id": "abc"})
        assert clickup_user_id(home) is None


class TestClickupOrgFieldId:
    def test_returns_empty_when_absent(self, tmp_path):
        from mcpbrain.config import clickup_org_field_id
        assert clickup_org_field_id(_write_config(tmp_path, {})) == ""

    def test_returns_value(self, tmp_path):
        from mcpbrain.config import clickup_org_field_id
        home = _write_config(tmp_path, {"clickup_org_field_id": "abc-123"})
        assert clickup_org_field_id(home) == "abc-123"
