"""Task 13: carry-over on shared-drive cache import.

When an install imports a cache artifact for a file whose content it already
holds (every local chunk's Drive `modified` equals the artifact's), the import
goes through the reflow carry-over (reflow.plan + Store.apply_reflow) instead
of a plain replace, so this install's enrichment state and provenance
(relations, observations, actions, ...) survive the re-chunk. Spec
2026-09-24 extraction-fidelity §3/§4 ("Shared-drive ingest cache").
"""
import json

import pytest

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import ENRICH_LOGIC_VERSION, ReflowOrphanError, Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")
PDF = "application/pdf"
M = "2026-09-01T10:00:00.000Z"
V = [0.1, 0.2, 0.3, 0.4]


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """The in-flight-unit guard reads config.app_dir()/enrich_queue by default;
    keep it off the real app dir."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    return home


def _store(tmp_path, name):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _local(tmp_path, texts=("alpha beta gamma", "delta epsilon"), modified=M,
           unenriched=(), name="A.sqlite3", fid="F"):
    """Install A: file F already held locally, enriched (except the indexes in
    `unenriched`), relation on chunk 1."""
    a = _store(tmp_path, name)
    for i, t in enumerate(texts):
        enr = i not in unenriched
        a.import_cached_chunk(
            f"gdrive-{fid}-{i}", t, f"h{i}",
            {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
             "chunk_total": len(texts), "drive_id": "D1", "mime_type": PDF,
             "modified": modified},
            V, enriched=enr, enriched_version=ENRICH_LOGIC_VERSION if enr else 0)
    with a._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Dana Okafor','person')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1','works_at','e2',?)", (f"gdrive-{fid}-1",))
    return a


def _publish(tmp_path, fs, texts, modified=M, enriched=True, fid="F"):
    """A peer install P publishes F re-chunked under the new extractor."""
    p = _store(tmp_path, f"P-{fid}.sqlite3")
    for i, t in enumerate(texts):
        p.import_cached_chunk(
            f"gdrive-{fid}-{i}", t, f"n{i}",
            {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
             "chunk_total": len(texts), "drive_id": "D1", "mime_type": PDF,
             "modified": modified, "extraction_version": 1},
            V, enriched=enriched, enriched_version=ENRICH_LOGIC_VERSION if enriched else 0)
    if enriched:
        extraction = {
            "thread_id": f"gdrive-{fid}", "org": "unknown", "content_type": "update",
            "summary": "Marcus Reyes reviewed the budget.",
            "messages": [{"message_id": "m1", "sender": "marcus@example.org",
                          "date": "2026-09-01", "subject": "Budget"}],
            "entities": [{"name": "Marcus Reyes", "type": "person"}],
            "relations": [], "actions": [], "topics": [],
        }
        p.set_enrich_payload(fid, json.dumps(extraction), ENRICH_LOGIC_VERSION)
    assert ingest_cache.publish_file(p, fs, "D1", fid, "vh2", PIN) is True


def _chunks(s):
    with s._connect() as db:
        return [tuple(r) for r in db.execute(
            "SELECT doc_id, text, enriched, enriched_version FROM chunks "
            "WHERE doc_id LIKE 'gdrive-F-%' ORDER BY doc_id")]


def _relation_doc(s):
    with s._connect() as db:
        return db.execute("SELECT source_doc_id FROM entity_relations "
                          "WHERE entity_a='e1'").fetchone()[0]


def _graph_counts(s):
    with s._connect() as db:
        return {t: db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("entities", "entity_relations", "entity_observations",
                          "actions", "graph_actions_legacy", "graph_decisions_legacy",
                          "chunks")}


def _reflow_map(s):
    with s._connect() as db:
        return [tuple(r) for r in db.execute(
            "SELECT owner, old_doc_id, new_doc_id FROM reflow_map ORDER BY old_doc_id")]


def test_same_modified_import_carries_enrichment_and_provenance(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])
    before = _graph_counts(a)

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    assert _chunks(a) == [("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1,
                           ENRICH_LOGIC_VERSION)]
    assert _relation_doc(a) == "gdrive-F-0"
    # Every covered row was already extracted locally: the peer's extraction is
    # NOT re-applied (no second set of actions/decisions, no summary chunk).
    after = _graph_counts(a)
    assert after == {**before, "chunks": before["chunks"] - 1}
    assert _reflow_map(a) == [("F", "gdrive-F-0", "gdrive-F-0"),
                              ("F", "gdrive-F-1", "gdrive-F-0")]
    with a._connect() as db:
        meta = json.loads(db.execute(
            "SELECT metadata FROM chunks WHERE doc_id='gdrive-F-0'").fetchone()[0])
        nvec = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
        src = db.execute("SELECT source FROM reflow_owners WHERE owner='F'").fetchone()[0]
        ent = db.execute("SELECT 1 FROM entities WHERE name='Marcus Reyes'").fetchone()
    assert meta["extraction_version"] == 1 and meta["drive_id"] == "D1"
    assert nvec == 1
    assert src == "drive_import"
    assert ent is None


def test_locally_unenriched_covered_row_applies_the_cached_extraction(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path, unenriched=(1,))
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    # The covered row's text was only half-extracted locally; the artifact's
    # extraction is applied, which is what justifies marking it enriched.
    assert _chunks(a) == [("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1,
                           ENRICH_LOGIC_VERSION)]
    with a._connect() as db:
        assert db.execute("SELECT 1 FROM entities WHERE name='Marcus Reyes'").fetchone()
    assert _relation_doc(a) == "gdrive-F-0"


def _unit(home, uid, body, claim=False):
    q = home / "enrich_queue"
    (q / "units").mkdir(parents=True, exist_ok=True)
    (q / "units" / f"{uid}.json").write_text(json.dumps(body))
    if claim:
        (q / "claims").mkdir(parents=True, exist_ok=True)
        (q / "claims" / uid).write_text("")


def test_in_flight_unit_defers_the_carry_over_import(tmp_path, _home):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])
    _unit(_home, "u1", {"unit_id": "u1", "kind": "thread", "threads": [
        {"thread_id": "gdrive-F", "messages": [
            {"message_id": "F", "chunk_doc_ids": ["gdrive-F-1"]}]}]}, claim=True)
    before = (_chunks(a), _graph_counts(a))

    with pytest.raises(ingest_cache.ImportDeferred):
        ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF)
    assert (_chunks(a), _graph_counts(a)) == before
    assert _relation_doc(a) == "gdrive-F-1" and _reflow_map(a) == []


def test_unit_for_another_file_does_not_defer(tmp_path, _home):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])
    _unit(_home, "u2", {"unit_id": "u2", "kind": "thread", "threads": [
        {"thread_id": "gdrive-G", "part_doc_ids": ["gdrive-G-0"]}]})
    (_home / "enrich_queue" / "units" / "junk.json").write_text("{not json")

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True
    assert _relation_doc(a) == "gdrive-F-0"


def test_bootstrap_continues_past_a_refused_file(tmp_path, _home):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path, fid="F")
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"], fid="F")
    _publish(tmp_path, fs, ["second file text"], fid="G")
    _unit(_home, "u1", {"unit_id": "u1", "kind": "thread",
                        "threads": [{"thread_id": "gdrive-F"}]})

    summary = ingest_cache.bootstrap_drive(a, fs, "D1", PIN)
    assert summary["imported"] == 1 and summary["skipped"] == 1
    with a._connect() as db:
        assert db.execute("SELECT 1 FROM chunks WHERE doc_id='gdrive-G-0'").fetchone()
    assert _relation_doc(a) == "gdrive-F-1"


def test_bootstrap_continues_past_an_orphan_error(tmp_path, monkeypatch):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path, fid="F")
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"], fid="F")
    _publish(tmp_path, fs, ["second file text"], fid="G")

    def boom(*_a, **_k):
        raise ReflowOrphanError("reflow F: 1 dangling reference(s)")

    monkeypatch.setattr(a, "apply_reflow", boom)
    summary = ingest_cache.bootstrap_drive(a, fs, "D1", PIN)
    assert summary["imported"] == 1 and summary["skipped"] == 1
    with a._connect() as db:
        assert db.execute("SELECT 1 FROM chunks WHERE doc_id='gdrive-G-0'").fetchone()


def test_uncovered_row_stays_unenriched_even_when_artifact_is_enriched(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    # Chunk 1 is text the local copy never had (e.g. recovered speaker notes).
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon",
                            "zeta speaker notes never extracted"])

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    assert _chunks(a) == [
        ("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1, ENRICH_LOGIC_VERSION),
        ("gdrive-F-1", "zeta speaker notes never extracted", 0, 0),
    ]
    # The relation's text now lives in chunk 0; it is remapped there, not left
    # pointing at the positional id that now holds different text.
    assert _relation_doc(a) == "gdrive-F-0"


def test_unenriched_artifact_keeps_the_plans_carried_state(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"], enriched=False)

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    # Local enrichment carried over by the plan even though the artifact has none.
    assert _chunks(a) == [("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1,
                           ENRICH_LOGIC_VERSION)]
    assert _relation_doc(a) == "gdrive-F-0"


def test_different_modified_takes_the_plain_replace_path(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"],
             modified="2026-09-02T09:00:00.000Z")

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    # Existing behaviour: rows replaced, shrunk tail swept, nothing remapped.
    assert [c[0] for c in _chunks(a)] == ["gdrive-F-0"]
    assert _relation_doc(a) == "gdrive-F-1"
    assert _reflow_map(a) == []


def test_no_local_chunks_takes_the_plain_path(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])
    b = _store(tmp_path, "B.sqlite3")

    assert ingest_cache.try_import(b, fs, "D1", "F", "vh2", PIN, mime=PDF) is True
    assert [c[0] for c in _chunks(b)] == ["gdrive-F-0"]
    assert _reflow_map(b) == []


def test_lineage_gone_plan_falls_back_to_plain_replace(tmp_path, monkeypatch):
    from mcpbrain import reflow
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])

    real = reflow.plan

    def gone(old, new):
        p = real(old, new)
        p.reasons = {k: "lineage_gone" for k in p.reasons}
        return p

    monkeypatch.setattr(reflow, "plan", gone)
    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True
    assert [c[0] for c in _chunks(a)] == ["gdrive-F-0"]
    assert _relation_doc(a) == "gdrive-F-1"
    assert _reflow_map(a) == []


def test_orphan_error_is_raised_not_silently_replaced(tmp_path, monkeypatch):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])

    def boom(*_a, **_k):
        raise ReflowOrphanError("reflow F: 1 dangling reference(s)")

    monkeypatch.setattr(a, "apply_reflow", boom)
    with pytest.raises(ReflowOrphanError):
        ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF)
    # Nothing replaced: the old chunks and the relation are untouched.
    assert [c[0] for c in _chunks(a)] == ["gdrive-F-0", "gdrive-F-1"]
    assert _relation_doc(a) == "gdrive-F-1"


# -- final review I3: a stale payload never rides on a partly-enriched file ----

def _artifact_enrich(fs, fid="F", ch="vh2"):
    art = ingest_cache._load(fs, ingest_cache._artifact_path(fid, ch, PIN, PDF))
    return art.enrich


def _payload_publisher(tmp_path, enriched_rows):
    p = _store(tmp_path, "PP.sqlite3")
    for i, t in enumerate(("alpha beta gamma", "fresh speaker notes")):
        p.import_cached_chunk(
            f"gdrive-F-{i}", t, f"n{i}",
            {"source_type": "gdrive", "file_id": "F", "chunk_index": i, "chunk_total": 2,
             "drive_id": "D1", "mime_type": PDF, "modified": M, "extraction_version": 1},
            V, enriched=i in enriched_rows,
            enriched_version=ENRICH_LOGIC_VERSION if i in enriched_rows else 0)
    p.set_enrich_payload("F", json.dumps({"thread_id": "gdrive-F", "summary": "old"}),
                         ENRICH_LOGIC_VERSION)
    return p


def test_publish_file_withholds_the_payload_when_a_chunk_is_unenriched(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    p = _payload_publisher(tmp_path, enriched_rows={0})
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True
    assert "extraction" not in _artifact_enrich(fs)


def test_publish_file_attaches_the_payload_when_every_chunk_is_enriched(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    p = _payload_publisher(tmp_path, enriched_rows={0, 1})
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True
    assert _artifact_enrich(fs)["extraction"]["summary"] == "old"


@pytest.mark.parametrize("body", ["not json{", "5", "null", "[1, 2]"])
def test_publish_file_withholds_a_corrupt_payload_instead_of_failing(tmp_path, body):
    """A corrupt payload body fails closed: the file still publishes, without
    the payload, instead of raising for that file on every cycle."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    p = _payload_publisher(tmp_path, enriched_rows={0, 1})
    p.set_enrich_payload("F", body, ENRICH_LOGIC_VERSION)
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True
    assert "extraction" not in (_artifact_enrich(fs) or {})


