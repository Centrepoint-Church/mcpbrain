# Daemon Scheduling Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every defect found reviewing the daemon-scheduling implementation (`5f37ff9..HEAD`, 15 unpushed commits) so it can be pushed and released.

**Architecture:** No architectural change. The two-timer design is sound and the review confirmed lock ordering is deadlock-free, the `BEGIN IMMEDIATE` conversion complete and correct, and scope discipline clean. This plan fixes correctness, fairness, safety and coverage defects within that design.

**Tech Stack:** Python 3.12, SQLite (WAL), `threading`, fastembed/onnxruntime, launchd / schtasks.

**Inputs:** `docs/superpowers/specs/2026-07-27-daemon-scheduling-design.md`,
`docs/superpowers/plans/2026-07-27-daemon-scheduling.md`.

## Context: why this exists

Three adversarial reviews plus live observation found the implementation does
not meet its primary acceptance criterion. Observed on the live daemon: **up
8m39s, heartbeat never advanced, 183 bulk-lock skip warnings, all four gated
passes skipping every tick and never running once.** The spec's acceptance
section records those same log lines as evidence of success; they are evidence
of the opposite. The change currently trades a 20-pass starvation for a 4-pass
one.

## Global Constraints

- Python 3.12; no new third-party dependencies.
- Preserve the `_is_due` / injectable `_clock` cadence machinery.
- `uv run pytest <paths> -q` scoped to edited + impacted files; `uv run ruff check mcpbrain/` clean before each commit.
- Work on `main`, commit per task. **Do not push and do not release** until Task 8 passes.
- The daemon should be stopped while working (`launchctl bootout gui/$(id -u)/com.mcpbrain`); Task 8 restarts it.

---

### Task 1: Make the regression untestable-to-reintroduce, and disarm `os._exit`

Do this first: it protects every later task, and closes a live footgun where a
future test could kill the pytest worker.

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_maintenance_scheduler.py`
- Create: `tests/test_run_loop_wiring.py`

**Interfaces:** Produces an autouse fixture `_no_real_exit`; no production change.

- [ ] **Step 1: Add the safety fixture**

In `tests/conftest.py`:

```python
import pytest


@pytest.fixture(autouse=True)
def _no_real_exit(monkeypatch):
    """os._exit(1) in the watchdog bypasses pytest entirely — a test that ever
    reaches it kills the worker with no traceback. Today that path is unreachable
    only by accident (frozen clocks, stubbed _stalled_phase); nothing structural
    prevents it. Neutralise it for every test."""
    from mcpbrain import daemon as _d
    monkeypatch.setattr(_d.Daemon, "_exit_for_restart",
                        lambda self: (_ for _ in ()).throw(
                            AssertionError("_exit_for_restart called in a test")))
    monkeypatch.setattr(_d.Daemon, "_spawn_replacement",
                        lambda self: (_ for _ in ()).throw(
                            AssertionError("_spawn_replacement called in a test")))
```

- [ ] **Step 2: Write the failing wiring test**

Create `tests/test_run_loop_wiring.py`:

```python
"""run() must start the maintenance thread and must NOT run passes inline.

The reviewed implementation could be reverted to the pre-fix shape — inline
_run_periodic_passes(), no thread — with every existing test still passing. That
is the exact bug this whole change exists to fix, so it needs a test that fails.
"""
import threading

from mcpbrain import daemon as d


def test_run_starts_the_maintenance_thread_and_does_not_run_passes_inline(monkeypatch, tmp_path):
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._wake = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._progress = {}
    dm._progress_lock = threading.Lock()
    dm._clock = lambda: 0.0
    dm._interval_s = 0.01
    dm._pending_update = False
    dm._maintenance_thread = None
    dm._maintenance_interval_s = 3600.0

    started = []
    inline = []
    monkeypatch.setattr(dm, "_start_maintenance_thread",
                        lambda: started.append(1))
    monkeypatch.setattr(dm, "_run_periodic_passes",
                        lambda: inline.append(1))
    monkeypatch.setattr(dm, "run_one", lambda: dm._stop.set() or {})
    monkeypatch.setattr(dm, "ensure_services", lambda: {})
    monkeypatch.setattr(dm, "_backup_under_bulk_lock", lambda: None)
    monkeypatch.setattr(d, "write_daemon_heartbeat", lambda home: None)

    dm.run()

    assert started == [1], "run() must start the maintenance thread"
    assert inline == [], "run() must NOT call _run_periodic_passes inline"
```

- [ ] **Step 3: Run it to verify it fails for the right reason**

Run: `uv run pytest tests/test_run_loop_wiring.py -q`
Expected: it should PASS against current `HEAD`. Prove it is load-bearing by
temporarily reverting: add `self._run_periodic_passes()` back into the `run()`
loop body, re-run, confirm FAIL, then `git checkout mcpbrain/daemon.py`.

- [ ] **Step 4: Fix the bare-daemon fixtures**

In `tests/test_maintenance_scheduler.py`, the fixtures at the tests named
`test_passes_run_while_the_cycle_thread_is_blocked`,
`test_maintenance_loop_exits_on_stop` and
`test_maintenance_loop_survives_a_raising_pass` construct a Daemon without
`_progress_lock`, so `_stalled_phase()` raises `AttributeError` on every tick
and the loop's `except` swallows it — meaning those tests only ever execute the
**first line** of the loop body. Add to each fixture:

```python
    dm._progress = {}
    dm._progress_lock = threading.Lock()
