"""Source controls (console redesign Task 4): per-source on/off and sync,
sender/label exclusions, cold and waiting lists, re-include and priority."""
import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from mcpbrain import config
from mcpbrain.store import Store


def _store(tmp_path, name="s.sqlite3"):
    s = Store(str(tmp_path / name), dim=4)
    s.init()
    return s


def _cfg(tmp_path, cfg: dict):
    (tmp_path / "config.json").write_text(json.dumps(cfg))


def _queue(store, source, ref_id, modified_at="2026-09-04T00:00:00"):
    store.enqueue_and_advance(
        [{"ref_id": ref_id, "version": "1", "event": "upsert",
          "modified_at": modified_at}], source=source, cursor="c-" + ref_id)


def _queue_row(store, source, ref_id):
    with store._connect() as db:
        r = db.execute("SELECT * FROM sync_queue WHERE source=? AND ref_id=?",
                       (source, ref_id)).fetchone()
    return dict(r) if r else None


# --- config accessors --------------------------------------------------------

def test_source_enabled_defaults_true_and_reads_sources_block(tmp_path):
    assert config.source_enabled(str(tmp_path), "gmail") is True
    assert config.source_enabled(str(tmp_path), "drive") is True
    assert config.source_enabled(str(tmp_path), "calendar") is True
    assert config.source_enabled(str(tmp_path), "anarlog") is False   # opt-in
    _cfg(tmp_path, {"sources": {"gmail": False}, "anarlog": {"enabled": True}})
    assert config.source_enabled(str(tmp_path), "gmail") is False
    assert config.source_enabled(str(tmp_path), "drive") is True
    assert config.source_enabled(str(tmp_path), "anarlog") is True


def test_exclusion_accessors_read_ingest_lists(tmp_path):
    assert config.excluded_senders(str(tmp_path)) == []
    assert config.excluded_labels(str(tmp_path)) == []
    _cfg(tmp_path, {"ingest": {"exclude_senders": [" News@Example.org ", ""],
                               "exclude_labels": ["Newsletters"]}})
    assert config.excluded_senders(str(tmp_path)) == ["News@Example.org"]
    assert config.excluded_labels(str(tmp_path)) == ["Newsletters"]


# --- per-source switch in the sync cycle -------------------------------------

def test_disabled_gmail_skips_discovery_and_defers_queued_rows(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    _cfg(tmp_path, {"sources": {"gmail": False}})
    s = _store(tmp_path)
    _queue(s, "gmail", "m1")
    called = []
    monkeypatch.setattr(sync_mod, "discover_gmail",
                        lambda *a, **k: called.append("discover") or 0)
    monkeypatch.setattr(sync_mod, "handle_gmail_item",
                        lambda *a, **k: called.append("work"))
    out = sync_mod.run_sync_cycle(s, embedder=None, gmail_service=object(),
                                  home=str(tmp_path))
    assert called == []
    assert out["worked"] == {"processed": 0, "failed": 0}
    row = _queue_row(s, "gmail", "m1")
    assert row is not None, "a disabled source's row is kept, not dropped"
    assert row["attempts"] == 0 and not row["last_error"], "deferred, not failed"
    # Delayed, so it does not hold the head of the queue every cycle.
    assert row["next_attempt_at"] and row["next_attempt_at"] > datetime.now(
        timezone.utc).replace(tzinfo=None).isoformat()


def test_disabled_anarlog_defers_queued_rows(tmp_path):
    from mcpbrain import sync as sync_mod
    s = _store(tmp_path)
    _queue(s, "anarlog", "sess-1")
    out = sync_mod.run_sync_cycle(s, embedder=None, home=str(tmp_path))
    assert out["worked"]["failed"] == 0
    row = _queue_row(s, "anarlog", "sess-1")
    assert row is not None and row["attempts"] == 0


def test_sync_only_runs_one_source(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    s = _store(tmp_path)
    _queue(s, "gmail", "m1", modified_at="2026-09-05T00:00:00")
    calls = []
    monkeypatch.setattr(sync_mod, "discover_gmail",
                        lambda *a, **k: calls.append("gmail") or 0)
    monkeypatch.setattr(sync_mod, "discover_calendar",
                        lambda *a, **k: calls.append("calendar") or 0)

    def fake_drive(service, store, source="drive", *, budget=None):
        calls.append("drive")
        _queue(store, "drive", "f1", modified_at="2026-09-04T00:00:00")
        return 1

    monkeypatch.setattr(sync_mod, "discover_drive", fake_drive)
    monkeypatch.setattr(sync_mod, "handle_drive_item",
                        lambda *a, **k: calls.append("work-drive"))
    monkeypatch.setattr(sync_mod, "handle_gmail_item",
                        lambda *a, **k: calls.append("work-gmail"))
    out = sync_mod.run_sync_cycle(s, embedder=None, gmail_service=object(),
                                  calendar_service=object(), drive_service=object(),
                                  home=str(tmp_path), only="drive")
    assert calls == ["drive", "work-drive"]
    assert out["worked"] == {"processed": 1, "failed": 0}
    row = _queue_row(s, "gmail", "m1")
    assert row is not None and row["attempts"] == 0 and row["next_attempt_at"] is None


def test_sync_now_source_is_one_shot(tmp_path, monkeypatch):
    import mcpbrain.daemon as daemon_module
    from mcpbrain.daemon import Daemon
    from mcpbrain.daemon import SingleWriterLock

    class _Emb:
        dim = 4

    d = Daemon(_store(tmp_path), _Emb(),
               services={"gmail_service": object(), "drive_service": object()},
               lock=SingleWriterLock(tmp_path / "d.lock"))
    seen = []
    monkeypatch.setattr(daemon_module, "run_cycle",
                        lambda store, emb, **kw: seen.append(kw) or {})
    d.sync_now("drive")
    d.run_one()
    d.run_one()
    assert seen[0]["sync_only"] == "drive"
    assert seen[1].get("sync_only") is None
    d.sync_now("drive")
    d.sync_now()                 # a later "sync everything" wins
    d.run_one()
    assert seen[2].get("sync_only") is None


# --- Gmail sender / label exclusions -----------------------------------------

def _b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode()


def _raw(mid, sender, labels=("INBOX",), body="Hall B is booked for Sunday."):
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels),
            "payload": {"mimeType": "text/plain",
                        "headers": [{"name": "Subject", "value": "Hall B"},
                                    {"name": "From", "value": sender}],
                        "body": {"data": _b64(body)}}}


