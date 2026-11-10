"""Tests for B4 consolidation pass (cluster → semantic note)."""
import json
import os
import pytest
from pathlib import Path


@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    return s


@pytest.fixture
def home_consolidation(tmp_path):
    """Home dir with consolidation enabled."""
    h = tmp_path / "home-c"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({"consolidation": True}))
    return str(h)


# ---------------------------------------------------------------------------
# _token_set and _cluster_chunks (unit)
# ---------------------------------------------------------------------------

def test_token_set_filters_short_words():
    """Words under 4 chars are ignored."""
    from mcpbrain.consolidation import _token_set
    ts = _token_set("the cat sat on a budget spreadsheet")
    assert "budget" in ts
    assert "spreadsheet" in ts
    assert "the" not in ts
    assert "cat" not in ts


def test_cluster_chunks_groups_similar():
    """Chunks with shared vocabulary are clustered together."""
    from mcpbrain.consolidation import _cluster_chunks

    chunks = [
        {"doc_id": f"a-{i}", "text": "budget review forecast financial planning quarterly"}
        for i in range(4)
    ] + [
        {"doc_id": "b-0", "text": "personal dentist appointment health insurance claim"}
    ]

    clusters = _cluster_chunks(chunks, threshold=0.1)
    # The four budget chunks should cluster together; b-0 is unrelated
    assert len(clusters) >= 1
    budget_cluster = clusters[0]
    ids = [c["doc_id"] for c in budget_cluster]
    assert all(i.startswith("a-") for i in ids)


def test_cluster_chunks_min_size():
    """Clusters smaller than _MIN_CLUSTER_SIZE are dropped."""
    from mcpbrain.consolidation import _cluster_chunks

    # Two identical chunks → one cluster of size 2 (< 3 default minimum)
    chunks = [
        {"doc_id": "x", "text": "planning review forecast quarterly"},
        {"doc_id": "y", "text": "planning review forecast quarterly"},
    ]
    clusters = _cluster_chunks(chunks)
    # Both should be in ONE cluster but cluster size < min → dropped
    assert len(clusters) == 0


# ---------------------------------------------------------------------------
# should_consolidate
# ---------------------------------------------------------------------------

def test_should_consolidate_false_when_flag_off(store, tmp_path):
    """should_consolidate is False when flag is disabled."""
    from mcpbrain.consolidation import should_consolidate

    home_off = str(tmp_path / "home-coff")
    os.makedirs(home_off)
    (Path(home_off) / "config.json").write_text(json.dumps({"consolidation": False}))

    # Seed enough salience
    for i in range(10):
        store.upsert_chunk(f"ep-{i}", "budget quarterly review financial forecast", f"h{i}", {})
        with store._connect() as db:
            db.execute(f"UPDATE chunks SET embedded=1, salience=6.0, memory_type='episodic' WHERE doc_id='ep-{i}'")

    assert not should_consolidate(store, home_off)


def test_should_consolidate_true_when_threshold_met(store, home_consolidation):
    """should_consolidate is True when accumulated salience ≥ threshold."""
    from mcpbrain.consolidation import should_consolidate, CONSOLIDATION_THRESHOLD

    needed = int(CONSOLIDATION_THRESHOLD / 5.0) + 1
    for i in range(needed):
        store.upsert_chunk(f"ep-{i}", f"text chunk {i}", f"h{i}", {})
        with store._connect() as db:
            db.execute(f"UPDATE chunks SET embedded=1, salience=5.0, memory_type='episodic' WHERE doc_id='ep-{i}'")

    assert should_consolidate(store, home_consolidation)


# ---------------------------------------------------------------------------
# consolidate (with mocked claude CLI)
# ---------------------------------------------------------------------------

def _seed_cluster(store, prefix: str, n: int = 4, salience: float = 6.0):
    """Seed n related episodic chunks."""
    for i in range(n):
        store.upsert_chunk(
            f"{prefix}-{i}",
            "budget quarterly review financial forecast planning annual report",
            f"h-{prefix}-{i}",
            {},
        )
        with store._connect() as db:
            db.execute(
                f"UPDATE chunks SET embedded=1, salience={salience}, "
                f"memory_type='episodic', memory_tier='warm' WHERE doc_id='{prefix}-{i}'"
            )


