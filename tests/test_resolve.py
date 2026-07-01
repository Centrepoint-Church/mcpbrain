import json

from mcpbrain.resolve import (
    canonical_key,
    _candidate_pairs,
    resolve_entities,
    _email_equality_merges,
)
from mcpbrain.store import Store


# --- R5: canonical_key ----------------------------------------------------

def test_canonical_key_strips_honorific():
    assert canonical_key("Ps Dana") == canonical_key("Dana")


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
    # "dana" bumped twice so it's the highest-mentions survivor.
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    # honorific variant, same type -> same canonical key as "dana".
    store.upsert_entity("ps-dana", "Ps Dana", "person", seen="2026-05-30")
    # same key "prayer" but DIFFERENT types -> must NOT merge.
    store.upsert_entity("prayer", "Prayer", "topic", seen="2026-05-30")
    store.upsert_entity("prayer-person", "Prayer", "person", seen="2026-05-30")

    out = resolve_entities(store, client=None)

    assert out["mode"] == "deterministic"
    assert out["auto_merges"] >= 1
    assert out["llm_merges"] == 0
    assert out["llm_calls"] == 0

    ids = {e["id"] for e in store.list_entities()}
    # ps-dana folded into dana.
    assert "dana" in ids
    assert "ps-dana" not in ids
    # cross-type "prayer" pair both survive.
    assert "prayer" in ids
    assert "prayer-person" in ids


def test_deterministic_survivor_is_highest_mentions(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("ps-dana", "Ps Dana", "person", seen="2026-05-30")

    resolve_entities(store, client=None)
    survivor = next(e for e in store.list_entities() if e["id"] == "dana")
    # mentions summed (3 + 1).
    assert survivor["mentions"] == 4
    assert all(e["id"] != "ps-dana" for e in store.list_entities())


def test_deterministic_survivor_tiebreak_is_id_deterministic(tmp_path):
    # Two distinct ids, SAME name "Dana" -> same canonical key + same type, so
    # they group. Equal mentions (1 each) and equal name length, so the only
    # discriminator is id. With ORDER BY id in the query and id as the final
    # max() tiebreaker, the survivor must be the same id every run.
    def run_once():
        store = Store(tmp_path / "tiebreak.sqlite3", dim=4)
        store.init()
        store.upsert_entity("dana-1", "Dana", "person", seen="2026-05-30")
        store.upsert_entity("dana-2", "Dana", "person", seen="2026-05-30")
        resolve_entities(store, client=None)
        ids = {e["id"] for e in store.list_entities()}
        return ids

    first = run_once()
    (tmp_path / "tiebreak.sqlite3").unlink()
    second = run_once()

    # max() on (mentions, len(name), id) keeps the lexicographically-larger id.
    assert first == {"dana-2"}
    assert "dana-1" not in first
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
        {"id": "dana", "name": "Dana", "type": "person"},
        {"id": "dana-okafor", "name": "Dana Okafor", "type": "person"},
        {"id": "daniel-p", "name": "Daniel P", "type": "person"},
        {"id": "daniel-f", "name": "Daniel F", "type": "person"},
        {"id": "5pm-prayer", "name": "5pm Prayer", "type": "meeting"},
        {"id": "5pm-prayer-meeting", "name": "5pm Prayer Meeting", "type": "meeting"},
        # cross-type org sharing a token must never pair with a person/meeting.
        {"id": "acc", "name": "ACC", "type": "org"},
    ]
    keys = _pair_keys(_candidate_pairs(entities))

    assert ("dana", "dana-okafor") in keys
    assert ("daniel-f", "daniel-p") in keys
    assert ("5pm-prayer", "5pm-prayer-meeting") in keys
    # no pair includes the cross-type org.
    assert all("acc" not in pk for pk in keys)


