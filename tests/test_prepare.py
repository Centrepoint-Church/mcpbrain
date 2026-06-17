"""Tests for the prepare step: un-enriched threads -> pending.json spool.

The prepare module codes against a Phase-1 contract (see prepare.py module
docstring): batch objects expose .thread_id, .doc_ids, .chunks. Phase-1 module
functions are reached through monkeypatchable seams (prepare._group_unenriched_threads,
prepare._reassemble_thread, prepare._build_known_people, prepare._org_domain_lines).
Tests stub those seams so the lazy real imports never fire.

Note: _read_projects and _read_areas seams were removed in §9E.
"""

import datetime as _datetime
import json


from mcpbrain import prepare

_NOW = _datetime.datetime(2026, 6, 2, 9, 30, 0, tzinfo=_datetime.timezone.utc)


# --- fakes -----------------------------------------------------------------

class FakeBatch:
    def __init__(self, thread_id, doc_ids, chunks):
        self.thread_id = thread_id
        self.doc_ids = doc_ids
        self.chunks = chunks


class FakeStore:
    def __init__(self, contexts=None, actions=None, entities=None):
        self._contexts = contexts or {}
        self._actions = actions or {}
        self._entities = entities or []
        self.marked = []

    def mark_enriched(self, doc_ids):
        self.marked.append(list(doc_ids))

    def thread_context(self, thread_id):
        return self._contexts.get(thread_id, "")

    def unified_actions(self, thread_id=None, status="open"):
        return self._actions.get(thread_id, [])

    def entities_for_resolution(self):
        return self._entities


def _msg(message_id, sender, date, subject, text, labels="INBOX"):
    return {
        "message_id": message_id, "sender": sender, "date": date,
        "labels": labels, "subject": subject, "text": text,
    }


def _stub_context(monkeypatch, *, people=None, domains=None):
    monkeypatch.setattr(prepare, "_build_known_people",
                        lambda store, batch_thread_ids: people or [])
    monkeypatch.setattr(prepare, "_org_domain_lines", lambda: domains or [])


def _stub_reassemble(monkeypatch):
    # Each chunk is already a message dict carrying body in "text"; reassemble
    # orders by date and returns the messages envelope. The fake mirrors the
    # documented behaviour of thread_enrich.reassemble_thread.
    def fake(chunks):
        return sorted(chunks, key=lambda c: c["date"])
    monkeypatch.setattr(prepare, "_reassemble_thread", fake)


# --- 2.1 noise filter ------------------------------------------------------

def test_is_noise_sender():
    assert prepare._is_noise("noreply@x.com", "Hello") is True
    assert prepare._is_noise("joel@example.org", "Hello") is False


def test_is_noise_subject():
    assert prepare._is_noise("a@b.com", "Unsubscribe now") is True
    assert prepare._is_noise("a@b.com", "Re: Hall B booking") is False


def test_is_noise_decorated_subject():
    # Leading emoji + space before "Out of office": the raw subject does not
    # match ^out of office, but the normalised one does.
    assert prepare._is_noise("a@b.com", "\U0001F4E7 Out of office") is True


def test_thread_is_noise_uses_lead_message():
    noise = prepare.thread_is_noise([
        _msg("m1", "noreply@x.com", "2026-06-01", "Newsletter", "..."),
        _msg("m2", "joel@example.org", "2026-06-02", "Re: Newsletter", "thanks"),
    ])
    assert noise is True
    clean = prepare.thread_is_noise([
        _msg("m1", "joel@example.org", "2026-06-01", "Hall B booking", "..."),
        _msg("m2", "noreply@x.com", "2026-06-02", "Unsubscribe", "..."),
    ])
    assert clean is False


# --- 2.1b bulk-mail body markers (mcpbrain addition) -----------------------

def test_is_bulk_body_positive_markers():
    # Strong bulk-mail markers that essentially never appear in genuine 1:1 mail.
    assert prepare._is_bulk_body("Trouble viewing? View in Browser.") is True
    assert prepare._is_bulk_body("Sent via https://mailchi.mp/abc/campaign") is True
    assert prepare._is_bulk_body("List-Unsubscribe: <mailto:x@y.com>") is True
    assert prepare._is_bulk_body("View this email in your browser") is True
    # Bare 'unsubscribe' only counts alongside a URL.
    assert prepare._is_bulk_body("To unsubscribe click http://x.com/u") is True