# -- final review I8: the import carry-over honours the reflow halt ------------

def test_carry_over_import_is_deferred_while_reflow_is_halted(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])
    a.set_cursor("reflow:halted", "reflow X: 1 dangling reference(s)")
    before = (_chunks(a), _graph_counts(a))
    with pytest.raises(ingest_cache.ImportDeferred):
        ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF)
    assert (_chunks(a), _graph_counts(a)) == before
    assert _relation_doc(a) == "gdrive-F-1" and _reflow_map(a) == []


# -- residual R1: a payload publishes only when it covers the whole file ------

def _noop_apply(store, extraction, *, doc_ids, entity_index=None):
    return {}


def _drain_unit(store, home, fid, chunk_ids, summary_text):
    """Run the REAL drain over one Drive unit whose message carries exactly
    `chunk_ids` (what prepare packed: the file's unenriched chunks)."""
    from mcpbrain import drain as drain_mod
    units = home / "enrich_queue" / "units"
    units.mkdir(parents=True, exist_ok=True)
    msg = {"message_id": fid, "sender": "", "date": "2026-09-01", "labels": "",
           "subject": "Budget", "chunk_doc_ids": list(chunk_ids)}
    (units / "u1.json").write_text(json.dumps(
        {"kind": "thread", "threads": [{"thread_id": fid, "messages": [msg]}]}))
    inbox = home / "enrich_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    env = {"thread_id": fid, "org": "unknown", "content_type": "update",
           "summary": summary_text, "entities": [], "topics": [], "actions": [],
           "relations": [], "messages": [{k: v for k, v in msg.items()
                                          if k != "chunk_doc_ids"}],
           "resolved_action_ids": [], "updated_actions": [],
           "reply_needed": False, "reply_reason": ""}
    (inbox / "u1.json").write_text(json.dumps(
        {"unit_id": "u1", "extractions": [env], "merge_answers": []}))
    return drain_mod.drain(store, home=home, apply=_noop_apply)


