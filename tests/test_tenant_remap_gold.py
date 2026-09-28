"""bin/tenant.py remap-gold: repoint gold expected_chunk_ids through reflow_map.

doc_ids are positional and REUSED by a reflow, so "does the id still have a
chunk row" says nothing about whether its text moved: after a reflow
gdrive-F-4 usually still exists but holds different text. remap-gold
therefore applies every reflow batch (one apply_reflow = one owner + one
timestamp, whose rows are SIMULTANEOUS) recorded after the file's watermark,
then records the new watermark in the file so a re-run is a no-op. A gold
file that predates the reflow has no watermark and is run with from_start.
"""
import importlib.util
from pathlib import Path

from mcpbrain.reflow import plan
from mcpbrain.store import Store
from mcpbrain.sync.normalise import Chunk

_spec = importlib.util.spec_from_file_location(
    "tenant_cli", Path(__file__).resolve().parent.parent / "bin" / "tenant.py")
tenant = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tenant)


class _FakeStore:
    """Exposes exactly the one method remap_gold calls."""

    def __init__(self, rows=()):
        self._rows = [{"id": i + 1, "owner": o, "old_doc_id": a, "new_doc_id": b, "at": t}
                      for i, (o, a, b, t) in enumerate(rows)]

    def reflow_map_rows(self, after_id=0):
        return [r for r in self._rows if r["id"] > after_id]


