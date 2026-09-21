import json
from mcpbrain.sync.anarlog import prosemirror_to_markdown, transcript_to_text


def test_headings_become_markdown():
    body = json.dumps({"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1},
         "content": [{"type": "text", "text": "ACC Staff Meeting"}]},
        {"type": "heading", "attrs": {"level": 2},
         "content": [{"type": "text", "text": "Summary"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "We met."}]},
    ]})
    assert prosemirror_to_markdown(body) == (
        "# ACC Staff Meeting\n\n## Summary\n\nWe met.")


def test_bullet_list_becomes_dashes():
    body = json.dumps({"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "First"}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Second"}]}]},
        ]},
    ]})
    assert prosemirror_to_markdown(body) == "- First\n- Second"


def test_nested_marks_keep_text():
    body = json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "bold", "marks": [{"type": "strong"}]},
            {"type": "text", "text": " and plain"},
        ]},
    ]})
    assert prosemirror_to_markdown(body) == "bold and plain"


def test_empty_and_malformed_bodies_return_empty_string():
    assert prosemirror_to_markdown("") == ""
    assert prosemirror_to_markdown("not json") == ""
    assert prosemirror_to_markdown(json.dumps({"type": "doc"})) == ""


def test_transcript_joins_text_in_order():
    words = json.dumps([
        {"id": "w:0", "text": "Hello there."},
        {"id": "w:1", "text": "Second part."},
    ])
    assert transcript_to_text(words) == "Hello there. Second part."


def test_transcript_handles_word_level_entries():
    words = json.dumps([
        {"id": "w:0", "text": "Hello", "start_ms": 0},
        {"id": "w:1", "text": "there", "start_ms": 100},
    ])
    assert transcript_to_text(words) == "Hello there"


def test_transcript_malformed_returns_empty_string():
    assert transcript_to_text("") == ""
    assert transcript_to_text("not json") == ""


def test_prosemirror_content_not_list():
    body = json.dumps({"type": "doc", "content": "oops"})
    assert prosemirror_to_markdown(body) == ""


def test_prosemirror_content_with_non_dict_entries():
    body = json.dumps({"type": "doc", "content": [None, 5, "x"]})
    assert prosemirror_to_markdown(body) == ""


def test_prosemirror_nested_content_not_list():
    body = json.dumps({"type": "doc", "content": [
        {"type": "paragraph", "content": {"a": 1}}
    ]})
    assert prosemirror_to_markdown(body) == ""
