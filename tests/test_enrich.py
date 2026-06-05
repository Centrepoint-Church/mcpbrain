import json
import sys

from mcpbrain.enrich import slugify, extract, enrich_document, run_enrichment, resolve_client
from mcpbrain.store import Store


# --- slugify --------------------------------------------------------------

def test_slugify_basic():
    assert slugify("Taryn Hamilton") == "taryn-hamilton"


def test_slugify_punctuation_and_repeats():
    assert slugify("  Joel   Chelliah!! ") == "joel-chelliah"
    assert slugify("AV / Lighting") == "av-lighting"
    assert slugify("ACC (National)") == "acc-national"


def test_slugify_empty_input():
    assert slugify("") == ""
    assert slugify("!!!") == ""


# --- fake client ----------------------------------------------------------

GOOD_JSON = {
    "entities": [{"name": "Taryn Hamilton", "type": "person", "org": "Centrepoint"}],
    "relations": [{"from": "Taryn Hamilton", "relation": "reports_to", "to": "Joel Chelliah"}],
    "actions": [{"text": "Send the campus budget to the board", "owner": "Josh", "deadline": "2026-06-10"}],
    "decisions": [{"text": "Approved new AV spend for Byford", "decided_on": "2026-05-30"}],
}


class _Resp:
    def __init__(self, text):
        self.text = text


class _Models:
    def __init__(self, text):
        self._text = text

    def generate_content(self, model=None, contents=None, config=None):
        return _Resp(self._text)


class FakeClient:
    def __init__(self, text):
        self.models = _Models(text)


class _RaisingModels:
    """Simulates an API/transport failure (404 model error, quota, network)."""

    def __init__(self, exc):
        self._exc = exc

    def generate_content(self, model=None, contents=None, config=None):
        raise self._exc


class RaisingClient:
    def __init__(self, exc=None):
        self.models = _RaisingModels(exc or RuntimeError("404 model not available"))


class _SpyModels:
    """Captures the model= and config= kwargs passed to generate_content."""

    def __init__(self, text):
        self._text = text
        self.last_model = None
        self.last_config = None

    def generate_content(self, model=None, contents=None, config=None):
        self.last_model = model
        self.last_config = config
        return _Resp(self._text)


class SpyClient:
    def __init__(self, text):
        self.models = _SpyModels(text)


# --- build_prompt ---------------------------------------------------------

def test_build_prompt_is_domain_anchored_and_constrained():
    from mcpbrain.enrich import build_prompt
    p = build_prompt("some body", {"source_type": "gmail", "subject": "Hi",
                                   "sender": "a@b.com", "date": "2026-05-30"})
    # domain framing
    assert "Josh Kemp" in p
    assert "Centrepoint" in p and "ACC" in p and "Courageous Church" in p and "Curtin" in p
    # skip instruction
    assert "skip" in p
    assert "newsletter" in p.lower()
    # entity type allowlist
    assert "person|org|project|topic" in p
    # org enum
    assert "Centrepoint|ACC|Courageous Church|Curtin|external|unknown" in p
    # explicit junk-exclusion rule
    assert "do not extract" in p.lower()
    assert "dollar" in p.lower()
    # relation allowlist
    assert "works_at" in p and "reports_to" in p and "mentioned_with" in p
    # provenance carried for gmail
    assert "subject: Hi" in p or "subject" in p


# --- extract --------------------------------------------------------------

def test_extract_passes_response_mime_type_json():
    """extract must force JSON output via config response_mime_type."""
    spy = SpyClient(json.dumps(GOOD_JSON))
    extract(spy, "body", {})
    assert spy.models.last_config is not None
    assert spy.models.last_config.get("response_mime_type") == "application/json"


def test_extract_skip_true_returns_all_empty():
    """skip=true is a permanent decision -> all-empty lists (caller marks done)."""
    payload = dict(GOOD_JSON)
    payload["skip"] = True
    client = FakeClient(json.dumps(payload))
    out = extract(client, "body", {})
    assert out == {"entities": [], "relations": [], "actions": [], "decisions": []}


def test_extract_skip_absent_is_not_skipped():
    """Missing skip key behaves as skip=false (not skipped)."""
    client = FakeClient(json.dumps(GOOD_JSON))  # no skip key
    out = extract(client, "body", {})
    assert out["entities"][0]["name"] == "Taryn Hamilton"