def test_remap_gold_rewrites_through_the_map_and_keeps_formatting(tmp_path):
    store = _FakeStore([("F", "gdrive-F-1", "gdrive-F-0", "t1")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("# keep me\n- id: c1\n  expected_chunk_ids:\n"
                    "    - gdrive-F-1\n    - gmail-X-body-0\n")
    changes = tenant.remap_gold(gold, store, from_start=True)
    assert changes == [("gdrive-F-1", "gdrive-F-0")]
    text = gold.read_text()
    assert "# keep me" in text and "    - gdrive-F-0\n" in text and "gdrive-F-1" not in text
    assert "    - gmail-X-body-0\n" in text


def test_remap_gold_dry_run_writes_nothing(tmp_path):
    store = _FakeStore([("F", "gdrive-F-1", "gdrive-F-0", "t1")])
    gold = tmp_path / "gold.yaml"
    original = "- id: c1\n  expected_chunk_ids:\n    - gdrive-F-1\n"
    gold.write_text(original)
    assert tenant.remap_gold(gold, store, dry_run=True, from_start=True) == [("gdrive-F-1", "gdrive-F-0")]
    assert gold.read_text() == original


def test_one_batch_is_simultaneous_never_chained(tmp_path):
    """i->j and j->k in ONE reflow: an id that was i ends at j, not k."""
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1"),
                        ("F", "gdrive-F-3", "gdrive-F-2", "t1")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("- expected_chunk_ids:\n    - gdrive-F-4\n    - gdrive-F-3\n")
    changes = tenant.remap_gold(gold, store, from_start=True)
    assert changes == [("gdrive-F-4", "gdrive-F-3"), ("gdrive-F-3", "gdrive-F-2")]


def test_later_batches_compose_and_rerun_is_a_no_op(tmp_path):
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1"),
                        ("F", "gdrive-F-3", "gdrive-F-1", "t2")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("- expected_chunk_ids:\n    - gdrive-F-4\n")
    assert tenant.remap_gold(gold, store, from_start=True) == [("gdrive-F-4", "gdrive-F-1")]
    assert tenant.remap_gold(gold, store) == []          # watermark recorded
    assert "- gdrive-F-1" in gold.read_text()


def test_no_reflow_rows_changes_nothing(tmp_path):
    gold = tmp_path / "gold.yaml"
    original = "- id: c1\n  expected_chunk_ids:\n    - gdrive-F-1\n"
    gold.write_text(original)
    assert tenant.remap_gold(gold, _FakeStore(), from_start=True) == []
    assert gold.read_text() == original


def test_quoted_ids_trailing_comments_and_blank_lines_survive(tmp_path):
    """Final review I4: the real tenant gold set quotes its ids and carries
    trailing comments; the old regex matched neither (and ate blank lines)."""
    store = _FakeStore([("F", "gdrive-F-1", "gdrive-F-0", "t1"),
                        ("G", "gdrive-G-2", "gdrive-G-1", "t2")])
    gold = tmp_path / "gold.yaml"
    gold.write_text('- expected_chunk_ids:\n'
                    '    - "gdrive-F-1"   # the budget table\n'
                    "\n"
                    "    - 'gdrive-G-2'\n"
                    '    - "gmail-M-body-0" # untouched\n')
    assert tenant.remap_gold(gold, store, from_start=True) == [("gdrive-F-1", "gdrive-F-0"),
                                             ("gdrive-G-2", "gdrive-G-1")]
    text = gold.read_text()
    assert '    - "gdrive-F-0"   # the budget table\n\n' in text
    assert "    - 'gdrive-G-1'\n" in text
    assert '    - "gmail-M-body-0" # untouched\n' in text


def test_real_store_remaps_a_live_positional_id_whose_text_moved(tmp_path):
    """Against a real Store + apply_reflow: old gdrive-F-2's text now lives in
    gdrive-F-1, while gdrive-F-2 still EXISTS (holding other text). The gold id
    must follow the text."""
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    texts = ["alpha one two", "beta three four", "gamma five six", "delta seven"]
    for i, t in enumerate(texts):
        s.upsert_chunk(f"gdrive-F-{i}", t, f"h{i}",
                       {"source_type": "gdrive", "file_id": "F", "chunk_index": i,
                        "chunk_total": 4})
    new_texts = ["alpha one two\nbeta three four", "gamma five six", "delta seven"]
    new = [Chunk(f"gdrive-F-{i}", t, f"n{i}", {"source_type": "gdrive", "file_id": "F",
                                               "chunk_index": i}, [t])
           for i, t in enumerate(new_texts)]
    s.apply_reflow("F", "drive", plan(s.owner_chunks(["gdrive-F-"]), new), [[0.1] * 4] * 3)
    assert s.get_chunk("gdrive-F-2") is not None
    gold = tmp_path / "gold.yaml"
    gold.write_text('- expected_chunk_ids:\n    - "gdrive-F-2"  # gamma\n'
                    "    - gdrive-F-3\n")
    ro = Store(tmp_path / "a.sqlite3", dim=4, read_only=True)
    assert tenant.remap_gold(gold, ro, from_start=True) == [("gdrive-F-2", "gdrive-F-1"),
                                          ("gdrive-F-3", "gdrive-F-2")]
    assert '    - "gdrive-F-1"  # gamma\n' in gold.read_text()
    assert tenant.remap_gold(gold, ro) == []


def test_attachment_ids_with_filename_spaces_are_matched(tmp_path):
    """Gmail attachment doc_ids carry the attachment filename, spaces and all;
    the real gold set holds them both quoted and bare."""
    a = "gmail-M1-att-0-Board pack (final) v2.pdf-0"
    b = "gmail-M2-att-1-Roster - term 3.xlsx-1"
    store = _FakeStore([("M1", a, "gmail-M1-att-0-Board pack (final) v2.pdf-1", "t1"),
                        ("M2", b, "gmail-M2-att-1-Roster - term 3.xlsx-0", "t2")])
    gold = tmp_path / "gold.yaml"
    gold.write_text(f'- expected_chunk_ids:\n    - "{a}"\n    - {b}  # roster\n')
    assert [o for o, _n in tenant.remap_gold(gold, store, from_start=True)] == [a, b]
    text = gold.read_text()
    assert '    - "gmail-M1-att-0-Board pack (final) v2.pdf-1"\n' in text
    assert "    - gmail-M2-att-1-Roster - term 3.xlsx-0  # roster\n" in text


# -- residual R2: the watermark is persisted whenever it advances, and a gold
# file without one never replays history onto ids added after the reflow ----

def test_watermark_is_written_when_it_advances_without_an_id_change(tmp_path):
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("- expected_chunk_ids:\n    - gdrive-G-0\n")
    assert tenant.remap_gold(gold, store, from_start=True) == []
    assert f"{tenant._GOLD_WATERMARK}1\n" in gold.read_text()
    # A later edit adds gdrive-F-4 -- which is CURRENT text now. A re-run must
    # not apply the already-seen batch to it.
    gold.write_text(gold.read_text() + "    - gdrive-F-4\n")
    assert tenant.remap_gold(gold, store) == []
    assert "    - gdrive-F-4\n" in gold.read_text()


def test_watermark_is_not_written_on_a_dry_run(tmp_path):
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1")])
    gold = tmp_path / "gold.yaml"
    original = "- expected_chunk_ids:\n    - gdrive-G-0\n"
    gold.write_text(original)
    assert tenant.remap_gold(gold, store, dry_run=True, from_start=True) == []
    assert gold.read_text() == original


def test_no_watermark_starts_at_the_current_max_id_unless_from_start(tmp_path):
    """A gold file with no watermark and no --from-start was (as far as we can
    tell) written against the CURRENT store: replaying reflow_map from id 0
    would move ids that already name current text."""
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1"),
                        ("F", "gdrive-F-5", "gdrive-F-4", "t1")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("- expected_chunk_ids:\n    - gdrive-F-4\n")
    info: dict = {}
    assert tenant.remap_gold(gold, store, info=info) == []
    assert info == {"after": 2, "watermark": 2, "adopted": True}
    text = gold.read_text()
    assert "    - gdrive-F-4\n" in text and f"{tenant._GOLD_WATERMARK}2\n" in text


def test_from_start_replays_a_watermarkless_file_from_id_zero(tmp_path):
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("- expected_chunk_ids:\n    - gdrive-F-4\n")
    info: dict = {}
    assert tenant.remap_gold(gold, store, from_start=True, info=info) == [
        ("gdrive-F-4", "gdrive-F-3")]
    assert info == {"after": 0, "watermark": 1, "adopted": False}


def test_cli_prints_where_a_watermarkless_file_started(tmp_path, monkeypatch, capsys):
    store = _FakeStore([("F", "gdrive-F-4", "gdrive-F-3", "t1")])
    gold = tmp_path / "gold.yaml"
    gold.write_text("- expected_chunk_ids:\n    - gdrive-F-4\n")
    monkeypatch.setattr(tenant, "_open_gold_store", lambda: store)
    assert tenant.main(["remap-gold", str(gold), "--write"]) == 0
    out = capsys.readouterr().out
    assert "no watermark" in out and "id 1" in out and "--from-start" in out
    assert "    - gdrive-F-4\n" in gold.read_text()
