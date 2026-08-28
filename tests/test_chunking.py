from mcpbrain.chunking import chunk_text, content_hash, slugify


def test_slugify_importable_from_chunking():
    from mcpbrain.chunking import slugify
    assert slugify("Taryn Hamilton") == "taryn-hamilton"
    assert slugify("") == ""


def test_canonical_name_importable_from_chunking():
    # _canonical_name now lives in chunking (beside slugify, no Gemini dep).
    from mcpbrain.chunking import _canonical_name
    assert _canonical_name("Ps Joel") == "Joel"
    assert _canonical_name(None) == ""


def test_slugify_none_safe():
    """Regression for the live NoneType.lower() crash: a present-but-null name
    (JSON null -> Python None) must coerce to "" rather than raise."""
    assert slugify(None) == ""
    assert slugify(123) == ""


def test_slugify_folds_accents():
    """R1: diacritics are folded via NFKD before slugify so accented and ASCII
    spellings of the same name collapse to one slug."""
    assert slugify("Chané") == "chane"
    assert slugify("Chané") == slugify("Chane")
    assert slugify("Renée Smith") == slugify("Renee Smith")


def test_slugify_ascii_cases_unchanged():
    """R1: accent-folding must not disturb existing ASCII behaviour."""
    assert slugify("Taryn Hamilton") == "taryn-hamilton"
    assert slugify("ACC (National)") == "acc-national"
    assert slugify("") == ""
    assert slugify(None) == ""


def test_short_text_is_single_chunk():
    assert chunk_text("hello world") == ["hello world"]


def test_long_text_splits_on_paragraphs():
    para = "word " * 400
    chunks = chunk_text(para + "\n\n" + para, max_tokens=200)
    assert len(chunks) >= 2


def test_content_hash_is_stable():
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")


def test_word_split_chunks_overlap_and_lose_nothing():
    """Ratify that the word-split path seeds each new chunk with the last `overlap`
    words of the previous chunk, and that no token is dropped across the full output.

    Uses max_tokens=20 (max_chars=80) and overlap=5 against a 2000-token sequence so
    we get many splits without relying on any hard-coded character counts.
    """
    overlap = 5
    tokens = [f"w{i}" for i in range(2000)]
    text = " ".join(tokens)  # single paragraph — no \n\n

    chunks = chunk_text(text, max_tokens=20, overlap=overlap)

    # Must actually split
    assert len(chunks) >= 2, f"Expected >= 2 chunks, got {len(chunks)}"

    # Every original token must appear in at least one chunk.
    all_chunk_words = set()
    for c in chunks:
        all_chunk_words.update(c.split())
    missing = set(tokens) - all_chunk_words
    assert not missing, f"Tokens missing from any chunk: {missing}"

    # Consecutive chunks overlap: the last `overlap` words at the tail of chunk N
    # must all appear at the head of chunk N+1 (within the first overlap+1 positions,
    # since the new word is appended after the tail).
    for i in range(len(chunks) - 1):
        tail_words = chunks[i].split()[-overlap:]
        head_words = chunks[i + 1].split()
        # The overlap words should form a contiguous prefix of the next chunk.
        assert head_words[: overlap] == tail_words, (
            f"Chunk {i} tail {tail_words} not found at head of chunk {i+1}: "
            f"{head_words[:overlap]}"
        )


def test_slugify_truncates_to_80_chars():
    assert len(slugify("A" * 90)) <= 80