class _Req:
    def __init__(self, r):
        self._r = r

    def execute(self, num_retries=0):
        return self._r


class _Gmail:
    def __init__(self, by_id, labels=()):
        self._by_id = by_id
        self._labels = list(labels)
        self.label_lists = 0

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        outer = self

        class _L:
            def list(self, userId):
                outer.label_lists += 1
                return _Req({"labels": outer._labels})
        return _L()

    def get(self, userId, id, format):
        return _Req(self._by_id[id])


def _skips(store, kind):
    with store._connect() as db:
        return [dict(r) for r in db.execute(
            "SELECT ref_id, summary FROM change_log WHERE change_type='ingest_skip' "
            "AND summary LIKE ?", (kind + "%",))]


def test_excluded_sender_is_not_indexed_and_skip_is_recorded(tmp_path):
    from mcpbrain.sync.gmail import GmailExclusions, handle_gmail_item
    s = _store(tmp_path)
    svc = _Gmail({"m1": _raw("m1", "Acme News <news@acme.example.org>"),
                  "m2": _raw("m2", "Sam Chen <sam@example.org>")})
    ex = GmailExclusions.build(senders=["acme.example.org"], labels=[])
    for mid in ("m1", "m2"):
        handle_gmail_item(svc, s, {"source": "gmail", "ref_id": mid}, exclusions=ex)
    assert s.get_chunk("gmail-m1-body-0") is None
    assert s.get_chunk("gmail-m2-body-0") is not None
    rows = _skips(s, "gmail_excluded")
    assert len(rows) == 1
    assert rows[0]["ref_id"] == "m1"
    assert "sender" in rows[0]["summary"] and "acme.example.org" in rows[0]["summary"]


def test_excluded_label_by_name_is_not_indexed(tmp_path):
    from mcpbrain.sync.gmail import GmailExclusions, handle_gmail_item, resolve_label_ids
    s = _store(tmp_path)
    svc = _Gmail({"m1": _raw("m1", "Pat Nguyen <pat@example.org>", labels=("INBOX", "Label_7")),
                  "m2": _raw("m2", "Pat Nguyen <pat@example.org>")},
                 labels=[{"id": "Label_7", "name": "Newsletters"}])
    ids = resolve_label_ids(s, svc, ["newsletters"])
    assert ids == {"newsletters": "Label_7"}
    # cached: a second resolve does not list again
    resolve_label_ids(s, svc, ["newsletters"])
    assert svc.label_lists == 1
    ex = GmailExclusions.build(senders=[], labels=["newsletters"], label_ids=ids)
    for mid in ("m1", "m2"):
        handle_gmail_item(svc, s, {"source": "gmail", "ref_id": mid}, exclusions=ex)
    assert s.get_chunk("gmail-m1-body-0") is None
    assert s.get_chunk("gmail-m2-body-0") is not None
    rows = _skips(s, "gmail_excluded")
    assert len(rows) == 1 and "label" in rows[0]["summary"]
    assert "newsletters" in rows[0]["summary"].lower()