```

- [ ] **Step 5: Make the starvation test real**

`test_passes_run_while_the_cycle_thread_is_blocked` currently simulates a
"wedged cycle" with an unrelated `threading.Thread(target=wedged.wait)` that
touches neither `_bulk_lock` nor `run_one`. Replace the wedge with a real one —
a thread that holds `_bulk_lock` for the duration — and assert that non-gated
passes still run:

```python
    holder_go = threading.Event()
    holder_release = threading.Event()

    def _hold_bulk_lock():
        with dm._bulk_lock:
            holder_go.set()
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_bulk_lock, daemon=True)
    holder.start()
    assert holder_go.wait(timeout=2.0), "holder never acquired the lock"
    # ... run the maintenance loop, assert `ran` advances ...
    holder_release.set()
    holder.join(timeout=5.0)
```

- [ ] **Step 6: Add join timeouts**

`tests/test_store_write_txn.py` and `tests/test_daemon_thread_safety.py` call
`t.join()` with no timeout, which hangs forever on a deadlock regression instead
of failing. Change every `t.join()` in both files to `t.join(timeout=30)` and
follow the loop with `assert not any(t.is_alive() for t in threads)`.

- [ ] **Step 7: Run and commit**

Run: `uv run pytest tests/test_run_loop_wiring.py tests/test_maintenance_scheduler.py tests/test_store_write_txn.py tests/test_daemon_thread_safety.py -q && uv run ruff check mcpbrain/`

```bash
git add tests/
git commit -m "test(daemon): pin the run() wiring, disarm os._exit, fix hollow fixtures

The headline regression could be reintroduced with every test still green.
Three maintenance tests were also only executing the first line of the loop
body, because their fixtures lacked _progress_lock and _stalled_phase raised
AttributeError every tick into a swallowing except."
```

---

### Task 2: Actually bound the cycle and give the gated passes a slot

The core functional defect. `prepare_units` never received the budget, so
`run_one()` still runs unbounded while holding `_bulk_lock` for its entire
duration — measured live at >8 minutes with zero heartbeat advance.

**Files:**
- Modify: `mcpbrain/prepare.py:738` (`prepare_units`)
- Modify: `mcpbrain/daemon.py` (`run_cycle` ~479, `run()` ~2686-2690, `_run_periodic_passes` ~2443)
- Modify: `mcpbrain/sync/__init__.py` (per-page budget checks)
- Test: `tests/test_bulk_lock_fairness.py` (create), `tests/test_index_bounded.py` (extend)

**Interfaces:**
- Produces: `prepare_units(..., budget=None)`, `Daemon._bulk_lock_wanted` (`threading.Event`),
  `Daemon._cycle_bulk_section()` context manager.

- [ ] **Step 1: Write the failing fairness test**

Create `tests/test_bulk_lock_fairness.py`:

```python
"""A busy cycle must not starve the four gated passes indefinitely.

Live evidence from the reviewed build: 183 consecutive
"bulk lock held for more than 5.0s" warnings and not one gated pass run. The
cycle held _bulk_lock for all of run_one() and re-acquired 1s later, so with
non-FIFO locks the maintenance thread lost essentially every race.
"""
import threading
import time

from mcpbrain import daemon as d


def _dm():
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_wanted = threading.Event()
    dm._bulk_lock_wait_s = 5.0
    dm._stop = threading.Event()
    return dm


def test_cycle_yields_when_maintenance_wants_the_lock():
    dm = _dm()
    got = []

    def maintenance():
        dm._bulk_lock_wanted.set()
        try:
            acquired = dm._bulk_lock.acquire(timeout=dm._bulk_lock_wait_s)
            got.append(acquired)
            if acquired:
                dm._bulk_lock.release()
        finally:
            dm._bulk_lock_wanted.clear()

    # Cycle does 20 short "phases", entering the bulk section each time.
    def cycle():
        for _ in range(20):
            with dm._cycle_bulk_section():
                time.sleep(0.02)

    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    time.sleep(0.05)
    m = threading.Thread(target=maintenance, daemon=True)
    m.start()
    m.join(timeout=10)
    c.join(timeout=10)

    assert got == [True], "maintenance never got the bulk lock while the cycle ran"


def test_bulk_section_releases_between_phases():
    """The lock must not be held across the whole cycle."""
    dm = _dm()
    seen_free = []

    def watcher():
        for _ in range(50):
            if dm._bulk_lock.acquire(blocking=False):
                dm._bulk_lock.release()
                seen_free.append(1)
                return
            time.sleep(0.01)

    def cycle():
        for _ in range(10):
            with dm._cycle_bulk_section():
                time.sleep(0.01)

    w = threading.Thread(target=watcher, daemon=True)
    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    w.start()
    w.join(timeout=5)
    c.join(timeout=5)
    assert seen_free, "bulk lock was never observably free during the cycle"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_bulk_lock_fairness.py -q`
Expected: FAIL — `_cycle_bulk_section` and `_bulk_lock_wanted` do not exist.

- [ ] **Step 3: Add the yielding bulk section**

In `Daemon.__init__`, beside `_bulk_lock`:

```python
        # Set by the maintenance thread while it is waiting for _bulk_lock.
        # CPython locks are not FIFO-fair, so without an explicit hand-off the
        # cycle thread — which re-acquires ~1s after releasing — wins nearly
        # every race and the four gated passes never run at all.
        self._bulk_lock_wanted = threading.Event()
