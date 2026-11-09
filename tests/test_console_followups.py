"""Console redesign follow-ups, task 1: words, locks, approvals, maintenance.

1  The action-update change reads as the action's text, never its id.
2  Every page word map answers an unknown code with words (console.js worded()).
3  Needs-you counts, the health reason and /api/findings agree on grouped findings.
4  The records-repo lock holds across processes and a killed holder never blocks.
5  config.write_config is a cross-process read-merge-write.
6  An approval whose bookkeeping fails after the change landed stays applied.
7  GET /api/maintenance: re-chunking and the ingestion clean-up, worded on Settings.
8  Graph tidy-up off: the daily review closes the lint findings it would read.
9  A second restart while one runs is ignored (route and tray).
10 Page assets: maintenance history, #hidden-said reset, grouped undone rows.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from mcpbrain import config
from mcpbrain.store import Store

_ROOT = Path(__file__).resolve().parent.parent
_WIZ = _ROOT / "mcpbrain" / "wizard"


# --- helpers -----------------------------------------------------------------

def _store(tmp_path, name="s.sqlite3") -> Store:
    s = Store(str(tmp_path / name), dim=4)
    s.init()
    return s


class FakeDaemon:
    def __init__(self):
        self.calls = []

    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}

    def stop(self):
        self.calls.append("stop")


def _req(srv, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {srv.token}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(h))
    return str(h)


@pytest.fixture
def server(tmp_path, home):
    from mcpbrain.control_api import ControlServer
    s = _store(tmp_path)
    srv = ControlServer(FakeDaemon(), home=home, store=s)
    srv.start()
    yield srv, s
    srv.stop()


def _node_ok():
    return shutil.which("node") is not None


def _blocks(text, marker):
    found = re.findall(rf"/\* {marker}:start \*/(.*?)/\* {marker}:end \*/", text, flags=re.S)
    assert found, f"{marker} block missing"
    return "\n".join(found)


def _node(tmp_path, js):
    p = tmp_path / "run.js"
    p.write_text(js)
    r = subprocess.run(["node", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# The console.js helpers the page blocks lean on, inlined for node.
_STUBS = r"""
const words = (s) => String(s || "").replace(/\s+—\s+/g, ", ").replace(/—/g, ", ");
const fmt = (n) => (typeof n === "number" ? n : 0).toLocaleString("en-AU");
const plural = (n, one, many) => fmt(n) + " " + (n === 1 ? one : (many || one + "s"));
"""


def _worded_js():
    return _blocks((_WIZ / "console.js").read_text(encoding="utf-8"), "worded")


# --- 1. action-update summary ------------------------------------------------

def test_action_update_summary_names_the_action_not_its_id(tmp_path):
    from mcpbrain import drain
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Book Hall B for the Acme planning day")
    d = tmp_path / "capture_inbox"
    d.mkdir()
    (d / "cap-1.json").write_text(json.dumps(
        {"kind": "action_update", "captured_at": "x", "source": "desktop",
         "action_id": aid, "status": "done"}))
    drain.drain_captures(s, home=tmp_path)
    row = s.recent_changes(5)[0]
    assert row["change_type"] == "capture_action_update"
    assert row["summary"] == 'Action "Book Hall B for the Acme planning day" marked done'
    assert row["ref_id"] == str(aid)
    assert "->" not in row["summary"]


# --- 2. worded fallbacks on every page ------------------------------------------

@pytest.mark.skipif(not _node_ok(), reason="node not installed")
def test_console_worded_helper(tmp_path):
    o = _node(tmp_path, _worded_js() + r"""
const M = {a: "Alpha", empty: ""};
console.log(JSON.stringify([worded(M, "a", "x"), worded(M, "zz", "x"), worded(M, "", "x"),
  worded(M, null, "x"), worded(M, "toString", "x"), worded(M, "empty", "x"), worded(null, "a", "x")]));""")
    assert o == ["Alpha", "x", "x", "x", "x", "x", "x"]
    js = (_WIZ / "console.js").read_text(encoding="utf-8")
    assert re.search(r"worded:\s*worded", js), "worded() is exported on mcpConsole"


@pytest.mark.skipif(not _node_ok(), reason="node not installed")
def test_dashboard_word_maps_fall_back_to_words(tmp_path):
    page = (_WIZ / "dashboard.html").read_text(encoding="utf-8")
    o = _node(tmp_path, _STUBS + _worded_js() + _blocks(page, "word-maps") + r"""
