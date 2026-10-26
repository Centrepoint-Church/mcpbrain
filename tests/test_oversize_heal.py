"""One-shot heal of chunks written before the oversize/CSS fixes (Task 5).

Store.oversize_heal_targets picks the owners whose re-render now comes out
different (CSS-polluted Gmail bodies, over-window Drive tables);
Store.reset_embedding_for_oversize sends every over-window chunk back through
index_pending, which embeds it across windows. The daemon runs both ONCE,
inside the reflow seed pass after its gates, and records the `oversize_heal`
meta marker.
"""
import json
import time

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

_ALL_SERVICES = {"gmail_service": object(), "drive_service": object(),
                 "calendar_service": object()}


def _daemon(tmp_path, monkeypatch, s):
    from mcpbrain import daemon as dmod
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
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

    # Gated (no recent backup): nothing happens and no marker.
    assert _tick(d) == {"reflow_seed": "no_recent_backup"}
    assert s.get_meta("oversize_heal") is None
    assert _queued(s) == []
    assert _embedded(s, "gdrive-T-0") == 1

    # Open: enqueue the targets (epoch modified_at, like the seed), reset every
    # over-window chunk -- target owners included -- and write the marker.
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    _tick(d)
    assert _queued(s) == [("reflow:drive", "T", "1970-01-01T00:00:00"),
                          ("reflow:gmail", "M1", "1970-01-01T00:00:00")]
    assert _embedded(s, "gdrive-T-0") == 0
    assert _embedded(s, "gdrive-P-0") == 0
    assert _embedded(s, "gdrive-Q-0") == 1
    rec = json.loads(s.get_meta("oversize_heal"))
    assert rec["state"] == "done" and rec["at"]
    assert (rec["gmail"], rec["drive"], rec["reembed"]) == (1, 1, 2)

    # Second open tick: a no-op (no scan, no new reset, marker unchanged).
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET embedded=1")
        db.execute("DELETE FROM sync_queue")
    before = s.get_meta("oversize_heal")
    monkeypatch.setattr(Store, "oversize_heal_targets",
                        lambda self, **k: (_ for _ in ()).throw(AssertionError("scanned")))
    monkeypatch.setattr(Store, "reset_embedding_for_oversize",
                        lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("scanned")))
    _tick(d)
    assert s.get_meta("oversize_heal") == before
    assert _embedded(s, "gdrive-T-0") == 1


def test_heal_does_not_run_when_disabled_or_halted(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _seed_heal_store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _daemon(tmp_path, monkeypatch, s)
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    assert _tick(d) == {"reflow_seed": "disabled"}
    assert s.get_meta("oversize_heal") is None
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: True)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    assert _tick(d) == {"reflow_seed": "halted"}
    assert s.get_meta("oversize_heal") is None


# --- doctor -------------------------------------------------------------------

def _doctor(tmp_path, monkeypatch):
    from tests.test_doctor import _conns
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    return doctor.run_doctor(str(tmp_path), model_present=lambda h: True,
                             conns=_conns(), repairs={})[1]


def _line(msg):
    return next(ln for ln in msg.splitlines() if "Chunk window" in ln)


def test_doctor_oversize_pending_before_heal(tmp_path, monkeypatch):
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * 3000, source_type="gdrive")
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"⚠️ {'Chunk window':<16} 1 chunk(s) longer than one window — re-embed pending")


def test_doctor_oversize_after_heal(tmp_path, monkeypatch):
    s = _store(tmp_path, "brain.sqlite3")
    _c(s, "d1", "y" * 3000, source_type="gdrive")
    s.set_meta("oversize_heal", json.dumps({"state": "done", "at": "x", "gmail": 0,
                                            "drive": 0, "reembed": 1}))
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"✅ {'Chunk window':<16} 1 chunk(s) longer than one window, "
        f"embedded across windows")


def test_doctor_no_oversize(tmp_path, monkeypatch):
    _store(tmp_path, "brain.sqlite3")
    assert _line(_doctor(tmp_path, monkeypatch)) == (
        f"✅ {'Chunk window':<16} 0 chunk(s) longer than one window")