```

Add the context manager near `_backup_under_bulk_lock`:

```python
    @contextmanager
    def _cycle_bulk_section(self):
        """Hold _bulk_lock for ONE chunk-mutating phase, yielding between phases.

        The spec's data flow says the cycle holds this lock "only around
        chunk-mutating phases"; holding it across all of run_one() is what
        starved the gated passes. Between phases we also pause briefly if
        maintenance is waiting, so the waiter actually wins the next acquire.
        """
        self._bulk_lock.acquire()
        try:
            yield
        finally:
            self._bulk_lock.release()
            if self._bulk_lock_wanted.is_set():
                # Give the waiter a scheduling window; without this the
                # re-acquire below beats it on an unfair lock.
                self._stop.wait(timeout=BULK_LOCK_YIELD_S)
```

and the constant beside `BULK_LOCK_ACQUIRE_S`:

```python
BULK_LOCK_YIELD_S = 0.25
```

- [ ] **Step 4: Narrow the cycle's hold and signal intent**

In `run()` (~2686-2690), remove the `with self._bulk_lock:` wrapper around
`run_one()` — the lock now moves inside `run_cycle` per phase:

```python
                try:
                    cycle_result = self.run_one()
```

In `run_cycle`, wrap **only** the chunk-mutating calls in
`with self._cycle_bulk_section():` — `run_sync_cycle(...)`, and the
`drain.drain(...)` call. Leave `prepare_units`, `drain_captures`,
`prune_change_log` and `agent_errs` outside it: they do not mutate `chunks`.

In `_run_periodic_passes`, set the intent flag around the bounded acquire:

```python
                if cp.needs_bulk_lock:
                    if not self._is_due(cp.interval_attr, cp.last_attr):
                        continue
                    self._bulk_lock_wanted.set()
                    try:
                        acquired = self._bulk_lock.acquire(timeout=self._bulk_lock_wait_s)
                    finally:
                        self._bulk_lock_wanted.clear()
                    if not acquired:
                        log.warning(
                            "periodic pass %s skipped this tick: bulk lock held for "
                            "more than %.1fs (cycle busy); will retry", cp.name,
                            self._bulk_lock_wait_s)
                        continue
```

Apply the same `_bulk_lock_wanted` set/clear around the acquire in
`_backup_under_bulk_lock`.

- [ ] **Step 5: Budget `prepare_units`**

In `mcpbrain/prepare.py`, add `budget=None` to `prepare_units`'s signature
(line 738) and, inside its per-thread loop, break when spent:

```python
        if budget is not None and budget.expired():
            log.info("prepare_units: budget spent after %d threads", len(units))
            break
```

Pass it at `daemon.py:479`: `prepare.prepare_units(..., budget=budget)`.

- [ ] **Step 6: Check the budget per page, not only per source**

In `mcpbrain/sync/__init__.py` the budget is currently checked only between
sources. The 1h44m SSL hang observed live happened *inside* `sync_gmail`. Add a
check inside each source's pagination loop, immediately after each page is
processed, using the same early-return shape already used between sources.

- [ ] **Step 7: Run and commit**

Run: `uv run pytest tests/test_bulk_lock_fairness.py tests/test_index_bounded.py tests/test_maintenance_scheduler.py tests/test_daemon.py -q && uv run ruff check mcpbrain/`

```bash
git add mcpbrain/daemon.py mcpbrain/prepare.py mcpbrain/sync/__init__.py tests/test_bulk_lock_fairness.py
git commit -m "fix(daemon): bound prepare_units and stop starving the gated passes

prepare_units never received the budget, so run_one() stayed unbounded while
holding _bulk_lock for its whole duration — live: 8m39s, zero heartbeat
advance, 183 consecutive skip warnings, no gated pass ever running. The lock
now moves inside run_cycle per chunk-mutating phase, with an explicit hand-off
because CPython locks are not FIFO-fair."
```

---

### Task 3: Watchdog safety and correctness

The watchdog can currently kill the daemon mid-backup via `os._exit`, which
bypasses `finally` — the exact mechanism that orphaned ~24 GB of
`mcpbrain-snap-*` directories and froze the host earlier today.

**Files:**
- Modify: `mcpbrain/daemon.py` (`_note_progress` sites, `_record_watchdog_exit`, `_recent_watchdog_exits`, `_maintenance_loop`, `status`)
- Test: `tests/test_daemon_watchdog.py` (extend)

**Interfaces:** Produces `Daemon._backup_in_progress` (`threading.Event`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon_watchdog.py`:

```python
def test_watchdog_does_not_exit_during_a_backup(tmp_path, monkeypatch):
    """os._exit bypasses finally; killing mid-snapshot orphans the temp dir.
    That is how ~24GB of mcpbrain-snap-* was left behind on 2026-07-27."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._backup_in_progress = threading.Event()
    dm._backup_in_progress.set()
    called = []
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.append("exit"))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.append("spawn"))
    dm._recover_from_stall()
    assert called == [], "watchdog must not exit while a backup is running"


def test_watchdog_history_survives_a_reboot(tmp_path, monkeypatch):
    """Persisted timestamps must be wall-clock. time.monotonic resets on reboot,
    which would make every historical entry look recent and disable self-healing
    permanently."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._record_watchdog_exit()
    import json
    written = json.loads((tmp_path / "watchdog_exits.json").read_text())
    import time as _t
    assert abs(written[0] - _t.time()) < 60, "expected wall-clock, got monotonic"


def test_backup_phase_reports_progress(tmp_path, monkeypatch):
    """A multi-minute backup must not read as a stall."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._note_progress("backup")
    assert "backup" in dm._progress
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_daemon_watchdog.py -q`
Expected: FAIL on all three new tests.

- [ ] **Step 3: Persist wall-clock, not monotonic**

`_record_watchdog_exit` and `_recent_watchdog_exits` currently use
`self._clock()` (monotonic). Change **only the persisted history** to
`time.time()`, and compare against `time.time() - WATCHDOG_WINDOW_S`. Leave
`_progress` on `_clock` (monotonic is correct for in-process durations).

- [ ] **Step 4: Stamp progress during backup, and refuse to exit mid-backup**

Add `self._backup_in_progress = threading.Event()` in `__init__`. In
`_backup_under_bulk_lock`, set it around the `maybe_backup()` call, clear in a
`finally`, and call `self._note_progress("backup")` immediately before and
after. In `_recover_from_stall`, return early without exiting when
`self._backup_in_progress.is_set()`, logging that recovery is deferred.

- [ ] **Step 5: Distinguish a wedge from a repeatedly-failing cycle**

`run()` catches cycle exceptions without stamping progress, so a deterministic
raise looks identical to a hang and burns the 3-exit budget on something a
restart cannot fix. In the `except` handler in `run()`, add
`self._note_progress("cycle_error")` and track consecutive failures; when the
count exceeds 3, log an ERROR naming the exception and skip watchdog recovery
(the daemon stays up, visibly failing, rather than restart-looping).

- [ ] **Step 6: Keep the watchdog alive if the maintenance thread dies**

The spec says a dead scheduler must not take the watchdog with it, but the
watchdog lives inside `_maintenance_loop`. In `run()`'s loop body, after
`_backup_under_bulk_lock()`, add a liveness check that restarts the thread if
`self._maintenance_thread` is not alive and `_stop` is unset, logging it.

- [ ] **Step 7: Lock `status()` and suppress `stalled` while paused**

`status()` reads `dict(self._progress)` without `_progress_lock`, and reports
`stalled` even while paused. Take the lock, and report `stalled: None` when
`self._pause.is_set()`.

- [ ] **Step 8: Run and commit**

Run: `uv run pytest tests/test_daemon_watchdog.py tests/test_daemon.py tests/test_doctor.py -q && uv run ruff check mcpbrain/`

```bash
git add mcpbrain/daemon.py tests/test_daemon_watchdog.py
git commit -m "fix(daemon): watchdog must not kill a backup, and must survive a reboot

os._exit bypasses finally, so firing mid-snapshot orphans the temp dir — the
mechanism that left ~24GB of mcpbrain-snap-* and froze the host. Backup now
reports progress and blocks recovery while running. Exit history persists
wall-clock: monotonic resets on reboot, which would have disabled self-healing
permanently and pinned doctor to a red Watchdog line."
```

---

### Task 4: Lifecycle and cross-thread state

**Files:**
- Modify: `mcpbrain/daemon.py` (`run()` update path, `stop()`, `_pending_*` handling, `_embedder`, `_stash_take`)
- Test: `tests/test_daemon_thread_safety.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_daemon_thread_safety.py`:

```python
def test_pending_update_stops_the_maintenance_thread():
    """Breaking out of run() for an update releases SingleWriterLock; if the
    maintenance thread is still writing, the successor process makes two
    writers — exactly what the file lock exists to prevent."""
    import threading
    from mcpbrain import daemon as d
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._maintenance_thread = threading.Thread(target=dm._stop.wait, daemon=True)
    dm._maintenance_thread.start()
    dm._shutdown_maintenance()
    assert dm._stop.is_set()
    assert not dm._maintenance_thread.is_alive()


def test_stash_delete_does_not_drop_a_fresh_batch():
    """run_one snapshots, the cycle runs, then it deletes drained keys. If a
    pass rewrote that key meanwhile, the fresh batch is deleted unattached."""
    import threading
    from mcpbrain import daemon as d
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._pending_blocks = {"k": ["old"]}
    dm._stash_generation = {"k": 1}
    taken = dm._stash_snapshot()
    dm._pending_blocks["k"] = ["fresh"]          # maintenance thread rewrites
    dm._stash_generation["k"] = 2
    dm._stash_clear_drained({"k_drained": 1}, taken)
    assert dm._pending_blocks.get("k") == ["fresh"], "fresh batch was dropped"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_daemon_thread_safety.py -q`
Expected: FAIL — `_shutdown_maintenance`, `_stash_snapshot`,
`_stash_clear_drained`, `_stash_generation` do not exist.

- [ ] **Step 3: Stop the maintenance thread before an update restart**

Add:

```python
    def _shutdown_maintenance(self, timeout: float = 10.0) -> None:
        """Stop and join the maintenance thread. Must run before run() releases
        SingleWriterLock on the _pending_update path, or update_from_index
        reinstalls while maintenance is still writing SQLite and the successor
        (which now waits HANDOVER_LOCK_WAIT_S for the lock) becomes a second
        concurrent writer."""
        self._stop.set()
        t = getattr(self, "_maintenance_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
```

Call it immediately before the `break` on `_pending_update` in `run()`, and
from `stop()`.

- [ ] **Step 4: Make stash clearing generation-safe**

Replace the read-then-delete in `run_one` with a snapshot that records a
generation per key, and a clear that only deletes keys whose generation is
unchanged. Add `self._stash_generation: dict = {}` in `__init__`, bump it in
every writer alongside the value, and implement:

```python
    def _stash_snapshot(self) -> dict:
        with self._stash_lock:
            return {
                "blocks": dict(self._pending_blocks),
                "audit": dict(self._pending_audit),
                "synthesis": list(self._pending_synthesis),
                "gen": dict(self._stash_generation),
            }

    def _stash_clear_drained(self, drained: dict, taken: dict) -> None:
        """Delete only keys that have NOT been rewritten since the snapshot."""
        with self._stash_lock:
            for name in ("_pending_blocks", "_pending_audit"):
                store = getattr(self, name)
                for key in list(store):
                    if f"{key}_drained" not in drained:
                        continue
                    if self._stash_generation.get(key) != (taken.get("gen") or {}).get(key):
                        continue      # rewritten mid-cycle; keep the fresh batch
                    del store[key]
```

- [ ] **Step 5: Bound the embedder acquire from the maintenance thread**

`_embedder` holds `_embedder_lock` for the whole lazy build (a cold fastembed
download takes minutes) and `_run_self_improve` — a **non**-gated pass — touches
it, parking the maintenance thread along with `_note_progress` and the watchdog
check. Give the maintenance-side access a bounded acquire that skips the pass
for that tick, mirroring `BULK_LOCK_ACQUIRE_S`.

- [ ] **Step 6: Remove the dead `_stash_take`**

`_stash_take` has no caller and, per its own docstring, raises on the
list-shaped `_pending_synthesis`. Delete it and its two tests, which are now
superseded by the generation tests above.

- [ ] **Step 7: Run and commit**

Run: `uv run pytest tests/test_daemon_thread_safety.py tests/test_daemon.py -q && uv run ruff check mcpbrain/`

```bash
git add mcpbrain/daemon.py tests/test_daemon_thread_safety.py
git commit -m "fix(daemon): thread lifecycle and generation-safe stash clearing

The update path released the writer lock without stopping the maintenance
thread, allowing two writer processes. Stash clearing was check-then-clear
across the whole cycle window, so a pass rewriting a key mid-cycle had its
fresh batch deleted unattached. Removes the dead _stash_take."
```

---

### Task 5: Make Windows supervision real

Both halves of "supervised Windows" are currently false, verified by reading the
generated artefacts.

**Files:**
- Modify: `mcpbrain/agents.py` (`schtasks_xml` ~181-205, `_install_schtasks` ~322)
- Modify: `mcpbrain/daemon.py` (`_recover_from_stall` supervision test)
- Test: `tests/test_schtasks_xml.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schtasks_xml.py`:

```python
"""The on-logon task must actually start, and actually restart on failure."""
import xml.etree.ElementTree as ET

from mcpbrain import agents


def _xml(tmp_path):
    return agents.schtasks_xml(shim_path=tmp_path / "agents" / "com.mcpbrain.vbs")


def test_xml_is_wellformed_and_has_required_sections(tmp_path):
    root = ET.fromstring(_xml(tmp_path))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:RegistrationInfo", ns) is not None
    assert root.find(".//t:Principals", ns) is not None, \
        'Actions Context="Author" requires a matching Principal id'


def test_exec_launches_wscript_not_the_vbs_directly(tmp_path):
    """Task Scheduler's Exec is CreateProcess; it cannot run a .vbs."""
    root = ET.fromstring(_xml(tmp_path))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    cmd = root.find(".//t:Exec/t:Command", ns).text
    args = root.find(".//t:Exec/t:Arguments", ns).text
    assert cmd.lower().endswith("wscript.exe"), f"Command was {cmd!r}"
    assert ".vbs" in args


def test_restart_on_failure_present(tmp_path):
    root = ET.fromstring(_xml(tmp_path))
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:RestartOnFailure/t:Count", ns) is not None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_schtasks_xml.py -q`
Expected: FAIL — `<Command>` is the `.vbs`, no `RegistrationInfo`/`Principals`.

- [ ] **Step 3: Fix the XML**

Emit `<RegistrationInfo>` and a `<Principals>` block whose `<Principal id="Author">`
matches `Actions Context="Author"`, and change the action to:

```python
        "  <Actions Context=\"Author\">\n"
        "    <Exec>\n"
        "      <Command>wscript.exe</Command>\n"
        f"      <Arguments>\"{shim_path_xml}\"</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
```

Drop the unused `home` parameter.

- [ ] **Step 4: Make the daemon's supervision test honest**

The shim runs `sh.Run "...", 0, False` — non-waiting — so wscript exits 0
immediately, the task completes successfully, and the daemon's later
`os._exit(1)` is invisible to Task Scheduler. `RestartOnFailure` therefore
cannot fire. Two options; take (a):

