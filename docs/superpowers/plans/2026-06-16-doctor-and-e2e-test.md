# Doctor + End-to-End Test Implementation Plan

> **Session summary (2026-06-16):** Implemented in full on branch `worktree-doctor-e2e` via subagent-driven-development. PR #2 open at Centrepoint-Church/mcpbrain. All tasks complete: 13 commits, 1235 tests passing (1234 + maintenance/__init__.py fix), 4 e2e tests in 1.25s, ruff clean. Key decisions: injectable reprobe/agent_installed seams to avoid OS side effects in tests; enrichment double-count fixed via `enr_already_counted` guard; FTS populated via zero-vector `write_embedding` calls (no real ML model); `_run_full_loop` helper deduplicated the pipeline in e2e tests. Merge note: 2-line conflict with Spec 1 (fleet-report) in `mcpbrain/cli.py` — keep both entries on their own lines.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `mcpbrain doctor` (a pure-Python diagnose-and-auto-fix entrypoint that reuses the existing probes and agent-repair calls) plus a CI-runnable end-to-end test that drives the real sync→prepare→drain→graph+dashboard loop with only Google and the Cowork extractor stubbed.

**Architecture:** `doctor.run_doctor(home, *, repairs=…)` calls `probes.all_connections()`, classifies each result against a static disposition table into auto-fixable / guided / ok, attempts the injected idempotent repairs (daemon restart, agent re-register, records bootstrap), re-probes the ones it touched, and formats a plain-language report with an exit code. Repairs are injected so tests substitute fakes with no real launchd/git side effects. The e2e harness feeds a `FakeGoogleService` (shaped like a `googleapiclient` resource) through the real `backfill_*` sync functions into a temp `Store`, runs the real `prepare`, hand-writes a contract-valid `enrich_inbox` file (standing in for the Claude extractor), runs the real `drain` against the real `graph_write.apply`, then asserts the graph grew, the dashboard surfaces the seeded action, and FTS finds a known chunk.

**Tech Stack:** Python 3.12, pytest, ruff, GitHub Actions. Tests: `uv run pytest`; lint: `uv run ruff check mcpbrain/`.

**Worktree & Dependencies:** This worktree owns `mcpbrain/doctor.py` (new), `tests/test_doctor.py` (new), `tests/e2e/*` (new: `__init__.py`, `conftest.py`, `fixtures/`, `test_full_loop.py`), and edits to `.github/workflows/ci.yml`. It SHARES `mcpbrain/cli.py` with Spec 1 — both add one subcommand to the same registration tuple and the same dispatch dict (Spec 1 adds `fleet-report`, this adds `doctor`). Expect a ~2-line merge conflict, resolved by whoever merges second; there is no logic dependency. `doctor` reuses code that already exists in 0.0.6 (`probes.all_connections`, `agents.restart_agent` / `agents.install_agent`, `records.ensure_records_repo`) and depends on NO other spec's new code. It PROVIDES the `mcpbrain doctor` subcommand that Spec 2's remedy strings reference (one-way, text-only — Spec 2 imports nothing here, so there is no reciprocal dependency and the two can merge in any order). Create an isolated worktree via superpowers:using-git-worktrees at execution.

---

## Verified codebase facts (re-confirm with Read before editing)