def test_extract_plain_json():
    client = FakeClient(json.dumps(GOOD_JSON))
    out = extract(client, "body text", {"source_type": "gmail"})
    assert out["entities"][0]["name"] == "Taryn Hamilton"
    assert out["relations"][0]["relation"] == "reports_to"
    assert len(out["actions"]) == 1
    assert len(out["decisions"]) == 1


def test_extract_blocked_response_returns_empty_not_raise():
    """A safety-blocked response (resp.text raises) is a PERMANENT property of the
    content, so extract returns empty (chunk gets marked done) rather than
    propagating like a transient API error and re-queuing forever."""
    class _BlockedResp:
        @property
        def text(self):
            raise ValueError("response.text quick accessor: no text part (blocked)")

    class _BlockedModels:
        def generate_content(self, model=None, contents=None, config=None):
            return _BlockedResp()

    class _BlockedClient:
        models = _BlockedModels()

    out = extract(_BlockedClient(), "body text", {})
    assert out == {"entities": [], "relations": [], "actions": [], "decisions": []}


def test_extract_fenced_json():
    fenced = "```json\n" + json.dumps(GOOD_JSON) + "\n```"
    client = FakeClient(fenced)
    out = extract(client, "body text", {})
    assert out["entities"][0]["org"] == "Centrepoint"


def test_extract_garbage_returns_empty_lists():
    client = FakeClient("not json at all {oops")
    out = extract(client, "body", {})
    assert out == {"entities": [], "relations": [], "actions": [], "decisions": []}


def test_extract_missing_keys_default_empty():
    client = FakeClient(json.dumps({"entities": [{"name": "X", "type": "person"}]}))
    out = extract(client, "body", {})
    assert out["entities"] == [{"name": "X", "type": "person"}]
    assert out["relations"] == []
    assert out["actions"] == []
    assert out["decisions"] == []


def test_extract_propagates_api_error():
    """The core regression: an API/transport failure must PROPAGATE, not be
    swallowed into empty lists. Empty would let run_enrichment mark the chunk
    enriched with zero extraction (the silent-burn bug)."""
    import pytest
    client = RaisingClient(RuntimeError("404 model not available"))
    with pytest.raises(RuntimeError, match="404"):
        extract(client, "body", {})


def test_extract_bad_output_returns_empty_lists():
    """A successful API call returning unparseable model OUTPUT returns empty,
    no raise. Distinguished from an API failure."""
    client = FakeClient("not json at all {oops")
    out = extract(client, "body", {})
    assert out == {"entities": [], "relations": [], "actions": [], "decisions": []}


def test_extract_uses_default_model_when_none():
    """Default model resolves to the current Flash-Lite, not the dead 2.0-flash."""
    spy = SpyClient(json.dumps(GOOD_JSON))
    extract(spy, "body", {})
    assert spy.models.last_model == "gemini-2.5-flash-lite"


def test_extract_model_env_override(monkeypatch):
    """MCPBRAIN_ENRICH_MODEL flows into the model passed to generate_content."""
    import importlib
    from mcpbrain import enrich as enrich_mod
    monkeypatch.setenv("MCPBRAIN_ENRICH_MODEL", "gemini-custom-test")
    importlib.reload(enrich_mod)
    try:
        spy = SpyClient(json.dumps(GOOD_JSON))
        enrich_mod.extract(spy, "body", {})
        assert spy.models.last_model == "gemini-custom-test"
    finally:
        monkeypatch.delenv("MCPBRAIN_ENRICH_MODEL", raising=False)
        importlib.reload(enrich_mod)


def test_extract_trailing_second_object_parses_first():
    """Live-drain regression: model returns a valid object FOLLOWED BY a second
    object. json.loads raises 'Extra data'; we must parse the first object and
    ignore the trailing content (not return empty)."""
    raw = json.dumps(GOOD_JSON) + "\n{\"stray\": true}"
    client = FakeClient(raw)
    out = extract(client, "body", {})
    assert out["entities"][0]["name"] == "Taryn Hamilton"
    assert out["relations"][0]["relation"] == "reports_to"
    assert len(out["actions"]) == 1
    assert len(out["decisions"]) == 1