const U = "zz_new_code";
console.log(JSON.stringify({kind: kindLabel(U), type: typeLabel(U), skip1: skipText(U + ": Budget.xlsx", 1),
  skip3: skipText(U, 3), skip0: skipText(U, 1), reason: reasonText(U), source: sourceText(U),
  known: [kindLabel("tidy"), typeLabel("fact_added")[0], reasonText("bulk"), sourceText("drive"),
          skipText("extraction_empty", 2)]}));""")
    assert o["kind"] == "an automatic change"
    assert o["type"] == ["an automatic change", "automatic changes"]
    assert o["reason"] == "skipped for another reason"
    assert o["source"] == "another source"
    for k in ("skip1", "skip3", "skip0"):
        assert "skipped for another reason" in o[k].lower(), o[k]
    assert "Budget.xlsx" in o["skip1"]
    assert "zz_new_code" not in json.dumps(o)
    assert o["known"] == ["Graph tidy-up", "fact added", "bulk mail", "Drive",
                          "Skipped 2 items, no text could be read from it"]


@pytest.mark.skipif(not _node_ok(), reason="node not installed")
def test_settings_word_maps_fall_back_to_words(tmp_path):
    page = (_WIZ / "settings.html").read_text(encoding="utf-8")
    o = _node(tmp_path, _STUBS + _worded_js() + _blocks(page, "word-maps") + r"""
const U = "zz_new_code";
console.log(JSON.stringify([typeWord(U), connLabel({name: U, label: U}), typeWord("org"),
  connLabel({name: "claude"})]));""")
    assert o == ["Entity", "Another connection", "Organisation", "Claude Desktop"]


@pytest.mark.skipif(not _node_ok(), reason="node not installed")
def test_graph_word_maps_fall_back_to_words(tmp_path):
    page = (_WIZ / "graph.html").read_text(encoding="utf-8")
    o = _node(tmp_path, _STUBS + _worded_js() + _blocks(page, "word-maps") + r"""