def test_sender_rule_forms():
    from mcpbrain.sync.gmail import GmailExclusions
    ex = GmailExclusions.build(senders=["news@acme.example.org", "@example.net",
                                        "example.com"], labels=["CATEGORY_PROMOTIONS"])
    assert ex.match("News <NEWS@acme.example.org>", [])
    assert not ex.match("Other <other@acme.example.org>", [])
    assert ex.match("x <a@example.net>", [])
    assert ex.match("x <a@mail.example.com>", [])        # subdomain of a domain rule
    assert not ex.match("x <a@notexample.com>", [])
    assert ex.match("x <a@b.org>", ["INBOX", "CATEGORY_PROMOTIONS"])
    assert ex.match("x <a@b.org>", ["INBOX"]) is None


def test_empty_body_skip_is_flushed(tmp_path):
    from mcpbrain.sync.gmail import handle_gmail_item
    s = _store(tmp_path)
    svc = _Gmail({"m1": _raw("m1", "sam@example.org", body="")})
    handle_gmail_item(svc, s, {"source": "gmail", "ref_id": "m1"})
    rows = _skips(s, "gmail_empty_body")
    assert len(rows) == 1 and rows[0]["ref_id"] == "m1"


def test_cycle_applies_configured_exclusions(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    _cfg(tmp_path, {"ingest": {"exclude_senders": ["acme.example.org"]}})
    s = _store(tmp_path)
    _queue(s, "gmail", "m1")
    monkeypatch.setattr(sync_mod, "discover_gmail", lambda *a, **k: 0)
    monkeypatch.setattr(sync_mod, "generated_label_ids", lambda *a, **k: frozenset())
    svc = _Gmail({"m1": _raw("m1", "news@acme.example.org")})
    out = sync_mod.run_sync_cycle(s, embedder=None, gmail_service=svc,
                                  home=str(tmp_path))
    assert out["worked"]["processed"] == 1
    assert s.get_chunk("gmail-m1-body-0") is None
    assert len(_skips(s, "gmail_excluded")) == 1


# --- re-include, priority, cold/waiting lists --------------------------------

def _chunk(store, doc_id, *, thread, labels="INBOX", subject="Hall B", body=None,
           **extra):
    meta = {"source_type": "gmail", "thread_id": thread, "message_id": doc_id,
            "subject": subject, "sender": "Sam Chen <sam@example.org>",
            "to": "dana@example.org", "date": "Mon, 2 Nov 2026 09:00:00 +0000",
            "labels": labels, "content_type": "email_body", **extra}
    store.upsert_chunk(doc_id, body or f"Can you confirm the booking for {doc_id}?",
                       "h-" + doc_id, meta)


def test_reinclude_keeps_promotions_chunk_eligible_through_prepare(tmp_path, monkeypatch):
    """Review Focus 4: the salience and derived gates must not re-cold it."""
    from mcpbrain import prepare
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _cfg(tmp_path, {"salience_gate": True})
    s = _store(tmp_path)
    _chunk(s, "promo-1", thread="t-promo", labels="INBOX,CATEGORY_PROMOTIONS")
    _chunk(s, "draft-1", thread="t-draft", labels="DRAFT")
    prepare.prepare_units(s, thread_cap=10, char_budget=100000,
                          resolution_due=False, home=str(tmp_path))
    state = {c["doc_id"] for c in s.unenriched_chunks()}
    assert "promo-1" not in state and "draft-1" not in state   # both went cold

    assert s.reinclude_chunks(["promo-1", "draft-1"]) == 2
    prepare.prepare_units(s, thread_cap=10, char_budget=100000,
                          resolution_due=False, home=str(tmp_path))
    with s._connect() as db:
        rows = {r["doc_id"]: dict(r) for r in db.execute(
            "SELECT doc_id, enrich_state, enrich_attempts FROM chunks")}
    assert rows["promo-1"]["enrich_state"] != "cold"
    assert rows["draft-1"]["enrich_state"] != "cold"
    meta = s.get_chunk("promo-1")["metadata"]
    assert meta["force_enrich"] is True
    units = [json.loads(p.read_text())
             for p in (tmp_path / "enrich_queue" / "units").glob("*.json")]
    tids = {t["thread_id"] for u in units if u["kind"] == "thread" for t in u["threads"]}
    assert {"t-promo", "t-draft"} <= tids


def test_reinclude_resets_enrich_columns(tmp_path):
    s = _store(tmp_path)
    _chunk(s, "c1", thread="t1")
    s.set_enrich_state(["c1"], "cold")
    s.mark_enriched(["c1"])
    s.bump_enrich_attempts(["c1"])
    assert s.reinclude_chunks(["c1", "missing"]) == 1
    with s._connect() as db:
        r = dict(db.execute("SELECT enriched, enriched_version, enrich_state, "
                            "enrich_attempts FROM chunks WHERE doc_id='c1'").fetchone())
    assert r == {"enriched": 0, "enriched_version": 0, "enrich_state": "",
                 "enrich_attempts": 0}


def test_should_enrich_and_derived_honour_force_enrich():
    from mcpbrain import prepare
    promo = {"doc_id": "x", "text": "t", "metadata": {
        "source_type": "gmail", "thread_id": "t", "labels": "CATEGORY_PROMOTIONS"}}
    assert prepare.should_enrich(promo) is False
    promo["metadata"]["force_enrich"] = True
    assert prepare.should_enrich(promo) is True
    draft = {"doc_id": "y", "text": "t", "metadata": {
        "source_type": "gmail", "thread_id": "t", "labels": "DRAFT", "force_enrich": True}}
    assert prepare._is_derived(draft, None) is False


def test_prioritised_chunks_come_first(tmp_path):
    s = _store(tmp_path)
    for i in range(4):
        _chunk(s, f"c{i}", thread=f"t{i}")
    assert [c["doc_id"] for c in s.unenriched_chunks()] == ["c3", "c2", "c1", "c0"]
    assert s.prioritise_chunks(["c0", "c1", "nope"]) == 2
    assert [c["doc_id"] for c in s.unenriched_chunks()] == ["c1", "c0", "c3", "c2"]
    assert [c["doc_id"] for c in s.unenriched_chunks(limit=1)] == ["c1"]


def test_prioritised_thread_is_grouped_first(tmp_path):
    from mcpbrain import thread_enrich
    s = _store(tmp_path)
    for i in range(3):
        _chunk(s, f"c{i}", thread=f"t{i}")
    s.prioritise_chunks(["c0"])
    batches = thread_enrich.group_unenriched_threads(s, thread_cap=1)
    assert [b.thread_id for b in batches] == ["t0"]


def test_cold_reason_names_the_rule():
    from mcpbrain.source_class import Rules
    from mcpbrain.source_controls import cold_reason
    g = {"source_type": "gmail", "thread_id": "t"}
    assert cold_reason({**g, "labels": "INBOX,CATEGORY_PROMOTIONS"}, "cold") == "promotions"
    assert cold_reason({**g, "bulk": True}, "cold") == "bulk"
    assert cold_reason({"content_subtype": "table"}, "cold") == "table"
    assert cold_reason({**g, "labels": "DRAFT", "superseded": True}, "cold") == "superseded draft"
    assert cold_reason({"observation_type": "consolidated"}, "cold") == "consolidated note"
    rules = Rules(owner_emails=frozenset({"dana@example.org"}),
                  subject_prefixes=("[Brief]",), drive_folders=("Acme/Generated",))
    own = {**g, "subject": "[Brief] Monday", "sender": "dana@example.org",
           "to": "dana@example.org"}
    assert cold_reason(own, "cold", rules=rules) == "own automated mail"
    drv = {"source_type": "gdrive", "folder_path": "Acme/Generated/Week 1"}
    assert cold_reason(drv, "cold", rules=rules) == "Drive folder"
    assert cold_reason({**g}, "") == ""


def test_cold_and_waiting_lists_page_with_reason(tmp_path):
    from mcpbrain import source_controls as sc
    s = _store(tmp_path)
    for i in range(5):
        _chunk(s, f"p{i}", thread=f"tp{i}", labels="CATEGORY_PROMOTIONS",
               subject=f"Sale {i}")
    s.set_enrich_state([f"p{i}" for i in range(5)], "cold")
    for i in range(3):
        _chunk(s, f"w{i}", thread=f"tw{i}")
    s.prioritise_chunks(["w0"])
    page1 = sc.list_cold(s, str(tmp_path), page=1, page_size=2)
    assert page1["total"] == 5 and page1["page"] == 1
    assert [i["doc_id"] for i in page1["items"]] == ["p4", "p3"]
    it = page1["items"][0]
    assert set(it) == {"doc_id", "title", "source", "date", "reason"}
    assert it["title"] == "Sale 4" and it["source"] == "gmail" and it["reason"] == "promotions"
    page3 = sc.list_cold(s, str(tmp_path), page=3, page_size=2)
    assert [i["doc_id"] for i in page3["items"]] == ["p0"]
    waiting = sc.list_waiting(s, str(tmp_path), page=1, page_size=50)
    assert waiting["total"] == 3
    assert [i["doc_id"] for i in waiting["items"]] == ["w0", "w2", "w1"]
    assert waiting["items"][0]["reason"] == "prioritised"
    s.reinclude_chunks(["p0"])
    waiting = sc.list_waiting(s, str(tmp_path), page=1, page_size=50)
    assert {i["doc_id"]: i["reason"] for i in waiting["items"]}["p0"] == "re-included"


def test_reflow_gave_up_list(tmp_path):
    from mcpbrain import source_controls as sc
    s = _store(tmp_path)
    _chunk(s, "g1", thread="tg", reflow_skipped="gave_up")
    _chunk(s, "g2", thread="tg2", reflow_skipped="source_gone")
    out = sc.list_reflow_gave_up(s, str(tmp_path), page=1, page_size=50)
    assert out["total"] == 1 and out["items"][0]["doc_id"] == "g1"
    assert out["items"][0]["reason"] == "reflow gave up"


# --- routes ------------------------------------------------------------------

class _FakeDaemon:
    def __init__(self):
        self.synced = []

    def status(self):
        return {}

    def sync_now(self, source=None):
        self.synced.append(source)


@pytest.fixture
def srv(tmp_path):
    from mcpbrain.control_api import ControlServer
    s = _store(tmp_path)
    d = _FakeDaemon()
    server = ControlServer(d, home=str(tmp_path), store=s)
    server.start()
    yield server, d, s
    server.stop()


def _call(server, path, body=None):
    url = f"http://127.0.0.1:{server.port}{path}"
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST")
    req.add_header("Authorization", f"Bearer {server.token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else {})


def test_sync_now_route_takes_a_source(srv, tmp_path):
    server, d, _ = srv
    assert _call(server, "/api/sync-now", {})[0] == 200
    assert _call(server, "/api/sync-now", {"source": "drive"})[0] == 200
    assert d.synced == [None, "drive"]
    assert _call(server, "/api/sync-now", {"source": "dropbox"})[0] == 400
    _cfg(tmp_path, {"sources": {"gmail": False}})
    code, body = _call(server, "/api/sync-now", {"source": "gmail"})
    assert code == 409 and "off" in body["error"]
    assert d.synced == [None, "drive"]


def test_chunk_list_routes(srv):
    server, _, s = srv
    _chunk(s, "p1", thread="tp", labels="CATEGORY_PROMOTIONS")
    s.set_enrich_state(["p1"], "cold")
    _chunk(s, "w1", thread="tw")
    _chunk(s, "g1", thread="tg", reflow_skipped="gave_up")
    s.mark_enriched(["g1"])
    code, body = _call(server, "/api/chunks/cold?page=1&page_size=50")
    assert code == 200 and body["total"] == 1 and body["items"][0]["reason"] == "promotions"
    code, body = _call(server, "/api/chunks/waiting")
    assert code == 200 and [i["doc_id"] for i in body["items"]] == ["w1"]
    code, body = _call(server, "/api/chunks/reflow-gave-up")
    assert code == 200 and [i["doc_id"] for i in body["items"]] == ["g1"]
    assert _call(server, "/api/chunks/cold?page=x")[0] == 400


def test_reinclude_and_prioritise_routes(srv):
    server, _, s = srv
    _chunk(s, "p1", thread="tp", labels="CATEGORY_PROMOTIONS")
    s.set_enrich_state(["p1"], "cold")
    _chunk(s, "w1", thread="tw")
    _chunk(s, "w2", thread="tw2")
    code, body = _call(server, "/api/chunks/reinclude", {"doc_ids": ["p1"]})
    assert code == 200 and body == {"ok": True, "changed": 1}
    assert s.get_chunk("p1")["metadata"]["force_enrich"] is True
    code, body = _call(server, "/api/chunks/prioritise", {"doc_ids": ["w1"]})
    assert code == 200 and body == {"ok": True, "changed": 1}
    assert s.unenriched_chunks()[0]["doc_id"] == "w1"
    assert _call(server, "/api/chunks/reinclude", {"doc_ids": "p1"})[0] == 400
    assert _call(server, "/api/chunks/prioritise", {})[0] == 400


def test_enrich_backfill_routes_are_gone(srv):
    server, _, _ = srv
    assert _call(server, "/api/enrich-backfill/start", {})[0] == 404
    assert _call(server, "/api/enrich-backfill/cancel", {})[0] == 404