- `mcpbrain/cli.py`: subcommands live in a tuple inside `main(argv)` (the `for name in (...)` block) and are dispatched through a dict literal `{name: lambda…}[ns.cmd]()`. Helpers like `_monitor_main()` import lazily. `rest` holds the post-subcommand argv from `p.parse_known_args`.
- `mcpbrain/probes.py`: `all_connections(home, store=None) -> dict` keyed `google`, `claude`, `clickup`, `backup`, `records`, `enrichment`; each value is `{"state": "ok"|"needs_action"|"not_started", "detail": str, "last_verified": …}`.
- `mcpbrain/monitor.py`: `run_monitor(home) -> tuple[int, str]`; `main()` prints the message and `sys.exit(code)`. Model `run_doctor` / doctor `main` on this.
- `mcpbrain/agents.py`: `restart_agent(platform)`, `install_agent(platform, *, mcpbrain_bin, home)`, `uninstall_agent(platform)`. `install_agent`/`restart_agent` raise on unsupported platforms and on loader failure. The OS-touching bodies are `# pragma: no cover` — so tests MUST inject fakes, never call the real ones.
- Records bootstrap: `mcpbrain/records.py::ensure_records_repo(repo_dir, *, git_name="mcpbrain", git_email="mcpbrain@localhost", profile=None) -> str`. Idempotent; git-inits + scaffolds. `config.records_dir(home)` resolves the repo path. `probe_records` reports `not_started` when `<records_dir>/.git` is absent.
- `mcpbrain/setup.py`: `_platform()` maps `sys.platform` → `"linux"|"darwin"|"win32"`; `_mcpbrain_bin()` returns `shutil.which("mcpbrain") or sys.argv[0] or "mcpbrain"`. doctor mirrors these for the agent repairs.
- Sync (e2e): `sync.gmail.backfill_gmail(service, store, after, before=None, max_messages=None)`, `sync.calendar.backfill_calendar_window(service, store, *, time_min, time_max, calendar_id="primary", max_events=None)`, `sync.drive.backfill_drive(service, store, modified_after, modified_before=None, max_files=None)`. Backfills take NO cursor side effects — the cleanest e2e entry. They call `service.users().messages().list/get(...).execute()`, `service.events().list(...).execute()`, `service.files().list(...).execute()` + `export`/`get_media`.
- `normalise_gmail` drops messages whose headers carry `list-unsubscribe`/`list-id`/bulk `precedence`/`auto-submitted` — fixtures must NOT carry those headers. Body must be > 0 chars after reply-chain + signature stripping.
- `mcpbrain/prepare.py::prepare(store, *, thread_cap, char_budget, resolution_due, now=None, …) -> dict`; writes `<home>/enrich_queue/pending.json` via `config.app_dir()` (reads `MCPBRAIN_HOME`); returns `{"batch_id", "threads", "merge_pairs"}`. Skips noise threads (lead message sender/subject/body). `_build_context` reads `config.app_dir()` and the org taxonomy, so the e2e test sets `MCPBRAIN_HOME` and minimal config.
- `mcpbrain/drain.py::drain(store, *, home=None, apply=None, embedder=None) -> dict`; reads `<home>/enrich_inbox/*.json`, validates against `contract`, applies via the injected `apply`, marks chunks, deletes the file and matching `pending.json` on full success. Quarantines malformed files to `enrich_inbox/bad/`.
- `mcpbrain/graph_write.py::apply(store, extraction, *, doc_ids, identity=None, clock=None, embedder=None, owner=None) -> dict`; writes entities, relations, role observations, topics, the `email_context` row, the action lifecycle (`_write_actions` — so an extraction carrying `actions` DOES create an `actions` row), and a semantic doc `enriched-<thread_id>`.
- `mcpbrain/contract.py`: `validate_batch_file(d)` validates wrapper + every extraction. An extraction needs `thread_id` (non-empty str), `org` (non-empty str), `content_type` in `chunking._VALID_CONTENT_TYPES`, `summary` (str), non-empty `messages` (each with non-empty `message_id` + `date`; `sender` optional), and `entities`/`actions`/`relations`/`topics` lists; each action needs a non-empty `description`; each relation needs `source_name`/`type`/`target_name`. `drain._regroup_parts` consumes `extractions`; chunks are matched by `message_id` via `store.doc_ids_for_messages`, so the extraction `messages[].message_id` MUST equal the synced chunk metadata `message_id` (which for Gmail = the raw message `id`).
- `mcpbrain/store.py`: `Store(path, dim, read_only=False)`; `store.init()` creates the schema. Tests use `Store(tmp_path/"brain.db", dim=4); s.init()`. Assertion methods: `list_entities()`, `list_relations()`, `entities_for_resolution()`, `get_chunk(doc_id)`, `fts_search(query, limit)` (returns `[(doc_id, score), …]`), `unified_actions(status=…)`.
- `mcpbrain/dashboard.py::assemble(store, home) -> dict`; `actions_today` reads open `actions` rows owned by the owner or with empty owner. `assemble` degrades each source on error, so a green path needs the home configured enough that `actions_today` does not raise. Returns `{"actions": {...}, "calendar", "clickup", "inbox", "circles", "changes", "findings", "as_of"}`.
- `.github/workflows/ci.yml`: two jobs — `smoke` (uv tool install + daemon/mcp smoke) and `tests` (venv `pip install -e ".[dev]"` then `.venv/bin/pytest`). The e2e test runs inside the existing `tests` job by default (it is a normal pytest file). Add a `-m e2e` marker registration so it can ALSO be selected/excluded, and a tiny dedicated CI step that runs `pytest -m e2e` to make the e2e gate visible in the build log.

---

## Part A — `mcpbrain doctor`

### Task A1 — failing test: doctor module + run_doctor exists, all-ok path

- [ ] Create `tests/test_doctor.py` with the all-healthy case. This pins the signature `run_doctor(home, *, conns=None, repairs=None, platform=None, mcpbrain_bin=None) -> tuple[int, str]` and that no repair runs when everything is ok.

```python
"""Tests for mcpbrain.doctor — injected probes + injected repairs, no OS side effects.

doctor reuses probes.all_connections and a repair layer. Every test injects a
fake `conns` dict (the probe output shape) and fake `repairs` callables, so no
real launchd/git/agent side effects occur. The disposition table lives in
doctor; these tests assert the behaviour it drives.
"""

from mcpbrain import doctor


def _conns(**states):
    """Build an all-ok probe dict, overriding individual keys.

    Shape mirrors probes.all_connections: name -> {state, detail, last_verified}.
    Pass e.g. claude="needs_action" to flip one probe.
    """
    base = {k: {"state": "ok", "detail": "Connected", "last_verified": None}
            for k in ("google", "claude", "clickup", "backup", "records", "enrichment")}
    for name, state in states.items():
        base[name] = {"state": state, "detail": state, "last_verified": None}
    return base


class _Recorder:
    """A repair callable that records it was called and returns a fixed result."""

    def __init__(self, ok=True):
        self.calls = 0
        self.ok = ok

    def __call__(self, *a, **k):
        self.calls += 1
        if not self.ok:
            raise RuntimeError("repair blew up")


def test_all_ok_exit_zero_no_repairs():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    code, msg = doctor.run_doctor("/tmp/home", conns=_conns(), repairs=repairs)
    assert code == 0
    assert all(r.calls == 0 for r in repairs.values())
    assert "mcpbrain doctor" in msg
```

- [ ] Run: `uv run pytest tests/test_doctor.py -q` → **FAIL** (no `mcpbrain/doctor.py`).

### Task A2 — minimal impl: doctor.py with disposition table + run_doctor skeleton

- [ ] Create `mcpbrain/doctor.py`. Implement the disposition table and a `run_doctor` that probes (or accepts injected `conns`), classifies, runs auto repairs through an injected `repairs` dispatch, re-probes touched keys, formats, and returns `(exit_code, message)`. Keep the repair calls injected with a default dispatch built from `agents.*` + `records.ensure_records_repo`.