const U = "zz_new_code";
console.log(JSON.stringify([kindWord(U), kindPlural(U), kindKey(U), kindWord("thread"),
  kindPlural("org")]));""")
    assert o == ["Unclassified", "Other kinds", "unknown", "Email thread", "Organisations"]


@pytest.mark.parametrize("page", ["dashboard.html", "settings.html", "graph.html"])
def test_pages_echo_no_raw_code_as_a_fallback(page):
    text = (_WIZ / page).read_text(encoding="utf-8")
    # MAP[code] || code, or (MAP[code] || [code]), or words(<code>) as a fallback.
    assert not re.search(r"\[(\w+(?:\.\w+)?)\]\s*\|\|\s*\[?\1\b", text)
    assert not re.search(r"\[([\w.]+)\]\s*\|\|\s*[\w.]+\s*\|\|", text)
    assert "words(r.key)" not in text
    assert "i.source || \"\"" not in text


# --- 3. grouped findings counted once -----------------------------------------------

def test_needs_you_counts_grouped_findings(server, home):
    from mcpbrain import console_stats
    srv, s = server
    s.record_finding("agent_stderr", "a.err", summary="Enrichment task failed", severity="warning")
    s.record_finding("agent_stderr", "b.err", summary="Enrichment task failed", severity="warning")
    code, out = _req(srv, "GET", "/api/findings")
    assert code == 200 and len(out["items"]) == 1 and out["items"][0]["count"] == 2
    status = {"open_findings": s.open_findings_count(), "connections": {}, "backfill": {}}
    st = console_stats.stats(s, home, status)
    assert st["needs_you"]["findings"] == 1
    assert "1 open finding" in st["health"]["reasons"]


# --- 4. records lock across processes -------------------------------------------------

def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _records(tmp_path) -> Path:
    repo = tmp_path / "records"
    (repo / "state").mkdir(parents=True)
    (repo / "memory").mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@example.org")
    _git(repo, "config", "user.name", "t")
    (repo / "MEMORY.md").write_text("# Memory Index\n")
    (repo / "state" / "decisions.md").write_text(
        "Append new decisions at the top. One line per decision.\n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "seed")
    return repo


def _write_memories(repo, tag, n, out_path):
    """Child process: commit n memories, recording what happened to each."""
    from mcpbrain import records_write
    results = []
    for i in range(n):
        try:
            records_write.write_memory(repo, slug=f"{tag}-{i}", description=f"Acme note {i}",
                                       body=f"Hall B note {tag} {i}.")
            results.append("ok")
        except records_write.RecordsBusy:
            results.append("busy")
        except Exception as exc:  # noqa: BLE001 -- the parent asserts on it
            results.append(f"error: {exc!r}")
    Path(out_path).write_text(json.dumps(results))


def _hold_records_lock(repo, ready_path):
    """Child process: take the records lock and keep it until killed."""
    from mcpbrain import records_write
    with records_write.repo_lock(repo):
        Path(ready_path).write_text("held")
        time.sleep(120)


def _wait_for(path, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if Path(path).exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def test_two_processes_commit_to_one_records_repo(tmp_path):
    repo = _records(tmp_path)
    ctx = mp.get_context("spawn")
    outs = [tmp_path / "a.json", tmp_path / "b.json"]
    procs = [ctx.Process(target=_write_memories, args=(str(repo), tag, 6, str(o)))
             for tag, o in zip(("acme", "pat"), outs)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    results = [json.loads(o.read_text()) for o in outs]
    for r in results:
        assert all(x in ("ok", "busy") for x in r), r
    _git(repo, "fsck", "--strict")
    log = _git(repo, "log", "--name-only", "--format=")
    for tag, r in zip(("acme", "pat"), results):
        for i, x in enumerate(r):
            if x == "ok":
                assert f"memory/{tag}-{i}.md" in log
    assert not (repo / ".git" / "index.lock").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX")
def test_records_lock_held_by_another_process_refuses_then_frees_on_kill(tmp_path, monkeypatch):
    from mcpbrain import records_write
    repo = _records(tmp_path)
    ready = tmp_path / "ready"
    p = mp.get_context("spawn").Process(target=_hold_records_lock, args=(str(repo), str(ready)))
    p.start()
    try:
        _wait_for(ready)
        monkeypatch.setattr(records_write, "LOCK_TIMEOUT_S", 0.3)
        with pytest.raises(records_write.RecordsBusy):
            records_write.append_decision(str(repo), text="Acme moves Hall B to Sunday")
    finally:
        os.kill(p.pid, signal.SIGKILL)
        p.join(10)
    # The OS dropped the dead holder's lock: nothing stale blocks the next writer.
    assert records_write.append_decision(str(repo), text="Acme moves Hall B to Sunday") is True
    assert (repo / ".git" / "mcpbrain-records.lock").exists()
    _git(repo, "fsck", "--strict")


def test_records_lock_is_reentrant_in_one_thread(tmp_path):
    from mcpbrain import records_write
    repo = _records(tmp_path)
    with records_write.repo_lock(str(repo)):
        assert records_write.append_decision(str(repo), text="Acme moves Hall B to Sunday") is True


def test_gardener_apply_takes_the_records_lock(tmp_path, monkeypatch):
    """brain_gardener_apply runs in the MCP server process: its write goes
    through the same cross-process lock as the daemon's writers."""
    src = (_ROOT / "mcpbrain" / "records_write.py").read_text(encoding="utf-8")
    for fn in ("write_gardener_reference", "write_gardener_context"):
        assert re.search(rf"@_locked\s*\ndef {fn}\(", src), fn


# --- 5. config writes across processes --------------------------------------------------

