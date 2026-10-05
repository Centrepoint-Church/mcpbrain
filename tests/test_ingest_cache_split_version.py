"""The prose split suffix (+s<SPLIT_VERSION>) in the shared-drive ingest-cache
fingerprint, and the reflow selector's 24 h guard on 'ordinary' outcomes.

Live defect (0.7.137): non-block, non-tabular prose MIMEs (text/html,
text/plain, ...) are chunked by chunking.chunk_text, whose output changed with
SPLIT_VERSION 1 -- but their cache fingerprint did not. A reflowed shared-drive
file routed to the ordinary path therefore re-imported its pre-SPLIT_VERSION
artifact (old word-split chunks, no split_version stamp), the split rule
re-selected it, and the reflow:drive backlog never reached zero.
"""
import base64
import gzip
import json
import struct
from datetime import datetime, timedelta, timezone

from mcpbrain import ingest_cache
from mcpbrain.chunking import SPLIT_VERSION
from mcpbrain.org_contracts import (DRIVE_ID_META_KEY, CacheArtifact, CacheChunk,
                                    FleetPin, artifact_filename)
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")
HTML = "text/html"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _b64(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")


def _write_raw_artifact(fs, file_id, content_hash, *, chunker, mime_type=HTML,
                        texts=("old html text",), extra=None, published_at="2026-07-03"):
    """An artifact a PEER install left behind under a specific fingerprint."""
    chunks = []
    for i, t in enumerate(texts):
        meta = {"source_type": "gdrive", "file_id": file_id, "chunk_index": i,
                "chunk_total": len(texts), "mime_type": mime_type,
                "content_subtype": "prose", **(extra or {})}
        chunks.append(CacheChunk(idx=i, text=t, embedding_b64=_b64([0.1, 0.2, 0.3, 0.4]),
                                 metadata=meta))
    art = CacheArtifact(file_id=file_id, content_hash=content_hash,
                        extraction_method="text", chunker_version=chunker,
                        embed_model="bge-small", dim=4, chunks=tuple(chunks),
                        enrich={}, published_by="p@x.org", published_at=published_at)
    fname = artifact_filename(file_id, content_hash, "bge-small", 4, chunker)
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{fname}",
                 gzip.compress(json.dumps(art.to_dict()).encode()))


# -- fingerprint --------------------------------------------------------------

def test_prose_mimes_carry_the_split_suffix():
    base = ingest_cache.effective_chunker_version(PIN)
    for mime in ("text/html", "text/plain", "text/markdown", "application/json",
                 "message/rfc822"):
        assert ingest_cache.effective_chunker_version(PIN, mime) == f"{base}+s{SPLIT_VERSION}"


def test_tabular_block_and_unknown_mimes_keep_their_fingerprint():
    base = ingest_cache.effective_chunker_version(PIN)
    for mime in ("text/csv", "application/csv", "text/tab-separated-values", XLSX,
                 "application/vnd.ms-excel", "application/vnd.google-apps.spreadsheet",
                 "", "image/png"):
        assert ingest_cache.effective_chunker_version(PIN, mime) == base
    assert ingest_cache.effective_chunker_version(PIN, PDF) == base + "+x1"


def test_split_mimes_are_derived_from_drive_routing():
    from mcpbrain.sync import blocks, drive, tabular
    got = ingest_cache.split_suffixed_mimes()
    assert got == ((drive._DOWNLOAD_TEXT | set(drive._DOWNLOAD_BINARY))
                   - tabular.TABLE_MIMES - set(blocks.EXTRACTION_VERSIONS))
    assert "text/html" in got and "text/csv" not in got


# -- read path ----------------------------------------------------------------