def test_consolidate_writes_semantic_note(store, home_consolidation, monkeypatch):
    """consolidate() writes a semantic note chunk via mocked claude CLI."""
    from mcpbrain import consolidation

    _seed_cluster(store, "bud", n=4)

    monkeypatch.setattr(consolidation, "_call_claude",
                        lambda prompt, timeout=60: "Q3 budget review: finance team committed to reducing spend. [bud-0] [bud-1]")

    result = consolidation.consolidate(store, home_consolidation, threshold=0.0)

    assert result["notes_written"] >= 1
    # A semantic note chunk now exists
    with store._connect() as db:
        rows = db.execute(
            "SELECT doc_id, memory_type, memory_tier FROM chunks WHERE memory_type='semantic'"
        ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["memory_tier"] == "hot"


def test_consolidate_marks_sources_hot(store, home_consolidation, monkeypatch):
    """Source chunks are promoted to hot after consolidation."""
    from mcpbrain import consolidation

    _seed_cluster(store, "src", n=4)

    monkeypatch.setattr(consolidation, "_call_claude",
                        lambda prompt, timeout=60: "Summary of source material. [src-0] [src-1]")

    consolidation.consolidate(store, home_consolidation, threshold=0.0)

    with store._connect() as db:
        rows = db.execute(
            "SELECT memory_tier FROM chunks WHERE doc_id LIKE 'src-%'"
        ).fetchall()
    for r in rows:
        assert r["memory_tier"] == "hot", f"Expected hot, got {r['memory_tier']}"


def test_write_note_short_summary_keeps_single_doc_id(store):
    from mcpbrain.consolidation import _write_note

    cluster = [{"doc_id": "src-1"}]
    doc_id = _write_note(store, cluster, "A short note.")

    assert doc_id == f"note-consolidated-{__import__('hashlib').sha256(b'A short note.').hexdigest()[:16]}"
    with store._connect() as db:
        rows = db.execute("SELECT doc_id FROM chunks WHERE doc_id LIKE ?",
                          (f"{doc_id}%",)).fetchall()
    assert [r["doc_id"] for r in rows] == [doc_id]


def test_write_note_long_summary_splits_into_multiple_chunks(store):
    from mcpbrain.consolidation import _write_note

    cluster = [{"doc_id": "src-1"}]
    long_summary = "This is a sentence about the project. " * 200  # well over 2000 chars

    doc_id = _write_note(store, cluster, long_summary)

    # doc_id is only the FIRST piece's id (e.g. "...-0"); strip the trailing
    # "-<index>" to get the shared base and match every sibling chunk. (A
    # plain f"{doc_id}%" wildcard only matches "-0" itself, since indices
    # aren't zero-padded and "-1" doesn't start with "-0".)
    base_doc_id, _, _ = doc_id.rpartition("-")
    with store._connect() as db:
        rows = db.execute(
            "SELECT doc_id, length(text) as len, metadata FROM chunks "
            "WHERE doc_id LIKE ? ORDER BY doc_id",
            (f"{base_doc_id}-%",)).fetchall()

    assert len(rows) > 1, "a long summary must split into multiple chunks"
    for r in rows:
        assert r["len"] <= 2000
    import json
    metas = [json.loads(r["metadata"]) for r in rows]
    assert [m["chunk_index"] for m in metas] == list(range(len(rows)))
    assert all(m["chunk_total"] == len(rows) for m in metas)


def test_consolidate_skips_when_claude_returns_empty(store, home_consolidation, monkeypatch):
    """If claude returns empty string, no note is written (no crash)."""
    from mcpbrain import consolidation

    _seed_cluster(store, "cl0", n=4)

    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: "")

    result = consolidation.consolidate(store, home_consolidation, threshold=0.0)
    assert result["notes_written"] == 0


def test_consolidate_noop_when_disabled(store, tmp_path, monkeypatch):
    """consolidate() returns zeros when flag is off."""
    from mcpbrain import consolidation

    home_off = str(tmp_path / "c-off")
    os.makedirs(home_off)
    (Path(home_off) / "config.json").write_text(json.dumps({"consolidation": False}))

    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: "Summary.")

    _seed_cluster(store, "no", n=4)
    result = consolidation.consolidate(store, home_off, threshold=0.0)
    assert result["notes_written"] == 0
    assert result["clusters_found"] == 0


