import json

from mcpbrain.resolve import (
    canonical_key,
    _candidate_pairs,
    _adjudicate,
    resolve_entities,
)
from mcpbrain.store import Store


# --- R5: canonical_key ----------------------------------------------------

def test_canonical_key_strips_honorific():
    assert canonical_key("Ps Joel") == canonical_key("Joel")


def test_canonical_key_folds_accents():
    assert canonical_key("Chané") == canonical_key("Chane")


def test_canonical_key_slugifies_punctuation():
    assert canonical_key("ACC (National)") == canonical_key("acc national")


def test_canonical_key_empty_is_empty():
    assert canonical_key("") == ""
    assert canonical_key(None) == ""


# --- R5: deterministic same-type merge ------------------------------------

def test_deterministic_merges_same_type_only(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    # "joel" bumped twice so it's the highest-mentions survivor.
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    # honorific variant, same type -> same canonical key as "joel".
    store.upsert_entity("ps-joel", "Ps Joel", "person", seen="2026-05-30")
    # same key "prayer" but DIFFERENT types -> must NOT merge.
    store.upsert_entity("prayer", "Prayer", "topic", seen="2026-05-30")
    store.upsert_entity("prayer-person", "Prayer", "person", seen="2026-05-30")

    out = resolve_entities(store, client=None)

    assert out["mode"] == "deterministic"
    assert out["auto_merges"] >= 1
    assert out["llm_merges"] == 0
    assert out["llm_calls"] == 0

    ids = {e["id"] for e in store.list_entities()}
    # ps-joel folded into joel.
    assert "joel" in ids
    assert "ps-joel" not in ids
    # cross-type "prayer" pair both survive.
    assert "prayer" in ids
    assert "prayer-person" in ids


def test_deterministic_survivor_is_highest_mentions(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("ps-joel", "Ps Joel", "person", seen="2026-05-30")

    resolve_entities(store, client=None)
    survivor = next(e for e in store.list_entities() if e["id"] == "joel")
    # mentions summed (3 + 1).
    assert survivor["mentions"] == 4
    assert all(e["id"] != "ps-joel" for e in store.list_entities())


def test_deterministic_survivor_tiebreak_is_id_deterministic(tmp_path):
    # Two distinct ids, SAME name "Joel" -> same canonical key + same type, so
    # they group. Equal mentions (1 each) and equal name length, so the only
    # discriminator is id. With ORDER BY id in the query and id as the final
    # max() tiebreaker, the survivor must be the same id every run.
    def run_once():
        store = Store(tmp_path / "tiebreak.sqlite3", dim=4)
        store.init()
        store.upsert_entity("joel-1", "Joel", "person", seen="2026-05-30")
        store.upsert_entity("joel-2", "Joel", "person", seen="2026-05-30")
        resolve_entities(store, client=None)
        ids = {e["id"] for e in store.list_entities()}
        return ids

    first = run_once()
    (tmp_path / "tiebreak.sqlite3").unlink()
    second = run_once()

    # max() on (mentions, len(name), id) keeps the lexicographically-larger id.
    assert first == {"joel-2"}
    assert "joel-1" not in first
    # Deterministic: same survivor both runs.
    assert first == second


def test_resolve_mode_reflects_client_presence(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("solo", "Solo", "person", seen="2026-05-30")
    out = resolve_entities(store, client=object())
    assert out["mode"] == "live"
    assert out["auto_merges"] == 0


# --- R6: blocking + fuzzy candidate scoring -------------------------------

def _pair_keys(pairs):
    return {tuple(sorted((a["id"], b["id"]))) for a, b in pairs}


def test_candidate_pairs_blocking_and_scoring():
    entities = [
        {"id": "joel", "name": "Joel", "type": "person"},
        {"id": "joel-chelliah", "name": "Joel Chelliah", "type": "person"},
        {"id": "daniel-p", "name": "Daniel P", "type": "person"},
        {"id": "daniel-f", "name": "Daniel F", "type": "person"},
        {"id": "5pm-prayer", "name": "5pm Prayer", "type": "meeting"},
        {"id": "5pm-prayer-meeting", "name": "5pm Prayer Meeting", "type": "meeting"},
        # cross-type org sharing a token must never pair with a person/meeting.
        {"id": "acc", "name": "ACC", "type": "org"},
    ]
    keys = _pair_keys(_candidate_pairs(entities))

    assert ("joel", "joel-chelliah") in keys
    assert ("daniel-f", "daniel-p") in keys
    assert ("5pm-prayer", "5pm-prayer-meeting") in keys
    # no pair includes the cross-type org.
    assert all("acc" not in pk for pk in keys)


def test_candidate_pairs_excludes_key_identical():
    # "Joel" and "Ps Joel" share a canonical key -> deterministic handles them,
    # so they must NOT surface as a fuzzy candidate.
    entities = [
        {"id": "joel", "name": "Joel", "type": "person"},
        {"id": "ps-joel", "name": "Ps Joel", "type": "person"},
    ]
    assert _candidate_pairs(entities) == []


def test_candidate_pairs_no_cross_type():
    entities = [
        {"id": "prayer-topic", "name": "Prayer Group", "type": "topic"},
        {"id": "prayer-person", "name": "Prayer Group", "type": "person"},
    ]
    # identical names but different types -> not paired (and key-identical anyway).
    assert _candidate_pairs(entities) == []


# --- R7: LLM adjudication + full resolve_entities -------------------------

class _JoelClient:
    """Fake Gemini client: same=true only when the two entries under
    adjudication are Joel variants. The A:/B: lines carry the names being
    compared (the prompt boilerplate mentions Daniel as an example, so we key
    off those lines, not the whole prompt)."""

    def __init__(self):
        self.calls = 0
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.calls += 1
        compared = [ln for ln in contents.splitlines()
                    if ln.startswith("A:") or ln.startswith("B:")]
        names = " ".join(compared)
        if "Joel" in names and "Daniel" not in names:
            payload = {"same": True, "canonical": "Joel Chelliah"}
        else:
            payload = {"same": False, "canonical": ""}
        return _Resp(json.dumps(payload))


class _Resp:
    def __init__(self, text):
        self.text = text


class _RaisingClient:
    def __init__(self):
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        raise RuntimeError("transient API error")


class _AlwaysFalseClient:
    def __init__(self):
        self.calls = 0
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        self.calls += 1
        return _Resp(json.dumps({"same": False, "canonical": ""}))


def _seed_fuzzy_store(tmp_path, name="r7.sqlite3"):
    store = Store(tmp_path / name, dim=4)
    store.init()
    # Fuzzy pair the adjudicator SHOULD merge.
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", seen="2026-05-30")
    # Fuzzy pair that must STAY distinct (different initials).
    store.upsert_entity("daniel-p", "Daniel P", "person", seen="2026-05-30")
    store.upsert_entity("daniel-f", "Daniel F", "person", seen="2026-05-30")
    return store


def test_adjudicate_parses_same_true():
    out = _adjudicate(_JoelClient(), {"name": "Joel", "type": "person"},
                      {"name": "Joel Chelliah", "type": "person"})
    assert out["same"] is True
    assert out["canonical"] == "Joel Chelliah"


def test_adjudicate_unparseable_is_false():
    out = _adjudicate(_RespClient("not json at all"),
                      {"name": "A", "type": "person"},
                      {"name": "B", "type": "person"})
    assert out["same"] is False


class _RespClient:
    def __init__(self, text):
        self._text = text
        self.models = self

    def generate_content(self, model=None, contents=None, config=None):
        return _Resp(self._text)


def test_resolve_live_adjudicates_fuzzy_correctly(tmp_path):
    store = _seed_fuzzy_store(tmp_path)
    out = resolve_entities(store, client=_JoelClient())

    assert out["mode"] == "live"
    assert out["llm_merges"] >= 1
    assert out["kept_distinct"] >= 1

    ids = {e["id"] for e in store.list_entities()}
    # Joel pair merged into one survivor named "Joel Chelliah".
    assert ("joel" in ids) ^ ("joel-chelliah" in ids)
    survivor = next(e for e in store.list_entities()
                    if e["id"] in ("joel", "joel-chelliah"))
    assert survivor["name"] == "Joel Chelliah"
    # Daniel pair both survive — different people.
    assert "daniel-p" in ids
    assert "daniel-f" in ids


def test_resolve_tiered_no_client_leaves_fuzzy_untouched(tmp_path):
    store = _seed_fuzzy_store(tmp_path)
    out = resolve_entities(store, client=None)

    assert out["mode"] == "deterministic"
    assert out["llm_calls"] == 0
    ids = {e["id"] for e in store.list_entities()}
    # No fuzzy merges without a client.
    assert {"joel", "joel-chelliah", "daniel-p", "daniel-f"} <= ids


def test_resolve_cap_zero_makes_no_calls(tmp_path):
    store = _seed_fuzzy_store(tmp_path)
    client = _JoelClient()
    out = resolve_entities(store, client=client, max_adjudications=0)

    assert out["llm_calls"] == 0
    assert client.calls == 0
    assert out["llm_merges"] == 0
    ids = {e["id"] for e in store.list_entities()}
    assert {"joel", "joel-chelliah", "daniel-p", "daniel-f"} <= ids


def test_resolve_api_error_does_not_crash(tmp_path):
    store = Store(tmp_path / "apierr.sqlite3", dim=4)
    store.init()
    # Deterministic pair: same canonical key "joel". "joel" has more mentions,
    # so it survives and "ps-joel" folds into it.
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("ps-joel", "Ps Joel", "person", seen="2026-05-30")
    # Fuzzy pair the adjudicator never gets to judge (client raises).
    store.upsert_entity("daniel-p", "Daniel P", "person", seen="2026-05-30")
    store.upsert_entity("daniel-f", "Daniel F", "person", seen="2026-05-30")

    out = resolve_entities(store, client=_RaisingClient())

    assert out["mode"] == "live"
    assert out["llm_merges"] == 0
    assert out["llm_calls"] == 0
    ids = {e["id"] for e in store.list_entities()}
    # Deterministic merge of Ps Joel into Joel still happened.
    assert "ps-joel" not in ids
    assert "joel" in ids
    # Fuzzy pairs left unmerged.
    assert "daniel-p" in ids
    assert "daniel-f" in ids


def test_resolve_cap_bounds_attempts_under_persistent_failure(tmp_path):
    """max_adjudications caps ATTEMPTS, not successes: a client that raises on
    every call must still stop at the cap rather than walk every candidate pair."""
    store = Store(tmp_path / "capfail.sqlite3", dim=4)
    store.init()
    # Three same-type persons sharing the token "daniel" -> 3 pairwise candidates,
    # distinct canonical keys (so they're fuzzy, not deterministic).
    for slug, name in (("daniel-a", "Daniel A"), ("daniel-b", "Daniel B"),
                       ("daniel-c", "Daniel C")):
        store.upsert_entity(slug, name, "person", seen="2026-05-30")

    class _CountingRaisingModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, model=None, contents=None, config=None):
            self.calls += 1
            raise RuntimeError("429 rate limit")

    class _CountingRaisingClient:
        def __init__(self):
            self.models = _CountingRaisingModels()

    client = _CountingRaisingClient()
    out = resolve_entities(store, client=client, max_adjudications=2)

    # Exactly 2 attempts were made despite 3 candidate pairs; the cap stopped it.
    assert client.models.calls == 2
    assert out["llm_calls"] == 0      # none succeeded
    assert out["llm_merges"] == 0
    # All three survive (nothing merged under failure).
    assert {"daniel-a", "daniel-b", "daniel-c"} <= {e["id"] for e in store.list_entities()}


def test_resolve_idempotent_second_run(tmp_path):
    store = _seed_fuzzy_store(tmp_path)
    resolve_entities(store, client=_JoelClient())

    # Second run: nothing left to merge. Any remaining distinct pair (Daniel)
    # returns false, and the Joel pair is already one entity.
    out2 = resolve_entities(store, client=_AlwaysFalseClient())
    assert out2["auto_merges"] == 0
    assert out2["llm_merges"] == 0


def test_resolve_merged_away_guard(tmp_path):
    # Three pairwise candidates sharing a token. The adjudicator merges the
    # first viable pair; a later pair referencing the merged-away id must be
    # skipped without error.
    store = Store(tmp_path / "guard.sqlite3", dim=4)
    store.init()
    store.upsert_entity("sam", "Sam", "person", seen="2026-05-30")
    store.upsert_entity("sam-jones", "Sam Jones", "person", seen="2026-05-30")
    store.upsert_entity("sam-jonas", "Sam Jonas", "person", seen="2026-05-30")

    class _MergeFirstClient:
        def __init__(self):
            self.calls = 0
            self.models = self

        def generate_content(self, model=None, contents=None, config=None):
            self.calls += 1
            # Merge only on the very first adjudication; refuse the rest.
            same = self.calls == 1
            return _Resp(json.dumps(
                {"same": same, "canonical": "Sam Jones" if same else ""}))

    out = resolve_entities(store, client=_MergeFirstClient())
    assert out["llm_merges"] == 1
    # Run completed without raising; two entities remain.
    assert len(store.list_entities()) == 2


# --- Fix 3: names with double-quotes are safely escaped in the prompt -------

def test_adjudicate_escapes_double_quotes_in_name():
    # A name containing a literal double-quote must not break the prompt's
    # JSON instruction; the fake client must receive a well-formed prompt and
    # return a parseable verdict without raising.
    a = {"name": 'Joel "JC" Chelliah', "type": "person"}
    b = {"name": "Joel Chelliah", "type": "person"}

    class _InspectingClient:
        def __init__(self):
            self.models = self
            self.last_prompt = None

        def generate_content(self, model=None, contents=None, config=None):
            self.last_prompt = contents
            return _Resp(json.dumps({"same": True, "canonical": "Joel Chelliah"}))

    client = _InspectingClient()
    result = _adjudicate(client, a, b)

    # Prompt must have been built without raising.
    assert client.last_prompt is not None
    # The name must appear JSON-encoded (outer double-quote present, inner escaped).
    assert '"Joel \\"JC\\" Chelliah"' in client.last_prompt
    # Result must be the expected dict shape with no exception.
    assert isinstance(result, dict)
    assert "same" in result
    assert "canonical" in result
    assert result["same"] is True