def test_is_bulk_body_negative_normal_text():
    # Normal correspondence that happens to use 'view' or 'new' is NOT bulk.
    assert prepare._is_bulk_body("Can you view the new roster before Sunday?") is False
    assert prepare._is_bulk_body("New volunteers added to the Hall B list.") is False
    # Bare 'unsubscribe' without a URL must not over-trigger.
    assert prepare._is_bulk_body("I tried to unsubscribe from that mailing list ages ago.") is False
    assert prepare._is_bulk_body("") is False


def test_thread_is_noise_ubiquiti_body_marker():
    # Real leaked email: Ubiquiti mailchimp newsletter. Caught by BOTH the
    # updates@ sender token AND the body markers (mailchi.mp / View in Browser).
    assert prepare.thread_is_noise([
        _msg("m1", "Ubiquiti <updates@ui.com>", "2026-06-01",
             "Introducing: UniFi 5G Backup",
             "View in Browser. Sent via https://mailchi.mp/ui/unifi"),
    ]) is True


def test_thread_is_noise_microsoft_store():
    # Real leaked email: Microsoft Store retail blast.
    assert prepare.thread_is_noise([
        _msg("m1", "Microsoft Store <Microsoftstore@microsoftstore.microsoft.com>",
             "2026-06-01", "Now available: Forza Horizon 6 accessories",
             "Shop the controller and headset today"),
    ]) is True


def test_thread_is_noise_fivetran_left_uncaught():
    # Real leaked email: Fivetran vendor product notification. Documented as
    # ACCEPTABLY un-caught: support@ is too broad a sender to add, "New ...
    # Added" too generic a subject, and the HTML body here lacks bulk markers.
    # Catching it safely would require an over-broad rule, so we leave it.
    assert prepare.thread_is_noise([
        _msg("m1", "Fivetran <support@fivetran.com>", "2026-06-01",
             "New JOURNAL_CASH and CONTACT_PHONE Tables Added to Xero Connector",
             "We have added new tables to the Xero connector."),
    ]) is False


def test_thread_is_noise_clean_thread_with_body_not_flagged():
    # False-positive guard: a real internal thread whose body mentions
    # 'unsubscribe' in passing must NOT be flagged.
    assert prepare.thread_is_noise([
        _msg("m1", "joel@example.org", "2026-06-01", "Re: Hall B booking",
             "Confirmed for Saturday. Please unsubscribe me from the old roster thread."),
    ]) is False


# --- 2.1c false-positive guard tests (review fixes) -----------------------

def test_guard_introducing_internal_announcement_not_noise():
    # Fix 1: ^introducing removed. A genuine ministry announcement from a real
    # sender must NOT be flagged as noise.
    assert prepare.thread_is_noise([
        _msg("m1", "joel@example.org", "2026-06-01",
             "Introducing our new College Coordinator",
             "Excited to share that Sam will be coordinating the new college."),
    ]) is False


def test_guard_ubiquiti_still_noise_via_sender_and_body():
    # Fix 1 regression check: Ubiquiti newsletter must still be caught — by
    # the updates@ sender token and/or the body markers — NOT by the removed
    # ^introducing subject pattern.
    assert prepare.thread_is_noise([
        _msg("m1", "Ubiquiti <updates@ui.com>", "2026-06-01",
             "Introducing: UniFi 5G Backup",
             "View in Browser. Sent via https://mailchi.mp/ui/unifi"),
    ]) is True


def test_guard_percent_off_mid_subject_not_noise():
    # Fix 2: ^ anchor. Mid-subject "10% off" in a real financial email must NOT
    # be flagged. Retail blasts lead with the discount; real mail leads with context.
    assert prepare.thread_is_noise([
        _msg("m1", "accounts@venuehire.com.au", "2026-06-01",
             "Approved: 10% off the venue quote",
             "Hi Sam, we have approved the discount. See attached."),
    ]) is False


def test_guard_percent_off_leading_subject_is_noise():
    # Fix 2 positive: a subject that LEADS with the discount is still caught.
    assert prepare._is_noise("a@b.com", "50% off all gear this weekend only") is True