# --- B4 fix: embedding-based clustering (reuses bge vectors) ---

class _FakeEmbedder:
    """Deterministic unit-norm vectors: 'budget' texts → axis 0, 'roster' → axis 1."""
    def embed_passages(self, texts):
        out = []
        for t in texts:
            tl = (t or "").lower()
            if "budget" in tl:
                out.append([1.0, 0.0, 0.0])
            elif "roster" in tl:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


def test_cluster_uses_embeddings_when_embedder_given():
    from mcpbrain.consolidation import _cluster
    chunks = [
        {"doc_id": "a", "text": "the budget review for 2026"},
        {"doc_id": "b", "text": "annual budget planning meeting"},
        {"doc_id": "c", "text": "budget approval next steps"},
        {"doc_id": "d", "text": "sunday roster volunteers"},
        {"doc_id": "e", "text": "roster for the worship team"},
        {"doc_id": "f", "text": "roster sign-up sheet"},
    ]
    clusters = _cluster(chunks, _FakeEmbedder())
    # two semantic clusters (budget vs roster), each >= _MIN_CLUSTER_SIZE (3)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [3, 3]
    texts = [" ".join(x["text"] for x in cl).lower() for cl in clusters]
    assert any("budget" in t and "roster" not in t for t in texts)


def test_cluster_falls_back_to_lexical_when_embedder_fails():
    from mcpbrain.consolidation import _cluster
    class _Broken:
        def embed_passages(self, texts):
            raise RuntimeError("no embedder")
    chunks = [{"doc_id": str(i), "text": "budget review meeting agenda"} for i in range(3)]
    clusters = _cluster(chunks, _Broken())   # falls back to Jaccard, no raise
    assert len(clusters) == 1 and len(clusters[0]) == 3


# ---------------------------------------------------------------------------
# Graduation step: _graduate_note and consolidate() integration (1c)
# ---------------------------------------------------------------------------

def _make_cluster(n: int = 5, salience: float = 5.0) -> list[dict]:
    return [{"doc_id": f"ep-{i}", "text": f"chunk {i}", "salience": salience} for i in range(n)]


def test_infer_memory_type_vocabulary():
    """_infer_memory_type maps to {project, system, preference} and defaults to project."""
    from mcpbrain.consolidation import _infer_memory_type

    # System: tool/automation language
    assert _infer_memory_type([], "We rely on the ClickUp tool and a nightly automation pipeline.") == "system"
    # Preference: standing-rule / style language (checked before system)
    assert _infer_memory_type([], "House style: always use Australian English.") == "preference"
    assert _infer_memory_type([], "Preference for the deck-builder tool's output format.") == "preference"
    # Default: ordinary project note
    assert _infer_memory_type([], "Budget review Q3: finance committed to cost reduction.") == "project"
    # Tags on source chunks also feed inference
    cluster = [{"metadata": {"tags": ["automation", "integration"]}}]
    assert _infer_memory_type(cluster, "Notes from the sync.") == "system"


def test_graduate_note_calls_write_memory_above_threshold(home_consolidation, monkeypatch):
    """_graduate_note calls write_memory when source count and salience are above thresholds."""
    from mcpbrain.consolidation import _graduate_note
    from mcpbrain.store import Store

    store = Store(Path(home_consolidation) / "test.sqlite3", dim=4)
    store.init()

    written = []
    monkeypatch.setattr("mcpbrain.records_write.write_memory",
                        lambda repo, **kw: written.append(kw) or True)

    cluster = _make_cluster(n=5, salience=5.0)  # 5 sources, salience=5.0 — both above default thresholds
    summary = "Budget review Q3: finance committed to cost reduction. Key decisions logged."
    result = _graduate_note(store, home_consolidation, cluster, summary)

    assert result is True
    assert len(written) == 1
    assert "budget" in written[0]["slug"]
    assert written[0]["memory_type"] == "project"