def _reflowed_publisher(tmp_path, home):
    """P: F enriched as two chunks with a whole-file payload; a reflow keeps
    chunk 0's text (carried) and adds never-extracted text at chunk 1; drain
    then re-enriches ONLY that uncovered row."""
    from mcpbrain import reflow
    from mcpbrain.sync.normalise import Chunk
    p = _local(tmp_path, name="PR.sqlite3")
    p.set_enrich_payload("F", json.dumps({"thread_id": "F", "summary": "whole old"}),
                         ENRICH_LOGIC_VERSION)
    texts = ["alpha beta gamma\ndelta epsilon", "zeta speaker notes never extracted"]
    new = [Chunk(f"gdrive-F-{i}", t, f"n{i}",
                 {"source_type": "gdrive", "file_id": "F", "chunk_index": i,
                  "chunk_total": 2, "drive_id": "D1", "mime_type": PDF,
                  "modified": M, "extraction_version": 1}, [t])
           for i, t in enumerate(texts)]
    p.apply_reflow("F", "drive", reflow.plan(p.owner_chunks(["gdrive-F-"]), new), [V, V])
    assert [c[2] for c in _chunks(p)] == [1, 0]
    _drain_unit(p, home, "F", ["gdrive-F-1"], "only the speaker notes")
    assert [c[2] for c in _chunks(p)] == [1, 1]
    return p


