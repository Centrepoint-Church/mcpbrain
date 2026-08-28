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
from mcpbrain.store import Store

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

    def thread_summary_digest(self, thread_id, max_chars=1500):
        return ""

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


# --- 2.1d pre-enrichment filter additions ----------------------------------

def test_thread_is_noise_toolsonair_vendor_blast():
    # Real leaked email: ToolsOnAir product/license marketing, no relationship.
    assert prepare.thread_is_noise([
        _msg("m1", "ToolsOnAir <helpdesk@toolsonair.com>", "2026-06-01",
             "Discover what's new in ToolsOnAir Capture 2026.1",
             "Thank you for downloading and testing Just In Mac Lite."),
    ]) is True


def test_thread_is_noise_medium_digest():
    # Real leaked email: Medium digest. Caught by the medium.com sender token
    # even for an address that doesn't contain "noreply".
    assert prepare.thread_is_noise([
        _msg("m1", "Medium Daily Digest <digest@medium.com>", "2026-06-01",
             "Today's highlights", "Stories for Josh K"),
    ]) is True


def test_thread_is_noise_ops_brain_eval_harness_body_marker():
    # Real leaked content: internal eval-harness output landed in the graph as
    # a business "fyi" note. Caught via a body marker, not sender/subject,
    # since the sender/subject here look unremarkable.
    assert prepare.thread_is_noise([
        _msg("m1", "josh.k@centrepoint.church", "2026-06-01", "Test run",
             "ops-brain eval harness: 34/67 evals passed. FAIL ..."),
    ]) is True


def test_guard_peak_consultancy_real_correspondence_not_noise():
    # Deliberately NOT added to NOISE_SENDERS: John Hardy at Peak Consultancy
    # has genuine correspondence history (meeting invites, assistance
    # requests), so a sender/domain-level block would also suppress future
    # real mail from him. Only his occasional pure-marketing blasts slip
    # through uncaught — an accepted tradeoff, same as Fivetran above.
    assert prepare.thread_is_noise([
        _msg("m1", "John Hardy <john@peakconsultancy.com.au>", "2026-06-01",
             "Zoom meeting invitation",
             "Let's meet to discuss the email sequences you need help with."),
    ]) is False


# --- 2.2 noise threads skipped + marked enriched ---------------------------

def test_prepare_units_writes_unit_files_and_context(tmp_path, monkeypatch):
    # The work-queue producer: prepare_units groups + builds + writes immutable unit
    # files, each carrying its own scoped context (no shared context.json, no
    # pending.json), skipping noise threads and marking their chunks enriched so
    # they never re-queue.
    import json
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # This test exercises unit-writing mechanics with stub batches; the salience
    # gate (default ON as of 0.7.65) is covered separately in test_salience_gate.py
    # and needs real chunk dicts. Pin it off here to isolate prepare_units.
    (tmp_path / "config.json").write_text('{"salience_gate": false}')
    noise = FakeBatch("t-noise", ["d-n1"],
                      [_msg("m1", "noreply@x.com", "2026-06-01", "Newsletter", "x")])
    # Non-trivial body (has an action cue) so this thread exercises the normal
    # build_pending/write_units mechanics rather than the trivial-thread
    # short-circuit (covered separately by its own tests below).
    good = FakeBatch("t-good", ["d-g1"],
                     [_msg("m2", "joel@example.org", "2026-06-01", "Hall B",
                           "Can you confirm the Hall B booking for Sunday?")])
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [noise, good])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path))

    units = list((tmp_path / "enrich_queue" / "units").glob("*.json"))
    assert summary["units_written"] == len(units) >= 1
    assert not (tmp_path / "enrich_queue" / "context.json").exists()
    # only the non-noise thread is enriched (noise filtered, like the old prepare())
    tids = set()
    for u in units:
        d = json.loads(u.read_text())
        if d["kind"] == "thread":
            tids.update(t["thread_id"] for t in d["threads"])
            assert "context" in d  # every unit carries its own scoped context
    assert tids == {"t-good"}
    assert not (tmp_path / "enrich_queue" / "pending.json").exists()  # no single spool
    # the noise thread's chunk was marked enriched by _filter_noise so it never re-queues
    assert ["d-n1"] in store.marked