def test_extract_trailing_prose_parses_first():
    """Object followed by trailing prose/newlines still parses the object."""
    raw = json.dumps(GOOD_JSON) + "\n\nNote: done."
    client = FakeClient(raw)
    out = extract(client, "body", {})
    assert out["entities"][0]["name"] == "Taryn Hamilton"
    assert len(out["actions"]) == 1


def test_extract_leading_prose_parses_object():
    """Leading prose before the object: find the first '{' and decode from there."""
    raw = "Here is the JSON:\n" + json.dumps(GOOD_JSON)
    client = FakeClient(raw)
    out = extract(client, "body", {})
    assert out["entities"][0]["name"] == "Taryn Hamilton"


def test_extract_inline_brace_in_prose_skips_to_real_object():
    """An inline brace in leading prose (e.g. "{x}") must not abort the parse:
    the scanner skips the non-JSON brace and decodes the real object after it."""
    raw = "use {placeholder} then the result:\n" + json.dumps(GOOD_JSON)
    client = FakeClient(raw)
    out = extract(client, "body", {})
    assert out["entities"][0]["name"] == "Taryn Hamilton"
    assert out["relations"][0]["relation"] == "reports_to"


def test_extract_no_object_at_all_returns_empty():
    """No '{' anywhere -> all-empty lists, no raise (permanent, marks done)."""
    client = FakeClient("not json")
    out = extract(client, "body", {})
    assert out == {"entities": [], "relations": [], "actions": [], "decisions": []}


def test_extract_non_object_first_value_returns_empty():
    """First JSON value is an array, not an object -> empty, no raise."""
    client = FakeClient("[1, 2, 3]")
    out = extract(client, "body", {})
    assert out == {"entities": [], "relations": [], "actions": [], "decisions": []}


# --- enrich_document ------------------------------------------------------

def _store(tmp_path):
    s = Store(tmp_path / "e.sqlite3", dim=4)
    s.init()
    return s


def test_enrich_document_lands_expected_rows(tmp_path):
    s = _store(tmp_path)
    client = FakeClient(json.dumps(GOOD_JSON))
    meta = {"source_type": "gmail", "date": "2026-05-30", "thread_id": "thr-1"}
    summary = enrich_document(s, client, "gmail-1-body-0", "body text", meta)

    # entities = 2 new rows: taryn-hamilton (declared) + joel-chelliah (relation stub).
    # Counts reflect new graph rows created, so the stub counts once when first made.
    assert summary == {"entities": 2, "relations": 1, "actions": 1, "decisions": 1}

    taryn = s.get_entity("taryn-hamilton")
    assert taryn is not None
    assert taryn["org"] == "Centrepoint"
    assert taryn["type"] == "person"
    assert taryn["last_seen"] == "2026-05-30"

    rels = s.list_relations()
    assert len(rels) == 1
    r = rels[0]
    assert (r["entity_a"], r["relation"], r["entity_b"]) == (
        "taryn-hamilton", "reports_to", "joel-chelliah")
    assert r["source_doc_id"] == "gmail-1-body-0"

    # endpoint not in entities list still produced a stub
    joel = s.get_entity("joel-chelliah")
    assert joel is not None
    assert joel["type"] == "unknown"

    acts = s.list_actions()
    assert len(acts) == 1
    assert acts[0]["owner"] == "Josh"
    assert acts[0]["deadline"] == "2026-06-10"
    assert acts[0]["thread_id"] == "thr-1"
    assert acts[0]["source_doc_id"] == "gmail-1-body-0"

    decs = s.list_decisions()
    assert len(decs) == 1
    assert decs[0]["decided_on"] == "2026-05-30"
    assert decs[0]["source_doc_id"] == "gmail-1-body-0"


