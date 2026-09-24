"""brain_graph_correct: schema, daemon execution, confirmation channel (Task 6)."""
import jsonschema
import pytest

from mcpbrain import tools  # noqa: F401 -- populates the registry
from mcpbrain.tool_registry import spec


def _schema():
    return spec("brain_graph_correct").input_schema


def test_schema_rejects_smuggled_confirmation():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"op": "hide", "basis": "inferred", "reason": "r",
                             "entity_id": "x", "confirmed_via": "elicitation"}, _schema())


@pytest.mark.parametrize("args", [
    {"op": "hide", "basis": "user_stated", "reason": "r"},                  # no entity_id
    {"op": "reject_relation", "basis": "user_stated", "reason": "r", "entity_a": "a"},
    {"op": "undo"},                                                          # no correction_id
    {"op": "set_field", "basis": "user_stated", "reason": "r", "entity_id": "x",
     "field": "phone", "value": "1"},                                        # bad field
])
def test_schema_requires_per_op_fields(args):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(args, _schema())


def test_schema_accepts_valid_calls():
    jsonschema.validate({"op": "undo", "correction_id": 3}, _schema())
    jsonschema.validate({"op": "merge", "basis": "user_stated", "reason": "r",
                         "entity_id": "a", "other_id": "b"}, _schema())


def test_annotations_are_destructive():
    a = spec("brain_graph_correct").annotations
    assert a.destructive_hint is True and a.read_only_hint is False and a.idempotent_hint is False


def _daemon(tmp_path, monkeypatch):
    """A real Daemon over a real Store with an entity to correct.

    Follows the established construction pattern in test_tool_exec_routing.py's
    `_daemon` helper (a real `Daemon(store, None, services={}, lock=...)`)
    rather than the `Daemon.__new__` + attribute-seeding sketch, since the two
    differ and the real constructor is the pattern the rest of the suite uses.
    """
    from mcpbrain.daemon import Daemon, SingleWriterLock
    from mcpbrain.store import Store

    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "g.sqlite3", dim=4, read_only=False)
    store.init()
    with store._connect(write=True) as db:
        db.execute("INSERT INTO entities(id,name,type) VALUES('x','X','person')")
    daemon = Daemon(store, None, services={}, lock=SingleWriterLock(tmp_path / "d.lock"))
    return daemon, store


def test_daemon_applies_user_stated(tmp_path, monkeypatch):
    d, s = _daemon(tmp_path, monkeypatch)
    out = d.call_tool("brain_graph_correct", {"op": "hide", "basis": "user_stated",
                                              "reason": "junk", "entity_id": "x"})
    assert out["status"] == "applied"


def test_daemon_honours_out_of_band_confirmation_only(tmp_path, monkeypatch):
    d, s = _daemon(tmp_path, monkeypatch)
    args = {"op": "hide", "basis": "inferred", "reason": "r", "entity_id": "x"}
    assert d.call_tool("brain_graph_correct", args)["status"] == "pending"
    with s._connect(write=True) as db:
        db.execute("DELETE FROM graph_corrections")  # admin-delete-ok
    out = d.call_tool("brain_graph_correct", args, confirmation={"via": "elicitation"})
    assert out["status"] == "applied"