def test_filter_noise_runs_on_reassembled_messages(monkeypatch):
    # Realistic Phase-1 flow: raw chunks do NOT carry top-level sender/subject;
    # that data lives inside chunk metadata until reassemble_thread builds the
    # messages envelope. _reassemble_thread is the seam that turns the raw chunks
    # into message dicts. Noise detection must run on those messages, not on the
    # raw chunks (which would expose empty fields and never detect noise).
    # Exercises _filter_noise directly (the surviving noise-filter primitive
    # both prepare_units and the old prepare() were built on).
    raw_chunk = {"doc_id": "d-n1", "text": "newsletter body",
                 "metadata": {"thread_id": "t-noise", "sender": "noreply@x.com",
                              "subject": "Newsletter", "date": "2026-06-01"}}
    noise = FakeBatch("t-noise", ["d-n1"], [raw_chunk])
    good = FakeBatch("t-good", ["d-g1"],
                     [{"doc_id": "d-g1", "text": "body",
                       "metadata": {"thread_id": "t-good", "sender": "joel@example.org",
                                    "subject": "Hall B", "date": "2026-06-01"}}])
    store = FakeStore()

    def fake_reassemble(chunks):
        # Build a message-shaped lead from each raw chunk's metadata.
        return [
            _msg(c["doc_id"], c["metadata"]["sender"], c["metadata"]["date"],
                 c["metadata"]["subject"], c["text"])
            for c in chunks
        ]

    monkeypatch.setattr(prepare, "_reassemble_thread", fake_reassemble)

    kept = prepare._filter_noise(store, [noise, good])

    assert [b.thread_id for b in kept] == ["t-good"]
    assert ["d-n1"] in store.marked


# --- 2.1.1 trivial-thread short-circuit -------------------------------------

def test_is_trivial_thread_short_no_action_cue():
    assert prepare.is_trivial_thread([{"text": "Thanks, sounds good."}]) is True


def test_is_trivial_thread_false_when_over_char_budget():
    long_text = "x" * 301
    assert prepare.is_trivial_thread([{"text": long_text}]) is False


def test_is_trivial_thread_false_when_action_cue_present():
    assert prepare.is_trivial_thread([{"text": "Thanks! Can you send the file?"}]) is False
    assert prepare.is_trivial_thread([{"text": "Please review this."}]) is False
    assert prepare.is_trivial_thread([{"text": "Sounds good?"}]) is False


def test_is_trivial_thread_empty_messages_is_true():
    assert prepare.is_trivial_thread([]) is True


def test_prepare_units_applies_trivial_thread_without_model_unit(tmp_path, monkeypatch):
    # A trivial thread (short, no action cue) must be deterministically
    # extracted and marked enriched directly by prepare_units — it must never
    # reach build_pending/write_units, so no unit file carries it and no
    # model call happens for it.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"salience_gate": false}')
    store = Store(str(tmp_path / "b.sqlite3"), dim=4)
    store.init()
    store.upsert_chunk(
        doc_id="d-triv1", text="Thanks, sounds good.", content_hash="h-triv1",
        metadata={"thread_id": "t-trivial", "sender": "Dana Lee <dana@centrepoint.church>",
                  "subject": "Re: Hall B", "date": "2026-06-01"},
    )
    trivial = FakeBatch(
        "t-trivial", ["d-triv1"],
        [_msg("m1", "Dana Lee <dana@centrepoint.church>", "2026-06-01",
              "Re: Hall B", "Thanks, sounds good.")],
    )
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [trivial])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path))

    assert summary["threads"] == 0
    units_dir = tmp_path / "enrich_queue" / "units"
    units = list(units_dir.glob("*.json")) if units_dir.exists() else []
    for u in units:
        d = json.loads(u.read_text())
        if d["kind"] == "thread":
            assert all(t["thread_id"] != "t-trivial" for t in d["threads"])
    with store._connect() as db:
        row = db.execute(
            "SELECT thread_id FROM email_context WHERE thread_id='t-trivial'"
        ).fetchone()
        enriched = db.execute(
            "SELECT enriched FROM chunks WHERE doc_id='d-triv1'"
        ).fetchone()[0]
    assert row is not None, "trivial thread must get a deterministic email_context row"
    assert enriched == 1, "trivial thread's chunk must be marked enriched"


def test_prepare_units_leaves_nontrivial_thread_to_model_path(tmp_path, monkeypatch):
    # A non-trivial thread (long body / action cue) must still flow through
    # build_pending/write_units unchanged — no deterministic short-circuit.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"salience_gate": false}')
    store = FakeStore()
    nontrivial = FakeBatch(
        "t-real", ["d-r1"],
        [_msg("m1", "joel@example.org", "2026-06-01", "Hall B booking",
              "Can you confirm the Hall B booking for Sunday?")],
    )
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [nontrivial])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path))

    assert summary["threads"] == 1
    units = list((tmp_path / "enrich_queue" / "units").glob("*.json"))
    tids = set()
    for u in units:
        d = json.loads(u.read_text())
        if d["kind"] == "thread":
            tids.update(t["thread_id"] for t in d["threads"])
    assert tids == {"t-real"}
    assert store.marked == []  # not marked by the trivial path; drain marks it later