def test_enrich_document_counts_rows_written_not_extracted(tmp_path):
    s = _store(tmp_path)
    # one valid entity + one with empty name (skipped); one valid action + one empty (skipped)
    payload = {
        "entities": [
            {"name": "Taryn Hamilton", "type": "person", "org": "Centrepoint"},
            {"name": "", "type": "person", "org": ""},
        ],
        "relations": [],
        "actions": [
            {"text": "Send the budget", "owner": "Josh", "deadline": ""},
            {"text": "", "owner": "Josh", "deadline": ""},
        ],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    meta = {"source_type": "gmail", "date": "2026-05-30", "thread_id": "thr-1"}
    summary = enrich_document(s, client, "gmail-1-body-0", "body text", meta)

    # summary reflects rows written, not the two-item extracted lists
    assert summary == {"entities": 1, "relations": 0, "actions": 1, "decisions": 0}

    ents = s.list_entities()
    assert len(ents) == 1
    assert ents[0]["id"] == "taryn-hamilton"

    acts = s.list_actions()
    assert len(acts) == 1
    assert acts[0]["text"] == "Send the budget"


def test_enrich_document_filters_junk_entities_and_clamps_org(tmp_path):
    s = _store(tmp_path)
    payload = {
        "entities": [
            # 4-digit run in a person name (year/amount) -> numeric junk (person only)
            {"name": "$3000 on OpenAI API credits", "type": "person", "org": "external"},
            # subject-line prefix -> structural junk (person + org)
            {"name": "Re: 12 Open-Source GitHub Repos", "type": "org", "org": "external"},
            # URL -> structural junk (person + org)
            {"name": "https://example.com/signup", "type": "org", "org": "external"},
            # single-char -> length junk
            {"name": "X", "type": "person", "org": "external"},
            {"name": "Joel Chelliah", "type": "person", "org": "Centrepoint"},
            {"name": "Worship Night", "type": "topic", "org": "WORSHIP"},
        ],
        "relations": [
            # URL endpoint -> structural junk -> relation skipped
            {"from": "https://example.com/signup", "relation": "works_at", "to": "Joel Chelliah"},
        ],
        "actions": [],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    meta = {"source_type": "gmail", "date": "2026-05-30"}
    enrich_document(s, client, "doc-junk", "body", meta)

    ids = {e["id"] for e in s.list_entities()}
    # junk person/org rejected
    assert "3000-on-openai-api-credits" not in ids
    assert "re-12-open-source-github-repos" not in ids
    assert "https-example-com-signup" not in ids
    assert "x" not in ids
    # real person kept
    joel = s.get_entity("joel-chelliah")
    assert joel is not None and joel["org"] == "Centrepoint"
    # topic kept (exempt from length/pattern junk), org clamped off the enum
    topic = s.get_entity("worship-night")
    assert topic is not None
    assert topic["org"] != "WORSHIP"
    assert topic["org"] == ""
    # relation with URL (structural junk) endpoint skipped
    assert s.list_relations() == []


# --- _canonical_name (R2) -------------------------------------------------

def test_canonical_name_strips_honorifics():
    from mcpbrain.enrich import _canonical_name
    assert _canonical_name("Ps Joel") == "Joel"
    assert _canonical_name("Pastor Joel Chelliah") == "Joel Chelliah"
    assert _canonical_name("Dr. Sarah") == "Sarah"


def test_canonical_name_leaves_plain_names():
    from mcpbrain.enrich import _canonical_name
    assert _canonical_name("Joel Chelliah") == "Joel Chelliah"


def test_canonical_name_bare_honorific_unchanged():
    """A lone honorific with no following word is left as-is."""
    from mcpbrain.enrich import _canonical_name
    assert _canonical_name("Ps") == "Ps"


def test_canonical_name_collapses_whitespace():
    from mcpbrain.enrich import _canonical_name
    assert _canonical_name("  Joel   Chelliah ") == "Joel Chelliah"


def test_canonical_name_none_safe():
    from mcpbrain.enrich import _canonical_name
    assert _canonical_name(None) == ""


# --- R3: extraction-time type clamp + canonical names ---------------------

def test_enrich_document_clamps_invalid_declared_types_to_topic(tmp_path):
    """A declared entity with a type outside person|org|project|topic is stored
    as 'topic'; a valid 'person' is preserved."""
    s = _store(tmp_path)
    payload = {
        "entities": [
            {"name": "Easter Service", "type": "event", "org": "Centrepoint"},
            {"name": "Some Thing", "type": "weirdtype", "org": "Centrepoint"},
            {"name": "Joel Chelliah", "type": "person", "org": "Centrepoint"},
        ],
        "relations": [],
        "actions": [],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    enrich_document(s, client, "doc-clamp", "body", {"date": "2026-06-01"})

    assert s.get_entity("easter-service")["type"] == "topic"
    assert s.get_entity("some-thing")["type"] == "topic"
    assert s.get_entity("joel-chelliah")["type"] == "person"


def test_enrich_document_canonicalises_declared_entity_name(tmp_path):
    """A declared entity named with an honorific is stored under the canonical
    name and slug."""
    s = _store(tmp_path)
    payload = {
        "entities": [
            {"name": "Ps Joel Chelliah", "type": "person", "org": "Centrepoint"},
        ],
        "relations": [],
        "actions": [],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    enrich_document(s, client, "doc-canon", "body", {"date": "2026-06-01"})

    e = s.get_entity("joel-chelliah")
    assert e is not None
    assert e["name"] == "Joel Chelliah"
    assert s.get_entity("ps-joel-chelliah") is None


def test_enrich_document_canonicalises_relation_endpoints(tmp_path):
    """Relation endpoint names are canonicalised before slug/upsert so an
    honorific-prefixed endpoint resolves to the same entity."""
    s = _store(tmp_path)
    payload = {
        "entities": [
            {"name": "Taryn Hamilton", "type": "person", "org": "Centrepoint"},
        ],
        "relations": [
            {"from": "Taryn Hamilton", "relation": "reports_to", "to": "Pastor Joel Chelliah"},
        ],
        "actions": [],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    enrich_document(s, client, "doc-rel-canon", "body", {"date": "2026-06-01"})

    rels = s.list_relations()
    assert len(rels) == 1
    assert rels[0]["entity_b"] == "joel-chelliah"
    joel = s.get_entity("joel-chelliah")
    assert joel is not None
    # relation-endpoint STUB type is intentionally "unknown" (not clamped to topic)
    assert joel["type"] == "unknown"


def test_enrich_document_accent_folded_slug(tmp_path):
    """End-to-end: an accented name folds to an ASCII slug (so 'Renée'/'Renee'
    dedup) while the stored display name preserves the accent."""
    s = _store(tmp_path)
    payload = {
        "entities": [{"name": "Renée Smith", "type": "person", "org": "external"}],
        "relations": [], "actions": [], "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    enrich_document(s, client, "doc-accent", "body", {"date": "2026-06-01"})
    e = s.get_entity("renee-smith")
    assert e is not None
    assert e["name"] == "Renée Smith"        # display name keeps the accent
    assert s.get_entity("ren-e-smith") is None


# --- _is_junk_entity: numeric patterns are person-only (H5 Fix 1) ----------

def test_is_junk_entity_year_bearing_org_names_kept():
    from mcpbrain.enrich import _is_junk_entity
    # Year in an org/project name is legitimate — must NOT be junk-filtered.
    assert _is_junk_entity("Centrepoint 2026", "org") is False
    assert _is_junk_entity("Vision 2030", "org") is False
    assert _is_junk_entity("ALL IN 2026", "org") is False


def test_is_junk_entity_year_in_person_name_still_rejected():
    from mcpbrain.enrich import _is_junk_entity
    # A 4-digit run in a person name is almost always a date/amount artefact.
    assert _is_junk_entity("John 2026", "person") is True
    assert _is_junk_entity("$3000 on OpenAI API credits", "person") is True


def test_is_junk_entity_structural_patterns_rejected_for_both():
    from mcpbrain.enrich import _is_junk_entity
    # Structural junk (Re:/URL/email/brackets) must still be rejected for both types.
    assert _is_junk_entity("Re: Budget Meeting", "person") is True
    assert _is_junk_entity("Re: Budget Meeting", "org") is True
    assert _is_junk_entity("https://example.com/signup", "org") is True
    assert _is_junk_entity("user@example.com", "org") is True
    assert _is_junk_entity("[hidden]", "org") is True


def test_is_junk_entity_length_check_still_applies_to_both():
    from mcpbrain.enrich import _is_junk_entity
    assert _is_junk_entity("X", "org") is True    # < 2 chars
    assert _is_junk_entity("X", "person") is True
    assert _is_junk_entity("A" * 61, "org") is True   # > 60 chars
    assert _is_junk_entity("A" * 61, "person") is True


# --- relation endpoint uses "org" guard (H5 Fix 2) -------------------------

def test_relation_endpoint_year_bearing_org_not_dropped(tmp_path):
    """A relation to a year-bearing org endpoint must NOT be dropped after Fix 2."""
    s = _store(tmp_path)
    payload = {
        "entities": [
            {"name": "Joel Chelliah", "type": "person", "org": "Centrepoint"},
            {"name": "Centrepoint 2026", "type": "org", "org": "Centrepoint"},
        ],
        "relations": [
            {"from": "Joel Chelliah", "relation": "works_at", "to": "Centrepoint 2026"},
        ],
        "actions": [],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    enrich_document(s, client, "doc-fix2", "body", {"date": "2026-06-01"})

    rels = s.list_relations()
    assert len(rels) == 1
    assert rels[0]["entity_b"] == "centrepoint-2026"


def test_relation_endpoint_url_junk_still_dropped(tmp_path):
    """A relation whose endpoint is a URL must still be dropped (structural junk)."""
    s = _store(tmp_path)
    payload = {
        "entities": [
            {"name": "Joel Chelliah", "type": "person", "org": "Centrepoint"},
        ],
        "relations": [
            {"from": "Joel Chelliah", "relation": "works_at", "to": "https://example.com"},
        ],
        "actions": [],
        "decisions": [],
    }
    client = FakeClient(json.dumps(payload))
    enrich_document(s, client, "doc-fix2-url", "body", {"date": "2026-06-01"})

    assert s.list_relations() == []


def test_enrich_document_garbage_is_noop(tmp_path):
    s = _store(tmp_path)
    client = FakeClient("garbage")
    summary = enrich_document(s, client, "d1", "body", {})
    assert summary == {"entities": 0, "relations": 0, "actions": 0, "decisions": 0}
    assert s.list_entities() == []


def test_enrich_module_does_not_import_sdk_at_top():
    # importing enrich must not pull in the genai SDK
    import mcpbrain.enrich  # noqa: F401
    assert "google.genai" not in sys.modules


# --- run_enrichment (Task 4.3) -------------------------------------------

_DOCS = [
    ("doc-1", "body one", {"source_type": "gmail", "date": "2026-05-30", "thread_id": "t1"}),
    ("doc-2", "body two", {"source_type": "gmail", "date": "2026-05-31", "thread_id": "t2"}),
]


def _fresh_store(tmp_path):
    s = Store(tmp_path / "r.sqlite3", dim=4)
    s.init()
    return s


def test_run_enrichment_deferred_writes_nothing(tmp_path):
    """No client -> no graph writes; mode flag = 'deferred'."""
    s = _fresh_store(tmp_path)
    result = run_enrichment(s, _DOCS, client=None)

    assert result == {"mode": "deferred", "entities": 0, "relations": 0,
                      "actions": 0, "decisions": 0, "errors": 0}
    assert s.list_entities() == []
    assert s.list_actions() == []
    assert s.list_decisions() == []
    assert s.list_relations() == []
    assert s.get_meta("enrich_mode") == "deferred"


def test_run_enrichment_deferred_does_not_import_sdk(tmp_path):
    """Defer path must never trigger the Gemini SDK import."""
    # Remove google.genai from modules cache if somehow loaded in another test
    sys.modules.pop("google.genai", None)
    s = _fresh_store(tmp_path)
    run_enrichment(s, _DOCS, client=None)
    assert "google.genai" not in sys.modules


def test_run_enrichment_live_writes_rows_and_sets_mode(tmp_path):
    """With a fake client, run_enrichment writes rows and sets mode = 'live'."""
    s = _fresh_store(tmp_path)
    client = FakeClient(json.dumps(GOOD_JSON))
    result = run_enrichment(s, _DOCS, client=client)

    assert result["mode"] == "live"
    # Two docs share the same extraction; counts reflect NEW graph rows only.
    # Doc 1 creates taryn-hamilton + joel-chelliah stub (2 entities) and 1 relation.
    # Doc 2 merges both entities (no new rows) and hits the duplicate relation triple.
    assert result["entities"] == 2   # taryn-hamilton + joel-chelliah stub, created once
    assert result["relations"] == 1  # the duplicate triple in doc 2 is not counted
    assert result["actions"] == 2    # plain inserts, always new (1 per doc)
    assert result["decisions"] == 2  # plain inserts, always new (1 per doc)

    assert s.get_meta("enrich_mode") == "live"
    # Returned counts equal the rows actually written to the graph.
    assert result["entities"] == len(s.list_entities())
    assert result["relations"] == len(s.list_relations())
    assert result["actions"] == len(s.list_actions())
    assert result["decisions"] == len(s.list_decisions())


def test_run_enrichment_live_counts_match_written_rows(tmp_path):
    """Counts equal new graph rows: declared entities AND relation-endpoint stubs."""
    s = _fresh_store(tmp_path)
    client = FakeClient(json.dumps(GOOD_JSON))
    result = run_enrichment(s, [("doc-only", "body", {})], client=client)

    assert result["mode"] == "live"
    # 2 new entity rows: taryn-hamilton (declared) + joel-chelliah (relation stub).
    assert result["entities"] == 2
    assert result["entities"] == len(s.list_entities())
    assert result["relations"] == len(s.list_relations())
    assert result["actions"] == len(s.list_actions())
    assert result["decisions"] == len(s.list_decisions())


def test_run_enrichment_dedup_counts_shared_rows_once(tmp_path):
    """Two docs sharing an entity and an identical relation triple must count
    each shared row ONCE, and the returned summary must equal the rows actually
    written to the graph. Regression guard for the dedup-aware counters."""
    shared = {
        "entities": [{"name": "Taryn Hamilton", "type": "person", "org": "Centrepoint"}],
        "relations": [{"from": "Taryn Hamilton", "relation": "reports_to", "to": "Joel Chelliah"}],
        "actions": [],
        "decisions": [],
    }
    s = _fresh_store(tmp_path)
    client = FakeClient(json.dumps(shared))
    docs = [
        ("doc-a", "body a", {"source_type": "gmail", "date": "2026-05-30"}),
        ("doc-b", "body b", {"source_type": "gmail", "date": "2026-05-31"}),
    ]
    result = run_enrichment(s, docs, client=client)

    assert result["mode"] == "live"
    # taryn-hamilton (declared) + joel-chelliah (stub) created on doc-a only.
    assert result["entities"] == 2   # shared entities counted once across both docs
    assert result["relations"] == 1  # identical triple in doc-b is a deduped no-op

    # counts == rows written: the property the fix guarantees under dedup
    assert result["entities"] == len(s.list_entities())
    assert result["relations"] == len(s.list_relations())

    # entity merged rather than duplicated: one row, mentions bumped to 2
    assert s.get_entity("taryn-hamilton")["mentions"] == 2


def test_run_enrichment_live_marks_chunks_enriched(tmp_path):
    """Live path marks the processed chunks enriched: unenriched_chunks shrinks."""
    s = _fresh_store(tmp_path)
    # Seed two real chunks so the enriched flag has something to flip.
    s.upsert_chunk("doc-1", "body one", "h1", {"source_type": "gmail"})
    s.upsert_chunk("doc-2", "body two", "h2", {"source_type": "gmail"})
    assert len(s.unenriched_chunks()) == 2

    client = FakeClient(json.dumps(GOOD_JSON))
    docs = [(c["doc_id"], c["text"], c["metadata"]) for c in s.unenriched_chunks()]
    run_enrichment(s, docs, client=client)

    assert s.unenriched_chunks() == []  # both marked enriched


def test_run_enrichment_deferred_leaves_chunks_unenriched(tmp_path):
    """Defer path marks nothing (enriched stays 0) and writes no graph rows."""
    s = _fresh_store(tmp_path)
    s.upsert_chunk("doc-1", "body one", "h1", {"source_type": "gmail"})
    s.upsert_chunk("doc-2", "body two", "h2", {"source_type": "gmail"})

    docs = [(c["doc_id"], c["text"], c["metadata"]) for c in s.unenriched_chunks()]
    result = run_enrichment(s, docs, client=None)

    assert result["mode"] == "deferred"
    assert len(s.unenriched_chunks()) == 2  # nothing marked
    assert s.list_entities() == []          # no graph writes
    assert s.list_relations() == []
    assert s.list_actions() == []
    assert s.list_decisions() == []


class _PerDocModels:
    """generate_content raises for doc texts containing `fail_marker`, else
    returns good JSON. Lets one doc error while another succeeds."""

    def __init__(self, good_text, fail_marker):
        self._good = good_text
        self._marker = fail_marker

    def generate_content(self, model=None, contents=None, config=None):
        if self._marker in (contents or ""):
            raise RuntimeError("404 model not available")
        return _Resp(self._good)


class PerDocClient:
    def __init__(self, good_text, fail_marker):
        self.models = _PerDocModels(good_text, fail_marker)


def test_run_enrichment_does_not_mark_doc_whose_enrichment_raised(tmp_path):
    """An API error on one doc must NOT mark that doc enriched (stays enriched=0
    for retry), while a sibling doc that succeeded IS marked. Summary reports
    mode 'live' and errors>=1. Regression for the silent-burn bug."""
    s = _fresh_store(tmp_path)
    s.upsert_chunk("doc-good", "good body", "hg", {"source_type": "gmail"})
    s.upsert_chunk("doc-bad", "FAILME body", "hb", {"source_type": "gmail"})
    assert len(s.unenriched_chunks()) == 2

    client = PerDocClient(json.dumps(GOOD_JSON), fail_marker="FAILME")
    docs = [(c["doc_id"], c["text"], c["metadata"]) for c in s.unenriched_chunks()]
    result = run_enrichment(s, docs, client=client)

    assert result["mode"] == "live"
    assert result["errors"] >= 1

    remaining = {c["doc_id"] for c in s.unenriched_chunks()}
    assert "doc-bad" in remaining     # failed doc left for retry
    assert "doc-good" not in remaining  # successful doc marked enriched

    # The good doc still wrote graph rows.
    assert s.get_entity("taryn-hamilton") is not None


def test_run_enrichment_skip_marks_done_without_graph_rows(tmp_path):
    """A doc whose extraction returns skip=true writes no graph rows but IS
    marked enriched (skipping is a permanent decision, NOT an error)."""
    s = _fresh_store(tmp_path)
    s.upsert_chunk("doc-skip", "newsletter blast", "hs", {"source_type": "gmail"})
    assert len(s.unenriched_chunks()) == 1

    skip_payload = {"skip": True, "entities": [{"name": "Taryn", "type": "person"}],
                    "relations": [], "actions": [], "decisions": []}
    client = FakeClient(json.dumps(skip_payload))
    docs = [(c["doc_id"], c["text"], c["metadata"]) for c in s.unenriched_chunks()]
    result = run_enrichment(s, docs, client=client)

    assert result["mode"] == "live"
    assert result["errors"] == 0          # skip is not an error
    assert s.unenriched_chunks() == []    # marked enriched (permanent)
    assert s.list_entities() == []        # no graph rows written
    assert s.list_relations() == []


def test_run_enrichment_empty_docs_live_mode(tmp_path):
    """Empty doc list with a client still sets mode to 'live'."""
    s = _fresh_store(tmp_path)
    client = FakeClient(json.dumps(GOOD_JSON))
    result = run_enrichment(s, [], client=client)

    assert result == {"mode": "live", "entities": 0, "relations": 0,
                      "actions": 0, "decisions": 0, "errors": 0}
    assert s.get_meta("enrich_mode") == "live"


# --- resolve_client (Task 4.3) -------------------------------------------

def test_resolve_client_none_returns_none():
    assert resolve_client(None) is None


def test_resolve_client_empty_string_returns_none():
    assert resolve_client("") is None


def test_resolve_client_none_does_not_import_sdk():
    """Calling resolve_client(None) must not pull in google.genai."""
    sys.modules.pop("google.genai", None)
    resolve_client(None)
    assert "google.genai" not in sys.modules


def test_resolve_client_with_key_calls_make_gemini_client(monkeypatch, tmp_path):
    """resolve_client('key') delegates to make_gemini_client without importing the real SDK."""
    sentinel = object()
    from mcpbrain import enrich
    monkeypatch.setattr(enrich, "make_gemini_client", lambda key: sentinel)
    result = enrich.resolve_client("mykey")
    assert result is sentinel
