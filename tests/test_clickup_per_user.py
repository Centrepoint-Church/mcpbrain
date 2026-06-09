"""clickup.create_task / _normalise_task use the configured user + org field."""
import json

from mcpbrain import clickup


def _home(tmp_path, data):
    (tmp_path / "config.json").write_text(json.dumps(data))
    return str(tmp_path)


def test_create_task_uses_configured_assignee_and_org_field(tmp_path, monkeypatch):
    home = _home(tmp_path, {
        "clickup_api_key": "pk_x", "clickup_list_id": "L1",
        "clickup_user_id": 555, "clickup_org_field_id": "FIELD1",
    })
    captured = {}

    def fake_api(token, method, path, body=None):
        captured["body"] = body
        return {"id": "t1"}, 200

    monkeypatch.setattr(clickup, "_api", fake_api)
    monkeypatch.setattr(clickup, "org_to_option_id", lambda o, opts=None: "OPT1")

    out = clickup.create_task(home, name="Do thing", org="acme")
    assert out == {"id": "t1"}
    assert captured["body"]["assignees"] == [555]
    assert captured["body"]["custom_fields"] == [{"id": "FIELD1", "value": "OPT1"}]


def test_create_task_unassigned_when_no_user_configured(tmp_path, monkeypatch):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    captured = {}
    monkeypatch.setattr(clickup, "_api",
                        lambda *a, **k: (captured.update(body=k.get("body") or a[-1]) or {"id": "t1"}, 200))
    monkeypatch.setattr(clickup, "org_to_option_id", lambda o, opts=None: "")
    clickup.create_task(home, name="x")
    assert captured["body"]["assignees"] == []


def test_normalise_task_reads_configured_org_field(monkeypatch):
    monkeypatch.setattr(clickup, "option_id_to_org", lambda v, opts=None: "Acme" if v == "OPT1" else "")
    t = {"id": "1", "name": "x", "custom_fields": [{"id": "FIELD1", "value": "OPT1"}]}
    assert clickup._normalise_task(t, "FIELD1")["org"] == "Acme"
    # A different field id is ignored:
    assert clickup._normalise_task(t, "OTHER")["org"] == ""
