"""One-shot heal of chunks written before the oversize/CSS fixes (Task 5).

Store.oversize_heal_targets picks the owners whose re-render now comes out
different (CSS-polluted Gmail bodies, over-window Drive tables);
Store.reset_embedding_for_oversize sends every over-window chunk back through
index_pending, which embeds it across windows. The daemon runs both ONCE,
inside the reflow seed pass after its gates, and records the `oversize_heal`
meta marker.
"""
import json
import threading
import time
from contextlib import contextmanager

from mcpbrain import doctor
from mcpbrain.embed import contextual_prefix
from mcpbrain.store import Store

_CSS = "p { font-family: Arial; color: #333 } Hello Dana, see you Tuesday."


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _c(s, doc_id, text, **md):
    s.upsert_chunk(doc_id, text, doc_id + str(len(text)), md)


def _mark_embedded(s):
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET embedded=1")


def _embedded(s, doc_id):
    with s._connect() as db:
        return db.execute("SELECT embedded FROM chunks WHERE doc_id=?",
                          (doc_id,)).fetchone()[0]


# --- targets ----------------------------------------------------------------

def test_targets_css_gmail_and_oversize_drive_tables(tmp_path):
    s = _store(tmp_path)
    _c(s, "gmail-M1-body-0", _CSS, source_type="gmail", message_id="M1")
    _c(s, "gmail-M2-body-0", "@media screen { .x { mso-line-height: 1 } }",
       source_type="gmail", message_id="M2")
    # A brace alone (code, a template) is not CSS.
    _c(s, "gmail-M3-body-0", "use {name} in the template", source_type="gmail",
       message_id="M3")
    # A plain email mentioning a font is not CSS either.
    _c(s, "gmail-M4-body-0", "the font-family looked off", source_type="gmail",
       message_id="M4")
    _c(s, "gdrive-T-0", "r" * 2500, source_type="gdrive", file_id="T",
       content_subtype="table")
    _c(s, "gdrive-S-0", "r" * 1500, source_type="gdrive", file_id="S",
       content_subtype="table")
    _c(s, "gdrive-P-0", "p" * 2500, source_type="gdrive", file_id="P")
    got = s.oversize_heal_targets()
    assert sorted(got["reflow:gmail"]) == ["M1", "M2"]
    assert got["reflow:drive"] == ["T"]