def test_prepare_units_trivial_short_circuit_works_with_default_home(tmp_path, monkeypatch):
    # Regression: prepare_units() may be called with home=None (its default);
    # _apply_trivial_threads must resolve that to config.app_dir(), same as
    # every other config.*(home) call site in this module, not hand None
    # straight to config.read_config (which would raise on Path(None)).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"salience_gate": false}')
    store = Store(str(tmp_path / "b.sqlite3"), dim=4)
    store.init()
    store.upsert_chunk(
        doc_id="d-triv3", text="Thanks, sounds good.", content_hash="h-triv3",
        metadata={"thread_id": "t-trivial3", "sender": "joel@example.org",
                  "subject": "Re: Hall B", "date": "2026-06-01"},
    )
    trivial = FakeBatch(
        "t-trivial3", ["d-triv3"],
        [_msg("m1", "joel@example.org", "2026-06-01", "Re: Hall B", "Thanks, sounds good.")],
    )
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [trivial])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW)  # home omitted -> None

    assert summary["threads"] == 0


def test_prepare_units_trivial_short_circuit_respects_kill_switch(tmp_path, monkeypatch):
    # With enrich_trivial_thread_summary=False, a trivial thread must flow
    # through the normal model path unchanged (kill-switch only, default ON).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(
        json.dumps({"salience_gate": False, "enrich_trivial_thread_summary": False}))
    store = FakeStore()
    trivial = FakeBatch(
        "t-trivial2", ["d-triv2"],
        [_msg("m1", "joel@example.org", "2026-06-01", "Re: Hall B", "Thanks, sounds good.")],
    )
    monkeypatch.setattr(prepare, "_group_unenriched_threads",
                        lambda store, **kw: [trivial])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path))

    assert summary["threads"] == 1
    assert store.marked == []


# --- 2.3 thread block shape ------------------------------------------------

def test_prepare_thread_block_shape(monkeypatch):
    # Exercises _thread_block directly — the surviving assembly primitive both
    # build_pending (and, via it, prepare_units) call for each batch.
    batch = FakeBatch("t-a", ["d-a1"],
                      [_msg("m1", "joel@example.org", "2026-06-01", "Hall B", "body text")])
    store = FakeStore(
        contexts={"t-a": "Prior summary."},
        actions={"t-a": [{"id": 42, "owner": "Sam", "text": "Lodge it.", "deadline": "2026-06-10"}]},
    )
    _stub_reassemble(monkeypatch)

    t = prepare._thread_block(store, batch)

    assert t["thread_id"] == "t-a"
    assert t["prior_thread_context"] == "Prior summary."
    assert t["open_actions"][0]["id"] == 42
    m = t["messages"][0]
    assert set(m) >= {"message_id", "sender", "date", "labels", "subject", "text"}
    assert m["text"] == "body text"


def test_trivial_thread_keeps_short_commitments():
    # I2: a genuinely trivial ack is short-circuited, but a SHORT message carrying a
    # commitment/action must NOT be classified trivial — otherwise the model never
    # sees it and the action is silently dropped.
    from mcpbrain.prepare import is_trivial_thread
    assert is_trivial_thread([{"text": "Thanks, sounds good!"}]) is True
    assert is_trivial_thread([{"text": "Great, see you there."}]) is True
    assert is_trivial_thread(
        [{"text": "Confirmed, I'll send the contract Monday and wire the deposit."}]) is False
    assert is_trivial_thread(
        [{"text": "Sounds good — we'll follow up next week."}]) is False


def test_thread_block_has_org_hint(monkeypatch):
    """_thread_block attaches org_hint derived from the lead sender's domain,
    via graph_write.org_from_email against the configured taxonomy. A lead
    sender at a domain the taxonomy recognises yields a non-empty org_hint
    equal to what org_from_email would return directly.
    """
    from mcpbrain import orgs as _orgs

    tax = _orgs.OrgTaxonomy(
        names=("Centrepoint",),
        domain_map={"centrepoint.church": "Centrepoint"},
    )
    monkeypatch.setattr(_orgs, "taxonomy_from_config", lambda: tax)

    batch = FakeBatch("t-a", ["d-a1"], [
        _msg("m1", "Sam Lee <sam.lee@centrepoint.church>", "2026-06-01",
             "Hall B", "body text"),
    ])
    store = FakeStore()
    _stub_reassemble(monkeypatch)

    block = prepare._thread_block(store, batch)

    assert block["org_hint"] == "Centrepoint"


def test_thread_block_org_hint_empty_when_no_messages(monkeypatch):
    """No lead message (empty thread) degrades org_hint to '' rather than raising."""
    batch = FakeBatch("t-a", [], [])
    store = FakeStore()
    _stub_reassemble(monkeypatch)

    block = prepare._thread_block(store, batch)

    assert block["org_hint"] == ""


def test_prepare_messages_ordered_by_date(monkeypatch):
    batch = FakeBatch("t-a", ["d-a1", "d-a2"], [
        _msg("m2", "a@b.com", "2026-06-02", "Re: x", "second"),
        _msg("m1", "a@b.com", "2026-06-01", "x", "first"),
    ])
    store = FakeStore()
    _stub_reassemble(monkeypatch)

    msgs = prepare._thread_block(store, batch)["messages"]
    assert [m["message_id"] for m in msgs] == ["m1", "m2"]


