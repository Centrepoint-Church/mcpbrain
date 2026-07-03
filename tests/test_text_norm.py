from mcpbrain.text_norm import singularize

def test_simple_plural():
    assert singularize("budgets") == "budget"

def test_es_plural():
    assert singularize("churches") == "church"

def test_already_singular_unchanged():
    assert singularize("budget") == "budget"

def test_lowercases():
    assert singularize("Budgets") == "budget"

def test_empty_returns_empty():
    assert singularize("") == ""

def test_non_plural_word_unchanged():
    # inflect returns False for non-plurals; helper must fall back to input.
    assert singularize("worship") == "worship"