def test_guard_shop_floor_walkthrough_not_noise():
    # Fix 3: adjacency required. "Shop floor walkthrough today" must NOT match
    # the tightened \bshop (?:now|today)\b pattern.
    assert prepare.thread_is_noise([
        _msg("m1", "taryn@example.org", "2026-06-01",
             "Shop floor walkthrough today",
             "Can we do the op-shop walkthrough at 2pm?"),
    ]) is False


def test_guard_shop_now_cta_is_noise():
    # Fix 3 positive: the canonical retail CTA "Shop now" is still caught.
    assert prepare._is_noise("a@b.com", "Shop now for the best deals") is True


def test_guard_shop_today_cta_is_noise():
    # Fix 3 positive: "Shop today" (adjacent) is still caught.
    assert prepare._is_noise("a@b.com", "Shop today — limited stock") is True


# --- 2.2 noise threads skipped + marked enriched ---------------------------

def test_prepare_skips_noise_threads_and_marks_them(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    noise = FakeBatch("t-noise", ["d-n1"],
                      [_msg("m1", "noreply@x.com", "2026-06-01", "Newsletter", "x")])
    good = FakeBatch("t-good", ["d-g1"],
                     [_msg("m2", "joel@example.org", "2026-06-01", "Hall B", "x")])
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [noise, good])
    # The noise filter reassembles each batch's chunks into messages, then runs
    # thread_is_noise on those messages. _stub_reassemble returns the chunks as
    # message dicts, so the noise lead carries the noreply sender.
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=False, now=_NOW)

    data = _read_pending(tmp_path)
    assert [t["thread_id"] for t in data["threads"]] == ["t-good"]
    assert ["d-n1"] in store.marked


def test_filter_noise_runs_on_reassembled_messages(tmp_path, monkeypatch):
    # Realistic Phase-1 flow: raw chunks do NOT carry top-level sender/subject;
    # that data lives inside chunk metadata until reassemble_thread builds the
    # messages envelope. _reassemble_thread is the seam that turns the raw chunks
    # into message dicts. Noise detection must run on those messages, not on the
    # raw chunks (which would expose empty fields and never detect noise).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    raw_chunk = {"doc_id": "d-n1", "text": "newsletter body",
                 "metadata": {"thread_id": "t-noise", "sender": "noreply@x.com",
                              "subject": "Newsletter", "date": "2026-06-01"}}
    noise = FakeBatch("t-noise", ["d-n1"], [raw_chunk])
    good = FakeBatch("t-good", ["d-g1"],
                     [{"doc_id": "d-g1", "text": "body",
                       "metadata": {"thread_id": "t-good", "sender": "joel@example.org",
                                    "subject": "Hall B", "date": "2026-06-01"}}])
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [noise, good])

    def fake_reassemble(chunks):
        # Build a message-shaped lead from each raw chunk's metadata.
        return [
            _msg(c["doc_id"], c["metadata"]["sender"], c["metadata"]["date"],
                 c["metadata"]["subject"], c["text"])
            for c in chunks
        ]

    monkeypatch.setattr(prepare, "_reassemble_thread", fake_reassemble)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=False, now=_NOW)

    data = _read_pending(tmp_path)
    assert [t["thread_id"] for t in data["threads"]] == ["t-good"]
    assert ["d-n1"] in store.marked


# --- 2.3 thread block shape ------------------------------------------------

def test_prepare_thread_block_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"],
                      [_msg("m1", "joel@example.org", "2026-06-01", "Hall B", "body text")])
    store = FakeStore(
        contexts={"t-a": "Prior summary."},
        actions={"t-a": [{"id": 42, "owner": "Sam", "text": "Lodge it.", "deadline": "2026-06-10"}]},
    )
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=False, now=_NOW)
    t = _read_pending(tmp_path)["threads"][0]

    assert t["thread_id"] == "t-a"
    assert t["prior_thread_context"] == "Prior summary."
    assert t["open_actions"][0]["id"] == 42
    m = t["messages"][0]
    assert set(m) >= {"message_id", "sender", "date", "labels", "subject", "text"}
    assert m["text"] == "body text"