def test_thread_block_falls_back_to_digest_when_thread_context_empty(monkeypatch):
    calls = {"digest": 0}

    class _Store(FakeStore):
        def thread_context(self, thread_id):
            return ""  # not yet synthesized
        def thread_summary_digest(self, thread_id, max_chars=1500):
            calls["digest"] += 1
            return "- 2026-06-01: Joel asked about Hall B."

    store = _Store()
    batch = FakeBatch("t1", ["d1"], [
        _msg("m1", "joel@example.org", "2026-06-01", "Hall B", "text"),
    ])
    _stub_reassemble(monkeypatch)

    block = prepare._thread_block(store, batch)

    assert calls["digest"] == 1
    assert block["prior_thread_context"] == "- 2026-06-01: Joel asked about Hall B."


def test_thread_block_prefers_real_synthesis_over_digest(monkeypatch):
    calls = {"digest": 0}

    class _Store(FakeStore):
        def thread_context(self, thread_id):
            return "A real synthesized narrative."
        def thread_summary_digest(self, thread_id, max_chars=1500):
            calls["digest"] += 1
            return "should never be used"

    store = _Store()
    batch = FakeBatch("t1", ["d1"], [
        _msg("m1", "joel@example.org", "2026-06-01", "Hall B", "text"),
    ])
    _stub_reassemble(monkeypatch)

    block = prepare._thread_block(store, batch)

    assert calls["digest"] == 0
    assert block["prior_thread_context"] == "A real synthesized narrative."


# --- 2.4 context + cap + long-thread guard ---------------------------------

def test_build_pending_attaches_context(tmp_path, monkeypatch):
    # build_pending's context is now the STANDING-only block (Task 14):
    # known_people moved to write_units, scoped per unit, and is no longer
    # part of what _build_context/build_pending produce.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"],
                      [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore()
    _stub_reassemble(monkeypatch)

    monkeypatch.setattr(prepare, "_org_domain_lines",
                        lambda: ["example.org → Acme"])

    data = prepare.build_pending(store, [batch], char_budget=100000,
                                 now=_NOW, resolution_due=False)
    ctx = data["context"]

    assert "known_people" not in ctx
    assert "community_summaries" not in ctx
    assert "projects" not in ctx
    assert "areas" not in ctx
    assert ctx["org_domain_map"] == ["example.org → Acme"]


def test_prepare_units_caps_threads(tmp_path, monkeypatch):
    # Thread-cap enforcement lives in prepare_units (kept = non_trivial[:thread_cap]),
    # not in build_pending, which assembles whatever batches it is given.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text('{"salience_gate": false}')
    batches = [
        FakeBatch(f"t-{i}", [f"d-{i}"],
                  [_msg(f"m{i}", "a@b.com", "2026-06-01", "x",
                        "Can you confirm the Hall B booking for Sunday?")])
        for i in range(5)
    ]
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: batches)
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=2, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path))
    assert summary["threads"] == 2


def test_build_pending_long_thread_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    big = "x" * 60
    batch = FakeBatch("t-long", ["d-1", "d-2", "d-3"], [
        _msg("m1", "a@b.com", "2026-06-01", "s1", big),
        _msg("m2", "a@b.com", "2026-06-02", "s2", big),
        _msg("m3", "a@b.com", "2026-06-03", "s3", big),
    ])
    store = FakeStore(contexts={"t-long": "Prior."})
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    data = prepare.build_pending(store, [batch], char_budget=100,
                                 now=_NOW, resolution_due=False)
    threads = data["threads"]

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


def test_split_long_thread_carries_org_hint_single_oversized_message():
    # The single-oversized-message early return must also carry org_hint.
    block = {
        "thread_id": "t-x",
        "prior_thread_context": "",
        "open_actions": [],
        "org_hint": "Centrepoint",
        "messages": [_msg("m-big", "a@b.com", "2026-06-01", "x", "x" * 200)],
    }
    parts = prepare._split_long_thread(block, char_budget=50)
    assert len(parts) == 1
    assert parts[0]["org_hint"] == "Centrepoint"


def test_split_long_thread_carries_org_hint_across_parts():
    # A thread split into multiple parts must carry org_hint into every part —
    # it's thread-level metadata (derived from the lead sender), same as
    # thread_id/prior_thread_context/open_actions, not per-message data.
    big = "x" * 60
    block = {
        "thread_id": "t-long",
        "prior_thread_context": "",
        "open_actions": [],
        "org_hint": "Centrepoint",
        "messages": [
            _msg("m1", "a@b.com", "2026-06-01", "s1", big),
            _msg("m2", "a@b.com", "2026-06-02", "s2", big),
            _msg("m3", "a@b.com", "2026-06-03", "s3", big),
        ],
    }
    parts = prepare._split_long_thread(block, char_budget=100)
    assert len(parts) > 1
    assert all(p["org_hint"] == "Centrepoint" for p in parts)