def test_graduate_note_refuses_a_note_holding_personal_details(home_consolidation, monkeypatch):
    """A note naming an applicant's date of birth and personal mailbox stays a
    consolidated note in the store; it never becomes a durable memory file."""
    from mcpbrain.consolidation import _graduate_note
    from mcpbrain.store import Store

    store = Store(Path(home_consolidation) / "test-pd.sqlite3", dim=4)
    store.init()

    written = []
    monkeypatch.setattr("mcpbrain.records_write.write_memory",
                        lambda repo, **kw: written.append(kw) or True)

    cluster = _make_cluster(n=5, salience=5.0)  # passes both thresholds
    summary = ("Dana Okafor (dana.okafor@gmail.com, born 30 May 2006) applied "
               "for the Acme internship.")
    assert _graduate_note(store, home_consolidation, cluster, summary) is False
    assert written == []


def test_graduate_note_skipped_below_source_count(home_consolidation, monkeypatch):
    """_graduate_note is skipped when cluster has fewer than graduation_min_sources chunks."""
    from mcpbrain.consolidation import _graduate_note
    from mcpbrain.store import Store

    store = Store(Path(home_consolidation) / "test2.sqlite3", dim=4)
    store.init()

    written = []
    monkeypatch.setattr("mcpbrain.records_write.write_memory",
                        lambda repo, **kw: written.append(kw) or True)

    cluster = _make_cluster(n=3, salience=5.0)  # 3 sources < default min_sources=4
    result = _graduate_note(store, home_consolidation, cluster, "Short summary.")

    assert result is False
    assert written == []


def test_graduate_note_skipped_below_salience(home_consolidation, monkeypatch):
    """_graduate_note is skipped when mean salience is below graduation_min_salience."""
    from mcpbrain.consolidation import _graduate_note
    from mcpbrain.store import Store

    store = Store(Path(home_consolidation) / "test3.sqlite3", dim=4)
    store.init()

    written = []
    monkeypatch.setattr("mcpbrain.records_write.write_memory",
                        lambda repo, **kw: written.append(kw) or True)

    cluster = _make_cluster(n=5, salience=1.0)  # salience=1.0 < default min_salience=3.5
    result = _graduate_note(store, home_consolidation, cluster, "Low-salience summary.")

    assert result is False
    assert written == []


def test_graduation_thresholds_config_tunable(tmp_path, monkeypatch):
    """graduation_min_sources and graduation_min_salience are overridden by config."""
    from mcpbrain.consolidation import _graduate_note
    from mcpbrain.store import Store

    home = str(tmp_path / "home-grad")
    os.makedirs(home)
    (Path(home) / "config.json").write_text(json.dumps({
        "consolidation": True,
        "graduation_min_sources": 2,   # lower threshold
        "graduation_min_salience": 1.0,  # lower threshold
    }))

    store = Store(Path(home) / "test.sqlite3", dim=4)
    store.init()

    written = []
    monkeypatch.setattr("mcpbrain.records_write.write_memory",
                        lambda repo, **kw: written.append(kw) or True)

    cluster = _make_cluster(n=3, salience=2.0)  # 3 sources, salience=2.0 — passes lowered thresholds
    result = _graduate_note(store, home, cluster, "Lowered-threshold summary text.")

    assert result is True
    assert len(written) == 1


def test_consolidate_triggers_graduation(store, home_consolidation, monkeypatch):
    """consolidate() calls _graduate_note after writing a semantic note."""
    from mcpbrain import consolidation

    graduated = []

    def _tracking_graduate(store, home, cluster, summary, **_kw):
        graduated.append({"n": len(cluster), "summary": summary})
        return False  # skip real git I/O; return value doesn't matter for consolidate

    monkeypatch.setattr(consolidation, "_graduate_note", _tracking_graduate)
    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: _card(
        "A semantic summary from the cluster.", standing=True))

    _seed_cluster(store, "g", n=4, salience=5.0)
    result = consolidation.consolidate(store, home_consolidation, threshold=0.0)

    assert result["notes_written"] >= 1
    assert len(graduated) >= 1
    assert graduated[0]["n"] == 4