```python
"""mcpbrain doctor — diagnose every health dimension and auto-fix the local,
idempotent failures, pointing at the exact next step for anything only
Claude/Cowork/the user can fix.

Reuses probes.all_connections (so CLI, wizard, monitor and doctor never
disagree) and adds a repair layer. Each probe maps to one of three
dispositions:

  auto    — a local idempotent fix exists: attempt it, re-probe, report fixed/❌
  guided  — only Claude/Cowork/the user can fix it: print the exact remedy
  ok/—    — healthy or deliberately unconfigured: report, do nothing

The repair calls are INJECTED (default dispatch wraps agents.* and the
records bootstrap) so the logic is unit-testable with stubs — no real
launchd/git/agent side effects in tests.

Scheduled-task health is INFERRED from probe_enrichment: the daemon cannot read
the Cowork app DB, so doctor cannot verify the four scheduled tasks directly.
It states this honestly. Recreating tasks is therefore always a guided step
(/mcpbrain-fix), never auto.
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime, timezone

# Probe key -> disposition. "auto" keys carry the repair-dispatch key to call;
# "guided" keys carry the remedy string to print. Keys absent here are reported
# verbatim with no action.
#
# Note: probe keys are google/claude/clickup/backup/records/enrichment. The
# report adds a synthetic "scheduled_tasks" line inferred from enrichment.
_DISPOSITIONS: dict[str, dict] = {
    "claude":     {"kind": "auto", "repair": "daemon",
                   "label": "Daemon",
                   "guided": "Install the mcpbrain plugin and run /reload-plugins"},
    "records":    {"kind": "auto", "repair": "records", "label": "Records"},
    "google":     {"kind": "guided", "label": "Google",
                   "guided": "Run: mcpbrain auth"},
    "clickup":    {"kind": "guided", "label": "ClickUp",
                   "guided": "Re-enter your ClickUp key in the mcpbrain wizard"},
    "enrichment": {"kind": "guided", "label": "Enrichment",
                   "guided": "Open Claude or run /mcpbrain-fix in Cowork"},
    "backup":     {"kind": "guided", "label": "Backup",
                   "guided": "Re-run a backup from the mcpbrain wizard"},
}

# States that mean "needs attention". not_started is deliberately healthy for
# the optional connections (clickup/backup/enrichment): an unconfigured feature
# is not a fault. claude not_started (plugin never connected) and records
# not_started (repo never created) ARE actionable, so they are handled per-key.
_FAIL_STATES = {"needs_action"}


def _platform() -> str:
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "darwin"
    if p in ("win32", "cygwin"):
        return "win32"
    return p


def _mcpbrain_bin() -> str:
    return shutil.which("mcpbrain") or sys.argv[0] or "mcpbrain"


def _default_repairs(home: str, platform: str, mcpbrain_bin: str) -> dict:
    """The real repair dispatch: idempotent local fixes only.

    daemon  — restart the OS login agent (regenerates control_port/token).
    agent   — (re-)register the login agent when it is missing entirely.
    records — git-init + scaffold the records repo from templates.

    Each wraps a real side-effecting call; injected fakes replace them in tests.
    """
    from mcpbrain import agents, config, records

    def _repair_daemon():
        agents.restart_agent(platform)

    def _repair_agent():
        agents.install_agent(platform, mcpbrain_bin=mcpbrain_bin, home=home)

    def _repair_records():
        records.ensure_records_repo(
            config.records_dir(home),
            git_name=config.owner_full_name(home) or "mcpbrain",
            git_email=config.owner_email(home) or "mcpbrain@localhost",
        )

    return {"daemon": _repair_daemon, "agent": _repair_agent, "records": _repair_records}


def _is_problem(key: str, state: str) -> bool:
    """True when this probe state is an actionable problem for doctor."""
    if key in ("claude", "records"):
        # plugin never connected / repo never created are actionable.
        return state in _FAIL_STATES or state == "not_started"
    return state in _FAIL_STATES


def run_doctor(home, *, conns=None, repairs=None, platform=None,
               mcpbrain_bin=None) -> tuple[int, str]:
    """Diagnose, auto-fix the idempotent local failures, report, return (code, msg).

    Pure-ish: probes and repairs are injectable. With nothing injected it reads
    the live probes and builds the real repair dispatch. Exit code is 0 when
    nothing needs user action after auto-fix, else 1.
    """
    from mcpbrain import probes

    platform = platform or _platform()
    mcpbrain_bin = mcpbrain_bin or _mcpbrain_bin()
    if conns is None:
        conns = probes.all_connections(home)
    if repairs is None:
        repairs = _default_repairs(str(home), platform, mcpbrain_bin)

    lines: list[str] = []
    fixed = 0
    need_action = 0

    for key, disp in _DISPOSITIONS.items():
        probe = conns.get(key, {"state": "not_started", "detail": ""})
        state = probe.get("state", "not_started")
        label = disp["label"]

        if not _is_problem(key, state):
            lines.append(f"✅ {label:<16} {probe.get('detail') or 'OK'}")
            continue

        if disp["kind"] == "auto" and state in _FAIL_STATES:
            # plugin-not-connected (claude not_started) is NOT auto-fixable —
            # only a needs_action (daemon down but agent installed) is.
            repair = repairs.get(disp["repair"])
            try:
                repair()
                conns[key] = probes.all_connections(home).get(key, probe) \
                    if conns is None else _reprobe(home, key, probe)
                new_state = conns[key].get("state", state)
            except Exception as exc:  # noqa: BLE001 — a failed repair is reported, never fatal
                lines.append(f"❌ {label:<16} {probe.get('detail')} → repair failed: {exc}")
                need_action += 1
                continue
            if not _is_problem(key, new_state):
                lines.append(f"❌ {label:<16} {probe.get('detail')} → restarting... ✅ fixed")
                fixed += 1
            else:
                lines.append(f"❌ {label:<16} {probe.get('detail')} → repair did not fix it; "
                             f"run {disp.get('guided', 'mcpbrain setup')}")
                need_action += 1
            continue

        if key == "records" and state == "not_started":
            # records repo never created — auto-create it.
            repair = repairs.get("records")
            try:
                repair()
                new = _reprobe(home, "records", probe)
                if not _is_problem("records", new.get("state", state)):
                    lines.append(f"❌ {label:<16} not created → creating... ✅ fixed")
                    fixed += 1
                else:
                    lines.append(f"❌ {label:<16} could not create records repo")
                    need_action += 1
            except Exception as exc:  # noqa: BLE001
                lines.append(f"❌ {label:<16} records repo create failed: {exc}")
                need_action += 1
            continue

        # guided (incl. claude not_started = plugin not connected)
        remedy = disp.get("guided", "see the mcpbrain wizard")
        lines.append(f"⚠️  {label:<16} {probe.get('detail')} → {remedy}")
        need_action += 1

    # Scheduled tasks: inferred from enrichment, never auto. Stated honestly.
    enr = conns.get("enrichment", {}).get("state", "not_started")
    if enr == "ok":
        lines.append("✅ Scheduled tasks  enrichment fresh ⇒ enrich task firing")
    else:
        lines.append("⚠️  Scheduled tasks  not directly checkable → "
                     "run /mcpbrain-fix in Cowork to recreate the enrich/gardener/"
                     "meeting-packs/reference-gardener tasks")
        need_action += 1

    header = (f"mcpbrain doctor — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC   "
              f"(home: {home})")
    summary = f"{fixed} fixed automatically, {need_action} need your action (see ↑)."
    message = "\n".join([header, "", *lines, "", summary])
    return (1 if need_action else 0), message


def _reprobe(home, key: str, fallback: dict) -> dict:
    """Re-run the live probes and return this key's fresh result."""
    from mcpbrain import probes
    return probes.all_connections(home).get(key, fallback)


def run_doctor_main(argv=None) -> None:
    from mcpbrain import config
    code, msg = run_doctor(str(config.app_dir()))
    print(msg)
    sys.exit(code)
```