def test_old_fingerprint_html_artifact_is_not_imported(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _write_raw_artifact(fs, "H1", "md5a", chunker=ingest_cache.effective_chunker_version(PIN))
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5a", PIN, mime=HTML) is False
    assert B.get_chunk("gdrive-H1-0") is None


def test_new_fingerprint_html_artifact_is_imported_with_its_stamp(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _write_raw_artifact(fs, "H1", "md5a",
                        chunker=ingest_cache.effective_chunker_version(PIN, HTML),
                        extra={"split_version": SPLIT_VERSION})
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5a", PIN, mime=HTML) is True
    assert B.get_chunk("gdrive-H1-0")["metadata"]["split_version"] == SPLIT_VERSION


def test_new_fingerprint_artifact_without_the_stamp_is_refused(tmp_path):
    """Defence in depth: an artifact under the +s fingerprint whose chunks do
    not carry split_version is old-shape output mislabelled -- importing it
    would restart the loop."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _write_raw_artifact(fs, "H1", "md5a",
                        chunker=ingest_cache.effective_chunker_version(PIN, HTML))
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5a", PIN, mime=HTML) is False


# -- write path ---------------------------------------------------------------

def _local_html(store, fid, *, stamped):
    md = {"source_type": "gdrive", "file_id": fid, "chunk_index": 0, "chunk_total": 1,
          DRIVE_ID_META_KEY: "D1", "mime_type": HTML}
    if stamped:
        md["split_version"] = SPLIT_VERSION
    store.import_cached_chunk(f"gdrive-{fid}-0", "html text", "ch0", md, [0.1, 0.2, 0.3, 0.4])


def test_stamped_html_publishes_under_the_suffixed_fingerprint(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    _local_html(A, "H1", stamped=True)
    assert ingest_cache.publish_file(A, fs, "D1", "H1", "md5a", PIN) is True
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5a", PIN, mime=HTML) is True


def test_unstamped_local_html_never_publishes_under_the_suffixed_fingerprint(tmp_path):
    """A pending publish recorded by pre-fix code (chunks never stamped) must
    not be laundered into the new fingerprint: it publishes under the base
    pipeline it actually belongs to."""
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    A = _store(tmp_path, "A.sqlite3")
    _local_html(A, "H1", stamped=False)
    assert ingest_cache.publish_file(A, fs, "D1", "H1", "md5a", PIN) is True
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5a", PIN, mime=HTML) is False
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5a", PIN) is True


# -- GC / bootstrap -----------------------------------------------------------

def test_bootstrap_imports_a_suffixed_html_artifact(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _write_raw_artifact(fs, "H1", "md5a",
                        chunker=ingest_cache.effective_chunker_version(PIN, HTML),
                        extra={"split_version": SPLIT_VERSION})
    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 1
    assert B.get_chunk("gdrive-H1-0") is not None


def test_bootstrap_rejects_a_pre_split_html_artifact(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _write_raw_artifact(fs, "H2", "md5b", chunker=ingest_cache.effective_chunker_version(PIN))
    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 0 and summary["skipped"] == 1
    assert B.get_chunk("gdrive-H2-0") is None


def test_gc_collects_a_stale_suffixed_html_version_and_keeps_the_current(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    cur = ingest_cache.effective_chunker_version(PIN, HTML)
    stamp = {"split_version": SPLIT_VERSION}
    _write_raw_artifact(fs, "H1", "md5old", chunker=cur, extra=stamp)
    _write_raw_artifact(fs, "H1", "md5new", chunker=cur, extra=stamp)
    assert ingest_cache.gc_superseded(fs, "D1", "H1", "md5new", PIN) == 1
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5old", PIN, mime=HTML) is False
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5new", PIN, mime=HTML) is True


def test_gc_batch_collects_a_stale_suffixed_html_version(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    cur = ingest_cache.effective_chunker_version(PIN, HTML)
    stamp = {"split_version": SPLIT_VERSION}
    _write_raw_artifact(fs, "H1", "md5old", chunker=cur, extra=stamp)
    _write_raw_artifact(fs, "H1", "md5new", chunker=cur, extra=stamp)
    assert ingest_cache.gc_superseded_batch(fs, "D1", {"H1": "md5new"}, PIN) == 1
    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "H1", "md5new", PIN, mime=HTML) is True


# -- selector 24 h guard ------------------------------------------------------

def _unstamped_owner(s, fid="H"):
    for i in range(2):
        s.upsert_chunk(f"gdrive-{fid}-{i}", f"t{i}", f"h{i}",
                       {"source_type": "gdrive", "file_id": fid, "mime_type": HTML,
                        "chunk_index": i, "chunk_total": 2})


def _set_outcome(s, owner, outcome, at):
    with s._connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO reflow_owners(owner, source, at, chunks_new,"
                   " carried, reenrich, outcome) VALUES(?,?,?,0,0,0,?)",
                   (owner, "drive", at.isoformat(), outcome))


def test_selector_skips_a_recent_ordinary_owner_and_retries_after_24h(tmp_path):
    s = _store(tmp_path)
    _unstamped_owner(s)
    now = datetime.now(timezone.utc)
    _set_outcome(s, "H", "ordinary", now - timedelta(hours=1))
    assert ("reflow:drive", "H") not in s.reflow_candidates(50)
    _set_outcome(s, "H", "ordinary", now - timedelta(hours=25))
    assert ("reflow:drive", "H") in s.reflow_candidates(50)


def test_selector_guard_is_only_for_ordinary_outcomes(tmp_path):
    s = _store(tmp_path)
    _unstamped_owner(s)
    _set_outcome(s, "H", "carried", datetime.now(timezone.utc))
    assert ("reflow:drive", "H") in s.reflow_candidates(50)


def test_held_owner_is_counted_not_hidden(tmp_path):
    """A guarded owner leaves the SEED but stays in remaining, reported as held."""
    s = _store(tmp_path)
    _unstamped_owner(s)
    _set_outcome(s, "H", "ordinary", datetime.now(timezone.utc) - timedelta(hours=1))
    assert ("reflow:drive", "H") not in s.reflow_candidates(50)           # seed: held
    assert ("reflow:drive", "H") in s.reflow_candidates(50, hold_recent_ordinary=False)
    st = s.reflow_stats(live_remaining=True)
    assert st["held"] == 1 and st["remaining"] == 1
    assert s.reflow_live_counts(50) == (1, 1)


def test_held_count_is_zero_once_the_guard_expires(tmp_path):
    s = _store(tmp_path)
    _unstamped_owner(s)
    _set_outcome(s, "H", "ordinary", datetime.now(timezone.utc) - timedelta(hours=25))
    st = s.reflow_stats(live_remaining=True)
    assert st["held"] == 0 and st["remaining"] == 1


def _seed_daemon(tmp_path, monkeypatch, s):
    import time
    from mcpbrain import daemon as dmod
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = {"gmail_service": object(), "drive_service": object(),
                                         "calendar_service": object()}, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    return d


def test_held_owner_blocks_backlog_empty_and_integrity_check(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _unstamped_owner(s)
    _set_outcome(s, "H", "ordinary", datetime.now(timezone.utc) - timedelta(hours=1))
    calls = []
    monkeypatch.setattr("mcpbrain.doctor._run_integrity_check",
                        lambda home: calls.append(home) or [])
    d = _seed_daemon(tmp_path, monkeypatch, s)
    assert d._run_reflow_seed() == {"reflow_seed": "ok", "enqueued": 0}   # not re-seeded
    assert calls == [] and not s.get_cursor("reflow:integrity_checked")
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["remaining"] == 1 and last["held"] == 1
    # The non-live status (/api/status) carries the seed's held figure too.
    assert s.reflow_status()["held"] == 1


def test_doctor_warns_on_held_owners(tmp_path):
    from mcpbrain.doctor import reflow_line
    s = _store(tmp_path)
    _unstamped_owner(s)
    _set_outcome(s, "H", "ordinary", datetime.now(timezone.utc) - timedelta(hours=1))
    line = reflow_line(s)
    assert line.startswith("⚠️")
    assert "1 owner(s) held: ordinary path did not converge" in line


# -- bootstrap robustness -----------------------------------------------------

def test_bootstrap_prefers_an_older_valid_split_artifact_over_a_newer_base_one(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _write_raw_artifact(fs, "H1", "md5s", texts=("new split text",),
                        chunker=ingest_cache.effective_chunker_version(PIN, HTML),
                        extra={"split_version": SPLIT_VERSION}, published_at="2026-07-01")
    _write_raw_artifact(fs, "H1", "md5b", texts=("old base text",),
                        chunker=ingest_cache.effective_chunker_version(PIN),
                        published_at="2026-08-01")
    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 1 and summary["skipped"] == 0
    assert B.get_chunk("gdrive-H1-0")["text"] == "new split text"


def test_split_stamp_check_survives_malformed_metadata():
    assert ingest_cache._carries_split_stamp([{"split_version": "abc"}]) is False
    assert ingest_cache._carries_split_stamp([{"split_version": [1]}]) is False
    assert ingest_cache._carries_split_stamp(["not a dict"]) is False
    assert ingest_cache._carries_split_stamp([{"split_version": SPLIT_VERSION}, 7]) is False


def test_bootstrap_continues_past_a_malformed_split_artifact(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    cur = ingest_cache.effective_chunker_version(PIN, HTML)
    _write_raw_artifact(fs, "BAD", "md5x", chunker=cur, extra={"split_version": "abc"})
    _write_raw_artifact(fs, "GOOD", "md5g", chunker=cur,
                        extra={"split_version": SPLIT_VERSION})
    B = _store(tmp_path, "B.sqlite3")
    summary = ingest_cache.bootstrap_drive(B, fs, "D1", PIN)
    assert summary["imported"] == 1 and summary["skipped"] == 1
    assert B.get_chunk("gdrive-GOOD-0") is not None
    assert B.get_chunk("gdrive-BAD-0") is None


# -- end to end ---------------------------------------------------------------

class _Emb:
    dim = 4

    def embed_passages(self, xs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]


class _HtmlSvc:
    """files().get -> fmeta (newer modifiedTime, same md5); get_media -> html."""
    def __init__(self, html, modified, md5):
        self.html, self.modified, self.md5 = html, modified, md5

    def files(self):
        return self

    def get(self, **kw):
        fm = {"id": kw["fileId"], "name": "page.html", "mimeType": HTML,
              "modifiedTime": self.modified, "md5Checksum": self.md5, "parents": [],
              "version": "7", "owners": []}

        class R:
            def execute(self, num_retries=0):
                return fm
        return R()

    def get_media(self, **kw):
        body = self.html.encode()

        class R:
            def execute(self, num_retries=0):
                return body
        return R()


def test_shared_drive_html_reflow_ordinary_path_stamps_and_converges(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain.sync import drive
    from mcpbrain.sync.reflow_handler import ReflowContext

    s = _store(tmp_path)
    old_mod, new_mod, md5 = "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z", "md5html"
    old_texts = ("old word split one", "old word split two")
    for i, t in enumerate(old_texts):
        s.upsert_chunk(f"gdrive-H-{i}", t, f"h{i}",
                       {"source_type": "gdrive", "file_id": "H", "mime_type": HTML,
                        "content_subtype": "prose", "modified": old_mod,
                        "chunk_index": i, "chunk_total": 2, DRIVE_ID_META_KEY: "D1"})
    assert ("reflow:drive", "H") in s.reflow_candidates(50)

    fs = LocalDirFleetStorage(tmp_path / "fleet")
    # The pre-SPLIT_VERSION artifact for this exact file version (same md5).
    _write_raw_artifact(fs, "H", md5, chunker=ingest_cache.effective_chunker_version(PIN),
                        texts=old_texts, extra={"modified": old_mod})

    html = "\n\n".join(f"<p>Paragraph {i} " + "word " * 300 + "</p>" for i in range(6))
    svc = _HtmlSvc(html, new_mod, md5)

    def _shared(item):
        drive.handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN,
                                       drive_id="D1")

    ctx = ReflowContext(s, _Emb(), str(tmp_path), drive_service=svc,
                        normal_handlers={"drive": _shared})
    assert ctx.handle({"source": "reflow:drive", "ref_id": "H", "attempts": 0}) is None

    rows = s.owner_chunks(["gdrive-H-"])
    assert rows and all(r["metadata"].get("split_version") == SPLIT_VERSION for r in rows)
    assert all(r["metadata"].get("modified") == new_mod for r in rows)
    # A local miss: queued to republish (under +s, once embedded).
    assert s.pending_publishes("D1") == [("H", md5)]
    # Converged on its own, not merely held back by the 24 h guard.
    with s._connect(write=True) as db:
        db.execute("DELETE FROM reflow_owners")
    assert ("reflow:drive", "H") not in s.reflow_candidates(50)