def test_consolidated_multi_chunk_note_carries_note_id(tmp_path):
    """store.note_chunks groups on note_id, falling back to doc_id. Without
    note_id every piece of a chunked consolidated note groups to ITSELF, so one
    note surfaces as N separate notes in memory_index and gets N distil verdicts.
    The capture path stamps it; this path must too."""
    from mcpbrain.consolidation import _write_note
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    summary = "\n\n".join(f"para{i} " + "z" * 700 for i in range(8))
    base = _write_note(s, [{"doc_id": "src-1"}], summary)
    assert base is not None
    with s._connect() as db:
        rows = db.execute(
            "SELECT doc_id, metadata FROM chunks WHERE doc_id LIKE 'note-consolidated-%'"
        ).fetchall()
    assert len(rows) > 1                       # genuinely multi-chunk
    import json
    ids = {json.loads(r[1]).get("note_id") for r in rows}
    assert len(ids) == 1 and None not in ids   # all pieces share one note_id
    assert s.note_chunks() and len(s.note_chunks()) == 1   # one note, not N


def test_consolidated_note_written_cold_and_never_core(store):
    """A consolidated note is a machine summary, never evidence: written cold
    (still a row, so still searchable), so it never re-queues for extraction and
    is never a core candidate."""
    from mcpbrain.consolidation import _write_note
    doc_id = _write_note(store, [{"doc_id": "src-1"}], "A short durable note.")
    with store._connect() as db:
        state = db.execute("SELECT enrich_state FROM chunks WHERE doc_id=?",
                           (doc_id,)).fetchone()[0]
    assert state == "cold"
    assert doc_id not in {c["doc_id"] for c in store.unenriched_chunks()}
    assert doc_id not in {c["doc_id"] for c in store.top_core_candidates(limit=50)}
    long_id = _write_note(store, [{"doc_id": "src-2"}], ("Word " * 400 + "\n\n") * 6)
    with store._connect() as db:
        states = {r[0] for r in db.execute(
            "SELECT enrich_state FROM chunks WHERE doc_id LIKE ?",
            (long_id.rsplit("-", 1)[0] + "-%",))}
    assert states == {"cold"}


# ---------------------------------------------------------------------------
# Model preamble never becomes the note (or its memory title and slug)
# ---------------------------------------------------------------------------

def test_strip_preamble_drops_task_commentary_before_the_note():
    from mcpbrain.consolidation import _strip_preamble
    raw = ("This is a memory consolidation task, not a coding task, so I'll "
           "write the note directly.\n\n"
           "Acme's board approved the hall lease [gmail-1-body-0].")
    assert _strip_preamble(raw) == "Acme's board approved the hall lease [gmail-1-body-0]."


def test_strip_preamble_handles_a_same_line_lead_in():
    from mcpbrain.consolidation import _strip_preamble
    raw = ("These excerpts are role description fragments rather than a task "
           "requiring skill invocation — writing the consolidated note directly.\n"
           "Dana Okafor leads Acme's finance team [gmail-2-body-0].")
    assert _strip_preamble(raw) == "Dana Okafor leads Acme's finance team [gmail-2-body-0]."


def test_strip_preamble_empties_a_reply_that_is_only_commentary():
    from mcpbrain.consolidation import _strip_preamble
    assert _strip_preamble(
        "These excerpts are governance document text with no relevant "
        "memory-worthy content, but I'll produce the requested note.") == ""


def test_strip_preamble_keeps_a_cited_note_that_opens_with_these_excerpts():
    from mcpbrain.consolidation import _strip_preamble
    note = ("These excerpts cover Acme's two open roles [gmail-3-att-0-1]. "
            "Sam Chen will interview in March [gmail-3-body-0].")
    assert _strip_preamble(note) == note


def test_strip_preamble_leaves_an_ordinary_note_alone():
    from mcpbrain.consolidation import _strip_preamble
    note = "Pat Nguyen chairs Acme's operations meeting [gmail-4-body-0]."
    assert _strip_preamble(note) == note


# ---------------------------------------------------------------------------
# Structured reply: only a note judged standing graduates, titled by the model
# ---------------------------------------------------------------------------

def _card(note, *, standing, title="", description=""):
    import json as _json
    return _json.dumps({"note": note, "title": title, "description": description,
                        "standing": standing})


