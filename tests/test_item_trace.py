"""item_trace: the console's "Not in yet" list and "Find an item" trace."""
import json
import urllib.error
import urllib.request

import pytest

from mcpbrain import item_trace
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(str(tmp_path / "s.sqlite3"), dim=4)
    s.init()
    return s


def _mail(s, doc_id, *, thread, subject="Hall B booking", labels="INBOX", **extra):
    meta = {"source_type": "gmail", "thread_id": thread, "message_id": doc_id,
            "subject": subject, "date": "Mon, 2 Nov 2026 09:00:00 +0000",
            "labels": labels, **extra}
    s.upsert_chunk(doc_id, f"Can you confirm {subject} for {doc_id}?", "h-" + doc_id, meta)


def _file(s, fid, n, *, name="Budget.xlsx", **extra):
    for i in range(n):
        s.upsert_chunk(f"gdrive-{fid}-{i}", f"row {i} of {name}", f"h-{fid}-{i}",
                       {"source_type": "gdrive", "file_id": fid, "file_name": name,
                        "mime_type": "application/vnd.google-apps.document",
                        "chunk_index": i, **extra})


def _index(s):
    for c in s.unembedded_chunks(limit=1000):
        s.write_embedding(c["rowid"], [0.1, 0.2, 0.3, 0.4])


def _queue(s, source, ref_id, **cols):
    s.enqueue_items([{"ref_id": ref_id, "event": "upsert",
                      "modified_at": "2026-11-11T00:00:00Z"}], source=source)
    if cols:
        with s._connect(write=True) as db:
            sets = ", ".join(f"{k}=?" for k in cols)
            db.execute(f"UPDATE sync_queue SET {sets} WHERE ref_id=?", (*cols.values(), ref_id))


# ---- queue -------------------------------------------------------------------

def test_queue_states_tell_held_from_failing():
    now = "2026-11-13T10:00:00"
    assert item_trace.queue_state({"attempts": 0, "next_attempt_at": None}, now) == "due"
    assert item_trace.queue_state({"attempts": 0, "next_attempt_at": "2026-11-13T10:10:00"},
                                  now) == "waiting"
    assert item_trace.queue_state({"attempts": 2, "next_attempt_at": "2026-11-13T11:00:00"},
                                  now) == "retrying"


def test_sync_queue_lists_rows_with_state_and_known_title(tmp_path):
    s = _store(tmp_path)
    _file(s, "FID1", 2, name="Hardy Report.pdf")
    _queue(s, "drive:D1", "FID1", next_attempt_at="2999-01-01T00:00:00")
    _queue(s, "gmail", "m-new", attempts=3, last_error="HTTP 500",
           next_attempt_at="2999-01-01T00:00:00")
    out = item_trace.sync_queue(s)
    assert out["total"] == 2 and out["counts"] == {"due": 0, "waiting": 1, "retrying": 1}
    by_ref = {i["ref_id"]: i for i in out["items"]}
    assert by_ref["FID1"]["source"] == "drive" and by_ref["FID1"]["title"] == "Hardy Report.pdf"
    assert by_ref["FID1"]["state"] == "waiting"
    assert by_ref["m-new"]["state"] == "retrying" and by_ref["m-new"]["error"] == "HTTP 500"
    assert by_ref["m-new"]["title"] == ""          # not in the store yet


# ---- trace -------------------------------------------------------------------

def test_trace_by_file_id_counts_every_stage(tmp_path):
    s = _store(tmp_path)
    _file(s, "FID1", 4)
    s.mark_enriched(["gdrive-FID1-0"])
    s.set_enrich_state(["gdrive-FID1-1"], "cold")
    s.upsert_chunk("gdrive-FID1-3", "row 3", "h-x",
                   {"source_type": "gdrive", "file_id": "FID1", "file_name": "Budget.xlsx",
                    "reflow_skipped": "gave_up"})
    _queue(s, "drive:D1", "FID1")
    (item,) = item_trace.trace(s, str(tmp_path), "FID1")["items"]
    assert item["title"] == "Budget.xlsx" and item["source"] == "drive"
    assert (item["pieces"], item["in_graph"], item["kept_out"], item["waiting"],
            item["gave_up"]) == (4, 1, 1, 2, 1)
    assert item["kept_out_reasons"] == ["short document"]
    assert [q["state"] for q in item["queue"]] == ["due"]
    assert item["in_batch"] is False