> NOTE during impl: the `conns[key] = … if conns is None else …` line in the auto branch is awkward — simplify to always call `_reprobe(home, key, probe)` after a successful repair (the injected-conns tests must therefore also inject the re-probe via `repairs`, OR the re-probe must be injectable). See Task A3: re-probe is what the tests exercise, so make re-probe an injectable seam (`reprobe=` param defaulting to `_reprobe`) and pass a fake in tests. Adjust the signature to `run_doctor(home, *, conns=None, repairs=None, reprobe=None, platform=None, mcpbrain_bin=None)` and replace `_reprobe(...)` calls with `reprobe(home, key, probe)`. Do this in A3 where the re-probe behaviour is first tested.

- [ ] Run: `uv run pytest tests/test_doctor.py -q` → **PASS** (all-ok path; A1 injects no failing probe so no repair/reprobe runs).
- [ ] Commit: `feat(doctor): run_doctor skeleton with disposition table + all-ok path`.

### Task A3 — failing test: daemon down + agent installed → repair called, re-probe ✅, reported fixed

- [ ] Add to `tests/test_doctor.py`. This drives the auto branch AND the re-probe seam, so introduce the `reprobe=` injectable here.

```python
def test_daemon_down_repair_called_reprobe_fixed():
    daemon = _Recorder()
    repairs = {"daemon": daemon, "agent": _Recorder(), "records": _Recorder()}
    # First probe: claude needs_action (daemon down). After repair, reprobe ok.
    conns = _conns(claude="needs_action")
    reprobed = {"claude": {"state": "ok", "detail": "Connected", "last_verified": None}}

    def fake_reprobe(home, key, fallback):
        return reprobed.get(key, fallback)

    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=fake_reprobe)
    assert daemon.calls == 1
    assert "fixed" in msg
    # daemon was the only problem and it fixed → exit 0 IF nothing else needs action.
    # scheduled-tasks line keys off enrichment (ok here) so it does not add need_action.
    assert code == 0
```

- [ ] Run: `uv run pytest tests/test_doctor.py -q` → **FAIL** (no `reprobe` param).

### Task A4 — impl: add the `reprobe` seam and wire the auto/records branches through it

- [ ] Edit `mcpbrain/doctor.py`: add `reprobe=None` to `run_doctor`, default `reprobe = reprobe or _reprobe`, and replace every `_reprobe(home, key, probe)` / inline re-probe with `reprobe(home, key, probe)`. Remove the awkward `if conns is None` ternary in the auto branch (always `reprobe(...)`).
- [ ] Run: `uv run pytest tests/test_doctor.py -q` → **PASS**.
- [ ] Commit: `feat(doctor): injectable reprobe seam; daemon-down auto-fix path`.

### Task A5 — failing test: agent missing → install_agent repair called

- [ ] The `claude` probe returns `not_started` when the heartbeat file is absent — but per the disposition table, claude `not_started` is "plugin not connected" (guided), while a missing OS agent is a separate signal. Per the spec the auto repair for "launchd/schtasks agent missing" is `install_agent`. Model this: when claude is `needs_action` (daemon known-down) the `daemon` repair (restart) runs; when there is no control_port/token AND the agent is absent, the `agent` repair (install) runs. Because doctor only has probe states to work from, treat the **agent-missing** signal as a `not_started` claude with no heartbeat — but the cleanest unit-testable contract is: doctor exposes the agent-install repair via the disposition for a dedicated synthetic check. To keep this honest and test it, add an explicit `_AGENT_PROBE` helper `_agent_installed(home, platform)` (checks the launchd plist / schtasks presence) injected as `agent_installed=`. Write the test:

```python
def test_agent_missing_install_called():
    agent = _Recorder()
    repairs = {"daemon": _Recorder(), "agent": agent, "records": _Recorder()}
    # claude needs_action AND the OS agent is reported missing → install, not restart.
    conns = _conns(claude="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: {"state": "ok", "detail": "ok",
                                                           "last_verified": None},
                                  agent_installed=lambda h, p: False)
    assert agent.calls == 1
    assert repairs["daemon"].calls == 0
    assert "fixed" in msg
```

- [ ] Run → **FAIL** (no `agent_installed` param).

### Task A6 — impl: agent-installed seam selects install vs restart

- [ ] Edit `run_doctor`: add `agent_installed=None` param; default `agent_installed = agent_installed or _agent_installed`. In the `claude` auto branch, when the state is a problem, call the **agent** repair if `not agent_installed(home, platform)`, else the **daemon** repair. Add `_agent_installed(home, platform)` checking the canonical agent path (reuse `agents._LAUNCHD_PATH` on darwin / a schtasks query stub on win32; on `linux`/unknown return `True` so CI's Linux box treats the agent as "present" and falls through to a daemon restart attempt — which the test stubs anyway).

```python
def _agent_installed(home, platform) -> bool:
    """True when the OS login agent is registered. Best-effort; defaults True
    on platforms without a cheap check so doctor prefers a restart over a
    redundant install."""
    if platform == "darwin":
        from mcpbrain import agents
        return agents._LAUNCHD_PATH.exists()
    return True
```

- [ ] Run → **PASS**.
- [ ] Commit: `feat(doctor): choose install vs restart via agent-installed seam`.

### Task A7 — failing test: records missing → records bootstrap called

```python
def test_records_missing_bootstrap_called():
    records = _Recorder()
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": records}
    conns = _conns(records="not_started")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: {"state": "ok", "detail": "Ready",
                                                           "last_verified": None})
    assert records.calls == 1
    assert "fixed" in msg
    assert code == 0
```

- [ ] Run → **FAIL or PASS** depending on A2/A4 records branch. If the `records not_started` branch already calls the repair and re-probes ✅, this passes; otherwise fix the branch to use `reprobe`. Run, observe, and make it green.
- [ ] Commit if a change was needed: `feat(doctor): auto-create missing records repo`.

### Task A8 — failing test: google expired → no repair, guided remedy, exit 1

```python
def test_google_expired_guided_no_repair_exit1():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    conns = _conns(google="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f)
    assert all(r.calls == 0 for r in repairs.values())
    assert "mcpbrain auth" in msg
    assert code == 1
```

- [ ] Run → should **PASS** with the A2 guided branch (google is `kind: guided`). If the formatting differs, adjust the remedy string. Confirm green.

### Task A9 — failing test: a repair that fails → reported ❌, exit 1

```python
def test_repair_failure_reported_exit1():
    repairs = {"daemon": _Recorder(ok=False), "agent": _Recorder(),
               "records": _Recorder()}
    conns = _conns(claude="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f,
                                  agent_installed=lambda h, p: True)  # → daemon repair
    assert "repair failed" in msg
    assert code == 1
```

- [ ] Run → **PASS** (the auto branch wraps the repair in try/except). Confirm green.

### Task A10 — failing test: scheduled-tasks inferred from enrichment (honest, never auto)

```python
def test_scheduled_tasks_inferred_from_enrichment():
    repairs = {"daemon": _Recorder(), "agent": _Recorder(), "records": _Recorder()}
    # enrichment idle → scheduled-tasks line is guided, never auto-fixed.
    conns = _conns(enrichment="needs_action")
    code, msg = doctor.run_doctor("/tmp/home", conns=conns, repairs=repairs,
                                  reprobe=lambda h, k, f: f)
    assert "Scheduled tasks" in msg
    assert "/mcpbrain-fix" in msg
    assert all(r.calls == 0 for r in repairs.values())
    assert code == 1
```

- [ ] Run → **PASS** (enrichment is guided; the synthetic scheduled-tasks line keys off it). Confirm green.
- [ ] Commit: `test(doctor): cover google/records/repair-failure/scheduled-tasks cases`.

### Task A11 — failing test: non-empty output guard

```python
def test_output_is_never_empty():
    code, msg = doctor.run_doctor("/tmp/home", conns=_conns(), repairs={})
    assert msg.strip(), "doctor must always print a report, even on the all-ok path"
    assert "mcpbrain doctor" in msg
```

- [ ] Run → **PASS** (header is always present). If `repairs={}` causes a KeyError on an ok path (it should not — no repair runs when nothing is a problem), guard `repairs.get(...)` returns and the auto branch handles `None` by reporting a guided remedy. Confirm green.

### Task A12 — wire `mcpbrain doctor` into the CLI

- [ ] failing test: add `tests/test_doctor.py::test_cli_dispatches_doctor` that imports `mcpbrain.cli` and asserts `"doctor"` is in the subcommand tuple and dispatch dict. Simplest: monkeypatch `mcpbrain.doctor.run_doctor_main` and call `cli.main(["doctor"])`, asserting it was invoked with the residual argv.

```python
def test_cli_dispatches_doctor(monkeypatch):
    import mcpbrain.cli as cli
    called = {}
    monkeypatch.setattr("mcpbrain.doctor.run_doctor_main",
                        lambda rest: called.setdefault("rest", rest))
    cli.main(["doctor", "--whatever"])
    assert "rest" in called
    assert called["rest"] == ["--whatever"]
```

- [ ] Run → **FAIL** (`doctor` not registered).
- [ ] Edit `mcpbrain/cli.py`:
  - add `"doctor"` to the `for name in (...)` tuple (append after `"restore"`).
  - add to the dispatch dict: `"doctor": lambda: __import__("mcpbrain.doctor", fromlist=["run_doctor_main"]).run_doctor_main(rest),`
  - confirm `run_doctor_main` accepts `argv=None`/`rest` — adjust its signature to `def run_doctor_main(argv=None)` and ignore extra args (doctor takes none today).

> Merge note: this is the exact 2-line site Spec 1 also edits (it appends `"fleet-report"`). Keep the additions on their own lines so the conflict is a trivial re-stack.

- [ ] Run → **PASS**.
- [ ] Lint: `uv run ruff check mcpbrain/doctor.py mcpbrain/cli.py` → clean.
- [ ] Commit: `feat(cli): add 'doctor' subcommand → doctor.run_doctor_main`.

### Task A13 — self-review pass on doctor

- [ ] Re-read `mcpbrain/doctor.py` against the spec's output-shape and exit-code requirements: `✅`/`❌`/`⚠️` glyphs present; header with date + home; auto branch attempts→re-probes→reports fixed/❌; guided branch prints the exact remedy; exit 0 only when `need_action == 0`. Run the full doctor suite: `uv run pytest tests/test_doctor.py -q` → all green.
- [ ] Commit any cleanups: `refactor(doctor): tidy report formatting`.

---

## Part B — Automated end-to-end test

### Task B1 — scaffold the e2e package + pytest marker registration

- [ ] Create `tests/e2e/__init__.py` (empty).
- [ ] Register the `e2e` marker so `pytest -m e2e` works and there are no unknown-marker warnings. `pyproject.toml` already has `[tool.pytest.ini_options]` with `markers = ["slow: long-running integration tests (real subprocess + model load)"]` (verified). Append the e2e marker to that list: `markers = ["slow: …", "e2e: full sync→prepare→drain→graph loop (CI gate)"]`. (This is a small shared edit — verify it does not collide with another spec; if `pyproject.toml` is contested, fall back to a `tests/e2e/conftest.py` `pytest_configure` that does `config.addinivalue_line("markers", "e2e: …")`.)
- [ ] Run: `uv run pytest -m e2e -q` → collects 0 tests, exit 0 (no error). Confirms the marker is registered.
- [ ] Commit: `test(e2e): scaffold e2e package + register e2e marker`.

### Task B2 — failing test: FakeGoogleService + Gmail fixture flows through real backfill into the store

- [ ] Create `tests/e2e/conftest.py` with a `FakeGoogleService` shaped like a `googleapiclient` resource (mirror the `_Req`/`.execute()` builder pattern from `tests/test_gmail_sync.py`), driven by JSON fixtures under `tests/e2e/fixtures/`. The service must answer:
  - `service.users().messages().list(...)` → `{"messages": [{"id": ...}, ...]}`
  - `service.users().messages().get(userId, id, format)` → the full message dict
  - `service.events().list(...)` → `{"items": [event, ...]}`
  - `service.files().list(...)` → `{"files": [meta, ...]}` and `service.files().export(...)`/`get_media(...)` → bytes
- [ ] Create `tests/e2e/fixtures/gmail_threads.json` — 2–3 threads (each a list of `messages.get` full-message dicts). Headers MUST avoid bulk markers (`List-Unsubscribe`, `List-Id`, `Precedence: bulk`, `Auto-Submitted`). Bodies are plain text > 10 chars, no signature/reply-chain triggers. Use deterministic `id`, `threadId`, `From`, `Date`, `Subject`. The `From` domain should map to a configured org (see B5 config) so `apply` links a real org.
- [ ] Create `tests/e2e/fixtures/calendar_event.json` (1 event, `id`/`summary`/`start`/`end`/`status: confirmed`) and `tests/e2e/fixtures/drive_doc.json` (1 file meta `id`/`name`/`mimeType: text/plain`/`modifiedTime`) + its body text.
- [ ] Write the first e2e test in `tests/e2e/test_full_loop.py` covering only sync → store:

```python
"""End-to-end loop: FakeGoogleService -> real sync -> real prepare ->
hand-written enrich_inbox (stubbed Cowork extractor) -> real drain ->
real graph_write.apply -> graph + dashboard.

Only two things are faked: Google's API (FakeGoogleService) and the Claude
enrichment step (a hand-authored, contract-valid enrich_inbox file). Everything
between is the real code path. The non-empty guards make a no-op pipeline fail
loudly.
"""
import pytest

from mcpbrain.store import Store
from mcpbrain.sync.gmail import backfill_gmail

pytestmark = pytest.mark.e2e


def test_gmail_backfill_lands_chunks(e2e_store, fake_google):
    n = backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    assert n >= 2, "fixture threads should produce chunks"
    # known chunk is searchable by keyword (FTS, no embedder needed).
    # fts_search(query, k) — k is positional, not a keyword arg.
    hits = e2e_store.fts_search("Hall B", 5)
    assert hits, "a known fixture phrase must be findable"
```

- [ ] Add `e2e_store` and `fake_google` fixtures to `tests/e2e/conftest.py` (Store on tmp_path dim=4 + `.init()`; `FakeGoogleService` loaded from the fixture JSON). Make a known phrase (e.g. "Hall B") appear in a Gmail fixture body.
- [ ] Run: `uv run pytest tests/e2e/test_full_loop.py -q` → **FAIL** (conftest/fixtures incomplete).

### Task B3 — impl: complete FakeGoogleService + fixtures so B2 passes

- [ ] Flesh out `FakeGoogleService` and the fixtures until B2 is green. Keep the service strict (raise `KeyError`/`AssertionError` on an unexpected call) so a sync-code change that calls a new method fails loudly rather than silently returning empty.
- [ ] Run → **PASS**.
- [ ] Commit: `test(e2e): FakeGoogleService + fixtures; gmail backfill lands chunks`.

### Task B4 — failing test: real prepare writes pending.json with the expected threads

- [ ] Add to `test_full_loop.py`:

```python
import json
from mcpbrain import prepare


def test_prepare_spools_pending(e2e_store, fake_google, e2e_home):
    backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    summary = prepare.prepare(e2e_store, thread_cap=20, char_budget=24000,
                              resolution_due=False)
    assert summary["threads"] >= 2, "non-noise fixture threads must spool"
    pending = json.loads((e2e_home / "enrich_queue" / "pending.json").read_text())
    tids = {t["thread_id"] for t in pending["threads"]}
    assert tids, "pending.json must list the synced threads"
```

- [ ] `e2e_home` fixture: a tmp dir set as `MCPBRAIN_HOME` (monkeypatch) so `config.app_dir()` resolves there; write a minimal `config.json` (owner_full_name, owner_email, one org whose domain matches the fixture `From`) so `_build_context` and the org taxonomy resolve. Mirror `tests/test_integration_spool.py`'s `home` fixture.
- [ ] Run → **FAIL** until `e2e_home` + config are wired.

### Task B5 — impl: e2e_home fixture + minimal config

- [ ] Add `e2e_home` to `tests/e2e/conftest.py`: monkeypatch `MCPBRAIN_HOME`, create `enrich_inbox/` and `enrich_queue/`, and write `config.json` via `config.write_config` (or a direct JSON write) with owner identity + an org entry. Verify the org domain matches the Gmail fixture `From` so `apply` links the org (re-read `mcpbrain/orgs.py::taxonomy_from_config` for the config shape).
- [ ] Run → **PASS**.
- [ ] Commit: `test(e2e): real prepare spools pending.json from synced threads`.

### Task B6 — failing test: hand-written enrich_inbox + real drain grows the graph

- [ ] The stubbed Cowork extractor is a hand-authored `enrich_inbox/<batch>.json` matching the spool. Read the produced `pending.json` to get `batch_id` + thread `message_id`s, then write a contract-valid batch (one extraction per thread, each with `entities` [person + org], a `works_at` relation, `topics`, and one `actions` entry with a `description` + owner so the dashboard surfaces it). `message_id`s MUST equal the synced chunk metadata (= Gmail raw `id`).

```python
from mcpbrain import drain, graph_write


def _hand_extraction(thread):
    msgs = thread["messages"]
    return {
        "thread_id": thread["thread_id"],
        "org": "Acme",  # must be a configured org (B5)
        "content_type": "request",
        "summary": "Joel asks Sam to confirm Hall B availability.",
        "entities": [
            {"name": "Joel Chelliah", "type": "person", "org": "Acme", "role": "Pastor"},
            {"name": "Acme Corp", "type": "org", "org": "Acme", "role": ""},
        ],
        "topics": ["facilities"],
        "actions": [{"description": "Confirm Hall B is free for Wednesday college",
                     "owner": ""}],
        "relations": [{"source_name": "Joel Chelliah", "type": "works_at",
                       "target_name": "Acme Corp"}],
        "messages": [{"message_id": m["message_id"], "sender": m.get("sender", ""),
                      "date": m["date"], "subject": m.get("subject", "")}
                     for m in msgs],
        "resolved_action_ids": [],
    }


def test_full_loop_grows_graph_and_dashboard(e2e_store, fake_google, e2e_home):
    backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    prepare.prepare(e2e_store, thread_cap=20, char_budget=24000, resolution_due=False)
    pending = json.loads((e2e_home / "enrich_queue" / "pending.json").read_text())

    batch = {"batch_id": pending["batch_id"],
             "extractions": [_hand_extraction(t) for t in pending["threads"]],
             "merge_answers": []}
    # validate against the real contract before drain consumes it
    from mcpbrain.contract import validate_batch_file
    assert validate_batch_file(batch) == [], "hand-written batch must satisfy the contract"
    (e2e_home / "enrich_inbox" / f"{batch['batch_id']}.json").write_text(json.dumps(batch))

    summary = drain.drain(e2e_store, home=e2e_home, apply=graph_write.apply)
    assert summary["applied"] >= 2

    # graph grew (non-empty guards: a no-op pipeline fails loudly)
    ents = e2e_store.list_entities()
    rels = e2e_store.list_relations()
    assert ents, "drain must have written entities"
    assert rels, "drain must have written relations"
    assert any(e["type"] == "person" for e in ents)
    assert any(e["type"] == "org" for e in ents)
    assert any(r.get("relation") == "works_at" or r.get("type") == "works_at"
               for r in rels)
```

- [ ] Run → **FAIL** until the extraction `message_id`s and the org config align with the synced chunks. Use `superpowers:systematic-debugging` if `doc_ids_for_messages` returns empty (it means the fixture `id` ≠ extraction `message_id`).

### Task B7 — impl: align fixture message ids + org so the loop grows the graph

- [ ] Reconcile fixture `id`/`threadId` ↔ extraction `message_id`/`thread_id`, and the `From` domain ↔ configured org, until B6 is green. Inspect the real relation column name with a one-off (`e2e_store.list_relations()`) and fix the assertion's key accordingly.
- [ ] Run → **PASS**.
- [ ] Commit: `test(e2e): full loop grows entities + relations via real drain/apply`.

### Task B8 — failing test: dashboard surfaces the seeded action + FTS finds a known chunk

- [ ] Add to `test_full_loop.py`:

```python
from mcpbrain import dashboard


def test_dashboard_and_search_after_loop(e2e_store, fake_google, e2e_home):
    backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    prepare.prepare(e2e_store, thread_cap=20, char_budget=24000, resolution_due=False)
    pending = json.loads((e2e_home / "enrich_queue" / "pending.json").read_text())
    batch = {"batch_id": pending["batch_id"],
             "extractions": [_hand_extraction(t) for t in pending["threads"]],
             "merge_answers": []}
    (e2e_home / "enrich_inbox" / f"{batch['batch_id']}.json").write_text(json.dumps(batch))
    drain.drain(e2e_store, home=e2e_home, apply=graph_write.apply)

    payload = dashboard.assemble(e2e_store, str(e2e_home))
    actions = payload["actions"]
    total = sum(len(actions[b]) for b in ("overdue", "due_today", "upcoming", "blocked"))
    assert total >= 1, "the seeded action must surface in the dashboard"

    hits = e2e_store.fts_search("Hall B", 5)
    assert hits, "a known chunk must be findable after the full loop"
```

- [ ] Run → **PASS** if the seeded action's owner is empty (kept by `actions_today`) and the home config is sufficient for `assemble`. Debug with `systematic-debugging` if `actions_today` degrades (it returns empty buckets on error — check `store._path` and that the `actions` table was created by `apply`).

### Task B9 — impl/cleanup: make the dashboard + search assertions green

- [ ] Ensure the seeded `actions[].owner` is `""` (so it is not filtered to another owner) and `e2e_home` config sets `owner_name`. Confirm `assemble` returns without swallowing the actions source. Refactor the three loop tests to share a `loop_run` fixture/helper that runs sync→prepare→write-batch→drain once and returns `(store, home)`, so each assertion test reads the same end state without duplicating the pipeline.
- [ ] Run the whole e2e file → **PASS**.
- [ ] Commit: `test(e2e): dashboard surfaces seeded action + FTS finds known chunk`.

### Task B10 — wire the e2e gate into CI

- [ ] Read `.github/workflows/ci.yml`. The e2e test already runs inside the existing `tests` job (`.venv/bin/pytest` collects it). To make the gate explicit in the build log AND ensure it cannot be silently skipped, add a dedicated step to the `tests` job AFTER "Run the offline suite":

```yaml
      - name: Run the end-to-end loop (gate)
        # The full sync→prepare→drain→graph+dashboard loop with only Google and
        # the Cowork extractor stubbed. Fast (small fixture, local sqlite, no
        # network). A red e2e fails the build.
        run: .venv/bin/pytest -m e2e -q
```

- [ ] Confirm the e2e test does NOT require the bge model or network (it uses `fts_search`, not `hybrid_search`, and dim=4 stores). If any assertion pulled in an embedder, replace it with the keyword path so the e2e step stays seconds-fast.
- [ ] Run locally: `uv run pytest -m e2e -q` → green; and `uv run pytest -q` (full suite) → green.
- [ ] Lint: `uv run ruff check mcpbrain/` → clean.
- [ ] Commit: `ci: run the e2e loop as an explicit build gate`.

---

## Task C — final verification + self-review (no commit beyond the above)

- [ ] Run the full suite: `uv run pytest -q` → all green (use `superpowers:verification-before-completion` — paste the real summary line, do not assert from memory).
- [ ] Run lint: `uv run ruff check mcpbrain/ tests/` → clean.
- [ ] Self-review against the spec: doctor covers run_doctor (probe→classify→auto-fix→re-probe→format→exit code), the auto/guided disposition table, the honest scheduled-tasks inference, the output shape + glyphs, the non-empty guard, and every spec test case (daemon-down, agent-missing, records-missing, google-expired, all-ok, repair-failure, scheduled-tasks) with INJECTED stub repairs. The e2e covers FakeGoogleService + fixtures, real sync (backfill), real prepare→pending.json, hand-written contract-valid enrich_inbox, real drain→graph_write.apply, graph + dashboard + search assertions, the non-empty (loud-fail) guards, and the CI gate.
- [ ] Confirm no real launchd/git/agent side effects run in any doctor test (all via injected `repairs`/`reprobe`/`agent_installed`).

---

## Optional (separate from this spec's core) — `/mcpbrain-fix` Cowork skill

> The spec lists `/mcpbrain-fix` only as the referenced remedy string; its full body is explicitly out of this spec's core. Implement ONLY if the executing session has time after the above is green and merged-ready.

### Task D1 (OPTIONAL) — `/mcpbrain-fix` repair skill

- [ ] Create a Cowork skill (mirror the existing `mcpbrain-setup` skill structure under the plugin's skills dir) that recreates the four scheduled tasks (enrich / gardener / meeting-packs / reference-gardener) by driving the Cowork UI the way the install skill does. This is text/markdown + UI steps, not Python — it does NOT call an LLM and adds no daemon code path.
- [ ] No automated test (UI skill). Manually verify the steps against the existing setup skill. Commit separately: `feat(skill): /mcpbrain-fix recreates the four scheduled tasks`.

---

## Files touched

- New: `mcpbrain/doctor.py`
- New: `tests/test_doctor.py`
- New: `tests/e2e/__init__.py`, `tests/e2e/conftest.py`, `tests/e2e/test_full_loop.py`, `tests/e2e/fixtures/*.json`
- Modified: `mcpbrain/cli.py` (one tuple entry + one dispatch entry — shared 2-line conflict site with Spec 1)
- Modified: `.github/workflows/ci.yml` (one e2e step in the `tests` job)
- Modified: `pyproject.toml` (register the `e2e` marker — or fall back to `tests/e2e/conftest.py::pytest_configure` if contested)
- Optional: a `/mcpbrain-fix` Cowork skill (separate commit)