def test_split_long_thread_splits_within_a_single_message():
    """A Drive doc is ONE message, so the old between-messages split could not
    touch it — it logged a warning and shipped a 5MB unit no drainer could hold.
    Splitting at chunk seams is lossless: the parts concatenate to the original."""
    from mcpbrain.prepare import _split_long_thread
    pieces = [f"chunk{i} " + "x" * 90 for i in range(10)]
    block = {
        "thread_id": "f", "prior_thread_context": "", "open_actions": [],
        "org_hint": "",
        "messages": [{"message_id": "f", "sender": "", "date": "", "labels": "",
                      "subject": "doc.pdf", "text": "\n\n".join(pieces),
                      "chunk_doc_ids": [f"gdrive-f-{i}" for i in range(10)],
                      "chunk_pieces": pieces, "chunk_has_gap": False}],
    }
    parts = _split_long_thread(block, 300)
    assert len(parts) > 1
    assert [p["part"] for p in parts] == list(range(1, len(parts) + 1))
    assert all(p["of"] == len(parts) for p in parts)
    # Lossless: every chunk appears exactly once, in order.
    covered = [d for p in parts for d in p["part_doc_ids"]]
    assert covered == [f"gdrive-f-{i}" for i in range(10)]
    # And the text survives intact.
    rejoined = "\n\n".join(m["text"] for p in parts for m in p["messages"])
    assert rejoined == "\n\n".join(pieces)


def test_split_long_thread_short_message_is_untouched():
    from mcpbrain.prepare import _split_long_thread
    block = {"thread_id": "t", "prior_thread_context": "", "open_actions": [],
             "org_hint": "",
             "messages": [{"message_id": "m1", "text": "short",
                           "chunk_doc_ids": ["gmail-m1-0"]}]}
    assert _split_long_thread(block, 24000) == [block]


def test_split_message_at_seams_when_chunks_pack_several_paragraphs():
    """The common real case, which the old "\\n\\n"-re-split derivation could not
    handle: chunk_text packs several paragraphs into ONE chunk whenever they fit
    the budget, so a chunk's stored text contains internal blank lines. Splitting
    the JOINED message on "\\n\\n" then produced more pieces than chunk_doc_ids
    (60 paragraphs / 8 chunks, measured) and the length guard shipped the message
    whole — defeating the whole point of seam splitting for ordinary documents."""
    from mcpbrain.thread_enrich import _CHUNK_JOIN
    from mcpbrain.prepare import _split_message_at_seams

    # 4 chunks, each holding 3 paragraphs -> 12 pieces if naively re-split.
    chunk_pieces = [_CHUNK_JOIN.join(f"c{c}p{p} " + "x" * 60 for p in range(3))
                    for c in range(4)]
    msg = {"message_id": "f", "text": _CHUNK_JOIN.join(chunk_pieces),
           "chunk_doc_ids": [f"gdrive-f-{i}" for i in range(4)],
           "chunk_pieces": chunk_pieces, "chunk_has_gap": False}
    assert len(msg["text"].split(_CHUNK_JOIN)) == 12 != len(msg["chunk_doc_ids"])

    out = _split_message_at_seams(msg, 500)

    assert len(out) > 1                                   # it now actually splits
    covered = [d for p in out for d in p["chunk_doc_ids"]]
    assert covered == [f"gdrive-f-{i}" for i in range(4)]  # every chunk once, in order
    assert _CHUNK_JOIN.join(p["text"] for p in out) == msg["text"]   # lossless


def test_split_message_at_seams_bails_on_a_gap_marker():
    """A partially-enriched/cold document gets a gap marker, so the pieces no
    longer reconstruct the text. Ship whole rather than mark the wrong rows."""
    from mcpbrain.thread_enrich import _CHUNK_JOIN, _GAP_MARKER
    from mcpbrain.prepare import _split_message_at_seams

    pieces = ["a" * 300, "b" * 300, "c" * 300]
    msg = {"message_id": "f",
           "text": pieces[0] + _GAP_MARKER + pieces[1] + _CHUNK_JOIN + pieces[2],
           "chunk_doc_ids": ["d0", "d2", "d3"],
           "chunk_pieces": pieces, "chunk_has_gap": True}
    assert _split_message_at_seams(msg, 400) == [msg]


def test_split_message_at_seams_bails_without_chunk_pieces():
    """A unit written before this change carries no chunk_pieces; splitting on a
    guess is exactly what this fix removes."""
    from mcpbrain.prepare import _split_message_at_seams

    msg = {"message_id": "f", "text": "a" * 300 + "\n\n" + "b" * 300,
           "chunk_doc_ids": ["d0", "d1"]}
    assert _split_message_at_seams(msg, 100) == [msg]


# --- 2.5 merge-review block ------------------------------------------------

def test_build_pending_no_merge_review_when_not_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"], [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore(entities=[
        {"id": "joel-chelliah", "name": "Joel Chelliah", "type": "person"},
        {"id": "joel-c", "name": "Joel C", "type": "person"},
    ])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    data = prepare.build_pending(store, [batch], char_budget=100000,
                                 now=_NOW, resolution_due=False)
    assert data["merge_review"] == []