def test_trace_marks_an_item_inside_an_enrichment_batch(tmp_path):
    s = _store(tmp_path)
    _mail(s, "m1", thread="t1")
    units = tmp_path / "enrich_queue" / "units"
    units.mkdir(parents=True)
    (units / "u-1.json").write_text(json.dumps(
        {"threads": [{"thread_id": "t1", "messages": [{"message_id": "m1"}]}]}))
    (item,) = item_trace.trace(s, str(tmp_path), "t1")["items"]
    assert item["in_batch"] is True and item["source"] == "gmail"


def test_trace_by_words_groups_a_thread_into_one_item(tmp_path):
    s = _store(tmp_path)
    _mail(s, "m1", thread="t1", subject="Quarterly roof inspection")
    _mail(s, "m2", thread="t1", subject="Re: Quarterly roof inspection",
          labels="CATEGORY_PROMOTIONS")
    s.set_enrich_state(["m2"], "cold")
    _mail(s, "m3", thread="t2", subject="Unrelated lunch")
    _index(s)
    items = item_trace.trace(s, str(tmp_path), "roof inspection")["items"]
    assert [i["id"] for i in items] == ["t1"]
    assert items[0]["pieces"] == 2 and items[0]["kept_out_reasons"] == ["promotions"]


def test_trace_an_id_only_in_the_sync_queue(tmp_path):
    s = _store(tmp_path)
    _queue(s, "drive:D1", "NEWFILE01", attempts=1, last_error="boom")
    (item,) = item_trace.trace(s, str(tmp_path), "NEWFILE01")["items"]
    assert item["pieces"] == 0 and item["queue"][0]["state"] == "retrying"


def test_trace_nothing_found_and_blank_query(tmp_path):
    s = _store(tmp_path)
    assert item_trace.trace(s, str(tmp_path), "no-such-thing-here")["items"] == []
    assert item_trace.trace(s, str(tmp_path), "   ")["items"] == []


def test_chunks_by_meta_key_refuses_unindexed_keys(tmp_path):
    with pytest.raises(ValueError):
        _store(tmp_path).chunks_by_meta_key("subject", "x")


# ---- routes ------------------------------------------------------------------

class _FakeDaemon:
    def status(self):
        return {"paused": False}


@pytest.fixture
def srv(tmp_path):
    from mcpbrain.control_api import ControlServer
    s = _store(tmp_path)
    server = ControlServer(_FakeDaemon(), home=str(tmp_path), store=s)
    server.start()
    yield server, s
    server.stop()


def _get(server, path):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}")
    req.add_header("Authorization", f"Bearer {server.token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_routes(srv):
    server, s = srv
    _file(s, "FID1", 1)
    _queue(s, "drive:D1", "FID1")
    code, body = _get(server, "/api/sync-queue?page=1")
    assert code == 200 and body["total"] == 1
    code, body = _get(server, "/api/trace?q=FID1")
    assert code == 200 and body["items"][0]["id"] == "FID1"
    assert _get(server, "/api/sync-queue?page=x")[0] == 400


def test_an_enrichment_summary_never_names_an_item(tmp_path):
    """Live 2026-11-13: a Drive file's summary row carries the file id under
    thread_id and showed up as a second, empty item."""
    s = _store(tmp_path)
    _file(s, "FID9", 2)
    s.upsert_chunk("enriched-c1", "People: Dana. Topics: budget.", "h-e",
                   {"source_type": "gmail_enriched_v2", "thread_id": "FID9"})
    _index(s)
    items = item_trace.trace(s, str(tmp_path), "FID9")["items"]
    assert [(i["key"], i["id"], i["pieces"]) for i in items] == [("file_id", "FID9", 2)]
    # Words found only in the summary find nothing: the summary is not an item.
    assert item_trace.trace(s, str(tmp_path), "People Topics")["items"] == []
