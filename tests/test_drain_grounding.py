

def test_grounding_is_inert_when_payload_messages_carry_no_body():
    """A pushed payload has message METADATA only — no text/body (0.7.72).

    With two such messages `" ".join(["", ""])` is `" "`, which is truthy, so the
    empty-source guard was skipped and every extracted name was judged against a
    single space: measured on real payloads, entities 5->0 and 6->0, discarding
    the entire extraction for any multi-message thread. One message was safe by
    accident (`"".join` gives ""), which is why this never showed up.
    """
    from mcpbrain.drain import _grounding_filter

    extraction = {
        "thread_id": "t1",
        "messages": [                     # metadata only, exactly as pushed
            {"message_id": "m1", "sender": "Josh Kemp", "subject": "Budget"},
            {"message_id": "m2", "sender": "Jess Williams", "subject": "Re: Budget"},
        ],
        "entities": [{"name": "Centrepoint Church", "type": "org"},
                     {"name": "Josh Kemp", "type": "person"}],
        "relations": [{"source_name": "Josh Kemp", "target_name": "Centrepoint Church",
                       "type": "works_at"}],
    }

    out, dropped = _grounding_filter(extraction)

    assert dropped == 0, "no body text means nothing to ground against — drop nothing"
    assert len(out["entities"]) == 2
    assert len(out["relations"]) == 1


def test_grounding_still_filters_when_real_body_text_is_present():
    """The guard must not turn the filter off when it CAN do its job."""
    from mcpbrain.drain import _grounding_filter

    extraction = {
        "thread_id": "t1",
        "messages": [{"message_id": "m1", "text": "Spoke with Josh Kemp about the budget."}],
        "entities": [{"name": "Josh Kemp", "type": "person"},
                     {"name": "Zyzzyx Corporation", "type": "org"}],
    }

    out, dropped = _grounding_filter(extraction)

    assert dropped == 1
    assert [e["name"] for e in out["entities"]] == ["Josh Kemp"]