def test_targets_exclude_queued_and_skipped_owners(tmp_path):
    s = _store(tmp_path)
    _c(s, "gmail-Q-body-0", _CSS, source_type="gmail", message_id="Q")
    _c(s, "gmail-K-body-0", _CSS, source_type="gmail", message_id="K",
       reflow_skipped="gave_up")
    _c(s, "gmail-K-body-1", _CSS, source_type="gmail", message_id="K")
    _c(s, "gdrive-D-0", "r" * 2500, source_type="gdrive", file_id="D",
       content_subtype="table", reflow_skipped="source_gone")
    _c(s, "gdrive-E-0", "r" * 2500, source_type="gdrive", file_id="E",
       content_subtype="table")
    s.enqueue_items([{"ref_id": "Q", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:gmail")
    s.enqueue_items([{"ref_id": "E", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:drive")
    assert s.oversize_heal_targets() == {"reflow:gmail": [], "reflow:drive": []}


# --- re-embed reset -----------------------------------------------------------

def test_reset_measures_the_prefixed_passage(tmp_path):
    s = _store(tmp_path)
    md = {"source_type": "gmail", "sender": "Dana Okafor <dana.okafor@example.org>",
          "subject": "Northgate Trust board pack", "date": "2026-10-01"}
    plen = len(contextual_prefix(md))
    assert plen > 20
    _c(s, "fits", "a" * (2000 - plen), **md)            # exactly one window
    _c(s, "over-by-prefix", "b" * (2000 - plen + 1), **md)
    _c(s, "long", "c" * 4000, source_type="gdrive", file_id="L")
    _c(s, "short", "d" * 100, source_type="gdrive", file_id="S")
    _mark_embedded(s)
    assert s.reset_embedding_for_oversize(2000) == 2
    assert [_embedded(s, d) for d in ("fits", "over-by-prefix", "long", "short")] \
        == [1, 0, 0, 1]


def test_reset_without_prefix_measures_raw_text(tmp_path):
    s = _store(tmp_path)
    md = {"source_type": "gmail", "sender": "Dana Okafor", "subject": "x" * 50}
    _c(s, "raw-fits", "a" * 1990, **md)
    _c(s, "raw-over", "b" * 2001, **md)
    _mark_embedded(s)
    assert s.reset_embedding_for_oversize(2000, use_prefix=False) == 1
    assert _embedded(s, "raw-fits") == 1 and _embedded(s, "raw-over") == 0


# --- daemon: once, behind the seed gates ----------------------------------------

def test_reset_tolerates_odd_metadata_and_batches_inside_the_section(tmp_path):
    """The store's json_valid CHECK keeps malformed text out, but metadata that
    is valid JSON yet not an object must not break the scan: it is measured
    with no prefix."""
    s = _store(tmp_path)
    for i in range(4):
        _c(s, f"long{i}", "c" * 2500, source_type="gdrive", file_id=f"L{i}")
    _c(s, "bad", "c" * 2500, source_type="gdrive")
    _mark_embedded(s)
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET metadata='[]' WHERE doc_id='bad'")
    entered = []

    @contextmanager
    def section():
        entered.append(1)
        yield

    assert s.reset_embedding_for_oversize(2000, bulk_section=section, batch=2) == 5
    assert len(entered) == 3                              # 2 + 2 + 1 rows
    assert _embedded(s, "bad") == 0


# --- daemon: behind the seed gates, bounded by the seed window -----------------

_ALL_SERVICES = {"gmail_service": object(), "drive_service": object(),
                 "calendar_service": object()}


def _daemon(tmp_path, monkeypatch, s):
    from mcpbrain import daemon as dmod
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    d._bulk_lock = threading.Lock()
    d._bulk_lock_waiters_lock, d._bulk_lock_waiters = threading.Lock(), 0
    d._stop = threading.Event()
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    # The end-of-backlog integrity check opens app_dir()/brain.sqlite3, which
    # these fixtures do not create; it is not what is under test here.
    d._reflow_backlog_empty = lambda: None
    return d


def _tick(d):
    d._last_reflow_seed = None
    return d._run_reflow_seed()


def _queued(s):
    with s._connect() as db:
        return sorted((r["source"], r["ref_id"], r["modified_at"]) for r in db.execute(
            "SELECT source, ref_id, modified_at FROM sync_queue"))


def _open(tmp_path):
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))


def _heal(s):
    raw = s.get_meta("oversize_heal")
    return json.loads(raw) if raw else None


def _no_scans(monkeypatch):
    def boom(self, *a, **k):
        raise AssertionError("scanned")
    monkeypatch.setattr(Store, "oversize_heal_targets", boom)
    monkeypatch.setattr(Store, "reset_embedding_for_oversize", boom)


def _seed_heal_store(tmp_path):
    s = _store(tmp_path)
    _c(s, "gmail-M1-body-0", _CSS, source_type="gmail", message_id="M1")
    _c(s, "gdrive-T-0", "r" * 2500, source_type="gdrive", file_id="T",
       content_subtype="table")
    _c(s, "gdrive-P-0", "p" * 2500, source_type="gdrive", file_id="P")
    _c(s, "gdrive-Q-0", "q" * 100, source_type="gdrive", file_id="Q")
    _mark_embedded(s)
    return s


def test_heal_waits_for_the_seed_gates_then_runs_once(tmp_path, monkeypatch):
    s = _seed_heal_store(tmp_path)
    d = _daemon(tmp_path, monkeypatch, s)

    # M3: no recent backup. The re-embed reset (non-destructive) runs under
    # the kill switch alone -- every over-window chunk, target owners included
    # -- and the marker remembers it; the reflow ENQUEUE waits for the gates.
    assert _tick(d) == {"reflow_seed": "no_recent_backup"}
    assert _queued(s) == []
    assert _embedded(s, "gdrive-T-0") == 0
    assert _embedded(s, "gdrive-P-0") == 0
    assert _embedded(s, "gdrive-Q-0") == 1
    rec = _heal(s)
    assert rec["state"] == "enqueuing" and rec["reembed"] == 2
    assert rec["remaining"] == {"reflow:gmail": ["M1"], "reflow:drive": ["T"]}
    assert (rec["gmail"], rec["drive"]) == (0, 0)

    # Open: enqueue the targets (epoch modified_at, like the seed) without a
    # second scan or reset.
    _open(tmp_path)
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET embedded=1")
    _no_scans(monkeypatch)
    _tick(d)
    assert _queued(s) == [("reflow:drive", "T", "1970-01-01T00:00:00"),
                          ("reflow:gmail", "M1", "1970-01-01T00:00:00")]
    assert _embedded(s, "gdrive-T-0") == 1
    rec = _heal(s)
    assert rec["state"] == "done" and rec["at"] and "remaining" not in rec
    assert (rec["gmail"], rec["drive"], rec["skipped"], rec["reembed"]) == (1, 1, 0, 2)

    # Second open tick: a no-op (no scan, no new reset, marker unchanged).
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET embedded=1")
        db.execute("DELETE FROM sync_queue")
    before = s.get_meta("oversize_heal")
    _no_scans(monkeypatch)
    _tick(d)
    assert s.get_meta("oversize_heal") == before
    assert _queued(s) == []
    assert _embedded(s, "gdrive-T-0") == 1


def test_heal_enqueues_at_most_the_seed_room_per_tick(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    monkeypatch.setattr(dmod, "REFLOW_WINDOW", 2)
    s = _store(tmp_path)
    for i in range(3):
        _c(s, f"gmail-G{i}-body-0", _CSS, source_type="gmail", message_id=f"G{i}")
        _c(s, f"gdrive-D{i}-0", "r" * 2500, source_type="gdrive", file_id=f"D{i}",
           content_subtype="table")
    _mark_embedded(s)
    d = _daemon(tmp_path, monkeypatch, s)
    _open(tmp_path)

    _tick(d)                                      # targets computed once, 2 queued
    rec = _heal(s)
    assert rec["state"] == "enqueuing" and rec["reembed"] == 3
    assert len(_queued(s)) == 2
    assert sum(len(v) for v in rec["remaining"].values()) == 4

    _no_scans(monkeypatch)                        # later ticks never rescan
    _tick(d)                                      # window still full: nothing added
    assert len(_queued(s)) == 2 and _heal(s)["state"] == "enqueuing"

    while _heal(s)["state"] != "done":
        with s._connect(write=True) as db:        # the reflow drains the window
            db.execute("DELETE FROM sync_queue")
        _tick(d)
        assert len(_queued(s)) <= 2
    rec = _heal(s)
    assert (rec["gmail"], rec["drive"], rec["skipped"]) == (3, 3, 0)


def test_heal_stops_while_the_seed_is_gated(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    monkeypatch.setattr(dmod, "REFLOW_WINDOW", 1)
    s = _store(tmp_path)
    for i in range(2):
        _c(s, f"gmail-G{i}-body-0", _CSS, source_type="gmail", message_id=f"G{i}")
    d = _daemon(tmp_path, monkeypatch, s)
    _open(tmp_path)
    _tick(d)
    assert len(_queued(s)) == 1 and _heal(s)["state"] == "enqueuing"
    with s._connect(write=True) as db:
        db.execute("DELETE FROM sync_queue")
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    _tick(d)
    assert _queued(s) == [] and _heal(s)["state"] == "enqueuing"


def test_heal_drops_owners_of_an_unworkable_source(tmp_path, monkeypatch):
    """A source this install cannot work (no Gmail scope, say) would only have
    its rows freed by the next seed. Its owners are dropped from the remainder
    and counted as skipped, so the heal still reaches "done"; the windowed
    re-embed still covers their chunks."""
    s = _seed_heal_store(tmp_path)
    d = _daemon(tmp_path, monkeypatch, s)
    d._reflow_source_sets = lambda home: ({"reflow:drive"}, {"reflow:drive"})
    _open(tmp_path)
    _tick(d)
    assert _queued(s) == [("reflow:drive", "T", "1970-01-01T00:00:00")]
    rec = _heal(s)
    assert rec["state"] == "done"
    assert (rec["gmail"], rec["drive"], rec["skipped"]) == (0, 1, 1)
    assert _embedded(s, "gmail-M1-body-0") == 1          # short: not over the window


def test_heal_failure_does_not_stop_the_seed(tmp_path, monkeypatch, caplog):
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", "t", source_type="gmail", message_id="N", chunk_total=2)
    d = _daemon(tmp_path, monkeypatch, s)

    def boom(self, **k):
        raise ValueError("bad row")
    monkeypatch.setattr(Store, "oversize_heal_targets", boom)
    _open(tmp_path)
    with caplog.at_level("WARNING", logger="mcpbrain.daemon"):
        out = _tick(d)
    assert out["reflow_seed"] == "ok" and out["enqueued"] == 1
    assert _heal(s) is None
    assert "oversize_heal failed" in caplog.text


def test_heal_does_not_run_when_disabled_or_halted(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _seed_heal_store(tmp_path)
    _open(tmp_path)
    d = _daemon(tmp_path, monkeypatch, s)
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    assert _tick(d) == {"reflow_seed": "disabled"}
    assert _heal(s) is None
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: True)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    assert _tick(d) == {"reflow_seed": "halted"}
    # Halted: the non-destructive reset still runs; nothing is enqueued.
    assert _heal(s)["state"] == "enqueuing" and _queued(s) == []
    assert _embedded(s, "gdrive-P-0") == 0


def test_heal_enqueues_only_sources_whose_service_is_up_now(tmp_path, monkeypatch):
    """M1: a source workable but whose service failed to build this tick keeps
    its owners in `remaining` (not skipped); they are enqueued once it is up."""
    s = _seed_heal_store(tmp_path)
    d = _daemon(tmp_path, monkeypatch, s)
    both = {"reflow:drive", "reflow:gmail"}
    d._reflow_source_sets = lambda home: ({"reflow:drive"}, both)
    _open(tmp_path)
    _tick(d)
    assert _queued(s) == [("reflow:drive", "T", "1970-01-01T00:00:00")]
    rec = _heal(s)
    assert rec["state"] == "enqueuing" and rec["remaining"] == {"reflow:gmail": ["M1"]}
    assert (rec["gmail"], rec["drive"], rec["skipped"]) == (0, 1, 0)
    d._reflow_source_sets = lambda home: (both, both)
    _tick(d)
    assert ("reflow:gmail", "M1", "1970-01-01T00:00:00") in _queued(s)
    rec = _heal(s)
    assert rec["state"] == "done" and (rec["gmail"], rec["skipped"]) == (1, 0)


# --- doctor -------------------------------------------------------------------

def _doctor(tmp_path, monkeypatch):
    from tests.test_doctor import _conns
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    return doctor.run_doctor(str(tmp_path), model_present=lambda h: True,
                             conns=_conns(), repairs={})[1]


def _line(msg):
    return next(ln for ln in msg.splitlines() if "Chunk window" in ln)


_W = f"{'Chunk window':<16}"


def test_doctor_pending_before_heal(tmp_path, monkeypatch):
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * 3000, source_type="gdrive")
    _mark_embedded(s)
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"⚠️ {_W} 1 chunk(s) longer than one window — re-embed pending")


def test_doctor_pending_while_heal_enqueuing(tmp_path, monkeypatch):
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * 3000, source_type="gdrive")
    _mark_embedded(s)
    s.set_meta("oversize_heal", json.dumps({"state": "enqueuing", "remaining": {}}))
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"⚠️ {_W} 1 chunk(s) longer than one window — re-embed pending")


def test_doctor_pending_counts_chunks_left_after_heal(tmp_path, monkeypatch):
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * 3000, source_type="gdrive")
    _c(s, "d2", "z" * 3000, source_type="gdrive")
    _mark_embedded(s)
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET embedded=0 WHERE doc_id='d2'")
    s.set_meta("oversize_heal", json.dumps({"state": "done"}))
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"⚠️ {_W} 2 chunk(s) longer than one window — re-embed pending (1 left)")