def test_slugify_and_entity_path_agree_on_accented_name(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain.graph_write import upsert_entity
    from mcpbrain.resolve import canonical_key
    assert slugify("Chané") == "chane"
    store = Store(tmp_path / "slug.sqlite3", dim=4); store.init()
    eid = upsert_entity(store, name="Chané", entity_type="person")
    assert eid == "chane" == canonical_key("Chané")


def test_a_token_longer_than_the_budget_emits_neither_empty_nor_oversize_chunks():
    """B6: the word-split path appended `current` while it was still "" (a
    zero-length chunk), then let the following chunk exceed max_chars. Verified
    live: 6 zero-length and 36 sub-5-char chunks exist in the store."""
    text = "x" * 5000  # one whitespace-free token, well over max_chars=2000

    chunks = chunk_text(text, max_tokens=500)

    assert all(c for c in chunks), "chunk_text emitted a zero-length chunk"
    assert all(len(c) <= 2000 for c in chunks), (
        f"chunk_text emitted an oversize chunk: {[len(c) for c in chunks]}"
    )
    assert "".join(chunks) == text, "hard-splitting a long token must lose nothing"


def test_an_oversize_token_mid_paragraph_does_not_corrupt_its_neighbours():
    text = "before " + ("y" * 3000) + " after"

    chunks = chunk_text(text, max_tokens=500)

    assert all(len(c) <= 2000 for c in chunks)
    joined = " ".join(chunks)
    assert "before" in joined and "after" in joined


def test_has_content_rejects_punctuation_only_text():
    """B1's 66,653 content-free chunks (37% of the live store) are ~2,000-char
    strings of '| | | | |' from empty spreadsheet cells — all embedded, none
    matchable, and 65,770 of them share a single content_hash."""
    from mcpbrain.chunking import has_content

    assert has_content("Budget 2026") is True
    assert has_content("| 42 |") is True
    assert has_content("|  |  |  |") is False
    assert has_content("| --- | --- |") is False
    assert has_content("") is False
    assert has_content("   \n\t ") is False


def test_has_content_accepts_non_ascii_alphanumerics():
    """str.isalnum rather than [A-Za-z0-9] precisely so a sheet of Chinese or
    accented names is not discarded as content-free."""
    from mcpbrain.chunking import has_content

    assert has_content("| 会議 |") is True
    assert has_content("| Åsa |") is True


def test_a_fully_packed_chunk_fits_the_embedder_window_once_prefixed():
    """I8: chunk_text's packing budget (max_tokens*4 = 2000) and
    index.EMBED_WINDOW_CHARS (2000) were the same number, but EMBED_WINDOW_CHARS
    measures the PREFIXED passage and embed.contextual_prefix (default ON) adds
    ~100 chars at embed time. So a fully-packed chunk tipped over the real window
    and tripped the oversize warning on nearly every batch — B3's tail truncation
    left open for the bulk of the corpus, not an edge case. chunk_text now
    reserves the same headroom semantic.SEMANTIC_MAX_CHARS (1800) does.
    """
    from mcpbrain.embed import contextual_prefix
    from mcpbrain.index import EMBED_WINDOW_CHARS

    meta = {"source_type": "gmail",
            "sender": "Samuel Taylor <samuel.taylor@example.org>",
            "date": "Tue, 02 Jun 2026 16:30:01 +0800",
            "subject": "Hall B booking and the revised winter budget",
            "org": "Centrepoint Church"}
    prefix = contextual_prefix(meta)
    assert len(prefix) > 100, f"prefix too short to discriminate: {len(prefix)}"

    # Many short paragraphs, so the packer fills chunks to just under the budget.
    text = "\n\n".join(f"Paragraph {i} about the winter budget review."
                       for i in range(200))
    chunks = chunk_text(text)

    biggest = max(len(c) for c in chunks)
    assert biggest > 1700, (
        f"no chunk got close to the budget ({biggest}) — the test would not "
        f"discriminate"
    )
    assert biggest + len(prefix) <= EMBED_WINDOW_CHARS, (
        f"prefixed chunk is {biggest + len(prefix)} chars, over the "
        f"{EMBED_WINDOW_CHARS}-char embedder window"
    )


def test_the_headroom_is_not_taken_out_of_a_small_explicit_budget():
    """The reservation must not eat a caller's deliberately tiny budget (it would
    go negative at max_tokens=20). Those chunks are nowhere near the embedder
    window, so there is nothing to reserve for."""
    chunks = chunk_text("w " * 500, max_tokens=20)

    assert chunks
    assert all(len(c) <= 80 for c in chunks)
    assert max(len(c) for c in chunks) > 40, (
        "the small budget was reduced anyway"
    )


def test_name_tokens_keeps_distinctive_tokens_only():
    from mcpbrain.chunking import name_tokens
    assert name_tokens("Joel Chelliah") == ["joel", "chelliah"]
    assert name_tokens("A B") == []          # nothing >= 4 chars


def test_name_in_text_matches_full_name_and_tokens():
    from mcpbrain.chunking import name_in_text
    assert name_in_text("Joel Chelliah", "spoke to joel chelliah today")
    assert name_in_text("Joel Chelliah", "ps joel will confirm")
    assert not name_in_text("Joel Chelliah", "nothing relevant here")
    assert not name_in_text("", "anything")
