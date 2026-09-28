import random
import string

from mcpbrain import chunking
from mcpbrain.chunking import chunk_text, split_long_paragraph, prose_max_chars
from tests.oracles.chunking_v0 import chunk_text_v0


def _para(rng, max_len):
    words = []
    while True:
        w = "".join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(1, 9)))
        if sum(len(x) + 1 for x in words) + len(w) >= max_len:
            break
        words.append(w)
    return " ".join(words) or "x"


def test_byte_identical_when_no_paragraph_overflows():
    rng = random.Random(1234)
    cap = prose_max_chars()
    for _ in range(300):
        paras = [_para(rng, rng.randint(5, cap - 1)) for _ in range(rng.randint(1, 8))]
        text = "\n\n".join(paras)
        assert chunk_text(text) == chunk_text_v0(text)


def test_prose_max_chars_is_1800():
    assert prose_max_chars() == 1800


def test_long_paragraph_splits_on_lines_and_keeps_newlines():
    lines = [f"Line {i}: " + "word " * 30 for i in range(40)]   # ~160 chars each
    para = "\n".join(lines)
    pieces = split_long_paragraph(para, 1800, overlap=0)
    assert len(pieces) > 1
    for p in pieces:
        assert len(p) <= 1800
        for line in p.split("\n"):
            assert line.strip() in {ln.strip() for ln in lines}
    # nothing lost, nothing reordered
    rebuilt = [ln for p in pieces for ln in p.split("\n")]
    assert rebuilt == lines


def test_overlap_carries_whole_trailing_lines():
    lines = [f"L{i} " + "w " * 20 for i in range(100)]
    pieces = split_long_paragraph("\n".join(lines), 600, overlap=50)
    for a, b in zip(pieces, pieces[1:]):
        assert b.split("\n")[0] in a.split("\n")   # seed line came from previous piece


def test_single_huge_line_falls_back_to_sentences_then_words():
    sent = "This is a sentence about the budget. "
    line = sent * 200                               # one line, ~7,400 chars
    pieces = split_long_paragraph(line, 1800, overlap=0)
    assert all(len(p) <= 1800 for p in pieces)
    assert all(p.rstrip().endswith(".") for p in pieces[:-1])   # sentence seams
    blob = "x" * 5000                               # no separators at all
    pieces = split_long_paragraph(blob, 1800, overlap=0)
    assert "".join(pieces) == blob


def test_chunk_text_no_longer_collapses_newlines_in_long_paragraph():
    para = "\n".join(f"Row {i} | value {i}" for i in range(300))   # > budget, no blank lines
    out = chunk_text(para)
    assert len(out) > 1
    assert all("\n" in c for c in out)


def test_split_version_constant():
    assert chunking.SPLIT_VERSION == 1