**(a)** Change the shim's third argument to `True` so wscript waits on the
daemon and propagates its exit code, making `RestartOnFailure` real. Verify the
window still stays hidden (style `0`).

If hardware QA later shows (a) regresses logon behaviour, fall back to **(b)**:
treat `schtasks` as unsupervised in `_recover_from_stall` and use the
spawn-replacement path on all Windows branches.

- [ ] **Step 5: Guard the install**

`_install_schtasks` calls `subprocess.run(..., check=True)`, so malformed XML
aborts `install_agent` entirely. Wrap the XML registration so a failure falls
back to the previous CLI form and logs loudly rather than failing the install.

- [ ] **Step 6: Run and commit**

Run: `uv run pytest tests/test_schtasks_xml.py tests/test_agents.py -q && uv run ruff check mcpbrain/`

```bash
git add mcpbrain/agents.py mcpbrain/daemon.py tests/test_schtasks_xml.py
git commit -m "fix(windows): make the on-logon task start, and restart on failure

Exec is CreateProcess and cannot run a .vbs, so the generated task would never
launch the daemon; and the non-waiting shim meant the task always completed 0,
so RestartOnFailure could never fire — making the watchdog's 'supervised' exit
on Windows kill the daemon until next logon."
```

---

### Task 6: Latency and store hygiene

**Files:** `mcpbrain/embed.py`, `mcpbrain/store.py`, `mcpbrain/doctor.py`
**Test:** `tests/test_store_write_txn.py` (extend), `tests/test_embed_locking.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_embed_locking.py`:

```python
"""Recall must not queue behind a bulk embedding batch."""
import threading
import time

from mcpbrain.embed import _LocalEmbedder


def test_embed_query_is_not_blocked_by_a_long_passage_batch():
    emb = _LocalEmbedder.__new__(_LocalEmbedder)
    emb.dim = 4
    emb._qp = ""
    emb._lock = threading.Lock()

    class _Model:
        def embed(self, texts):
            time.sleep(0.5)                      # a bulk batch
            return [[0.0] * 4 for _ in texts]

        def query_embed(self, texts):
            return iter([[0.0] * 4])

    emb._model = _Model()
    t = threading.Thread(target=lambda: emb.embed_passages(["x"] * 4), daemon=True)
    t.start()
    time.sleep(0.05)
    start = time.monotonic()
    emb.embed_query("hello")
    elapsed = time.monotonic() - start
    t.join(timeout=5)
    assert elapsed < 0.3, f"embed_query waited {elapsed:.2f}s behind the batch"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_embed_locking.py -q`
Expected: FAIL — `embed_query` blocks on the same `_lock`.

- [ ] **Step 3: Stop serialising recall behind ingest**

ONNX Runtime sessions are thread-safe for concurrent `Run()`; the lock exists
only to serialise the *lazy build*. Replace the blanket method lock with a build
-only lock: guard construction, and let `embed_passages`/`embed_query` call the
model without holding it.

- [ ] **Step 4: Reduce the write-retry ceiling on the recall path**

`_begin_immediate` retries 6× and each `BEGIN IMMEDIATE` can wait the full
`busy_timeout=5000`, so a worst case is ~31 s — inherited by
`/api/recall → decay.update_on_recall`. Lower `_BEGIN_RETRIES` to 3 and add an
optional `retries=` argument so the recall-path writers pass a smaller budget.

- [ ] **Step 5: Do not mask the original error on rollback**

`_connect(write=True)`'s `except BaseException: db.execute("ROLLBACK")` masks
the real failure when SQLite has already auto-rolled back (SQLITE_FULL /
SQLITE_IOERR) — a disk-full then surfaces as "cannot rollback - no transaction
is active", which is exactly what happened live. Wrap the rollback in its own
`try/except sqlite3.OperationalError: pass` so the original propagates.

- [ ] **Step 6: Small fixes**

- `archive_stale_actions` / `archive_duplicate_actions` take the write lock and
  then return early on `dry_run`; move the `dry_run` branch to a read connection.
- `embed.py` mutates process-global `OMP_NUM_THREADS`, which is a no-op once
  OpenMP has initialised. Set it only if `fastembed`/`onnxruntime` are not yet
  imported, and note the limitation in a comment.
- `doctor.run_doctor` now performs live HTTP by default; restore an explicit
  opt-in flag so `doctor` stays usable offline.
- Check `mcpbrain/fleet_cli.py:12` and `bin/relocate_ingest_cache.py:31`, which
  now inherit the 60 s read timeout; pass `DEFAULT_HTTP_TIMEOUT_S` if either
  moves large cache blobs.

- [ ] **Step 7: Run and commit**

Run: `uv run pytest tests/test_embed_locking.py tests/test_store_write_txn.py tests/test_doctor.py tests/test_action_ttl.py -q && uv run ruff check mcpbrain/`

```bash
git add mcpbrain/embed.py mcpbrain/store.py mcpbrain/doctor.py mcpbrain/fleet_cli.py bin/relocate_ingest_cache.py tests/test_embed_locking.py
git commit -m "fix: unblock recall from ingest, cap write retries, stop masking errors

The new embedder lock serialised embed_query behind embed_passages, putting
recall behind ingest — the opposite of a stated goal. Write retries could reach
~31s on the recall path. Rollback masked SQLITE_FULL, which is how the live
disk-full surfaced as a misleading 'no transaction is active'."
```

