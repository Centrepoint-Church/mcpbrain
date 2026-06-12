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
