from mcpbrain.resolve import (
    canonical_key,
    _candidate_pairs,
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
    """Even when a client is passed, resolve_entities returns deterministic mode (§9A)."""
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("solo", "Solo", "person", seen="2026-05-30")
    out = resolve_entities(store, client=object())
    assert out["mode"] == "deterministic"
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


# --- resolve_entities deterministic-only (§9A) ----------------------------

def test_resolve_tiered_no_client_leaves_fuzzy_untouched(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    # Fuzzy pair the adjudicator WOULD have merged (now left intact).
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", seen="2026-05-30")
    # Fuzzy pair that must stay distinct (different initials).
    store.upsert_entity("daniel-p", "Daniel P", "person", seen="2026-05-30")
    store.upsert_entity("daniel-f", "Daniel F", "person", seen="2026-05-30")

    out = resolve_entities(store, client=None)

    assert out["mode"] == "deterministic"
    assert out["llm_calls"] == 0
    ids = {e["id"] for e in store.list_entities()}
    # No fuzzy merges — deterministic-only.
    assert {"joel", "joel-chelliah", "daniel-p", "daniel-f"} <= ids


def test_resolve_idempotent_second_run(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("joel-chelliah", "Joel Chelliah", "person", seen="2026-05-30")
    store.upsert_entity("daniel-p", "Daniel P", "person", seen="2026-05-30")
    store.upsert_entity("daniel-f", "Daniel F", "person", seen="2026-05-30")

    resolve_entities(store, client=None)
    # Second run: nothing left to merge.
    out2 = resolve_entities(store, client=None)
    assert out2["auto_merges"] == 0
    assert out2["llm_merges"] == 0
