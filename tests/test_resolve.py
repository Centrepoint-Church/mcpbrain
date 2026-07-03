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


def test_is_role_address():
    from mcpbrain.resolve import is_role_address
    assert is_role_address("office@centrepoint.church") is True
    assert is_role_address("info@x.org") is True
    assert is_role_address("no-reply@x.org") is True
    assert is_role_address("hello+tag@x.org") is True          # +tag stripped
    assert is_role_address("john.smith@centrepoint.church") is False
    assert is_role_address("") is False
    assert is_role_address("notanemail") is False


def test_email_equality_skips_role_addresses(tmp_path):
    # C1: distinct real people who share a ROLE/shared inbox (office@, info@) must
    # NEVER be identity-merged — that would irreversibly collapse them. A genuine
    # duplicate on a PERSONAL address still merges.
    from mcpbrain.resolve import _email_equality_merges
    store = Store(tmp_path / "resolve.sqlite3", dim=4); store.init()
    store.upsert_entity("p1", "John Smith", "person", seen="2026-05-30")
    store.upsert_entity("p2", "Jane Doe", "person", seen="2026-05-30")
    store.upsert_entity("d1", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("d2", "Samuel Lee", "person", seen="2026-05-30")
    with store._connect() as db:
        db.execute("UPDATE entities SET email_addr='office@centrepoint.church' WHERE id IN ('p1','p2')")
        db.execute("UPDATE entities SET email_addr='sam.lee@x.org' WHERE id IN ('d1','d2')")

    merged = _email_equality_merges(store, home=str(tmp_path))

    ids = {e["id"] for e in store.list_entities()}
    assert {"p1", "p2"} <= ids, "distinct people on a role inbox must NOT merge"
    assert len({"d1", "d2"} & ids) == 1, "a real duplicate on a personal inbox should merge"
    assert merged == 1


def test_candidate_pairs_excludes_structural_types():
    # #4: candidate generation must only consider name-identity types. Structural
    # entities (document/thread/topic) explode the pair count (365k on the live
    # store) and can't be merged anyway (the apply-side guard rejects them), so they
    # must never become merge-review candidates in the first place.
    from mcpbrain.resolve import _candidate_pairs
    ents = [
        {"id": "p1", "name": "Sam Lee", "type": "person"},
        {"id": "p2", "name": "Sam Lee jr", "type": "person"},
        {"id": "d1", "name": "Budget 2026", "type": "document"},
        {"id": "d2", "name": "Budget 2027", "type": "document"},
        {"id": "t1", "name": "Weekly Sync", "type": "thread"},
        {"id": "t2", "name": "Weekly Standup", "type": "thread"},
    ]
    pairs = _candidate_pairs(ents)
    ids = {e["id"] for pair in pairs for e in pair}
    assert ids <= {"p1", "p2"}, "only person/org/project may be merge candidates"
    assert not any(e["type"] in ("document", "thread", "topic") for pair in pairs for e in pair)


def test_deterministic_merges_excludes_structural_types(tmp_path):
    # A document/thread/topic's identity is its SOURCE ID, not its title. Distinct
    # such nodes routinely share generic titles ("Untitled document", "TEST") and
    # must NEVER be merged on canonical name — doing so collapsed ~3,980 entities on
    # the real corpus. Only name-identity types (person/org/project) merge on name.
    from mcpbrain.resolve import _deterministic_merges
    store = Store(tmp_path / "resolve.sqlite3", dim=4)
    store.init()
    store.upsert_entity("doc-a", "Untitled document", "document", seen="2026-05-30")
    store.upsert_entity("doc-b", "Untitled document", "document", seen="2026-05-30")
    store.upsert_entity("thr-a", "TEST", "thread", seen="2026-05-30")
    store.upsert_entity("thr-b", "TEST", "thread", seen="2026-05-30")
    store.upsert_entity("top-a", "Budget", "topic", seen="2026-05-30")
    store.upsert_entity("top-b", "Budget", "topic", seen="2026-05-30")
    # a genuine person duplicate SHOULD still merge (name IS the identity).
    store.upsert_entity("sam-a", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("sam-b", "Sam Lee", "person", seen="2026-05-30")

    merged = _deterministic_merges(store)

    ids = {e["id"] for e in store.list_entities()}
    assert {"doc-a", "doc-b"} <= ids, "distinct documents must not merge on title"
    assert {"thr-a", "thr-b"} <= ids, "distinct threads must not merge on title"
    assert {"top-a", "top-b"} <= ids, "distinct topics must not merge on name"
    assert len({"sam-a", "sam-b"} & ids) == 1, "the person duplicate should merge"
    assert merged == 1, f"only the person pair should merge, got {merged}"


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
    # meetings are a STRUCTURAL type (identified by id/time, not title) — excluded
    # from candidate generation (#4), so they never pair even when titles overlap.
    assert all("5pm-prayer" not in pk and "5pm-prayer-meeting" not in pk for pk in keys)
    # the lone org is a singleton (no other org to pair with) — no pair includes it.
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
    # canonical-key pair: "Joel" / "Ps Joel" (name-based, same as existing coverage).
    store.upsert_entity("joel", "Joel", "person", seen="2026-05-30")
    store.upsert_entity("ps-joel", "Ps Joel", "person", seen="2026-05-30")
    # email-equality pair: distinct names, distinct canonical keys, shared email.
    store.upsert_entity("sam-1", "Sam Lee", "person", seen="2026-05-30")
    store.upsert_entity("sam-2", "Samuel Lee", "person", seen="2026-05-30")
    _set_email(store, "sam-1", "sam@example.org")
    _set_email(store, "sam-2", "sam@example.org")

    home = _enable_write_time_dedup(tmp_path)
    out = resolve_entities(store, client=None, home=home)

    assert out["auto_merges"] == 2
    ids = {e["id"] for e in store.list_entities()}
    # "Joel" / "Ps Joel" merge into one survivor (tiebreak: mentions, then
    # longer name, then id -> "Ps Joel" wins on name length here).
    assert len(ids & {"joel", "ps-joel"}) == 1
    assert len(ids & {"sam-1", "sam-2"}) == 1
