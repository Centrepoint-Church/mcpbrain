"""One owner identity for actions (2026-10-22 ingestion-integrity follow-up).

owner.entity_id is DERIVED (slugify of the configured full name), while the
owner's real graph node was minted from mail headers under a different display
name. Every name the owner's OWN node carries -- its name and its aliases --
plus the config aliases is the owner; anything else (a stranger who merely
shares a first name) is not.
"""
import json
from datetime import datetime, timezone

import pytest

from mcpbrain import action_owner as ao
from mcpbrain import graph_write as gw
from mcpbrain.store import Store

OWNER_EMAIL = "dana.okafor@example.org"
REAL = "dana-okafor"          # the owner's real node (minted from mail headers)
DERIVED = "dana-okafor-x"     # slugify(configured full name)

OWNER = gw.OwnerIdentity(name="Dana", entity_id=DERIVED,
                         aliases=frozenset({"dana", "dana okafor x"}))
NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def s(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({
        "owner_name": "Dana", "owner_full_name": "Dana Okafor X",
        "owner_email": OWNER_EMAIL}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    st = Store(tmp_path / "g.sqlite3", dim=4)
    st.init()
    return st


def _owner_node(s):
    with s._connect(write=True) as db:
        db.execute(
            "INSERT INTO entities(id, name, type, email_addr, aliases) "
            "VALUES (?, 'Dana Okafor', 'person', ?, ?)",
            (REAL, OWNER_EMAIL, "D. Okafor (via Forms), dokafor|Dana O"))


def _stranger(s):
    s.upsert_entity("dana-smith", "Dana Smith", "person")


def _ext(actions, *, sender="Marcus Reyes <marcus@example.com>", thread="t-1"):
    lead = {"message_id": "m1", "sender": sender, "date": "2026-05-30",
            "labels": "INBOX", "subject": "x", "body": ""}
    return {"thread_id": thread, "org": "Acme", "content_type": "request",
            "summary": "s", "contextual_summary": "", "entities": [], "topics": [],
            "actions": actions, "reply_needed": False, "reply_reason": "",
            "resolved_action_ids": [], "updated_actions": [], "relations": [],
            "messages": [lead]}


def _act(desc, owner_name, fallback="", due=""):
    return {"description": desc, "owner_name": owner_name,
            "owner_fallback": fallback, "due_date": due}


def _apply(s, ext, **kw):
    return gw.apply(s, ext, doc_ids=["d1"], clock=lambda: NOW, owner=OWNER,
                    identity="someone@example.com", **kw)


def _rows(s):
    return [(a["text"], a["owner"], a["owner_entity_id"])
            for a in s.list_unified_actions()]


class _Emb:
    """Texts in one SAME group embed identically; anything else orthogonal."""
    def __init__(self, same=(), dim=64):
        self.dim = dim
        self.same = [set(g) for g in same]
        self.calls = []
        self._seen = {}

    def embed_passages(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            g = next((i for i, grp in enumerate(self.same) if t in grp), None)
            idx = g if g is not None else self._seen.setdefault(t, 1 + len(self._seen))
            v = [0.0] * self.dim
            idx = idx % self.dim
            v[idx] = 1.0
            out.append(v)
        return out


# ---------------------------------------------------------------------------
# owner class
# ---------------------------------------------------------------------------

def test_owner_class_carries_node_names_and_both_ids(s):
    _owner_node(s)
    oc = gw.resolve_owner_class(s, OWNER)
    assert oc.name == "Dana" and oc.entity_id == REAL
    assert oc.ids == frozenset({REAL, DERIVED})
    for n in ("Dana", "dana okafor x", "Dana Okafor", "dokafor", "Dana O",
              "D. Okafor (via Forms)"):
        assert oc.is_owner_name(n), n
    assert not oc.is_owner_name("Dana Smith")
    assert not oc.is_owner_name("")


def test_owner_class_without_owner_node_uses_derived_id(s):
    oc = gw.resolve_owner_class(s, OWNER)
    assert oc.entity_id == DERIVED and oc.ids == frozenset({DERIVED})


def test_owner_class_unconfigured_matches_nothing(s):
    _owner_node(s)
    oc = gw.resolve_owner_class(s, gw.OwnerIdentity())
    assert not oc.is_owner_name("Dana Okafor") and not oc.ids


# ---------------------------------------------------------------------------
# rule 1 at write
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", ["Dana Okafor", "Dana O", "dokafor", "Dana",
                                     "dana okafor x"])
def test_owner_variants_written_canonically_with_real_id(s, variant):
    _owner_node(s)
    _apply(s, _ext([_act("Confirm the booking", variant)]))
    assert _rows(s) == [("Confirm the booking", "Dana", REAL)]


def test_inferred_owner_gets_real_id(s):
    _owner_node(s)
    _apply(s, _ext([_act("Review the draft policy", "")]))
    assert _rows(s) == [("Review the draft policy", "Dana", REAL)]


def test_owner_written_with_derived_id_when_no_node(s):
    _apply(s, _ext([_act("Confirm the booking", "Dana")]))
    assert _rows(s) == [("Confirm the booking", "Dana", DERIVED)]


def test_stranger_sharing_a_first_name_is_not_the_owner(s):
    _owner_node(s)
    _stranger(s)
    _apply(s, _ext([_act("Confirm the booking", "Dana Smith")]))
    assert _rows(s) == [("Confirm the booking", "Dana Smith", "dana-smith")]


def test_sender_fallback_to_owner_node_is_the_owner(s):
    _owner_node(s)
    _apply(s, _ext([_act("Confirm the booking", "", fallback="sender")],
                   sender=f"Dana Okafor <{OWNER_EMAIL}>"))
    rows = _rows(s)
    assert rows and all(r[1:] == ("Dana", REAL) for r in rows)


def test_owner_resolved_once_per_apply(s, monkeypatch):
    _owner_node(s)
    calls = []
    real = gw.resolve_owner_class
    monkeypatch.setattr(gw, "resolve_owner_class",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    _apply(s, _ext([_act("Confirm the booking", "Dana"),
                    _act("Pay the plumber invoice", "Dana Okafor"),
                    _act("Ring the caterer", "Dana O")]))
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# rule 2: dedupe owner compatibility
# ---------------------------------------------------------------------------

def test_variant_owner_row_is_a_dedupe_candidate(s):
    _owner_node(s)
    s.add_unified_action(text="Add the code of conduct to the student expectations",
                         owner="Dana Okafor", owner_entity_id=REAL, status="open",
                         source="email", thread_id="t-1",
                         created_at="2026-05-30T00:00:00Z")
    emb = _Emb([{"Add the code of conduct to the student expectations",
                 "Put the conduct code into the student expectations doc"}], dim=4)
    out = _apply(s, _ext([_act("Put the conduct code into the student expectations doc",
                               "Dana")]), embedder=emb)
    assert out["actions"] == 0
    assert len(s.list_unified_actions()) == 1


def test_variant_owner_row_is_a_dedupe_candidate_lexically(s):
    _owner_node(s)
    s.add_unified_action(text="Send the signed venue form", owner="Dana O",
                         owner_entity_id=REAL, status="open", source="email",
                         thread_id="t-1", created_at="2026-05-30T00:00:00Z")
    out = _apply(s, _ext([_act("Send the signed venue form today", "Dana")]))
    assert out["actions"] == 0


def test_stranger_row_is_still_not_a_candidate(s):
    _owner_node(s)
    _stranger(s)
    s.add_unified_action(text="Send the signed venue form", owner="Dana Smith",
                         owner_entity_id="dana-smith", status="open", source="email",
                         thread_id="t-1", created_at="2026-05-30T00:00:00Z")
    out = _apply(s, _ext([_act("Send the signed venue form", "Dana")]))
    assert out["actions"] == 1


# ---------------------------------------------------------------------------
# rule 3: the daily repair
# ---------------------------------------------------------------------------

def _add(s, text, owner, eid="", *, thread="t-1", deadline="", created="2026-05-01",
         source="email", status="open"):
    with s._connect(write=True) as db:
        return db.execute(
            "INSERT INTO actions(text, owner, owner_entity_id, status, deadline, source, "
            "thread_id, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (text, owner, eid, status, deadline, source, thread, created)).lastrowid


def _get(s, aid):
    with s._connect() as db:
        return dict(db.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone())


def _repair(s, emb=None, **kw):
    return ao.repair_owner_actions(s, OWNER, embedder=emb,
                                   as_of="2026-06-01T00:00:00Z", **kw)


def test_repair_canonicalises_owner_rows_and_records_prior(s):
    _owner_node(s)
    _stranger(s)
    a = _add(s, "One", "Dana", DERIVED)
    b = _add(s, "Two", "Dana Okafor", REAL, thread="t-2")
    c = _add(s, "Three", "Dana", "", thread="t-3")
    d = _add(s, "Four", "Dana Smith", "dana-smith", thread="t-4")
    e = _add(s, "Five", "Dana O", REAL, thread="t-5", status="done")
    f = _add(s, "Six", "unclear", "", thread="t-6")
    out = _repair(s)
    assert out["canonicalised"] == 3
    for aid, prior in ((a, "Dana"), (b, "Dana Okafor"), (c, "Dana")):
        r = _get(s, aid)
        assert (r["owner"], r["owner_entity_id"], r["prior_owner"]) == \
            ("Dana", REAL, prior)
    assert _get(s, d)["owner"] == "Dana Smith"
    assert _get(s, e)["owner"] == "Dana O"          # closed rows are left alone
    assert _get(s, f)["owner"] == "unclear"
    assert _repair(s)["canonicalised"] == 0       # idempotent


def test_repair_keeps_an_existing_prior_owner(s):
    _owner_node(s)
    a = _add(s, "One", "Dana Okafor", REAL)
    with s._connect(write=True) as db:
        db.execute("UPDATE actions SET prior_owner='Claude' WHERE id=?", (a,))
    _repair(s)
    assert _get(s, a)["prior_owner"] == "Claude"


SAME = [{"Add the code of conduct to the student expectations",
         "Put the conduct code into the student expectations doc"}]


def test_repair_collapses_owner_split_paraphrases_in_one_thread(s):
    _owner_node(s)
    keep = _add(s, "Add the code of conduct to the student expectations", "Dana",
                DERIVED, created="2026-05-01T10:00:00Z")
    drop = _add(s, "Put the conduct code into the student expectations doc",
                "Dana Okafor", REAL, created="2026-05-01T10:06:00Z")
    out = _repair(s, _Emb(SAME))
    assert out["deduped"] == 1
    assert _get(s, keep)["status"] == "open"
    r = _get(s, drop)
    assert (r["status"], r["resolved_by"]) == ("auto_archived", "dedup_owner")
    assert r["resolved_at"]


def test_repair_keeps_the_row_with_a_deadline(s):
    _owner_node(s)
    early = _add(s, "Add the code of conduct to the student expectations", "Dana",
                 created="2026-05-01")
    dated = _add(s, "Put the conduct code into the student expectations doc", "Dana O",
                 deadline="2026-06-30", created="2026-05-02")
    _repair(s, _Emb(SAME))
    assert _get(s, dated)["status"] == "open"
    assert _get(s, early)["status"] == "auto_archived"


def test_repair_never_merges_two_named_owners_or_other_threads(s):
    _owner_node(s)
    _stranger(s)
    a = _add(s, "Add the code of conduct to the student expectations", "Dana")
    b = _add(s, "Put the conduct code into the student expectations doc", "Dana Smith",
             "dana-smith")
    c = _add(s, "Put the conduct code into the student expectations doc", "Dana O",
             thread="t-other")
    assert _repair(s, _Emb(SAME))["deduped"] == 0
    assert {_get(s, x)["status"] for x in (a, b, c)} == {"open"}


def test_repair_ignores_non_email_actions(s):
    _owner_node(s)
    _add(s, "Add the code of conduct to the student expectations", "Dana")
    b = _add(s, "Put the conduct code into the student expectations doc", "Dana",
             source="capture")
    assert _repair(s, _Emb(SAME))["deduped"] == 0
    assert _get(s, b)["status"] == "open"


def test_repair_lexical_without_embedder(s):
    _owner_node(s)
    a = _add(s, "Send the signed venue form", "Dana", created="2026-05-01")
    b = _add(s, "Send the signed venue form today", "Dana Okafor", REAL,
             created="2026-05-02")
    assert _repair(s, None)["deduped"] == 1
    assert _get(s, a)["status"] == "open"
    assert _get(s, b)["resolved_by"] == "dedup_owner"


def test_repair_skips_work_when_nothing_changed(s):
    _owner_node(s)
    _add(s, "Add the code of conduct to the student expectations", "Dana")
    _add(s, "Ring the caterer", "Dana")
    emb = _Emb(SAME)
    _repair(s, emb)
    assert emb.calls
    emb.calls.clear()
    out = _repair(s, emb)
    assert emb.calls == [] and out == {"canonicalised": 0, "deduped": 0, "threads": 0}
    # A new row in the thread brings it back.
    new = _add(s, "Put the conduct code into the student expectations doc", "Dana")
    out = _repair(s, emb)
    assert out["deduped"] == 1 and _get(s, new)["status"] == "auto_archived"


def test_repair_is_bounded_per_run_and_resumes(s):
    _owner_node(s)
    for t in ("t-a", "t-b", "t-c"):
        _add(s, "Add the code of conduct to the student expectations", "Dana", thread=t)
        _add(s, "Put the conduct code into the student expectations doc", "Dana O",
             REAL, thread=t)
    emb = _Emb(SAME)
    first = _repair(s, emb, max_threads=1)
    # Canonicalisation touched all three threads: they all count as changed,
    # but only max_threads of them are compared per run.
    assert first["threads"] == 1 and first["deduped"] == 1
    total = first["deduped"]
    for _ in range(4):
        total += _repair(s, emb, max_threads=1)["deduped"]
    assert total == 3
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM actions WHERE status='open'"
                          ).fetchone()[0] == 3


def test_repair_embedder_failure_falls_back_to_lexical(s):
    _owner_node(s)

    class Boom:
        def embed_passages(self, texts):
            raise RuntimeError("model unavailable")

    _add(s, "Send the signed venue form", "Dana", created="2026-05-01")
    _add(s, "Send the signed venue form today", "Dana O", created="2026-05-02")
    assert _repair(s, Boom())["deduped"] == 1
