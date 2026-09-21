from mcpbrain.prepare import should_enrich


def _chunk(subtype, source="anarlog"):
    return {"metadata": {"source_type": source, "content_subtype": subtype}}


def test_transcript_is_not_enriched():
    assert should_enrich(_chunk("transcript")) is False


def test_summary_and_note_are_enriched():
    assert should_enrich(_chunk("summary")) is True
    assert should_enrich(_chunk("note")) is True


def test_transcript_gate_is_source_agnostic():
    # Any future transcript source is honoured without touching the gate.
    assert should_enrich(_chunk("transcript", source="someothertool")) is False


def test_table_gate_still_works():
    assert should_enrich(_chunk("table", source="drive")) is False