def test_parse_reply_reads_the_card_inside_code_fences():
    from mcpbrain.consolidation import _parse_reply
    raw = "```json\n" + _card("Acme leases Hall B [g-1].", standing=True,
                             title="Acme Hall B lease",
                             description="Acme's lease of Hall B.") + "\n```"
    assert _parse_reply(raw) == {"note": "Acme leases Hall B [g-1].",
                                 "title": "Acme Hall B lease",
                                 "description": "Acme's lease of Hall B.",
                                 "standing": True}


def test_parse_reply_treats_a_plain_note_as_never_standing():
    from mcpbrain.consolidation import _parse_reply
    card = _parse_reply("This is a memory consolidation task, so I'll write the "
                        "note directly.\n\nAcme leases Hall B [g-1].")
    assert card == {"note": "Acme leases Hall B [g-1].", "title": "",
                    "description": "", "standing": False}


def test_parse_reply_only_a_literal_true_is_standing():
    from mcpbrain.consolidation import _parse_reply
    import json as _json
    assert _parse_reply(_json.dumps({"note": "x [g-1]", "standing": "yes"}))["standing"] is False
    assert _parse_reply(_json.dumps({"note": 3}))["note"] == '{"note": 3}'


def _run_pass(store, home, monkeypatch, reply):
    from mcpbrain import consolidation
    graduated = []
    monkeypatch.setattr(consolidation, "_graduate_note",
                        lambda store, home, cluster, summary, **kw:
                        graduated.append(kw) or True)
    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: reply)
    _seed_cluster(store, "s", n=4, salience=5.0)
    result = consolidation.consolidate(store, home, threshold=0.0)
    return result, graduated


def test_a_note_judged_not_standing_is_written_but_not_graduated(store, home_consolidation,
                                                                 monkeypatch):
    result, graduated = _run_pass(store, home_consolidation, monkeypatch, _card(
        "Dana Okafor applied for the Acme internship [g-2].", standing=False,
        title="Dana Okafor internship application"))
    assert result["notes_written"] >= 1
    assert graduated == []


def test_a_standing_note_graduates_under_the_models_title(store, home_consolidation,
                                                          monkeypatch):
    result, graduated = _run_pass(store, home_consolidation, monkeypatch, _card(
        "Acme's board approved the Hall B lease [g-3].", standing=True,
        title="Acme Hall B lease", description="Acme's board approved the Hall B lease."))
    assert graduated and graduated[0]["title"] == "Acme Hall B lease"
    assert graduated[0]["description"] == "Acme's board approved the Hall B lease."


def test_graduated_slug_and_description_come_from_the_title(home_consolidation, monkeypatch):
    from mcpbrain.consolidation import _graduate_note
    from mcpbrain.store import Store
    store = Store(Path(home_consolidation) / "test-title.sqlite3", dim=4)
    store.init()
    written = []
    monkeypatch.setattr("mcpbrain.records_write.write_memory",
                        lambda repo, **kw: written.append(kw) or True)
    _graduate_note(store, home_consolidation, _make_cluster(n=5, salience=5.0),
                   "These excerpts cover the lease. Acme's board approved it [g-4].",
                   title="Acme Hall B lease", description="Board approved the Hall B lease.")
    assert written[0]["slug"] == "acme-hall-b-lease"
    assert written[0]["description"] == "Board approved the Hall B lease."


def test_ask_mode_proposal_carries_the_verdict_and_names_it(store, home_consolidation,
                                                           monkeypatch):
    from mcpbrain import auto_changes, consolidation
    proposals = []
    from mcpbrain import config
    monkeypatch.setattr(config, "auto_change_mode", lambda home, kind: "ask")
    monkeypatch.setattr(auto_changes, "propose",
                        lambda store, kind, target, summary, payload:
                        proposals.append((summary, payload)))
    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: _card(
        "Acme's board approved the Hall B lease [g-5].", standing=True,
        title="Acme Hall B lease", description="Board approved the lease."))
    _seed_cluster(store, "a", n=4, salience=5.0)
    consolidation.consolidate(store, home_consolidation, threshold=0.0)
    summary, payload = proposals[0]
    assert "keep it as standing memory: Acme Hall B lease" in summary
    assert payload["standing"] is True and payload["title"] == "Acme Hall B lease"