def test_doctor_healed_and_embedded(tmp_path, monkeypatch):
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * 3000, source_type="gdrive")
    _mark_embedded(s)
    s.set_meta("oversize_heal", json.dumps({"state": "done", "at": "x", "gmail": 0,
                                            "drive": 0, "skipped": 0, "reembed": 1}))
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"✅ {_W} 1 chunk(s) longer than one window, embedded across windows")


def test_doctor_no_oversize(tmp_path, monkeypatch):
    _store(tmp_path, "brain.sqlite3")
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"✅ {_W} 0 chunk(s) longer than one window")


def test_doctor_counts_the_prefixed_passage(tmp_path, monkeypatch):
    """I4: doctor uses the heal/embedder measure (prefix + text), so a chunk
    that crosses the window only with its context prefix is counted, both in
    the headline and in the pending (embedded=0) count."""
    from mcpbrain.embed import EMBED_WINDOW_CHARS, contextual_prefix
    md = {"source_type": "gdrive", "file_name": "Northgate Trust budget.pdf"}
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * (EMBED_WINDOW_CHARS - len(contextual_prefix(md)) + 5), **md)
    _c(s, "d2", "short", **md)
    assert s.count_passages_over_window(EMBED_WINDOW_CHARS) == (1, 1)
    assert s.count_passages_over_window(EMBED_WINDOW_CHARS, use_prefix=False) == (0, 0)
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"⚠️ {_W} 1 chunk(s) longer than one window — re-embed pending (1 left)")