def test_partial_reenrichment_payload_is_not_published_after_a_reflow(tmp_path, _home):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    p = _reflowed_publisher(tmp_path, _home)
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True
    assert "extraction" not in _artifact_enrich(fs)
    # A fresh install importing on the plain path re-enriches the whole file:
    # nothing is marked enriched from a payload that covers only chunk 1.
    b = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(b, fs, "D1", "F", "vh2", PIN, mime=PDF) is True
    assert [c[2] for c in _chunks(b)] == [0, 0]


def test_whole_file_drain_payload_is_published(tmp_path, _home):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    p = _local(tmp_path, name="PW.sqlite3", unenriched=(0, 1))
    _drain_unit(p, _home, "F", ["gdrive-F-0", "gdrive-F-1"], "the whole file")
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True
    assert _artifact_enrich(fs)["extraction"]["summary"] == "the whole file"


def test_fully_carried_reflow_keeps_a_whole_file_payload(tmp_path):
    from mcpbrain import reflow
    from mcpbrain.sync.normalise import Chunk
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    p = _local(tmp_path, name="PC.sqlite3")
    p.set_enrich_payload("F", json.dumps({"thread_id": "F", "summary": "whole old"}),
                         ENRICH_LOGIC_VERSION)
    new = [Chunk("gdrive-F-0", "alpha beta gamma\ndelta epsilon", "n0",
                 {"source_type": "gdrive", "file_id": "F", "chunk_index": 0,
                  "chunk_total": 1, "drive_id": "D1", "mime_type": PDF,
                  "modified": M, "extraction_version": 1},
                 ["alpha beta gamma\ndelta epsilon"])]
    p.apply_reflow("F", "drive", reflow.plan(p.owner_chunks(["gdrive-F-"]), new), [V])
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True
    assert _artifact_enrich(fs)["extraction"]["summary"] == "whole old"