def _write_config_keys(home, prefix, n):
    from mcpbrain import config as cfg
    for i in range(n):
        cfg.write_config(home, {f"{prefix}{i}": i})


def test_two_processes_writing_config_both_survive(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_write_config_keys, args=(str(h), p, 40)) for p in ("a_", "b_")]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    cur = config.read_config(str(h))
    for p in ("a_", "b_"):
        for i in range(40):
            assert cur.get(f"{p}{i}") == i, (p, i)
    assert (h / "config.lock").exists()


def test_settings_and_api_config_run_inside_the_config_lock():
    sa = (_ROOT / "mcpbrain" / "settings_api.py").read_text(encoding="utf-8")
    assert "config.config_lock(" in sa
    d = (_ROOT / "mcpbrain" / "daemon.py").read_text(encoding="utf-8")
    body = d[d.index("def apply_config("):]
    body = body[:body.index("\n    def ", 10)]
    assert "config.config_lock(" in body


def test_config_lock_is_reentrant(tmp_path):
    with config.config_lock(str(tmp_path)):
        config.write_config(str(tmp_path), {"owner_name": "Dana"})
    assert config.read_config(str(tmp_path))["owner_name"] == "Dana"


# --- 6. approvals never apply twice ---------------------------------------------------------

def _pending_row(s, pid):
    with s._connect() as db:
        return dict(db.execute("SELECT * FROM pending_changes WHERE id=?", (pid,)).fetchone())


def test_approval_recording_failure_after_apply_stays_applied(tmp_path, home, monkeypatch):
    from mcpbrain import auto_changes
    s = _store(tmp_path)
    calls = []

    def fake_apply(store, home_, payload):
        calls.append(payload)
        return store.record_change("profile_updated", ref_id="p-dana",
                                   summary="Profile rewritten")
    monkeypatch.setitem(auto_changes.APPLY, "profiles", fake_apply)
    pid = auto_changes.propose(s, "profiles", "p-dana", "Rewrite Dana Okafor's profile", {"n": 1})

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(auto_changes, "_settle", boom)
    out = auto_changes.decide(s, home, pid, approve=True)
    assert out["ok"] is True
    row = _pending_row(s, pid)
    assert row["status"] == "applied"
    cid = s.recent_changes(1)[0]["id"]
    assert row["change_log_id"] == cid
    assert "disk full" in row["error"]
    again = auto_changes.decide(s, home, pid, approve=True)
    assert again == {"ok": False, "reason": auto_changes.ALREADY_DECIDED}
    assert len(calls) == 1
    # recover_stale only touches 'deciding' rows: the applied row stays put.
    auto_changes.recover_stale(s)
    assert _pending_row(s, pid)["status"] == "applied"


def test_approval_without_a_change_row_still_settles_applied_on_failure(tmp_path, home, monkeypatch):
    from mcpbrain import auto_changes
    s = _store(tmp_path)
    calls = []
    monkeypatch.setitem(auto_changes.APPLY, "circles",
                        lambda store, h, p: calls.append(1) or 41)
    pid = auto_changes.propose(s, "circles", "7", "Name circle 7 Acme volunteers", {})

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(auto_changes, "_settle", boom)
    assert auto_changes.decide(s, home, pid, approve=True)["ok"] is True
    row = _pending_row(s, pid)
    assert row["status"] == "applied" and "database is locked" in row["error"]
    assert auto_changes.decide(s, home, pid, approve=True)["reason"] == auto_changes.ALREADY_DECIDED
    assert calls == [1]


def test_approval_records_change_id_in_the_change_transaction(tmp_path, home, monkeypatch):
    from mcpbrain import auto_changes
    s = _store(tmp_path)
    seen = {}

    def fake_apply(store, home_, payload):
        cid = store.record_change("profile_updated", ref_id="p-dana", summary="Profile rewritten")
        seen["row"] = _pending_row(store, pid)    # before decide() settles anything
        return cid
    monkeypatch.setitem(auto_changes.APPLY, "profiles", fake_apply)
    pid = auto_changes.propose(s, "profiles", "p-dana", "Rewrite Dana Okafor's profile", {})
    assert auto_changes.decide(s, home, pid, approve=True) == {"ok": True, "reason": ""}
    assert seen["row"]["status"] == "applied"
    assert seen["row"]["change_log_id"] == s.recent_changes(1)[0]["id"]
    assert _pending_row(s, pid)["error"] == ""