def test_prepare_messages_ordered_by_date(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1", "d-a2"], [
        _msg("m2", "a@b.com", "2026-06-02", "Re: x", "second"),
        _msg("m1", "a@b.com", "2026-06-01", "x", "first"),
    ])
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=False, now=_NOW)
    msgs = _read_pending(tmp_path)["threads"][0]["messages"]
    assert [m["message_id"] for m in msgs] == ["m1", "m2"]


# --- 2.4 context + cap + long-thread guard ---------------------------------

def test_prepare_attaches_context(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"],
                      [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)

    captured = {}

    def fake_people(store, batch_thread_ids):
        captured["ids"] = batch_thread_ids
        return [{"name": "Joel Chelliah", "org": "Acme", "role": "Senior Pastor"}]

    monkeypatch.setattr(prepare, "_build_known_people", fake_people)
    monkeypatch.setattr(prepare, "_org_domain_lines",
                        lambda: ["example.org → Acme"])

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=False, now=_NOW)
    ctx = _read_pending(tmp_path)["context"]

    assert captured["ids"] == ["t-a"]
    assert ctx["known_people"][0]["name"] == "Joel Chelliah"
    assert "projects" not in ctx
    assert "areas" not in ctx
    assert ctx["org_domain_map"] == ["example.org → Acme"]


def test_prepare_caps_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batches = [
        FakeBatch(f"t-{i}", [f"d-{i}"], [_msg(f"m{i}", "a@b.com", "2026-06-01", "x", "body")])
        for i in range(5)
    ]
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: batches)
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=2, char_budget=100000,
                    resolution_due=False, now=_NOW)
    assert len(_read_pending(tmp_path)["threads"]) == 2


def test_prepare_long_thread_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    big = "x" * 60
    batch = FakeBatch("t-long", ["d-1", "d-2", "d-3"], [
        _msg("m1", "a@b.com", "2026-06-01", "s1", big),
        _msg("m2", "a@b.com", "2026-06-02", "s2", big),
        _msg("m3", "a@b.com", "2026-06-03", "s3", big),
    ])
    store = FakeStore(contexts={"t-long": "Prior."})
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100,
                    resolution_due=False, now=_NOW)
    threads = _read_pending(tmp_path)["threads"]

    assert len(threads) > 1
    assert all(t["thread_id"] == "t-long" for t in threads)
    assert all(t["prior_thread_context"] == "Prior." for t in threads)
    parts = [t["part"] for t in threads]
    assert parts == list(range(1, len(threads) + 1))
    assert all(t["of"] == len(threads) for t in threads)
    # message order preserved across the split
    ids = [m["message_id"] for t in threads for m in t["messages"]]
    assert ids == ["m1", "m2", "m3"]


def test_split_long_thread_single_oversized_message():
    # A lone message larger than the budget stays in one part, body intact.
    block = {
        "thread_id": "t-x",
        "prior_thread_context": "",
        "open_actions": [],
        "messages": [_msg("m-big", "a@b.com", "2026-06-01", "x", "x" * 200)],
    }
    parts = prepare._split_long_thread(block, char_budget=50)
    assert len(parts) == 1
    assert parts[0]["messages"][0]["message_id"] == "m-big"


# --- 2.5 merge-review block ------------------------------------------------

def test_prepare_no_merge_review_when_not_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"], [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore(entities=[
        {"id": "joel-chelliah", "name": "Joel Chelliah", "type": "person"},
        {"id": "joel-c", "name": "Joel C", "type": "person"},
    ])
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=False, now=_NOW)
    assert _read_pending(tmp_path)["merge_review"] == []


def test_prepare_appends_merge_review_when_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"], [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore(entities=[
        {"id": "joel-chelliah", "name": "Joel Chelliah", "type": "person"},
        {"id": "joel-c", "name": "Joel C", "type": "person"},
    ])
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    prepare.prepare(store, thread_cap=10, char_budget=100000,
                    resolution_due=True, now=_NOW)
    mr = _read_pending(tmp_path)["merge_review"]
    assert len(mr) == 1
    pair = mr[0]
    assert pair["pair_id"] == "joel-c|joel-chelliah"
    assert {pair["a"]["id"], pair["b"]["id"]} == {"joel-chelliah", "joel-c"}
    assert pair["a"]["type"] == "person"