---

### Task 7: Config, and the snapshot orphan sweep

**Files:** `mcpbrain/config.py`, `mcpbrain/daemon.py`, `mcpbrain/backup.py`
**Test:** `tests/test_snapshot_orphans.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_snapshot_orphans.py`:

```python
"""Killed backups leave mcpbrain-snap-* temp dirs; ~24GB accumulated live."""
from mcpbrain import backup


def test_sweep_removes_stale_snapshot_dirs(tmp_path):
    old = tmp_path / "mcpbrain-snap-abc"
    old.mkdir()
    (old / "part.bin").write_bytes(b"x" * 16)
    keep = tmp_path / "unrelated"
    keep.mkdir()
    removed = backup.sweep_orphan_snapshots(tmp_path, max_age_s=0)
    assert removed == 1
    assert not old.exists() and keep.exists()


def test_sweep_spares_recent_dirs(tmp_path):
    fresh = tmp_path / "mcpbrain-snap-def"
    fresh.mkdir()
    assert backup.sweep_orphan_snapshots(tmp_path, max_age_s=3600) == 0
    assert fresh.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_snapshot_orphans.py -q`
Expected: FAIL — `sweep_orphan_snapshots` does not exist.

- [ ] **Step 3: Implement the sweep and call it at startup**

`make_encrypted_snapshot` cleans up in a `finally`, which cannot run when the
process is killed mid-backup — exactly what the watchdog now does deliberately.
Add `sweep_orphan_snapshots(parent, *, max_age_s)` to `backup.py`, removing
`mcpbrain-snap-*` directories older than `max_age_s`, and call it once from
`run()` before the loop, against the same temp parent
`make_encrypted_snapshot` uses (note: `/var/folders/...`, **not** `$HOME` —
that is where the 24 GB was found).

- [ ] **Step 4: Make the tuning constants config-overridable**

`CYCLE_BUDGET_S`, `MAINTENANCE_TICK_S`, `STALL_S`, `BULK_LOCK_ACQUIRE_S`,
`BULK_LOCK_YIELD_S`, `WATCHDOG_MAX_EXITS`, `WATCHDOG_WINDOW_S`,
`HANDOVER_LOCK_WAIT_S` and `embed_max_items` are module constants. Read each
through `config.read_config(home)` with the current value as the default,
following the `_cadences_from_config` pattern, so a wedged install can be tuned
without a release.

- [ ] **Step 5: Document the paused-behaviour change**

Cadence passes previously ran inline regardless of pause; they now skip while
paused. That is defensible but unspecified — add it to the spec's data-flow
section and to `resume()`'s docstring.

- [ ] **Step 6: Run and commit**

Run: `uv run pytest tests/test_snapshot_orphans.py tests/test_backup.py tests/test_daemon.py -q && uv run ruff check mcpbrain/`

```bash
git add mcpbrain/backup.py mcpbrain/config.py mcpbrain/daemon.py docs/superpowers/specs/2026-07-27-daemon-scheduling-design.md tests/test_snapshot_orphans.py
git commit -m "feat(backup): sweep orphaned snapshot temp dirs; make tuning configurable

make_encrypted_snapshot cleans up in a finally that cannot fire when the process
is killed mid-backup — ~24GB of mcpbrain-snap-* accumulated in /var/folders and
filled the disk. Now swept at startup. Tuning constants become config-readable
so a wedged install can be adjusted without a release."
```

---

### Task 8: Close the remaining coverage gaps and re-run acceptance

**Files:** tests only, plus spec acceptance section.

- [ ] **Step 1: Fix the tests that prove the wrong thing**

- `tests/test_store_write_txn.py::test_write_txn_rolls_back_on_error` passes
  unchanged with `write=True` reverted — it tests `with db:`, not this change.
  Rewrite it to assert an IMMEDIATE-specific property, or delete it as
  redundant.
- `tests/test_daemon_thread_safety.py`'s lock-existence test inspects
  `__init__.__code__.co_names`, so it passes if the locks are created but never
  used. Replace with a behavioural assertion. Remove its unused `tmp_path`.
- `tests/test_daemon_watchdog.py`'s seeding test re-implements the production
  line rather than calling `__init__`; make it construct a real Daemon.
- Delete the constant/table restatements in `test_maintenance_scheduler.py` and
  `test_auth_timeouts.py` that assert values against themselves.

- [ ] **Step 2: Add the missing coverage**

One test each, in the natural file: `_begin_immediate` retry/backoff actually
retrying (force a held lock); `PRAGMA journal_size_limit` applied;
`run_sync_cycle`'s inter-source `budget_spent` early return; **budget expiry
mid-phase** (both current tests use a pre-expired `Budget(0.0)`, so the
between-batches `break` never executes — use a clock that expires after the
first batch); `run_one`'s `Budget(CYCLE_BUDGET_S)` + `on_progress` wiring;
`run()`'s `_wake.wait(1.0 if more …)` re-wake; `Daemon._embedder`
double-checked locking; `main()`'s `probe.acquire(timeout_s=…)`;
`SingleWriterLock.acquire` retry on the non-fcntl branch; `dashboard.stats`
eligible/queued arithmetic.

- [ ] **Step 3: Full suite and lint**

Run: `uv run pytest -q && uv run ruff check mcpbrain/`
Expected: all pass (baseline before this plan: 2580 passed, 1 skipped), clean.