# --- 7. maintenance history -----------------------------------------------------------------

def test_maintenance_route_returns_both_blocks(server):
    srv, s = server
    code, out = _req(srv, "GET", "/api/maintenance")
    assert code == 200
    assert set(out) == {"reflow", "ingestion_cleanup"}
    assert "owners_done" in out["reflow"] and "by_outcome" in out["reflow"]
    assert out["ingestion_cleanup"]["state"] == "pending"


@pytest.mark.skipif(not _node_ok(), reason="node not installed")
def test_maintenance_history_is_worded(tmp_path):
    page = (_WIZ / "settings.html").read_text(encoding="utf-8")
    o = _node(tmp_path, _STUBS + _blocks(page, "maint-words") + r"""
const day = (iso) => iso ? "3 Nov 2026" : "";
const done = {reflow: {owners_done: 1234, by_outcome: {carried: 1229, gave_up: 5}, queued: 0, remaining: 0,
                       halted: null, last_seed: {status: "idle", at: "2026-11-03T02:00:00Z"}},
              ingestion_cleanup: {state: "done", at: "2026-11-02T01:00:00Z",
                                  counts: {archive: {"actions:archived": 40}, cold: {"chunks_cold:bulk": 60}}}};
const fresh = {reflow: {owners_done: 0, by_outcome: {}, queued: 0, remaining: null, halted: null, last_seed: null},
               ingestion_cleanup: {state: "pending"}};
const running = {reflow: {owners_done: 10, by_outcome: {gave_up: 1}, queued: 4, remaining: 6, halted: null,
                          last_seed: {status: "seeded", at: "2026-11-03T02:00:00Z"}},
                 ingestion_cleanup: {state: "failed", at: "2026-11-02T01:00:00Z", error: "integrity_check: boom"}};
console.log(JSON.stringify([maintLines(done, day), maintLines(fresh, day), maintLines(running, day)]));""")
    done, fresh, running = o
    assert done[0]["text"] == "Finished 3 Nov 2026. 1,234 items handled, 5 given up on."
    assert done[1]["text"] == "Finished 3 Nov 2026. 100 items handled, none given up on."
    assert "hasn’t run yet" in fresh[0]["text"] and "hasn’t run yet" in fresh[1]["text"]
    assert running[0]["text"] == "Running. 10 items handled so far, 1 given up on, 10 left."
    assert "integrity_check" not in running[1]["text"] and "boom" not in running[1]["text"]
    assert "stopped" in running[1]["text"].lower()
    for lines in o:
        for line in lines:
            assert line["title"] and "_" not in line["text"]


# --- 8. tidy-up off closes its lint findings -----------------------------------------------------

class _Emb:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _review_daemon(tmp_path):
    from mcpbrain.daemon import Daemon, SingleWriterLock
    s = _store(tmp_path, "review.sqlite3")
    clock = {"t": 0.0}
    d = Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path / "rv.lock"),
               clock=lambda: clock["t"])
    d._review_interval_s = 100.0
    return s, d, clock


def test_tidy_off_review_resolves_lint_findings_and_on_lets_them_back(tmp_path, home):
    from mcpbrain import review
    s, d, clock = _review_daemon(tmp_path)
    config.write_config(home, {"auto_changes": {"tidy": "off"}})
    for t in review.TIDY_FINDING_TYPES:
        s.record_finding(t, "ref-" + t, summary="found " + t)
    s.record_finding("agent_stderr", "a.err", summary="Enrichment task failed")
    out = d._run_review()
    assert out["skipped"] == "off"
    open_types = {f["finding_type"] for f in s.open_findings()}
    assert open_types == {"agent_stderr"}
    with s._connect() as db:
        verdicts = {r[0] for r in db.execute(
            "SELECT verdict FROM proactive_findings WHERE finding_type LIKE 'lint:%'")}
    assert verdicts == {"tidy_off"}
    # Re-detected while still off: stays closed.
    s.record_finding("lint:orphan_entity", "ref-lint:orphan_entity", summary="found again")
    assert not s.open_findings("lint:orphan_entity")
    # Tidy-up back on: the next pass lets the lint pass record them again.
    config.write_config(home, {"auto_changes": {"tidy": "apply"}})
    clock["t"] += 1000
    d._run_review()
    s.record_finding("lint:orphan_entity", "ref-lint:orphan_entity", summary="found again")
    assert len(s.open_findings("lint:orphan_entity")) == 1