def test_candidate_pairs_excludes_key_identical():
    # "Dana" and "Ps Dana" share a canonical key -> deterministic handles them,
    # so they must NOT surface as a fuzzy candidate.
    entities = [
        {"id": "dana", "name": "Dana", "type": "person"},
        {"id": "ps-dana", "name": "Ps Dana", "type": "person"},
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
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("dana-okafor", "Dana Okafor", "person", seen="2026-05-30")
    # Fuzzy pair that must stay distinct (different initials).
    store.upsert_entity("daniel-p", "Daniel P", "person", seen="2026-05-30")
    store.upsert_entity("daniel-f", "Daniel F", "person", seen="2026-05-30")

    out = resolve_entities(store, client=None)

    assert out["mode"] == "deterministic"
    assert out["llm_calls"] == 0
    ids = {e["id"] for e in store.list_entities()}
    # No fuzzy merges — deterministic-only.
    assert {"dana", "dana-okafor", "daniel-p", "daniel-f"} <= ids


def test_resolve_idempotent_second_run(tmp_path):
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("dana-okafor", "Dana Okafor", "person", seen="2026-05-30")
    store.upsert_entity("daniel-p", "Daniel P", "person", seen="2026-05-30")
    store.upsert_entity("daniel-f", "Daniel F", "person", seen="2026-05-30")

    resolve_entities(store, client=None)
    # Second run: nothing left to merge.
    out2 = resolve_entities(store, client=None)
    assert out2["auto_merges"] == 0
    assert out2["llm_merges"] == 0


# --- Task 5.3: email-equality deterministic merge -------------------------

def _set_email(store, entity_id, email_addr):
    with store._connect() as db:
        db.execute("UPDATE entities SET email_addr=? WHERE id=?", (email_addr, entity_id))


def _enable_write_time_dedup(tmp_path) -> str:
    """Write a config.json with write_time_dedup explicitly True and return the
    home path string, the pattern this session's other kill-switch tests use."""
    (tmp_path / "config.json").write_text(json.dumps({"write_time_dedup": True}))
    return str(tmp_path)


def _disable_write_time_dedup(tmp_path) -> str:
    (tmp_path / "config.json").write_text(json.dumps({"write_time_dedup": False}))
    return str(tmp_path)


def test_email_equality_merge_same_case(tmp_path):
    """Brief's literal acceptance test: two person entities sharing the same
    email_addr, flag on -> one survives, merge_log gains a method='email' row."""
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("sam-1", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("sam-2", "Samuel Lee", "person", seen="2026-05-30")
    _set_email(store, "sam-1", "sam@example.org")
    _set_email(store, "sam-2", "sam@example.org")

    home = _enable_write_time_dedup(tmp_path)
    merged = _email_equality_merges(store, home=home)

    assert merged == 1
    ids = {e["id"] for e in store.list_entities()}
    assert len(ids & {"sam-1", "sam-2"}) == 1

    log_rows = [r for r in store.list_entity_merges() if r["method"] == "email"]
    assert len(log_rows) == 1


def test_email_equality_merge_normalizes_case_and_whitespace(tmp_path):
    """'Sam@X.org' and 'sam@x.org ' (mixed case / stray whitespace) must group
    together under the normalized (stripped, lowercased) email."""
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("sam-1", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("sam-2", "Samuel Lee", "person", seen="2026-05-30")
    _set_email(store, "sam-1", "Sam@X.org")
    _set_email(store, "sam-2", " sam@x.org ")

    home = _enable_write_time_dedup(tmp_path)
    merged = _email_equality_merges(store, home=home)

    assert merged == 1
    ids = {e["id"] for e in store.list_entities()}
    assert len(ids & {"sam-1", "sam-2"}) == 1


def test_email_equality_merge_different_emails_not_merged(tmp_path):
    """Two person entities with DIFFERENT email_addr must not be merged."""
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("sam", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("pat", "Pat Nguyen", "person", seen="2026-05-30")
    _set_email(store, "sam", "sam@example.org")
    _set_email(store, "pat", "pat@example.org")

    home = _enable_write_time_dedup(tmp_path)
    merged = _email_equality_merges(store, home=home)

    assert merged == 0
    ids = {e["id"] for e in store.list_entities()}
    assert {"sam", "pat"} <= ids


def test_email_equality_merge_flag_off_no_merge(tmp_path):
    """write_time_dedup explicitly False -> the email-sharing pair must NOT be
    merged, proving the gate is real (not a no-op default-on check)."""
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("sam-1", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("sam-2", "Samuel Lee", "person", seen="2026-05-30")
    _set_email(store, "sam-1", "sam@example.org")
    _set_email(store, "sam-2", "sam@example.org")

    home = _disable_write_time_dedup(tmp_path)
    merged = _email_equality_merges(store, home=home)

    assert merged == 0
    ids = {e["id"] for e in store.list_entities()}
    assert {"sam-1", "sam-2"} <= ids


def test_resolve_entities_combines_deterministic_and_email_merges(tmp_path):
    """Top-level resolve_entities(store, home=...) must report the SUM of
    canonical-key merges and email-equality merges when both apply."""
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    # canonical-key pair: "Dana" / "Ps Dana" (name-based, same as existing coverage).
    store.upsert_entity("dana", "Dana", "person", seen="2026-05-30")
    store.upsert_entity("ps-dana", "Ps Dana", "person", seen="2026-05-30")
    # email-equality pair: distinct names, distinct canonical keys, shared email.
    store.upsert_entity("sam-1", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("sam-2", "Samuel Lee", "person", seen="2026-05-30")
    _set_email(store, "sam-1", "sam@example.org")
    _set_email(store, "sam-2", "sam@example.org")

    home = _enable_write_time_dedup(tmp_path)
    out = resolve_entities(store, client=None, home=home)

    assert out["auto_merges"] == 2
    ids = {e["id"] for e in store.list_entities()}
    # "Dana" / "Ps Dana" merge into one survivor (tiebreak: mentions, then
    # longer name, then id -> "Ps Dana" wins on name length here).
    assert len(ids & {"dana", "ps-dana"}) == 1
    assert len(ids & {"sam-1", "sam-2"}) == 1
