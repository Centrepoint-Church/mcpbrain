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


def test_schema_rejects_bad_enum_value():
    """`field` keeps its enum in the schema (unlike the per-op required-field
    rule below), so an invalid value is still a schema-level rejection."""
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"op": "set_field", "basis": "user_stated", "reason": "r",
                             "entity_id": "x", "field": "phone", "value": "1"}, _schema())


def test_schema_has_no_top_level_combinator():
    """The Anthropic Messages API rejects a tool input_schema with a top-level
    oneOf/allOf/anyOf ("input_schema does not support oneOf, allOf, or anyOf
    at the top level") -- a client forwarding this schema verbatim would 400
    on every request, not just a brain_graph_correct call. Per-op required
    fields are advertised in the description and enforced by
    graph_corrections.submit itself (see test_per_op_*_are_refused_by_the_daemon
    below), not by a schema combinator."""
    schema = _schema()
    assert not ({"oneOf", "allOf", "anyOf"} & set(schema)), schema


def test_schema_accepts_valid_calls():
    jsonschema.validate({"op": "undo", "correction_id": 3}, _schema())
    jsonschema.validate({"op": "merge", "basis": "user_stated", "reason": "r",
                         "entity_id": "a", "other_id": "b"}, _schema())
    # Missing per-op fields are now schema-VALID (only "op" is required at the
    # schema level) -- the daemon tests below prove they are still refused,
    # just by graph_corrections.submit rather than by jsonschema.
    jsonschema.validate({"op": "hide", "basis": "user_stated", "reason": "r"}, _schema())
    jsonschema.validate({"op": "undo"}, _schema())


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


@pytest.mark.parametrize("args", [
    {"op": "hide", "basis": "user_stated", "reason": "r"},                  # no entity_id
    {"op": "reject_relation", "basis": "user_stated", "reason": "r", "entity_a": "a"},
    {"op": "undo"},                                                          # no correction_id
])
def test_per_op_missing_fields_are_refused_by_the_daemon(tmp_path, monkeypatch, args):
    """Per-op required-field enforcement moved out of the schema (a top-level
    allOf is rejected by the Anthropic Messages API -- see
    test_schema_has_no_top_level_combinator) and into
    graph_corrections.submit, which is exercised here through the real daemon
    call path rather than jsonschema."""
    d, _s = _daemon(tmp_path, monkeypatch)
    out = d.call_tool("brain_graph_correct", args)
    assert out["status"] == "refused", out