# --- 9. one restart at a time ------------------------------------------------------------------------

def test_second_restart_within_one_restart_reports_already(server, monkeypatch):
    from mcpbrain import system_ops
    srv, _s = server
    calls = []
    monkeypatch.setattr(system_ops, "request_restart", lambda d, **kw: calls.append(kw))
    try:
        assert _req(srv, "POST", "/api/restart") == (202, {"restarting": True})
        assert _req(srv, "POST", "/api/restart") == (202, {"restarting": True, "already": True})
        assert len(calls) == 1
    finally:
        system_ops.release_restart()
    assert _req(srv, "POST", "/api/restart") == (202, {"restarting": True})
    system_ops.release_restart()


def test_request_restart_releases_the_claim_when_done():
    from mcpbrain import system_ops
    assert system_ops.claim_restart() is True
    assert system_ops.claim_restart() is False
    d = FakeDaemon()
    system_ops.request_restart(d, platform="darwin", restart=lambda p: None).join(5)
    assert system_ops.claim_restart() is True
    system_ops.release_restart()


class _DownClient:
    def status(self):
        from mcpbrain.control_client import DaemonUnavailable
        raise DaemonUnavailable("no daemon")


class _UpClient:
    def status(self):
        return {"paused": False, "chunk_count": 3}


def test_tray_start_is_ignored_while_one_is_in_flight():
    from mcpbrain.tray import TrayController
    now = {"t": 1000.0}
    started = []
    c = TrayController(_DownClient(), start_daemon=lambda: started.append(1),
                       clock=lambda: now["t"])
    c.refresh()

    def start_item():
        return [i for i in c.menu_items() if i[0] == "Start mcpbrain"][0]
    assert start_item()[2] is True
    start_item()[1]()
    time.sleep(0.2)
    assert started == [1]
    assert start_item()[2] is False
    c.on_start_daemon()
    time.sleep(0.2)
    assert started == [1]
    now["t"] += 61
    assert start_item()[2] is True
    # The daemon answering clears it too.
    start_item()[1]()
    time.sleep(0.2)
    assert start_item()[2] is False
    c._client = _UpClient()
    c.refresh()
    c._client = _DownClient()
    c.refresh()
    assert start_item()[2] is True


# --- 10. page assets ---------------------------------------------------------------------------------------

def test_settings_has_maintenance_history_under_advanced():
    page = (_WIZ / "settings.html").read_text(encoding="utf-8")
    adv = page[page.index('id="advanced"'):page.index("</section>", page.index('id="advanced"'))]
    assert "Maintenance history" in adv
    assert 'id="maint-body"' in adv
    assert '"/api/maintenance"' in page


def test_failed_restore_clears_the_earlier_success_line():
    page = (_WIZ / "settings.html").read_text(encoding="utf-8")
    m = re.search(r'text: "Restore".*?onclick: \(ev\) => \{(.*?)\}\}\)', page, flags=re.S)
    assert m, "Restore handler not found"
    handler = m.group(1)
    clear = handler.index('said($("hidden-said"), "")')
    assert clear < handler.index("/restore")


def test_grouped_undone_rows_are_struck_through():
    css = (_WIZ / "console.css").read_text(encoding="utf-8")
    rule = re.search(r"\.items li\.is-undone \.name\s*\{([^}]*)\}", css)
    assert rule and "line-through" in rule.group(1)
