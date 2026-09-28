from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _c(s, doc_id, **md):
    s.upsert_chunk(doc_id, "t " + doc_id, doc_id, md)


def test_selector_rules(tmp_path):
    s = _store(tmp_path)
    _c(s, "gdrive-A-0", source_type="gdrive", file_id="A", mime_type="application/pdf",
       chunk_total=1)                                            # rule 1 (single chunk still)
    _c(s, "gdrive-B-0", source_type="gdrive", file_id="B", mime_type="application/pdf",
       chunk_total=1, extraction_version=1)                       # current -> skip
    _c(s, "gdrive-C-0", source_type="gdrive", file_id="C", mime_type="text/plain",
       chunk_total=2)                                            # rule 3
    _c(s, "gdrive-D-0", source_type="gdrive", file_id="D", mime_type="text/plain",
       chunk_total=1)                                            # single chunk -> skip
    _c(s, "gdrive-X-0", source_type="gdrive", file_id="X", content_subtype="table",
       mime_type="text/csv", chunk_total=5)                      # table -> skip
    _c(s, "gmail-M-att-0-0", source_type="gmail", message_id="M",
       attachment_mime="application/pdf", chunk_total=1)         # rule 2
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)   # rule 3
    _c(s, "gmail-O-body-0", source_type="gmail", message_id="O", chunk_total=2,
       split_version=1)                                          # current -> skip
    got = set(s.reflow_candidates(50))
    assert got == {("reflow:drive", "A"), ("reflow:drive", "C"),
                   ("reflow:gmail", "M"), ("reflow:gmail", "N")}


def test_selector_excludes_queued(tmp_path):
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    s.enqueue_items([{"ref_id": "N", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:gmail")
    assert s.reflow_candidates(50) == []


def test_selector_anarlog_calendar_and_multi_chunk_owner_once(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        _c(s, f"gdrive-P-{i}", source_type="gdrive", file_id="P",
           mime_type="application/pdf", chunk_total=3)           # rules 1 AND 3
    _c(s, "anarlog-S-note-0", source_type="anarlog", session_id="S", chunk_total=2)
    _c(s, "cal-E-0", source_type="calendar", event_id="E", chunk_total=2)
    _c(s, "cal-G", source_type="calendar", event_id="G", chunk_total=1)
    assert sorted(s.reflow_candidates(50)) == [
        ("reflow:anarlog", "S"), ("reflow:calendar", "E"), ("reflow:drive", "P")]


def test_selector_limit_is_not_starved_by_queued_owners(tmp_path):
    s = _store(tmp_path)
    for o in "ABCDE":
        _c(s, f"gmail-{o}-body-0", source_type="gmail", message_id=o, chunk_total=2)
    s.enqueue_items([{"ref_id": o, "event": "reflow", "modified_at": "1970-01-01T00:00:00"}
                     for o in "ABC"], source="reflow:gmail")
    assert sorted(s.reflow_candidates(2)) == [("reflow:gmail", "D"), ("reflow:gmail", "E")]
    assert len(s.reflow_candidates(1)) == 1
    assert s.reflow_candidates(0) == []