def test_merge_pair_id_stable():
    a = {"id": "joel-chelliah", "name": "Joel Chelliah", "type": "person"}
    b = {"id": "joel-c", "name": "Joel C", "type": "person"}
    assert prepare._merge_pair(a, b)["pair_id"] == prepare._merge_pair(b, a)["pair_id"]
    assert prepare._merge_pair(a, b)["pair_id"] == "joel-c|joel-chelliah"


# --- 2.6 atomic write ------------------------------------------------------

def test_prepare_writes_pending_file(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"], [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [batch])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare(store, thread_cap=10, char_budget=100000,
                              resolution_due=False, now=_NOW)
    data = _read_pending(tmp_path)
    assert set(data) >= {"batch_id", "prepared_at", "context", "threads", "merge_review"}
    assert data["batch_id"] == "batch-20260602-093000"
    assert data["prepared_at"] == "2026-06-02T09:30:00Z"
    assert summary == {"batch_id": "batch-20260602-093000", "threads": 1, "merge_pairs": 0}


def test_prepare_overwrites_previous(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = FakeStore()
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    b1 = FakeBatch("t-1", ["d-1"], [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [b1])
    prepare.prepare(store, thread_cap=10, char_budget=100000, resolution_due=False, now=_NOW)

    b2 = FakeBatch("t-2", ["d-2"], [_msg("m2", "a@b.com", "2026-06-01", "x", "body")])
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [b2])
    import datetime as _dt
    later = _dt.datetime(2026, 6, 2, 10, 0, 0, tzinfo=_dt.timezone.utc)
    prepare.prepare(store, thread_cap=10, char_budget=100000, resolution_due=False, now=later)

    data = _read_pending(tmp_path)
    assert [t["thread_id"] for t in data["threads"]] == ["t-2"]


def test_prepare_no_unenriched_writes_empty_or_skips(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare(store, thread_cap=10, char_budget=100000,
                              resolution_due=False, now=_NOW)
    assert summary["threads"] == 0
    pending = tmp_path / "enrich_queue" / "pending.json"
    assert not pending.exists()


# --- build_pending: assemble dict without writing --------------------------

def test_build_pending_returns_dict_without_writing(tmp_path, monkeypatch):
    # build_pending must NOT touch the filesystem — no pending.json appears.
    import datetime
    from mcpbrain import prepare
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(prepare, "_reassemble_thread",
                        lambda chunks: [{"message_id": "m1", "date": "2026-01-01",
                                         "sender": "a@x.org", "subject": "Hi", "text": "hello"}])
    monkeypatch.setattr(prepare, "_build_context", lambda store, tids: {"owner_name": "Sam"})

    class _Batch:
        thread_id = "t1"; doc_ids = ["d1"]; chunks = [{"doc_id": "d1"}]

    now = datetime.datetime(2026, 6, 11, 9, 0, 0, tzinfo=datetime.timezone.utc)
    data = prepare.build_pending(object(), [_Batch()], char_budget=200_000, now=now,
                                 batch_id="fastbf-0-0")
    assert data["batch_id"] == "fastbf-0-0"
    assert data["prepared_at"] == "2026-06-11T09:00:00Z"
    assert len(data["threads"]) == 1 and data["threads"][0]["thread_id"] == "t1"
    assert data["merge_review"] == []
    assert not (tmp_path / "enrich_queue" / "pending.json").exists()


def test_merge_review_block_caps_pairs(monkeypatch):
    # Regression: the fuzzy finder can emit hundreds of thousands of pairs; the
    # block must cap them so pending.json stays small enough to load into context.
    from mcpbrain import prepare
    n = prepare._MERGE_REVIEW_CAP + 50
    fake = [({"id": f"a{i}", "name": "X", "type": "person"},
             {"id": f"b{i}", "name": "Y", "type": "person"}) for i in range(n)]
    monkeypatch.setattr(prepare, "_candidate_pairs", lambda ents: fake)

    class _Store:
        def entities_for_resolution(self):
            return []

    out = prepare._merge_review_block(_Store())
    assert len(out) == prepare._MERGE_REVIEW_CAP
    assert out[0]["a"]["id"] == "a0"  # order preserved, just truncated


# --- helpers ---------------------------------------------------------------

def _read_pending(home):
    return json.loads((home / "enrich_queue" / "pending.json").read_text())