- [ ] **Step 4: Live acceptance — the criteria that actually failed**

```bash
uv tool install --reinstall --no-cache ".[daemon]"
launchctl kickstart -k gui/$(id -u)/com.mcpbrain
```

Then verify, over at least 15 minutes:

- `daemon_heartbeat.json` advances **at least 5 times** — it never advanced once
  on the reviewed build;
- **each of the four gated passes runs at least once**
  (`grep -E "salience_score: scored|decay_pass: evaluated|consolidation: notes_written|stale_reextract"`)
  — none ran on the reviewed build across 183 skip warnings;
- skip warnings are occasional, not every tick;
- `/api/recall` p95 under active embedding stays under ~3 s with no
  `BrokenPipeError`;
- the enrichment queue refills (`ls enrich_queue/units/ | wc -l` non-zero and changing);
- `uv run python tests/eval/run_eval.py --gold --k 10` holds at or above
  recall@10 0.700 / MRR 0.511;
- disk free does not fall materially during a backup cycle, and no
  `mcpbrain-snap-*` directories survive it.

- [ ] **Step 5: Rewrite the spec's acceptance section**

The existing section records the four passes skipping every tick as success.
Replace it with the measured results from Step 4, and state plainly that the
earlier run did not meet the criteria.

- [ ] **Step 6: Commit**

```bash
git add tests/ docs/superpowers/specs/2026-07-27-daemon-scheduling-design.md
git commit -m "test: close coverage gaps; record honest acceptance results"
```

---

## Issue index

Every reviewed finding and where it is addressed.

| Source | Issue | Task |
|---|---|---|
| Live | `prepare_units` unbounded; cycle never completes | 2 |
| Live / C3 / M4 | Four gated passes starved every tick | 2 |
| C1 | Watchdog kills daemon mid-backup; `os._exit` orphans temp dirs | 3, 7 |
| C2 | `watchdog_exits.json` persists monotonic; breaks after reboot | 3 |
| C4 | `_pending_*` cross-thread lost update | 4 |
| C5 | `_embedder_lock` unbounded from maintenance thread | 4 |
| C6 | `_pending_update` leaves maintenance running → two writers | 4 |
| C7 / M6 | Raising cycle read as a stall | 3 |
| C8 | Recall serialised behind ingest | 6 |
| C9 | `ROLLBACK` masks SQLITE_FULL | 6 |
| C10 / M5 | ~31 s worst-case write on recall path | 6 |
| C11 | `dry_run` takes the write lock | 6 |
| C12 / L14 | `status()` unlocked `_progress`; `stalled` while paused | 3 |
| C13 / L8 | `_stash_take` dead code | 4 |
| C14 | Budget checked between sources, not pages | 2 |
| C15 / S2 | `os._exit` structurally reachable in tests | 1 |
| H1 | XML `<Exec>` cannot launch a `.vbs` | 5 |
| H2 | Non-waiting shim ⇒ `RestartOnFailure` never fires | 5 |
| H3 | XML likely rejected; `check=True` aborts install | 5 |
| M7 | Watchdog dies with the maintenance thread; no join | 3, 4 |
| L9 | `schtasks_xml(home=…)` unused | 5 |
| L10 | `OMP_NUM_THREADS` set too late | 6 |
| L11 | Tuning constants not config-overridable | 7 |
| L12 | Passes no longer run while paused (unspecified) | 7 |
| L13 | `doctor` does live HTTP by default | 6 |
| — | `fleet_cli` / `relocate_ingest_cache` inherit 60 s timeout | 6 |
| — | Orphaned `mcpbrain-snap-*` sweep | 7 |
| S1 | `run()` wiring untested; regression reintroducible | 1 |
| S3 | Real `_stash_lock` blocks uncovered | 1, 4 |
| S4 | Tests proving something other than their claim | 8 |
| S5 | Coverage gaps | 8 |
| S6 | `join()` without timeout | 1 |

## Notes for the implementer

- Task 1 first, always: it disarms `os._exit` in tests and pins the regression
  before anything else moves.
- Task 2 is the one that decides whether this work was worth doing. If the four
  gated passes still do not run under a real backlog, stop and reassess the
  fairness approach rather than tuning constants.
- Do **not** touch ingestion (`prepare.py`'s extraction logic, chunking,
  extractors) beyond adding the `budget` parameter in Task 2 — that is specs 2
  and 3.
- Nothing here is pushed. Do not push or release; Josh decides both.
- **Expect backup failures during Task 8 acceptance — they are pre-existing, not
  yours.** The live daemon logged `periodic backup failed: The write operation
  timed out` twice on 2026-07-27 *after* disk was freed to 54 GB, and its last
  successful backup was 2026-07-23. This is NOT the Task 6 timeout split: the
  backup path correctly passes `drive_timeout_s=DEFAULT_HTTP_TIMEOUT_S`
  (`daemon.py:2801`). The cause is snapshot size — the store is 11.9 GB, a large
  share of it the 66,653 content-free chunks documented in
  `2026-07-27-ingestion-defects-findings.md`, and four days of missed backups
  meant more to ship. Record it in the acceptance notes; do not try to fix it
  here. The real fix is the ingestion cleanup in specs 2/3, after which a
  re-measure will show whether 600 s is still too tight.