def test_build_pending_appends_merge_review_when_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    batch = FakeBatch("t-a", ["d-a1"], [_msg("m1", "a@b.com", "2026-06-01", "x", "body")])
    store = FakeStore(entities=[
        {"id": "joel-chelliah", "name": "Joel Chelliah", "type": "person"},
        {"id": "joel-c", "name": "Joel C", "type": "person"},
    ])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    data = prepare.build_pending(store, [batch], char_budget=100000,
                                 now=_NOW, resolution_due=True)
    mr = data["merge_review"]
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


# --- 2.6 atomic write (now exercised through write_units, see below) -------
#
# The old test_prepare_writes_pending_file / test_prepare_overwrites_previous
# tested _write_pending's single-file pending.json shape and its per-cycle
# whole-file overwrite semantics. Neither has a surviving equivalent:
# write_units (the real producer) writes a bounded QUEUE of immutable,
# content-addressed unit files that ACCUMULATE across cycles rather than being
# overwritten — the opposite of the old semantics. build_pending's assembled
# dict shape (batch_id/prepared_at format) is covered by
# test_build_pending_returns_dict_without_writing below; write_units' actual
# file-writing mechanics are covered by test_prepare_units_writes_unit_files_and_context
# above and the test_write_units_* tests below.

def test_prepare_units_no_unenriched_writes_no_unit_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = FakeStore()
    monkeypatch.setattr(prepare, "_group_unenriched_threads", lambda store, **kw: [])
    _stub_reassemble(monkeypatch)
    _stub_context(monkeypatch)

    summary = prepare.prepare_units(store, thread_cap=10, char_budget=100000,
                                    resolution_due=False, now=_NOW, home=str(tmp_path))
    assert summary["threads"] == 0
    units_dir = tmp_path / "enrich_queue" / "units"
    units = list(units_dir.glob("*.json")) if units_dir.exists() else []
    assert units == []
    # No shared context.json: context is scoped and written per unit now, and
    # there is no work here to write any unit for.
    assert not (tmp_path / "enrich_queue" / "context.json").exists()
    assert not (tmp_path / "enrich_queue" / "pending.json").exists()


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


# --- unit_pull_cap: configurable cap and lockstep assertion ----------------

def test_unit_pull_cap_default():
    # config.unit_pull_cap() must return 60_000 when unconfigured.
    from mcpbrain import config
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        assert config.unit_pull_cap(td) == 60_000


def test_unit_pull_cap_from_config():
    # config.unit_pull_cap() must respect an explicit 'unit_pull_cap' in config.json.
    from mcpbrain import config
    import tempfile, json
    with tempfile.TemporaryDirectory() as td:
        (config._path(td)).write_text(json.dumps({"unit_pull_cap": 80_000}))
        assert config.unit_pull_cap(td) == 80_000


def test_write_units_reads_cap_at_call_time(tmp_path, monkeypatch):
    from mcpbrain import prepare, config
    calls = {"n": 0}
    def _fake_cap(home=None):
        calls["n"] += 1
        return 12_345
    monkeypatch.setattr(config, "unit_pull_cap", _fake_cap)
    prepare.write_units({"context": {}, "threads": []}, home=str(tmp_path))
    assert calls["n"] >= 1, "write_units must read unit_pull_cap at call time, not import"


def test_write_units_packs_more_threads_with_higher_cap(tmp_path, monkeypatch):
    # With a higher pull_cap, write_units fits more threads into each unit file,
    # yielding fewer unit files for the same input.
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    import json as _json
    from mcpbrain import prepare

    # Build threads each ~4KB serialized — three should pack into one unit at
    # 60K cap (budget ~47K), but split into two at 12K cap (budget ~-1K → clamped
    # to 2K, fitting only one thread per unit).
    thread_body = "x" * 4000
    threads = [
        {"thread_id": f"t-{i}", "messages": [{"text": thread_body}]}
        for i in range(3)
    ]
    data = {"threads": threads, "context": {}}

    # Small cap — threads must split into multiple units.
    units_dir_small = tmp_path / "small" / "enrich_queue" / "units"
    summary_small = prepare.write_units(
        data, home=str(tmp_path / "small"), pull_cap=12_000
    )

    # Large cap — all threads fit in fewer units.
    units_dir_big = tmp_path / "big" / "enrich_queue" / "units"
    summary_big = prepare.write_units(
        data, home=str(tmp_path / "big"), pull_cap=60_000
    )

    assert summary_big["units_written"] < summary_small["units_written"], (
        f"Expected fewer units at high cap ({summary_big['units_written']}) than "
        f"low cap ({summary_small['units_written']})"
    )


