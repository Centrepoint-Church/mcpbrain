"""import_baseline creates actions owned by the configured owner, not the configured owner."""
import json

from mcpbrain.store import Store
from mcpbrain import clickup_sync


class FakeClient:
    def list_tasks_full(self, home, include_closed=True):
        return [{"id": "t1", "name": "A unique imported task", "closed": False,
                 "deadline": "", "org": "", "priority": "normal"}]


def test_import_baseline_uses_configured_owner(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({"owner_name": "Sam"}))
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()

    captured = {}
    orig = store.add_unified_action

    def spy(**kw):
        captured.update(kw)
        return orig(**kw)

    monkeypatch.setattr(store, "add_unified_action", spy)

    clickup_sync.import_baseline(store, str(tmp_path), client=FakeClient(), dry_run=False)
    assert captured.get("owner") == "Sam"
