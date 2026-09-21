import json
from mcpbrain.sync.anarlog import prosemirror_to_markdown, transcript_to_text


def test_headings_become_markdown():
    body = json.dumps({"type": "doc", "content": [
        {"type": "heading", "attrs": {"level": 1},
         "content": [{"type": "text", "text": "Northgate Trust Staff Meeting"}]},
        {"type": "heading", "attrs": {"level": 2},
         "content": [{"type": "text", "text": "Summary"}]},
        {"type": "paragraph", "content": [{"type": "text", "text": "We met."}]},
    ]})
    assert prosemirror_to_markdown(body) == (
        "# Northgate Trust Staff Meeting\n\n## Summary\n\nWe met.")


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


# --- I4: nested lists must not glue words together -------------------------

def _nested_body():
    """The dominant shape in anarlog's real AI notes: a listItem holding a
    paragraph AND a nested bulletList (live DB: 246 listItem/paragraph and 44
    listItem/bulletList children across the two meetings)."""
    return json.dumps({"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Two new regional managers introduced:"}]},
                {"type": "bulletList", "content": [
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "Marcus Reyes: the northern region"}]}]},
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "Priya Anand: the southern region"}]}]},
                ]},
            ]},
        ]},
    ]})


def test_nested_bullet_list_items_become_their_own_lines():
    out = prosemirror_to_markdown(_nested_body())
    assert out == (
        "- Two new regional managers introduced:\n"
        "  - Marcus Reyes: the northern region\n"
        "  - Priya Anand: the southern region")


def test_nested_bullet_list_glues_no_words_together():
    """The defect: _inline_text flattened a listItem's whole subtree with NO
    separator, so the live notes read
    'introduced:Marcus Reyes: the northern regionPriya Anand: the southern region' — person
    names fused to the preceding word, in the content chosen for enrichment."""
    out = prosemirror_to_markdown(_nested_body())
    for glued in ("introduced:Marcus", "regionPriya"):
        assert glued not in out
    # no two words anywhere fused across a line boundary
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    assert lines == ["- Two new regional managers introduced:",
                     "- Marcus Reyes: the northern region",
                     "- Priya Anand: the southern region"]


def test_ordered_nested_list_numbers_its_own_items():
    body = json.dumps({"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Top"}]},
                {"type": "orderedList", "content": [
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "One"}]}]},
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "Two"}]}]},
                ]},
            ]},
        ]},
    ]})
    assert prosemirror_to_markdown(body) == "- Top\n  1. One\n  2. Two"


def test_a_list_item_with_two_paragraphs_keeps_them_on_separate_lines():
    body = json.dumps({"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "require NCFI sign-off"}]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "Policy: no sponsors"}]},
            ]},
        ]},
    ]})
    out = prosemirror_to_markdown(body)
    assert "sign-offPolicy" not in out
    assert out == "- require NCFI sign-off\n  Policy: no sponsors"


def test_blockquote_with_nested_paragraphs_does_not_glue():
    body = json.dumps({"type": "doc", "content": [
        {"type": "blockquote", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "First para"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Second para"}]},
        ]},
    ]})
    assert prosemirror_to_markdown(body) == "First para\n\nSecond para"