def test_write_units_emits_a_unit_per_review_block(tmp_path):
    """The review cadence's blocks must become real work units. They were
    silently dropped because write_units only iterated UNIT_BLOCKS and the
    review families were missing from it."""
    data = {
        "batch_id": "batch-review",
        "prepared_at": "2026-07-25T00:00:00Z",
        "context": {},
        "threads": [],
        "review_orphan": [
            {"finding_id": 1, "packet": {"finding_type": "lint:orphan_entity",
                                         "ref_id": "e-ghost"}},
        ],
        "org_merge_review": [
            {"pair_id": "p-1", "a": {"name": "ACC"}, "b": {"name": "ACCI"}},
        ],
    }

    summary = prepare.write_units(data, home=str(tmp_path))

    blocks = {}
    for path in (tmp_path / "enrich_queue" / "units").glob("*.json"):
        unit = json.loads(path.read_text())
        if unit["kind"] == "block":
            blocks[unit["block"]] = unit["items"]

    assert "review_orphan" in blocks, f"got block units: {sorted(blocks)}"
    assert "org_merge_review" in blocks, f"got block units: {sorted(blocks)}"
    assert blocks["review_orphan"][0]["finding_id"] == 1
    assert summary["units_written"] == 2


# --- 2.7 should_enrich gate: cold-marking by source -----

def test_should_enrich_cold_marks_drive_html():
    """Drive text/html is a saved web page, not a document. On the live store it
    was exactly two files (5.07MB) — a SHEIN shop page and a Bookabin payment
    page. Cold is reversible and keeps them searchable."""
    from mcpbrain.prepare import should_enrich
    chunk = {"text": "x" * 5000,
             "metadata": {"source_type": "gdrive", "file_id": "f1",
                          "mime_type": "text/html", "content_subtype": "prose"}}
    assert should_enrich(chunk) is False


def test_should_enrich_keeps_drive_pdf():
    """Guard against over-broadening: a long PDF is still extracted."""
    from mcpbrain.prepare import should_enrich
    chunk = {"text": "x" * 5000,
             "metadata": {"source_type": "gdrive", "file_id": "f2",
                          "mime_type": "application/pdf", "content_subtype": "prose"}}
    assert should_enrich(chunk) is True


# --- Task 12: per-unit known_people scoping --------------------------------

def test_parse_aliases_splits_json_list_and_pipes():
    """entities.aliases stores pipe-delimited strings INSIDE JSON list elements."""
    from mcpbrain.prepare import _parse_aliases
    assert _parse_aliases('["Pete|Peter", "Ps Pete"]') == ["Pete", "Peter", "Ps Pete"]
    assert _parse_aliases("") == []
    assert _parse_aliases(None) == []


def test_scoped_known_people_keeps_core_and_mentioned_only():
    from mcpbrain.prepare import _build_people_index, _scoped_known_people
    core = [{"id": "c1", "name": "Core Person", "org": "Acme", "role": "CEO"}]
    pool = [
        {"id": "p1", "name": "Taryn Hamilton", "org": "Acme", "role": "Pastor",
         "aliases": []},
        {"id": "p2", "name": "Nobody Mentioned", "org": "Acme", "role": "X",
         "aliases": []},
    ]
    out = _scoped_known_people(core, _build_people_index(pool),
                               "please ask taryn hamilton about hall b")
    ids = [p["id"] for p in out]
    assert "c1" in ids and "p1" in ids and "p2" not in ids


def test_scoped_known_people_matches_on_alias():
    from mcpbrain.prepare import _build_people_index, _scoped_known_people
    pool = [{"id": "p1", "name": "Peter Hammer", "org": "Acme", "role": "X",
             "aliases": ["Pete|Peter"]}]
    out = _scoped_known_people([], _build_people_index(pool), "pete is away")
    assert [p["id"] for p in out] == ["p1"]


def test_scoped_known_people_respects_the_cap_and_keeps_core_first():
    from mcpbrain.prepare import _build_people_index, _scoped_known_people
    core = [{"id": f"c{i}", "name": f"Core{i} Person", "org": "Acme", "role": "R"}
            for i in range(40)]
    out = _scoped_known_people(core, _build_people_index([]), "", cap=500)
    import json
    assert len(json.dumps(out)) <= 500
    assert out and out[0]["id"] == "c0"      # core ranks first, never trimmed away


# --- Task 14: per-unit context replaces context.json ------------------------

def test_write_units_writes_context_into_each_unit(tmp_path):
    from mcpbrain.prepare import write_units
    data = {"threads": [{"thread_id": "t1",
                         "messages": [{"message_id": "m1", "text": "hi taryn"}]}],
            "context": {"owner_name": "Josh", "valid_orgs": ["Acme"],
                        "org_domain_map": [], "known_people": []}}
    write_units(data, home=str(tmp_path))
    import glob
    import json
    (f,) = glob.glob(str(tmp_path / "enrich_queue" / "units" / "*.json"))
    unit = json.loads(open(f).read())
    assert unit["context"]["owner_name"] == "Josh"
    assert not (tmp_path / "enrich_queue" / "context.json").exists()


def test_unit_payload_reads_the_units_own_context(tmp_path):
    from mcpbrain.tools import _unit_payload
    d = {"kind": "thread", "threads": [],
         "context": {"owner_name": "Josh", "known_people": [{"id": "a"}]}}
    out = _unit_payload(str(tmp_path), d, "u-1", False)
    assert out["context"]["known_people"] == [{"id": "a"}]


def test_context_carries_no_community_summaries():
    """Dead payload: 6,255 bytes/unit that nothing reads — not enrich_prompt.md,
    not the enrich-batch agent, not routines/enrich.md."""
    from mcpbrain.prepare import _build_context
    assert "community_summaries" not in _build_context(None, [])


def test_write_units_scopes_known_people_per_unit(tmp_path):
    # The unit's known_people must reflect what THAT unit's text mentions, not
    # a batch-wide list: a person mentioned in unit A's text should not appear
    # in unit B's context when unit B never mentions them.
    from mcpbrain.prepare import write_units
    core = [{"id": "c1", "name": "Core Person", "org": "Acme", "role": "CEO"}]
    pool = [{"id": "p1", "name": "Taryn Hansen", "org": "Acme", "role": "Pastor",
             "aliases": []}]
    # Padded so two threads together exceed the packing budget (>= 2000 bytes),
    # forcing each into its OWN unit -- scoping must be per-unit, not per-batch.
    filler = "padding " * 150
    data = {
        "threads": [
            {"thread_id": "t-a", "messages": [{"message_id": "m1",
                                               "text": f"ask taryn hansen {filler}"}]},
            {"thread_id": "t-b", "messages": [{"message_id": "m2",
                                               "text": f"totally unrelated {filler}"}]},
        ],
        "context": {"owner_name": "Josh"},
        "people_core": core, "people_pool": pool,
    }
    write_units(data, home=str(tmp_path), pull_cap=2_100)
    import json
    units = {}
    for f in (tmp_path / "enrich_queue" / "units").glob("*.json"):
        d = json.loads(f.read_text())
        units[d["threads"][0]["thread_id"]] = d
    assert len(units) == 2, "test setup must actually split into two units"
    a_ids = {p["id"] for p in units["t-a"]["context"]["known_people"]}
    b_ids = {p["id"] for p in units["t-b"]["context"]["known_people"]}
    assert "p1" in a_ids and "p1" not in b_ids
    assert "c1" in a_ids and "c1" in b_ids  # core is on every unit


def test_write_units_builds_the_people_index_once_per_call(tmp_path, monkeypatch):
    # The whole point of indexing once per write_units() call (not once per
    # unit) is avoiding an O(people) scan repeated per unit -- ~5,000 names x
    # ~130 units of wasted work every cycle on the live corpus.
    from mcpbrain import prepare
    calls = {"n": 0}
    real_index = prepare._build_people_index

    def counting_index(pool):
        calls["n"] += 1
        return real_index(pool)

    monkeypatch.setattr(prepare, "_build_people_index", counting_index)
    threads = [{"thread_id": f"t-{i}", "messages": [{"message_id": f"m{i}",
                                                     "text": "x" * 500}]}
              for i in range(5)]
    data = {"threads": threads, "context": {}, "people_pool": [], "people_core": []}
    prepare.write_units(data, home=str(tmp_path), pull_cap=2_100)
    units = list((tmp_path / "enrich_queue" / "units").glob("*.json"))
    assert len(units) > 1, "test setup must actually produce multiple units"
    assert calls["n"] == 1


def test_packing_budget_is_deterministic_and_large():
    """The old budget was max(2000, 60000 - 11000 - 45508 - 1500) = 2000 — the
    floor — because context.json had grown to 45KB. That is what made units
    one-thread-each and 7x underfilled."""
    from mcpbrain.prepare import CONTEXT_CAP, _UNIT_RULES_RESERVE
    budget = max(2000, 60_000 - _UNIT_RULES_RESERVE - CONTEXT_CAP - 1500)
    assert budget > 30_000


def test_reserve_is_not_the_stale_literal():
    from mcpbrain.prepare import _UNIT_RULES_RESERVE
    assert _UNIT_RULES_RESERVE != 11_000
    assert _UNIT_RULES_RESERVE >= 12_000


def test_no_unit_exceeds_pull_cap_with_rules(tmp_path):
    """The invariant ALL 868 live units violate today: 45,511 (context)
    + 24,554 (rules) = 70,065 > 60,000 before any work is added."""
    import glob, json
    from mcpbrain.prepare import write_units
    from mcpbrain.tools import _unit_payload
    data = {"threads": [{"thread_id": f"t{i}",
                         "messages": [{"message_id": f"m{i}", "text": "x" * 3000}]}
                        for i in range(20)],
            "context": {"owner_name": "Josh", "valid_orgs": [], "org_domain_map": []},
            "people_core": [], "people_pool": []}
    write_units(data, home=str(tmp_path))
    for f in glob.glob(str(tmp_path / "enrich_queue" / "units" / "*.json")):
        d = json.load(open(f))
        payload = _unit_payload(str(tmp_path), d, d["unit_id"], True)
        assert len(json.dumps(payload)) <= 60_000, f
