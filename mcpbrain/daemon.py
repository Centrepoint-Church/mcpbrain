"""Daemon orchestration loop with a single-writer lock.

The daemon is the sole WRITER of the store: it runs sync -> embed cycles on an
interval, with pause/resume and a "sync now" wake. The MCP server reads the same
store read-only. A POSIX advisory lock enforces single-instance writing so two
daemons can never touch the store at once.

Why single-writer matters: a 2026-05-31 ops-brain decision recorded a
ProcessPool/SQLite fork-deadlock. The lesson was that exactly one writer may
touch the store. This module's lock enforces that.

Carry-forwards (out of scope for Task 3.1):
- Enrich-in-loop: wired in Task H1. run_cycle now does sync -> embed -> enrich,
  gated by store.unenriched_chunks() (the enriched column lives in store.py, the
  sole schema owner). Tiered: no enrich_client -> defer no-op.
- Backup-in-loop: wired in Task H2. maybe_backup() runs each loop iteration and
  self-gates: OFF unless a BackupConfig is supplied (escrow key + injected Drive
  service + Shared Drive id + user_id), then time-based via an injected clock. It
  captures page deltas into the local outbox (backup_outbox); upload_backup_step
  ships them each iteration (backup_uploader, backup_prune). A backup failure is
  logged and swallowed so it never crashes the loop.
- Windows lock: SingleWriterLock uses msvcrt.locking on Windows (Task H3).
  The Windows branch is marked # pragma: no cover and verified at Phase 6
  packaging on a Windows box.
"""

from __future__ import annotations

try:
    import fcntl  # POSIX
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt  # Windows
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from mcpbrain import auth, backup, backup_restore, config, control_api, drain, graph_write, prepare
from mcpbrain import backup_outbox, backup_prune, backup_uploader, drive_resumable
from mcpbrain.agents import win_supervised
from mcpbrain.backup_state import record_discard, write_backup_state  # noqa: F401 — re-exported; moved there
from mcpbrain.budget import Budget
from mcpbrain.config import app_dir
from mcpbrain.retrieval import FUSION_DEPTH, hybrid_search
from mcpbrain.sync import run_sync_cycle
from mcpbrain import onboarding

# Import block modules at startup so their BLOCK_DRAINERS entries are registered
# before the first drain pass. All four imports are intentional side effects.
import mcpbrain.profile_synth   # noqa: F401 — registers BLOCK_DRAINERS["profile_synthesis"]
import mcpbrain.community_synth  # noqa: F401 — registers BLOCK_DRAINERS["community_synthesis"]
import mcpbrain.memory_distil    # noqa: F401 — registers BLOCK_DRAINERS["memory_distil"]
import mcpbrain.profile_audit    # noqa: F401 — registers BLOCK_DRAINERS["profile_audit"]

log = logging.getLogger(__name__)

# see mcpbrain.backup._NUM_RETRIES for the full rationale — googleapiclient's
# own num_retries already retries this error class (SSL errors, socket
# timeouts, ConnectionError/OSError) with randomized exponential backoff, so
# every plain Google API call in this module passes it. A local constant
# rather than importing backup's keeps the established per-module convention
# (sync/gmail.py, sync/drive.py, fleet.py all define their own).
_NUM_RETRIES = 5


def _configure_logging(root=None):
    """Configure logging format/level and, on Windows, a rotating log file.

    macOS launchd captures stdout/stderr to the plist's StandardOutPath; a
    hidden-console schtasks launch captures nothing, so Windows additionally
    attaches a RotatingFileHandler so daemon crashes are visible.
    """
    tgt = root if root is not None else logging.getLogger()
    tgt.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream_h = logging.StreamHandler()
    stream_h.setFormatter(fmt)
    tgt.addHandler(stream_h)
    if sys.platform == "win32":
        import logging.handlers as _lh
        log_path = config.app_dir() / "com.mcpbrain.log"
        rfh = _lh.RotatingFileHandler(str(log_path), maxBytes=1_000_000, backupCount=3)
        rfh.setFormatter(fmt)
        tgt.addHandler(rfh)

EMBED_BACKEND = "fastembed:bge-small:v1"
DEFAULT_BACKUP_INTERVAL_S = 3600
# After a FAILED backup, the next attempt is due after this, doubling per
# consecutive failure, never later than the configured interval. Before it a
# failure waited out the whole interval (86400s here): live 2026-10-15..18, a
# flaky link failed four daily attempts and left the store five days without a
# backup, which in turn held the reflow seed's 24h backup gate shut. An hour
# still bounds a backup that can never succeed to a handful of attempts a day,
# not the every-cycle re-snapshot of the 2026-08-04 incident.
BACKUP_RETRY_BASE_S = 3600
# An install whose outbox stays empty (nothing changes, nothing commits) still
# runs one prune pass this often, so chains past retain_days do not sit on
# Drive forever just because no new commit triggered the post-commit prune.
IDLE_PRUNE_INTERVAL_S = 86400
_CLICKUP_SYNC_INTERVAL_S: float = 300.0

# Spool prepare bounds. The per-cycle thread ceiling now lives in config
# (config.spool_thread_cap, default 2000) so it can be tuned live for backfill
# without a daemon restart. char_budget splits an over-long thread before the
# extractor sees it.
SPOOL_CHAR_BUDGET = 24000

# Per-cycle ceiling on how many already-enriched chunks are re-flowed for
# re-extraction under newer enrichment logic. A trickle so the existing corpus
# re-extracts in the background without swamping new-mail enrichment or token cost.
REEXTRACT_CAP = 50

# Wall-clock slice for one bulk-work cycle. The loop must always reach the
# bottom: maintenance, the enrichment producer and the heartbeat all live after
# run_one(), and an unbounded cycle starved them for four days (2026-07-23..27).
CYCLE_BUDGET_S = 60.0

# Discovery's slice of CYCLE_BUDGET_S. Discovery only LISTS (no fetch), so it
# is cheap and normally finishes well inside this; the slice exists so a large
# work queue can never starve discovery of new changes. Unlike the budgets this
# design replaces, it is a fairness knob, not a correctness mechanism -- a
# cutoff on either side of it now costs nothing.
DISCOVERY_BUDGET_S = 15.0

# Separate, independent wall-clock slice for drain_captures — the ONLY
# consumer of the MCP write-tool spool (brain_note/brain_decision/
# brain_memory_write/brain_action_create). It must NOT share CYCLE_BUDGET_S:
# that budget is constructed once per cycle and threaded through
# run_sync_cycle first, so on any cycle with a sync backlog (the live-store
# normal case per the CYCLE_BUDGET_S incident above) it is already fully
# spent by the time drain_captures would run, applying zero queued envelopes
# for as long as the backlog persists — silently contradicting the "queued...
# within ~a minute" contract elsewhere in the codebase. The capture spool is
# small and user-paced (not a bulk-sync-sized backlog), so a short budget of
# its own is enough while still bounding a pathological capture backlog.
CAPTURES_BUDGET_S = 10.0

# Separate, independent wall-clock slice for prepare.prepare_units (the
# enrichment PRODUCER) and drain.drain (the enrichment CONSUMER) -- the exact
# same defect CAPTURES_BUDGET_S was created to fix (see its comment above),
# just found later (final whole-branch review, Important I-4): both were
# still threaded the SAME `budget` object run_sync_cycle had already spent,
# so on any cycle with a sync backlog that budget is already expired by the
# time they run. Confirmed live: prepare_units was entered exactly twice in a
# full day of daemon operation and logged "budget spent after 0 threads"
# BOTH times -- including a fresh daemon's very first cycle, before any
# backup had even run -- so the enrichment queue silently stopped refilling
# for as long as any sync backlog persisted (the real, structural cause
# behind the "enrichment queue refilling" acceptance criterion not showing
# movement in Task 8's live run, not merely a session artifact of the
# backup issue as first recorded there). Each call site gets its OWN fresh
# Budget(ENRICH_SPOOL_BUDGET_S) (not a shared object between the two,
# either) so prepare_units running long can't itself starve drain.drain's
# slice within the same cycle. Sized like CYCLE_BUDGET_S rather than
# CAPTURES_BUDGET_S's small user-paced slice: prepare_units/drain.drain
# process the bulk thread/inbox backlog this whole plan is about, not a
# small captures spool.
ENRICH_SPOOL_BUDGET_S = 60.0

# Tick interval for the maintenance thread (Daemon._maintenance_loop). Each
# cadence pass still self-gates on its own interval, so a tick is cheap; this
# just bounds how promptly a newly-due pass is noticed.
MAINTENANCE_TICK_S = 60.0

# How long a cadence pass will wait for _bulk_lock before giving up for this
# tick. MUST be bounded and MUST stay well under MAINTENANCE_TICK_S: the cycle
# thread holds _bulk_lock for the whole of run_one(), so a wedged cycle holds it
# indefinitely. An unbounded `with self._bulk_lock:` in the dispatch loop parks
# the maintenance thread inside _run_periodic_passes forever, which also parks
# the _note_progress + watchdog check that follow it in _maintenance_loop --
# i.e. the self-healing watchdog would be unreachable during exactly the stall
# it exists to detect. Skipping the pass is free: it stays due and retries next
# tick.
BULK_LOCK_ACQUIRE_S = 5.0

# Brief pause the cycle thread takes, after releasing _bulk_lock between units
# of work, when another thread has signalled intent to acquire it (see
# Daemon._bulk_lock_intent / _bulk_lock_wanted). CPython locks are not
# FIFO-fair, so without this the cycle thread -- which re-acquires almost
# immediately -- wins nearly every race and the four gated passes never get a
# turn at all (183 consecutive skip warnings, live).
BULK_LOCK_YIELD_S = 0.25

# Zero progress for this long means the cycle is wedged, not merely slow. A
# sampled main thread once sat in _ssl__SSLSocket_read at 0% CPU for 1h44m.
STALL_S = 1800.0
WATCHDOG_MAX_EXITS = 3
WATCHDOG_WINDOW_S = 6 * 3600.0

# Reflow seed (extraction-fidelity, 2026-09-24): the sync_queue window the
# seed cadence tops up to, and how fresh a backup must be before it seeds at
# all (a reflow rewrites chunk rows -- it must never run further ahead of a
# recovery point than this).
REFLOW_WINDOW = 200
REFLOW_BACKUP_MAX_AGE_S = 86400.0
# Bound on the seed's `remaining` count (a visibility figure, not a limit).
REFLOW_REMAINING_CAP = 20000

# Brief bounded wait for the single-writer lock on daemon startup, so a
# successor spawned by the watchdog (unsupervised Windows) does not fail
# outright while its slow-exiting parent still holds the lockfile.
HANDOVER_LOCK_WAIT_S = 2.0

# Orphaned mcpbrain-snap-* work dirs (see backup.sweep_orphan_snapshots) older
# than this are swept -- at startup, and again every loop iteration inside
# _backup_under_bulk_lock. Generous on purpose: a snapshot in genuine progress
# is bounded by however long checkpoint+copy+encrypt takes (minutes, not
# hours) for any corpus size seen live, so an hour of headroom can only ever
# catch a truly orphaned dir, never a live one.
SNAPSHOT_ORPHAN_MAX_AGE_S = 3600.0

# Default cap on chunks embedded per index_pending call within one cycle (see
# run_cycle's/run_sync_cycle's own "embed_max_items" docs for the more_work
# implication). sync.run_sync_cycle independently keeps its own "2000"
# keyword default for direct/self-contained callers of that function -- this
# constant is daemon.py's single source of truth for every OTHER place that
# same default value is needed, so those spots can't drift from each other.
EMBED_MAX_ITEMS = 2000

# Retry schedule for a Google build (sync services, the backup config, the
# org-config merge) that failed TRANSIENTLY -- the network's fault, not the
# token's. Live 2026-10-09: the daemon was bootstrapped while
# oauth2.googleapis.com was briefly unreachable (a just-finished drain had
# exhausted local ephemeral ports, Errno 49). All three builds resolved
# "permanently" to nothing on that one failure, so the daemon ran for three days
# with no Gmail/Drive/Calendar sync, no backup, and the reflow (gated on a
# backup < 24 h old) frozen -- while doctor, which checks the token itself, said
# "Google Verified". A restart fixed everything. A transient failure now retries
# on this backoff (60 s, doubling, capped at 15 min) instead; a PERMANENT one
# (no token, revoked grant, missing scope, a programming error) still resolves
# once, exactly as before. The backoff sets the EARLIEST retry time: services
# are re-tried by whichever cycle or maintenance pass next asks for them, but the
# backup / org-config / pending-restore retries are checked once per run-loop
# iteration, so they land at the first iteration after it (~interval_s, 300 s
# by default).
GOOGLE_RETRY_FIRST_S = 60.0
GOOGLE_RETRY_MAX_S = 900.0

# Restore-on-first-run vs a transient outage. The reinstall-recovery case (an
# empty store with backup configured) is exactly when a login agent can start
# before Wi-Fi. If the restore is skipped and the daemon carries on, sync refills
# the empty store and the next backup uploads a near-empty snapshot; pruning to
# `retain` then rotates out every good snapshot, and once chunk_count() > 0 the
# restore never runs again. So: run() retries the restore BEFORE any writer
# starts, for up to RESTORE_STARTUP_WAIT_S; past that the daemon HOLDS -- no
# sync, no content-writing pass, no backup -- so the store stays empty, and
# RESTORE_PENDING_MARKER records the hold across restarts. The store is never
# overwritten once it has content.
RESTORE_STARTUP_WAIT_S = 1800.0
# Cadence passes that still run during the restore hold: none of them writes
# store content (they update the install, check probes, cap logs, install OCR,
# sweep dead MCP heartbeats).
_RESTORE_HOLD_PASSES = frozenset({"auto_update", "verify", "log_cap", "ocr_setup",
                                  "mcp_heartbeat_sweep"})
RESTORE_PENDING_MARKER = "restore_pending.json"


def _is_transient_google_error(exc) -> bool:
    """The reflow handler's network/rate-limit classifier, reused rather than
    duplicated: one definition of "the network's fault, not the token's".
    Imported lazily -- reflow_handler pulls in the sync stack."""
    from mcpbrain.sync.reflow_handler import _is_transient
    return _is_transient(exc)


class _GoogleBackoff:
    """Retry state for ONE Google-dependent build that failed transiently.

    `pending` while a retry is scheduled; `due()` once its retry time has
    passed (always True when nothing is pending). `failed(exc)` schedules the
    next retry, `succeeded()` clears it and logs the recovery once, `clear()`
    clears it silently (the build failed PERMANENTLY, which is reported by the
    build itself). `build_lock` single-flights the build across the cycle,
    maintenance and control-API threads: a caller that finds it held does not
    wait for someone else's network round-trip.
    """

    def __init__(self, what: str, clock=time.monotonic, wall_clock=time.time):
        self._what = what
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self.build_lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.attempts = 0
        self.retry_at: float | None = None
        self.since: str | None = None
        self.last_error = ""
        self.last_at = 0.0

    @property
    def pending(self) -> bool:
        return self.retry_at is not None

    def due(self) -> bool:
        with self._lock:
            return self.retry_at is None or self._clock() >= self.retry_at

    def failed(self, exc) -> None:
        from datetime import datetime, timezone
        with self._lock:
            self.attempts += 1
            delay = min(GOOGLE_RETRY_FIRST_S * (2 ** min(self.attempts - 1, 16)),
                        GOOGLE_RETRY_MAX_S)
            self.retry_at = self._clock() + delay
            now = self._wall_clock()
            if self.since is None:
                self.since = datetime.fromtimestamp(now, timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
            self.last_error = str(exc)[:200]
            self.last_at = now
            attempts = self.attempts
        log.warning("%s: Google unreachable (transient, attempt %d): %s -- will retry "
                    "in %.0fs", self._what, attempts, exc, delay)

    def succeeded(self) -> None:
        with self._lock:
            failures = self.attempts
            self._reset()
        if failures:
            log.info("Google services restored after %d attempt(s) (%s)",
                     failures + 1, self._what)

    def clear(self) -> None:
        with self._lock:
            self._reset()

    def snapshot(self) -> dict | None:
        with self._lock:
            if self.retry_at is None:
                return None
            return {"what": self._what, "since": self.since, "attempts": self.attempts,
                    "last_error": self.last_error, "last_at": self.last_at}


def _graph_apply():
    """Resolve Phase 1's graph_write.apply through an indirection seam.

    graph_write has landed (imported at module top), so this returns the real
    apply directly. The seam is kept as the monkeypatch surface that
    tests/test_run_cycle_modes.py patches to a stub.
    """
    return graph_write.apply


_OCR_MARKER = "ocr_install_attempted.json"


def run_ocr_setup(home: str) -> dict:
    """Install the tesseract OCR binary once, in the background. Never raises.

    Scanned, image-only PDFs have no text layer, so OCR is the only way to read
    them — and those skew towards signed contracts, letters and invoices. This ran
    inside `mcpbrain setup` before the wizard opened, which put a multi-minute
    package install directly in front of a waiting user.

    Attempted exactly ONCE, recorded in a marker file. A daily retry of a package
    install that already failed (no Homebrew, no winget, Linux) is noise;
    `mcpbrain doctor --repair` is the deliberate retry.

    The marker is stamped BEFORE the install starts, not just after it
    completes. This runs on a background thread (see Daemon._run_ocr_setup)
    that a watchdog/auto-update restart can kill mid-install via os._exit --
    an untracked subprocess.run(..., timeout=600) gets no chance to finish or
    report anything. Without the early stamp, a killed-mid-install successor
    process sees no marker, is due again on its very next tick, and launches a
    SECOND `brew install`/`winget install` against the same package-manager
    state as the orphaned first one. Recording "started" first means a restart
    mid-install is treated the same as a completed attempt -- never
    auto-retried, consistent with "attempted exactly once"; `doctor --repair`
    remains the deliberate retry either way.
    """
    import datetime as _dt
    from mcpbrain import ocr

    marker = Path(home) / _OCR_MARKER
    if ocr.tesseract_available():
        return {"status": "present"}
    if marker.exists():
        return {"status": "already_attempted"}

    def _stamp(payload: dict) -> None:
        try:
            marker.write_text(json.dumps({
                **payload,
                "attempted_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }, indent=2))
        except OSError as exc:
            log.warning("could not write the OCR marker: %s", exc)

    _stamp({"ok": None, "detail": "install started"})
    ok, detail = ocr.install_tesseract()
    _stamp({"ok": bool(ok), "detail": detail})
    log.info("ocr_setup: ok=%s detail=%s", ok, detail)
    return {"status": "ok" if ok else "skipped", "detail": detail}


@dataclass
class BackupConfig:
    """Config for the daemon's incremental encrypted backup.

    Supplying a BackupConfig is what TURNS BACKUP ON — the daemon never backs up
    when backup is None. Holds the escrow key, an INJECTED Drive service (so
    tests mock it; no real network), the Shared Drive id and per-user folder
    name (`user_id` now only names a legacy folder), the `backup` config block
    as `backup_outbox.Settings`, and the resumable-upload `transport` built
    from the same credentials as `drive_service`. `transport` may be None: the
    daemon then builds it on first use (`_build_backup_transport`).

    Capture writes page deltas into the local outbox (`backup_outbox`); the
    uploader ships them (`backup_uploader`); nothing is written to a single
    `snapshot.enc` any more.
    """

    key: bytes
    drive_service: object
    shared_drive_id: str
    user_id: str
    settings: backup_outbox.Settings = field(default_factory=backup_outbox.Settings)
    transport: object | None = None


@dataclass
class CadencePass:
    """Descriptor for a single periodic maintenance pass.

    Each entry in _CADENCE_PASSES maps a named pass to the instance attributes
    that hold its interval and last-run timestamp, and to the _run_X method that
    executes it. needs_configured gates graph-writing passes on config.is_configured;
    needs_backfill_clear is reserved for the dispatch loop (auto_update and verify
    run even during backfill).
    """

    name: str
    interval_attr: str
    last_attr: str
    fn_name: str
    needs_configured: bool = True
    needs_backfill_clear: bool = True
    needs_bulk_lock: bool = False


_CADENCE_PASSES: tuple[CadencePass, ...] = (
    CadencePass("auto_update", "_auto_update_interval_s", "_last_auto_update",
                "_run_auto_update", needs_configured=False, needs_backfill_clear=False),
    CadencePass("verify", "_verify_interval_s", "_last_verify",
                "_run_verify", needs_configured=False, needs_backfill_clear=False),
    # OCR binary install: a once-ever background attempt, gated on its own marker
    # (needs_configured=False — OCR is identity-agnostic and useful from the first
    # sync; needs_backfill_clear=False — installing a background binary has
    # nothing to do with whether a graph backfill is in progress, same
    # reasoning as auto_update/verify above). The interval only bounds how
    # soon after boot it is first tried; the marker is what makes it
    # once-ever.
    CadencePass("ocr_setup", "_ocr_setup_interval_s", "_last_ocr_setup",
                "_run_ocr_setup", needs_configured=False, needs_backfill_clear=False),
    # needs_configured=False: an unconfigured install still writes stderr, and it
    # is the one least likely to have anyone watching the disk.
    CadencePass("log_cap", "_log_cap_interval_s", "_last_log_cap",
                "_run_log_cap", needs_configured=False, needs_backfill_clear=False),
    CadencePass("communities", "_communities_interval_s", "_last_communities",
                "_run_communities"),
    CadencePass("lint", "_lint_interval_s", "_last_lint", "_run_lint"),
    CadencePass("synthesise", "_synthesise_interval_s", "_last_synthesise",
                "_run_synthesise"),
    CadencePass("proactive", "_proactive_interval_s", "_last_proactive",
                "_run_proactive"),
    CadencePass("waiting_on", "_waiting_on_interval_s", "_last_waiting_on",
                "_run_waiting_on"),
    CadencePass("blocks", "_blocks_interval_s", "_last_blocks", "_run_blocks"),
    CadencePass("audit", "_audit_interval_s", "_last_audit", "_run_audit"),
    CadencePass("stale_reextract", "_stale_reextract_interval_s",
                "_last_stale_reextract", "_run_stale_reextract", needs_bulk_lock=True),
    # S2 feedback aggregation: nightly Bayesian-smoothed CTR → chunk_quality.
    # needs_configured=False: feedback aggregation is identity-agnostic.
    CadencePass("feedback_aggregate", "_feedback_aggregate_interval_s",
                "_last_feedback_aggregate", "_run_feedback_aggregate",
                needs_configured=False),
    # Q4 org backfill: deterministic org_from_email over org-less entities.
    CadencePass("org_backfill", "_org_backfill_interval_s",
                "_last_org_backfill", "_run_org_backfill"),
    # Task 3.3: daily deterministic entity dedup (issue #23-fix validated).
    CadencePass("resolve_entities", "_resolve_entities_interval_s",
                "_last_resolve_entities", "_run_resolve_entities"),
    # Session-4: daily AI-adjudicated graph-hygiene review (build review units).
    CadencePass("review", "_review_interval_s", "_last_review", "_run_review"),
    # Daily action hygiene: age out long-dead actions + collapse duplicates.
    # needs_configured=False: sweeping our own actions table needs no identity.
    CadencePass("action_hygiene", "_action_hygiene_interval_s",
                "_last_action_hygiene", "_run_action_hygiene",
                needs_configured=False),
    # One-time-ish: drain the legacy doc_id-keyed enrich_payloads table left
    # aside by store.init(). Batched because the live table is 13.5GB and
    # deleting a row frees its overflow chain. needs_configured=False: draining
    # our own cache table needs no Google identity.
    CadencePass("enrich_payload_migration", "_enrich_payload_migration_interval_s",
                "_last_enrich_payload_migration", "_run_enrich_payload_migration",
                needs_configured=False, needs_bulk_lock=True),
    # Prune dead-pid mcp_heartbeat/<pid>.json files. Before this, the only thing
    # that pruned them was doctor.py's version_drift_line, and run_doctor has no
    # caller anywhere else -- so a dead MCP server's heartbeat file only got
    # cleaned up if a user manually ran `mcpbrain doctor`. Same unbounded-growth
    # shape as the ~24GB of orphaned mcpbrain-snap-* dirs found live on
    # 2026-07-27 (see backup.sweep_orphan_snapshots). needs_configured=False:
    # sweeping our own heartbeat files needs no Google identity.
    CadencePass("mcp_heartbeat_sweep", "_mcp_heartbeat_sweep_interval_s",
                "_last_mcp_heartbeat_sweep", "_run_mcp_heartbeat_sweep",
                needs_configured=False),
    # B3 salience scoring: structural importance per chunk.
    # needs_configured=False: salience is identity-agnostic.
    CadencePass("salience_score", "_salience_score_interval_s",
                "_last_salience_score", "_run_salience_score",
                needs_configured=False, needs_bulk_lock=True),
    # B5 decay pass: demote unaccessed low-salience chunks to cold tier.
    CadencePass("decay_pass", "_decay_pass_interval_s",
                "_last_decay_pass", "_run_decay_pass",
                needs_configured=False, needs_bulk_lock=True),
    # B4 consolidation: RAPTOR-style cluster+summarise episodic chunks.
    CadencePass("consolidation", "_consolidation_interval_s",
                "_last_consolidation", "_run_consolidation", needs_bulk_lock=True),
    # B6 voice analyser: weekly analysis-only procedural memory pass.
    CadencePass("voice_analyse", "_voice_analyse_interval_s",
                "_last_voice_analyse", "_run_voice_analyse"),
    # S4/S5 self-improvement: weekly drift check + bandit advisory + lessons.
    CadencePass("self_improve", "_self_improve_interval_s",
                "_last_self_improve", "_run_self_improve"),
    # Auto-graduation: flip data-gated flags (bandit/lessons/decay) ON when ready.
    CadencePass("auto_enable", "_auto_enable_interval_s",
                "_last_auto_enable", "_run_auto_enable"),
    # Org-baseline (Phase 0) cadences: registered as no-op stubs; subsystem B
    # fills the _run_* bodies.
    CadencePass("org_contrib_upload", "_org_contrib_upload_interval_s",
                "_last_org_contrib_upload", "_run_org_contrib_upload"),
    CadencePass("org_import", "_org_import_interval_s",
                "_last_org_import", "_run_org_import"),
    CadencePass("org_curate", "_org_curate_interval_s",
                "_last_org_curate", "_run_org_curate"),
    # Extraction-fidelity (2026-09-24): tops the reflow sync_queue up to
    # REFLOW_WINDOW. Gated on the kill switch, the halt flag, and a recent
    # backup -- see _run_reflow_seed.
    CadencePass("reflow_seed", "_reflow_seed_interval_s",
                "_last_reflow_seed", "_run_reflow_seed"),
)


class AlreadyRunningError(RuntimeError):
    """Raised when another daemon already holds the single-writer lock."""


class SingleWriterLock:
    """Advisory exclusive lock so only ONE daemon writes the store at a time.

    POSIX (fcntl available): uses ``fcntl.flock(LOCK_EX | LOCK_NB)`` on a
    lockfile under app_dir(). flock is released automatically when the process
    dies, so no stale-lock cleanup is needed.

    Windows (msvcrt available): uses ``msvcrt.locking(LK_NBLCK, 1)`` to lock
    byte 0 of the lockfile. The lockfile is opened in "r+b" mode (or created
    via "w+b" if it does not yet exist) so byte 0 always exists to lock.
    Verified at Phase 6 packaging on a Windows box.
    """

    def __init__(self, lock_path=None):
        self.lock_path = Path(lock_path) if lock_path is not None else app_dir() / "daemon.lock"
        self._fd = None

    def acquire(self, timeout_s: float = 0.0, interval_s: float = 0.1) -> None:
        """Take the lock, raising AlreadyRunningError if another daemon holds it.

        Non-blocking by default (timeout_s=0.0): one attempt, immediate raise —
        the semantics every existing caller relies on.

        `timeout_s` adds a brief bounded retry for ONE case: the watchdog's
        unsupervised-Windows handover, where _spawn_replacement starts the
        successor before the parent has finished exiting. Without it the
        successor races the parent's still-held lockfile and dies on the first
        attempt, leaving nothing running at all. Bounded on purpose — this is a
        handover window, not a queue; a genuinely-running second daemon must
        still lose quickly.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                self._acquire_once()
                return
            except AlreadyRunningError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(interval_s)

    def _acquire_once(self) -> None:
        if fcntl is not None:
            # POSIX path — current behaviour, fully tested on Linux/macOS.
            fd = open(self.lock_path, "w")
            try:
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError):
                fd.close()
                raise AlreadyRunningError(f"another daemon holds {self.lock_path}")
            self._fd = fd
        elif msvcrt is not None:  # pragma: no cover - Windows; verified at Phase 6
            # Windows path: msvcrt.locking is a byte-range lock (not advisory
            # like flock). It locks byte [0,1) of the file; that byte must
            # physically exist before calling locking() — on some Windows
            # versions locking past EOF raises OSError [Errno 22].
            # Use try/except to open r+b (existing file) or create via w+b
            # (new file), writing a sentinel byte so position 0 always exists.
            # This also removes the TOCTOU in the previous exists() check.
            try:
                fd = open(self.lock_path, "r+b")
                # A pre-existing but EMPTY lockfile (zero bytes) would make
                # locking() lock past EOF and raise OSError on some Windows
                # versions. Guarantee byte 0 exists on this path too.
                fd.seek(0)
                if not fd.read(1):
                    fd.seek(0)
                    fd.write(b"\x00")
                    fd.flush()
            except FileNotFoundError:
                fd = open(self.lock_path, "w+b")
                fd.write(b"\x00")
                fd.flush()
            try:
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                fd.close()
                raise AlreadyRunningError(f"another daemon holds {self.lock_path}")
            self._fd = fd
        else:
            raise RuntimeError("no file-locking backend available (neither fcntl nor msvcrt found)")

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows; verified at Phase 6
                self._fd.seek(0)
                msvcrt.locking(self._fd.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fd.close()
            self._fd = None

    def __enter__(self) -> "SingleWriterLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _gated_enrich_mode(mode: str, home: str) -> str:
    """Force enrichment OFF until the install is configured (identity + ≥1 org).

    Sync/index are identity-agnostic and still run every cycle; only enrichment —
    which writes owner identity and org taxonomy into the graph — is gated. "off"
    stays "off"; any other mode passes through only once config.is_configured.
    """
    if mode == "off":
        return "off"
    return mode if config.is_configured(home) else "off"


def _stamp_enrich_log(drained: dict) -> None:
    """Append a line to logs/enrich.log on a productive drain. This is the ONLY
    writer of that file — the enrichment health probe (probes.probe_enrichment)
    reads its mtime to tell 'Running' from 'Idle'. Without this stamp the probe is
    stuck on Idle no matter how much enrichment lands. Best-effort; never raises."""
    import datetime as _dt
    try:
        logdir = app_dir() / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        line = (f"{ts} drain applied={drained.get('applied', 0)} "
                f"marked={drained.get('marked', 0)} files={drained.get('files', 0)} "
                f"merges={drained.get('merges', 0)}\n")
        with (logdir / "enrich.log").open("a") as f:
            f.write(line)
    except OSError:
        pass


def run_cycle(store, embedder, *, gmail_service=None, calendar_service=None,
              drive_service=None, enrich_client=None,
              enrich_limit: int | None = None,
              enrich_mode: str = "off", resolution_due: bool = False,
              synthesis_requests: list | None = None,
              extra_blocks: dict | None = None, budget=None,
              on_progress=None, bulk_section=None,
              embed_max_items: int = EMBED_MAX_ITEMS) -> dict:
    """One sync -> embed -> enrich cycle.

    Sync each provided source and embed via run_sync_cycle (the tested core),
    then enrich according to enrich_mode:

      - "spool": prepare.prepare_units writes immutable work units to
        enrich_queue/units/, then drain.drain applies whatever an out-of-band
        extractor session has pushed to enrich_inbox/ since last cycle.
        run_cycle does NOT call the extractor itself. resolution_due gates the
        merge-review block in prepare, so it is appended exactly when the
        deterministic resolve tier would also fire.
      - "off": skip enrichment entirely.

    enrich_mode defaults to "off" (matching config.enrich_mode's default), so a
    direct caller that forgets to pass a mode does NOT silently run any enrichment
    path. The live daemon resolves the real mode from config in run_one and passes
    it in explicitly.

    Returns the sync result dict ({"gmail","calendar","drive","embedded"}) plus
    an "enrich" key holding the chosen path's summary and a "more_work" bool.

    `budget` (a `Budget`, or None for unbounded) is threaded into run_sync_cycle
    so a large sync backlog yields the cycle instead of holding it for hours;
    `more_work` is True when this cycle stopped early with work still to do —
    either the budget expired OR a bounded phase hit its own item cap
    (`embed_capped` from run_sync_cycle) — telling the caller to re-wake
    promptly instead of waiting the full interval. `drain_captures`,
    `prepare.prepare_units` and `drain.drain` do NOT share this budget object —
    see `CAPTURES_BUDGET_S` / `ENRICH_SPOOL_BUDGET_S` for why each gets its own
    independent slice instead (reusing `budget` here silently starved all
    three on any cycle where run_sync_cycle had already spent it, which is the
    live-store NORMAL case on a backlog).

    `on_progress` (Task 5), if given, is called with `"sync"` right after
    run_sync_cycle returns — a free function has no `self`, so the daemon
    passes `self._note_progress` in as this callback rather than run_cycle
    calling it directly. This gives the watchdog a progress signal for the
    sync phase specifically, distinct from the "cycle" mark run_one records
    only once the whole sync+enrich cycle (this function's full body) returns.

    `bulk_section` (Task 2), if given, is a zero-arg context-manager factory --
    the daemon passes `self._cycle_bulk_section` (a free function again has no
    `self`). A soak test found that wrapping this whole function's chunk-
    mutating calls in ONE `_bulk_lock` hold (even though each was already
    `budget`-bounded to CYCLE_BUDGET_S=60s) still starved the maintenance
    thread's 5s-bounded acquire almost every time on a sustained backlog --
    the fix is a lock DUTY CYCLE problem, not a fairness problem. So
    `bulk_section` is instead threaded straight into `run_sync_cycle`,
    `prepare.prepare_units`, and `drain.drain`/`drain.drain_captures`, each of
    which brackets its OWN small units of work (one message/event/file/embed-
    batch/inbox-file) with it, and the single-statement
    `reflow_outdated_chunks` call below gets its own small section. Defaults
    to `contextlib.nullcontext` (a no-op) so direct callers/tests that don't
    pass one keep running unlocked, exactly as before.

    `embed_max_items` (Task 7) is threaded straight into `run_sync_cycle`,
    which caps each `index_pending` call at this many chunks per cycle (see
    that constant's own docstring for the "more_work" implication). Defaults
    to 2000, matching `run_sync_cycle`'s own default, so a direct caller that
    doesn't pass one behaves exactly as before; the live daemon resolves the
    configured value via `Daemon._embed_max_items` (see `_tuning_from_config`)
    and passes it in explicitly.
    """
    if bulk_section is None:
        bulk_section = nullcontext
    result = run_sync_cycle(
        store, embedder,
        gmail_service=gmail_service,
        calendar_service=calendar_service,
        drive_service=drive_service,
        home=str(config.app_dir()),
        budget=budget,
        bulk_section=bulk_section,
        embed_max_items=embed_max_items,
    )
    if on_progress is not None:
        on_progress("sync")
    try:
        # drain_captures writes `chunks` (upsert_chunk for "ingest" captures) --
        # the same table the four gated maintenance passes mutate -- so it gets
        # its own bulk_section threading (Task 2 race-safety fix; this call
        # used to run with no lock at all). It gets its OWN independent
        # budget (CAPTURES_BUDGET_S), NOT the shared `budget` that
        # run_sync_cycle just spent -- reusing the same (often already-
        # expired, on a live backlog) budget object would silently starve
        # the MCP write-tool spool for as long as the sync backlog persists.
        # See CAPTURES_BUDGET_S for the full reasoning.
        drain_caps = drain.drain_captures(
            store, budget=Budget(CAPTURES_BUDGET_S), bulk_section=bulk_section)
        if drain_caps:
            log.info("captures applied: %d", drain_caps)
            from mcpbrain.memory_index import regenerate
            regenerate(store, str(config.app_dir()))
    except Exception as exc:
        log.warning("capture drain failed (cycle continues): %s", exc)
    try:
        pruned = store.prune_change_log()
        if pruned:
            log.debug("change_log: pruned %d old rows", pruned)
    except Exception as exc:
        log.warning("change_log prune failed: %s", exc)
    try:
        from mcpbrain import agent_errs
        agent_errs.check_agent_errs(store, config.app_dir())
    except Exception as exc:
        log.warning("agent_errs scan failed (cycle continues): %s", exc)
    if enrich_mode == "spool":
        # Change-driven re-extraction: trickle already-enriched chunks that predate
        # the current enrichment logic back through the queue, unless paused.
        if config.reextract_enabled(str(app_dir())):
            from mcpbrain.store import ENRICH_LOGIC_VERSION
            try:
                # reflow_outdated_chunks resets `chunks.enriched` in ONE bulk
                # UPDATE (bounded by REEXTRACT_CAP) -- a single SQL statement,
                # not a Python loop, so bulk_section brackets the whole call
                # rather than sub-sectioning it further (Task 2 race-safety fix
                # -- this used to run with no lock at all, racing the same
                # column stale_reextract resets).
                with bulk_section():
                    reflowed = store.reflow_outdated_chunks(ENRICH_LOGIC_VERSION, REEXTRACT_CAP)
                if reflowed:
                    log.info("re-extraction: re-flowed %d outdated chunk(s)", reflowed)
            except Exception as exc:  # noqa: BLE001 — never crash the cycle
                log.warning("re-extraction sweep skipped: %s", exc)
        # prepare_units (the enrichment PRODUCER) and drain.drain (the
        # enrichment CONSUMER) each get their OWN independent budget, exactly
        # like drain_captures above (Important I-4, final whole-branch
        # review) -- and NOT the same fresh budget as each other, so one
        # running long can't itself starve the other's slice this cycle. The
        # SHARED `budget` above (already spent by run_sync_cycle on any
        # cycle with a sync backlog -- the live-store normal case) used to be
        # threaded into both: confirmed live, prepare_units logged "budget
        # spent after 0 threads" on both of its only two entries in a full
        # day, including a fresh daemon's very first cycle before any backup
        # had even run. See ENRICH_SPOOL_BUDGET_S.
        prep = prepare.prepare_units(store, thread_cap=config.spool_thread_cap(str(app_dir())),
                                     char_budget=SPOOL_CHAR_BUDGET,
                                     resolution_due=resolution_due,
                                     synthesis_requests=synthesis_requests,
                                     extra_blocks=extra_blocks,
                                     home=str(app_dir()),
                                     budget=Budget(ENRICH_SPOOL_BUDGET_S),
                                     bulk_section=bulk_section)
        drained = drain.drain(store, apply=_graph_apply(), embedder=embedder,
                              budget=Budget(ENRICH_SPOOL_BUDGET_S),
                              bulk_section=bulk_section)
        result["enrich"] = {"mode": "spool", "prepare": prep, "drain": drained}
        if drained.get("files") or drained.get("applied"):
            _stamp_enrich_log(drained)
    else:  # "off" (or any unknown value)
        result["enrich"] = {"mode": "off"}
    # More work is pending if EITHER the wall-clock budget ran out mid-cycle OR a
    # bounded phase stopped on its own item cap. Budget expiry alone was not
    # enough: index_pending caps each call at embed_max_items (2000), so a large
    # backlog embedded 2000 chunks well inside the budget, reported more_work
    # False, and the loop then slept the full _interval_s (300 s) before taking
    # the next slice.
    result["more_work"] = bool(
        (budget is not None and budget.expired()) or result.get("embed_capped"))
    return result


def _mac_user_is_active() -> bool:
    """True iff macOS reports a genuine, user-active wake right now — false
    during a brief DarkWake maintenance cycle (lid closed, network stack only
    partially re-established).

    Live incident (2026-09-05/06): two consecutive periodic-backup failures,
    both `[Errno 49] Can't assign requested address`, both landing at
    ~23:00-23:15 — cross-referenced against `pmset -g log`, that window is a
    repeating cycle of brief DarkWake/Sleep "Maintenance Sleep" wakes on this
    machine. EADDRNOTAVAIL from a long-lived HTTP client is the classic
    signature of attempting a connection in exactly that window, before the
    interface has finished coming back up. The eventual success landed ~25
    minutes after a genuine lid-open wake.

    `UserIsActive` (from `pmset -g assertions`' system-wide summary block) is
    the signal used, not the older `IODisplayWrangler` IORegistry technique:
    verified directly on this (Apple Silicon) hardware that `IODisplayWrangler`
    does not exist in the IORegistry at all — that check is Intel-era and
    would silently never detect anything on current Macs.

    Fails OPEN (returns True) on any error, non-mac platform, or unparseable
    output — a broken check must never PERMANENTLY block backups. The cost of
    failing open is at worst a repeat of the occasional Errno 49 this exists
    to reduce; failing closed could mean silently never backing up again if a
    future macOS changes `pmset`'s output shape.
    """
    import re
    import subprocess
    try:
        out = subprocess.run(["pmset", "-g", "assertions"],
                             capture_output=True, text=True, timeout=2)
        if out.returncode != 0:
            return True
        m = re.search(r"^\s*UserIsActive\s+(\d+)", out.stdout, re.MULTILINE)
        return True if m is None else m.group(1) == "1"
    except (OSError, subprocess.SubprocessError):
        return True


# Routed tools whose handler reads an out-of-band `confirmation` (see
# Daemon.call_tool). Every other routed handler still takes exactly one
# argument, so this stays a short allowlist rather than a signature probe.
_CONFIRMABLE_TOOLS = frozenset({"brain_graph_correct"})


class Daemon:
    """Owns the store-writing loop: sync -> embed on an interval, with
    pause/resume and a single-writer lock.

    Orchestration scope: wires sync -> embed -> enrich -> maybe_backup.
    Enrichment is tiered via enrich_client (None -> defer no-op). Periodic backup
    is tiered via backup (None -> OFF); when configured it self-gates on a
    time-based cadence using the injected clock. Entity resolution runs
    deterministic-only each cycle (LLM adjudication removed in §9A).

    Threading model: pause/stop/wake are threading.Event objects so the tray
    (Task 3.2) and tests can drive the daemon without real timers. run() blocks
    on _wake.wait(interval_s) so pause/sync_now/stop are responsive.
    """

    @property
    def _embedder(self):
        # Every internal reader uses self._embedder; routing them through this
        # property makes construction lazy with zero call-site changes. This is
        # also the ONLY lazy-build site (every automatic warm — sync/enrich/
        # search cycles — goes through it, not just the wizard's ensure_model()
        # button), so _model_building is set around exactly this span.
        if self._embedder_obj is None:
            with self._embedder_lock:
                # Re-check inside the lock: another thread may have finished
                # the build while this one was waiting to acquire it.
                if self._embedder_obj is None:
                    if self._embedder_factory is None:
                        raise RuntimeError("embedder unavailable (model not loaded yet)")
                    self._model_building = True
                    try:
                        self._embedder_obj = self._embedder_factory()
                    finally:
                        self._model_building = False
        return self._embedder_obj

    def _embedder_bounded(self, timeout: float | None = None):
        """Like the _embedder property, but with a BOUNDED acquire: returns
        None instead of blocking if the embedder is still being built and the
        wait exceeds `timeout` (default: `self._bulk_lock_wait_s`, config-
        overridable -- see _tuning_from_config -- so this stays in step with
        BULK_LOCK_ACQUIRE_S's config override rather than freezing the
        default at class-definition time).

        The _embedder property holds _embedder_lock for the WHOLE lazy build
        (a cold fastembed download takes minutes) -- fine for the cycle
        thread, which genuinely needs the model to proceed and has nothing
        safe to skip to, but _run_self_improve (a non-gated maintenance pass,
        called inline by _run_periodic_passes same as every other pass) calling
        the property directly would park the ENTIRE maintenance tick --
        _note_progress + the watchdog stall check that follow it in
        _maintenance_loop included -- behind someone else's in-flight model
        download. Same "an unbounded acquire parks the watchdog" shape
        BULK_LOCK_ACQUIRE_S already exists to prevent for _bulk_lock, mirrored
        here for _embedder_lock. Skipping is safe: the caller's own
        _is_due-gated _last_* attribute only advances on a successful run, so
        a skipped tick is retried next time that pass is due, never silently
        lost.

        A fast path avoids the lock entirely once the embedder is built (the
        normal case after the first cycle): a plain attribute read of an
        already-set reference needs no lock under the GIL.
        """
        if self._embedder_obj is not None:
            return self._embedder_obj
        if timeout is None:
            timeout = self._bulk_lock_wait_s
        if not self._embedder_lock.acquire(timeout=timeout):
            return None
        try:
            if self._embedder_obj is None:
                if self._embedder_factory is None:
                    return None
                self._model_building = True
                try:
                    self._embedder_obj = self._embedder_factory()
                finally:
                    self._model_building = False
            return self._embedder_obj
        finally:
            self._embedder_lock.release()

    def model_status(self) -> dict:
        """Search-model state for the wizard: cached on disk / downloading / last error."""
        from mcpbrain.embed import model_weights_cached
        building = bool(getattr(self, "_model_downloading", False)
                        or getattr(self, "_model_building", False))
        return {
            "cached": bool(model_weights_cached()),
            "downloading": building,
            "error": getattr(self, "_model_error", None),
        }

    def ensure_model(self) -> None:
        """Start a background thread that builds the embedder (downloading the
        bge-small weights on first use). Idempotent: a second call while a
        download is in flight is a no-op."""
        if getattr(self, "_model_downloading", False):
            return

        def _run():
            try:
                self._embedder.embed_query("warm")   # forces fastembed download+load
                self._model_error = None
            except Exception as exc:  # noqa: BLE001 — surface to the wizard, don't crash
                self._model_error = str(exc)
            finally:
                self._model_downloading = False

        self._model_downloading = True
        self._model_error = None
        threading.Thread(target=_run, daemon=True).start()

    def __init__(self, store, embedder, *, services: dict | None = None,
                 interval_s: float = 300.0,
                 lock=None, enrich_client=None, enrich_batch: int = 100, backup=None,
                 backup_interval_s: float | None = None,
                 last_backup_attempt_epoch: float | None = None,
                 backup_failures: int = 0,
                 communities_interval_s: float | None = None,
                 lint_interval_s: float | None = None,
                 synthesise_interval_s: float | None = None,
                 proactive_interval_s: float | None = None,
                 waiting_on_interval_s: float | None = None,
                 blocks_interval_s: float | None = None,
                 audit_interval_s: float | None = None,
                 stale_reextract_interval_s: float | None = None,
                 auto_update_interval_s: float | None = None,
                 verify_interval_s: float | None = None,
                 clock=time.monotonic,
                 wall_clock=time.time,
                 enrich_mode: str = "off"):
        self._store = store
        # Tool handlers for the routed-execution path (/api/tool), built on first
        # use by _routed_tool_handlers(). See call_tool().
        self._tool_handlers: dict | None = None
        # Lazy embedder: hold the instance (may be None) in a backing field and
        # build on first use via _embedder_factory. Keeps the control server /
        # wizard reachable even before the model is downloaded.
        self._embedder_obj = embedder
        self._embedder_factory = None
        self._model_downloading = False
        self._model_building = False
        self._model_error = None
        self._enrich_client = enrich_client  # None -> enrichment defers (no-op)
        # Enrichment source: "spool" (the per-unit work queue) or "off".
        # Defaults to "off" so a newly-constructed daemon enriches nothing
        # until explicitly configured.
        # apply_config re-reads it from config under _config_lock, the same way
        # _enrich_client is re-wired, and run_one snapshots it per cycle.
        self._enrich_mode = enrich_mode
        # Cap chunks enriched per cycle so a post-migration backlog drains
        # progressively instead of enriching the whole corpus in one cycle.
        self._enrich_batch = enrich_batch
        # Track whether services were EXPLICITLY injected. None (the default)
        # means "auto-build from the user's token at run() time"; an explicit
        # dict (incl. {}) means "use exactly this, never call auth".
        self._services_resolved = services is not None
        # Filter the injected dict now; an auto-built dict is filtered the same
        # way in ensure_services(). Drop None values and stray kwargs.
        # For the services=None (auto-build) case, this starts as {} and is a
        # placeholder until ensure_services() populates it — ensure_services() is
        # the sole post-construction setter for that path. For an injected dict
        # it holds the filtered injected services and is never changed again.
        self._services = self._filter_services(services)
        self._interval_s = interval_s
        self._lock = lock if lock is not None else SingleWriterLock()
        # Single-flight guard for the interactive consent flow: a double-click
        # or wizard retry must not spawn a second OAuth redirect server +
        # browser tab. start_auth acquires this non-blocking and no-ops if held.
        self._auth_lock = threading.Lock()
        # Backup is OFF unless a BackupConfig is supplied. Time-based cadence
        # via an injected clock so tests are deterministic (no real sleeps).
        # _backup and _backup_interval_s are a CONSISTENT PAIR: apply_config
        # (HTTP handler thread) writes both and maybe_backup (loop thread) reads
        # both, so they are guarded together by _config_lock to stop an
        # interleave reading a new config with the old interval.
        self._config_lock = threading.Lock()
        self._backup = backup
        self._backup_interval_s = backup_interval_s
        if self._backup is not None and self._backup_interval_s is None:
            raise ValueError("backup_interval_s is required when backup is configured")
        self._clock = clock
        # Transient-failure retry state for the three Google-dependent builds
        # (see GOOGLE_RETRY_FIRST_S). Read through _google_backoff() so an
        # instance built without __init__ still works.
        self._services_backoff = _GoogleBackoff("sync services", clock, wall_clock)
        self._backup_backoff = _GoogleBackoff("backup", clock, wall_clock)
        self._org_config_backoff = _GoogleBackoff("org-config merge", clock, wall_clock)
        self._restore_backoff = _GoogleBackoff("restore", clock, wall_clock)
        # Set by run()'s _startup_restore from RESTORE_PENDING_MARKER; while
        # True, maybe_backup never uploads or prunes.
        self._restore_pending = False
        self._restore_wait_s = RESTORE_STARTUP_WAIT_S
        # Seed the cadence clock from the PERSISTED attempt stamp so a restart
        # is not mistaken for a first run. Without this, _last_backup started
        # None on every boot and maybe_backup treated that as "due now": on
        # 2026-08-04, 9 daemon starts uploaded 12.75GB where the configured
        # daily cadence calls for 4.25GB.
        #
        # The stamp arrives as an explicit argument rather than being read from
        # app_dir() here on purpose: conftest does not isolate MCPBRAIN_HOME
        # globally, so a constructor that read it would make the daemon tests
        # depend on the developer's real backup history. See
        # last_backup_attempt_epoch for the wiring the entry point uses.
        #
        # The backup cadence also keeps a WALL-CLOCK stamp (_last_backup_wall)
        # beside the monotonic one, stamped at the same points; maybe_backup
        # measures elapsed as the MAX of the two. On macOS time.monotonic()
        # (mach_absolute_time) does not advance while the machine sleeps, so a
        # monotonic-only "daily" backup needed 24h of AWAKE time -- live
        # 2026-10-05, no backup was even attempted for ~2 days across a slept
        # weekend. Monotonic is kept because it still counts correctly when
        # the wall clock is set backwards; the max only ever makes a backup
        # MORE due, never less. Backup ONLY: the other cadences count awake
        # time by design.
        self._wall_clock = wall_clock
        self._last_backup = None
        self._last_backup_wall = None
        if last_backup_attempt_epoch is not None:
            # Convert wall-clock (persisted) into this process's clock frame --
            # self._clock defaults to time.monotonic, whose epoch is arbitrary
            # per process, so the raw value is meaningless here. Clamp at 0 so a
            # backwards system-clock adjustment, which makes the stamp look
            # future-dated, reads as "just attempted" instead of yielding a
            # negative elapsed that would park the cadence indefinitely.
            now_wall = self._wall_clock()
            elapsed = max(0.0, now_wall - float(last_backup_attempt_epoch))
            self._last_backup = self._clock() - elapsed
            self._last_backup_wall = now_wall - elapsed
        # Set for the duration of maybe_backup()'s capture (copy + local
        # outbox write), never the upload. The watchdog's _recover_from_stall
        # checks this and defers recovery while it is set: os._exit bypasses
        # `finally`, so firing mid-capture leaves a half-written work copy --
        # the old full-snapshot form of this left ~24GB of mcpbrain-snap-* on
        # disk and froze the host on 2026-07-27.
        self._backup_in_progress = threading.Event()
        # Consecutive failed backups (seeded from backup_state.json at start-up
        # so a restart keeps the short retry); shortens the interval -- see
        # BACKUP_RETRY_BASE_S.
        self._backup_failures = max(0, int(backup_failures or 0))
        # Set by request_backup(): the next maybe_backup is due now.
        self._backup_requested = threading.Event()
        # Non-null while a backup runs: what it is doing, for /api/status.
        self._backup_progress: dict | None = None
        # Set by request_backup(), cleared once the outbox is empty: upload
        # even while a Mac is dark-waking (a person asked for it).
        self._backup_drain_requested = threading.Event()
        # Consecutive failed upload steps in THIS process: at 2 the Drive
        # service and upload transport are rebuilt (see upload_backup_step).
        self._upload_failures = 0
        # Outbox-full pause (see maybe_backup): wall time the outbox was found
        # full (None = not full) and the monotonic time of the last cheap
        # re-check. While set, capture neither sweeps nor copies.
        self._outbox_full_since: float | None = None
        self._outbox_full_checked: float | None = None
        # Wall time of the last prune pass: an idle install still prunes
        # expired chains once a day (IDLE_PRUNE_INTERVAL_S).
        self._last_prune_wall: float | None = None
        # Periodic community detection is OFF unless communities_interval_s is set.
        # Tiered like resolve: OFF by default; time-based cadence via self._clock.
        self._communities_interval_s: float | None = communities_interval_s
        self._last_communities = None
        # Periodic graph lint is OFF unless lint_interval_s is set.
        # Same three-shape contract as maybe_communities.
        self._lint_interval_s: float | None = lint_interval_s
        self._last_lint = None
        # Periodic thread synthesis is OFF unless synthesise_interval_s is set.
        # Cadence-gated: builds synthesis requests and stashes them so run_one
        # can pass them to prepare.prepare_units() in the next spool cycle.
        self._synthesise_interval_s: float | None = synthesise_interval_s
        self._last_synthesise = None
        self._pending_synthesis: list = []
        # Periodic proactive detection is OFF unless proactive_interval_s is set.
        # Same three-shape contract as maybe_communities / maybe_lint / maybe_synthesise.
        self._proactive_interval_s: float | None = proactive_interval_s
        self._last_proactive = None
        # Periodic waiting-on reconciliation is OFF unless waiting_on_interval_s is set.
        # Same three-shape contract as maybe_communities / maybe_lint / maybe_proactive.
        self._waiting_on_interval_s: float | None = waiting_on_interval_s
        self._last_waiting_on = None
        # Periodic block requests (profile_synthesis + community_synthesis + memory_distil)
        # are OFF unless blocks_interval_s is set. Cadence-gated: builds extra_blocks
        # requests and stashes them so run_one() can pass them to prepare.prepare_units().
        self._blocks_interval_s: float | None = blocks_interval_s
        self._last_blocks = None
        self._pending_blocks: dict = {}
        # Periodic profile audit is OFF unless audit_interval_s is set.
        # Same cadence pattern: builds audit requests and stashes for run_one().
        self._audit_interval_s: float | None = audit_interval_s
        self._last_audit = None
        self._pending_audit: dict = {}
        # Periodic ClickUp two-way sync is gated on clickup_api_key + clickup_list_id
        # being configured; interval is hardcoded to _CLICKUP_SYNC_INTERVAL_S.
        # _clickup_interval_s starts None and is set to the fixed constant on first
        # eligible call (used as the cadence-clock bookkeeping attribute).
        self._clickup_interval_s: float | None = None
        self._last_clickup = None
        # Periodic stale -> re-extraction trigger (Gap A) is OFF unless
        # stale_reextract_interval_s is set. Same three-shape cadence contract.
        self._stale_reextract_interval_s: float | None = stale_reextract_interval_s
        self._last_stale_reextract = None
        # S2 feedback aggregation: nightly Bayesian-smoothed CTR → chunk_quality.
        # OFF by default; the daemon sets it from config/cadences on start.
        self._feedback_aggregate_interval_s: float | None = None
        self._last_feedback_aggregate = None
        # Q4 org backfill: deterministic pass over org-less entities.
        # OFF by default; enabled via cadences config.
        self._org_backfill_interval_s: float | None = None
        self._last_org_backfill = None
        # Task 3.3: daily deterministic entity dedup (issue #23-fix validated).
        # Default 86400s (daily) via _CADENCE_DEFAULTS; set resolve_entities_interval_s: 0
        # in the cadences config to disable. (This attr is the pre-config placeholder.)
        self._resolve_entities_interval_s: float | None = None
        self._last_resolve_entities = None
        # Session-4: daily AI-adjudicated graph-hygiene review (build review units).
        # Default 86400s (daily) via _CADENCE_DEFAULTS; set review_interval_s: 0 in the
        # cadences config to disable. (This attr is the pre-config placeholder.)
        self._review_interval_s: float | None = None
        self._last_review = None
        # Daily action hygiene: age out long-dead actions + collapse duplicates.
        # Default 86400s via _CADENCE_DEFAULTS; set action_hygiene_interval_s: 0
        # in the cadences config to disable.
        self._action_hygiene_interval_s: float | None = None
        self._last_action_hygiene = None
        # Extraction-fidelity reflow seed: default 3600s via _CADENCE_DEFAULTS;
        # set reflow_seed_interval_s: 0 in the cadences config to disable.
        self._reflow_seed_interval_s: float | None = None
        self._last_reflow_seed = None
        # Drain the legacy doc_id-keyed enrich_payloads table store.init()
        # renames aside. Hourly and hardcoded (not config-driven): this is a
        # one-time-ish drain, not a tunable maintenance policy, and it
        # self-terminates once the legacy table is gone.
        self._enrich_payload_migration_interval_s = 3600.0
        self._last_enrich_payload_migration = None
        # Prune dead-pid mcp_heartbeat/<pid>.json files (hourly). Default 3600s
        # via _CADENCE_DEFAULTS; set mcp_heartbeat_sweep_interval_s: 0 in the
        # cadences config to disable.
        self._mcp_heartbeat_sweep_interval_s: float | None = None
        self._last_mcp_heartbeat_sweep = None
        # B3 salience scoring: structural importance per chunk (daily).
        self._salience_score_interval_s: float | None = None
        self._last_salience_score = None
        # B5 decay pass: nightly demotion of unaccessed low-salience chunks.
        self._decay_pass_interval_s: float | None = None
        self._last_decay_pass = None
        # B4 consolidation: RAPTOR-style nightly cluster+summarise.
        self._consolidation_interval_s: float | None = None
        self._last_consolidation = None
        # B6 voice analyser: weekly analysis-only procedural memory pass.
        self._voice_analyse_interval_s: float | None = None
        self._last_voice_analyse = None
        # S4/S5 self-improvement: weekly drift check + bandit advisory + lessons.
        self._self_improve_interval_s: float | None = None
        self._last_self_improve = None
        # Auto-graduation cadence: flip data-gated brain flags ON when ready.
        self._auto_enable_interval_s: float | None = None
        self._last_auto_enable = None
        # Org-baseline (Phase 0) cadences: registered as no-op stubs; subsystem B
        # fills the _run_* bodies. Intervals set from cadences config on start.
        self._org_contrib_upload_interval_s: float | None = None
        self._last_org_contrib_upload = None
        self._org_import_interval_s: float | None = None
        self._last_org_import = None
        self._org_curate_interval_s: float | None = None
        self._last_org_curate = None
        # Silent auto-update cadence: OFF unless auto_update_interval_s is set.
        self._auto_update_interval_s: float | None = auto_update_interval_s
        self._last_auto_update = None
        # Wall-clock twin of _last_auto_update, stamped at the same point;
        # _run_auto_update measures elapsed as the MAX of the two, so time
        # asleep counts (macOS monotonic pauses during sleep -- same defect
        # and same fix as the backup cadence, see _last_backup_wall). There is
        # no persisted last-attempt record to seed from: the first check after
        # a (re)start is always due, so both stamps start None together.
        self._last_auto_update_wall = None
        # Pending update version: set by maybe_auto_update (detect-only); consumed
        # by run() AFTER the write lock is released so uv install + restart never
        # happen under the held lock.
        self._pending_update: str | None = None
        # Periodic connection verification (network) is OFF unless verify_interval_s
        # is set. Defaults to hourly when configured without an explicit interval.
        # Writes connections.json which all_connections() overlays.
        self._verify_interval_s: float | None = verify_interval_s
        self._last_verify = None
        # OCR binary install: once-ever background attempt (see run_ocr_setup).
        # OFF by default; enabled via cadences config, same as the S2/Q4/B3/B5/
        # B4/B6 group above — a bare Daemon() (every test that constructs one
        # directly without going through apply_config()/main()'s cadences
        # wiring) must not reach a real `brew install`/`winget install` on its
        # first _run_periodic_passes() tick. Once configured, the interval only
        # bounds how soon after boot it is first tried; the marker file (not
        # this timestamp) is what makes it once-ever.
        self._ocr_setup_interval_s: float | None = None
        self._last_ocr_setup = None
        # Bound the launchd logs. Defaulted ON (not None) rather than waiting for
        # a cadences config entry: launchd rotates nothing, so a build with no
        # cadence config is exactly the build that fills its own disk. Hourly is
        # cheap — a stat() per file unless one is actually over the cap.
        self._log_cap_interval_s: float | None = 3600.0
        self._last_log_cap = None
        self._pause = threading.Event()   # set == paused
        self._stop = threading.Event()    # set == stop the loop
        self._wake = threading.Event()    # set == run a cycle now
        # Single-flight guard for enrich-backfill: non-blocking acquire means a
        # duplicate start_enrich_backfill call is a no-op. _backfill_active
        # signals run_one to yield its write cycle while the backfill is live.
        self._backfill_active = threading.Event()
        self._backfill_lock = threading.Lock()
        # Guards the _pending_* stashes. They are written by cadence passes and
        # read-and-cleared by run_one; once those run on different threads that
        # is a genuine read-delete race.
        self._stash_lock = threading.Lock()
        # Per-key write generation for the _pending_* stashes (Task 4). Bumped
        # alongside every write under _stash_lock; _stash_snapshot() captures the
        # generation map along with the values, and _stash_clear_drained() only
        # deletes a key whose generation is UNCHANGED since the snapshot. Without
        # this, run_one's old read-then-delete was check-then-clear across the
        # whole cycle window: a cadence pass rewriting a key (e.g. maybe_blocks
        # firing again) between run_one's snapshot and its post-drain clear had
        # its fresh batch deleted unattached, even though it was never drained.
        self._stash_generation: dict = {}
        # Guards the lazily-built embedder. index_pending (cycle thread) and
        # consolidation/self_improve (maintenance thread) share one ONNX model.
        self._embedder_lock = threading.Lock()
        # Coarse advisory lock. Held by the cycle around chunk-mutating phases
        # and acquired by the four cadence passes that also write `chunks`.
        # The maintenance thread's acquire is bounded (see BULK_LOCK_ACQUIRE_S)
        # so a wedged cycle can never park the watchdog behind it.
        self._bulk_lock = threading.Lock()
        self._bulk_lock_wait_s = BULK_LOCK_ACQUIRE_S
        # Brief pause after releasing _bulk_lock when another thread wants it
        # (see BULK_LOCK_YIELD_S / _cycle_bulk_section). Config-overridable the
        # same way as _bulk_lock_wait_s below (see _tuning_from_config; main()
        # reassigns both post-construction from the `tuning` config block).
        self._bulk_lock_yield_s = BULK_LOCK_YIELD_S
        # Count of threads currently waiting to acquire _bulk_lock (the
        # maintenance thread's _run_periodic_passes, and the cycle thread's
        # backup capture (_bounded_bulk_lock) -- TWO independent call sites can be waiting
        # at once). CPython locks are not FIFO-fair, so without an explicit
        # hand-off the cycle thread -- which re-acquires ~1s after releasing --
        # wins nearly every race and the four gated passes never run at all.
        # See _cycle_bulk_section / _bulk_lock_intent.
        #
        # A single threading.Event was tried first and is WRONG: one waiter's
        # cleanup (Event.clear() in its own finally) can erase another waiter's
        # still-pending "I'm waiting" signal -- e.g. maintenance sets the flag
        # and blocks on acquire(); the cycle thread's backup capture
        # (_bounded_bulk_lock, on the CYCLE thread) also sets the flag, wins the race,
        # runs, and clears the flag in ITS finally even though maintenance is
        # STILL genuinely waiting. The next _cycle_bulk_section release then
        # sees no intent and skips the yield pause, right when it's needed. A
        # plain int counter (incremented per waiter, decremented on its own
        # cleanup) can't be erased by an unrelated waiter's cleanup this way.
        self._bulk_lock_waiters = 0
        self._bulk_lock_waiters_lock = threading.Lock()
        # Set for the duration of a gated cadence pass's OWN execution (Task 4
        # known-gap attempt), from the moment it actually acquires _bulk_lock
        # to the moment it releases it -- set/cleared by _run_gated_pass,
        # which runs INLINE on the maintenance thread (a worker-thread variant
        # was tried and reverted -- see _run_periodic_passes's docstring).
        #
        # IMPORTANT: on THIS single thread, _recover_from_stall's check of
        # this flag is DEAD CODE, not a working watchdog mitigation --
        # _run_gated_pass's own `finally: active.clear()` runs strictly
        # BEFORE _run_periodic_passes() returns, which is strictly before
        # _maintenance_loop ever reaches _stalled_phase()/_recover_from_stall()
        # on that same thread, so the flag is ALWAYS already clear by the
        # time it's checked. Confirmed directly by a full-timeline simulation
        # of the real production call chain: 69/69 pass-completions produced
        # a spurious restart. Kept ONLY for status() observability (so a
        # human/dashboard polling mid-pass can see one is running); the
        # false-restart problem it was meant to fix (cause 1 of the Task 4
        # known-gap note) is an explicit, tracked, STILL-OPEN gap -- see
        # _recover_from_stall's and _run_periodic_passes's docstrings, and
        # docs/superpowers/plans/2026-07-27-daemon-scheduling-remediation.md,
        # Task 4 section.
        self._bulk_pass_active = threading.Event()
        # Maintenance runs on its own thread, ticking independently of the bulk
        # cycle (Task 4: the cycle used to call _run_periodic_passes() inline,
        # so an unbounded cycle starved every cadence pass for four days).
        self._maintenance_interval_s = MAINTENANCE_TICK_S
        self._maintenance_thread = None
        # Per-phase progress heartbeat (Task 5): the old heartbeat was written
        # only after the cadence passes, so a mid-cycle stall was invisible by
        # construction. _note_progress records a timestamp per named phase;
        # _stalled_phase compares the STALEST one against STALL_S.
        #
        # Seeded with "cycle" so a first cycle that NEVER completes is visible
        # to the watchdog. _stalled_phase returns None on an empty dict, and
        # once the maintenance thread is up "maintenance" is re-stamped every
        # tick — so without this seed a daemon that wedges before its first
        # run_one() returns would look permanently healthy. That is exactly the
        # live incident's shape ("stale for 35.9 h across three restarts").
        self._progress_lock = threading.Lock()
        self._progress: dict = {"cycle": self._clock()}
        # Consecutive run_one() exceptions (Task 3). A deterministic raise (a
        # code/config bug, not a hang) looks identical to a wedge from
        # _stalled_phase's point of view -- "cycle" simply stops advancing
        # either way -- and would otherwise burn the watchdog's 3-exit budget
        # restarting something a restart cannot fix. run() increments this on
        # every failed cycle and resets it on the next success; once it passes
        # 3, _recover_from_stall defers (stays up, visibly failing) instead of
        # restart-looping.
        self._cycle_error_streak = 0
        # Single-flight guard: the control-API force path (/api/bootstrap-baseline)
        # can fire on an HTTP thread while the loop thread independently bootstraps
        # the same cycle — two concurrent import_snapshot transactions into one
        # store would race and lost-update the resume marker.
        self._bootstrap_lock = threading.Lock()
        # Baseline bootstrap (subsystem C): import the org snapshot + shared-drive
        # ingest caches once, before the first sync. In-process latch; the on-disk
        # marker (onboarding.run_bootstrap) makes it idempotent across restarts.
        self._baseline_bootstrap_done = False
        # Tuning knobs (Task 7): default to the module constants so a Daemon
        # built without going through main() (every direct test construction)
        # behaves exactly as before. main() overrides these from the `tuning`
        # config block post-construction (see _tuning_from_config), the same
        # "not constructor params; wire after construction" pattern already
        # used above for the S2/Q4/B3/B5/B4/B6 cadences -- these are read at
        # call time wherever they're used, so a post-construction reassignment
        # here is exactly as effective as passing them in at construction.
        # Every read site uses getattr(self, "_x", MODULE_CONSTANT) rather
        # than a bare attribute access, so the many existing unit tests that
        # bypass __init__ entirely (Daemon.__new__(Daemon), setting only the
        # handful of attributes each test needs -- see test_daemon_watchdog.py
        # / test_bulk_lock_fairness.py) keep working unchanged: an instance
        # missing one of these attributes falls back to the exact same
        # constant it would have used before Task 7 introduced the attribute.
        self._cycle_budget_s = CYCLE_BUDGET_S
        self._stall_s = STALL_S
        self._watchdog_max_exits = WATCHDOG_MAX_EXITS
        self._watchdog_window_s = WATCHDOG_WINDOW_S
        self._embed_max_items = EMBED_MAX_ITEMS

    # -- service resolution -------------------------------------------------

    @staticmethod
    def _filter_services(services: dict | None) -> dict:
        """Keep only the recognised service kwargs; drop None values."""
        return {
            k: v for k, v in (services or {}).items()
            if k in ("gmail_service", "calendar_service", "drive_service") and v is not None
        }

    def ensure_services(self) -> dict:
        """Resolve self._services, building from the user's token if needed.

        Idempotent. If services were explicitly injected (the constructor arg
        was not None — even an empty dict), they are used as-is and auth is
        never called. Otherwise the services are built once from the user's
        token via auth.build_google_services(); a missing/invalid token is
        logged and degrades to empty services (no sync, no crash) for the rest
        of the run.

        A TRANSIENT failure (network, token-endpoint outage -- see
        _is_transient_google_error) does NOT resolve: it returns {} for now and
        schedules a retry on the _GoogleBackoff schedule. Calls before the
        retry time, or while another thread is mid-build, return {} without
        touching auth. Every consumer calls this per cycle/pass rather than
        caching the result, so the first successful retry reaches all of them.
        """
        if self._services_resolved:
            return self._services
        bo = self._google_backoff("_services_backoff", "sync services")
        if not bo.due() or not bo.build_lock.acquire(blocking=False):
            return self._services
        try:
            if self._services_resolved or not bo.due():
                return self._services
            from mcpbrain import auth
            try:
                built = auth.build_google_services()
            except Exception as exc:  # noqa: BLE001 — no/invalid token, network, etc.
                if _is_transient_google_error(exc):
                    bo.failed(exc)
                    return self._services
                log.warning(
                    "no Google credentials — running without sync "
                    "(authorise: python -m mcpbrain.auth): %s", exc
                )
                bo.clear()
                built = {}
                restored = False
            else:
                restored = True
            self._services = self._filter_services(built)
            self._services_resolved = True
            if restored:
                bo.succeeded()
            return self._services
        finally:
            bo.build_lock.release()

    def _google_backoff(self, attr: str, what: str) -> _GoogleBackoff:
        """The named _GoogleBackoff, created on first use for an instance that
        bypassed __init__ (several tests build Daemon via __new__)."""
        bo = getattr(self, attr, None)
        if bo is None:
            bo = _GoogleBackoff(what, getattr(self, "_clock", time.monotonic),
                                getattr(self, "_wall_clock", time.time))
            setattr(self, attr, bo)
        return bo

    def google_unavailable(self) -> dict | None:
        """{since, attempts, last_error, affects} while any Google build is
        waiting on a transient-failure retry, else None. `since` is the
        earliest outage start, `attempts` the most failed attempts of any one
        build, `last_error` the most recent failure."""
        snaps = [s for s in (
            self._google_backoff(a, w).snapshot() for a, w in (
                ("_services_backoff", "sync services"),
                ("_backup_backoff", "backup"),
                ("_org_config_backoff", "org-config merge"),
                ("_restore_backoff", "restore"))) if s]
        if not snaps:
            return None
        latest = max(snaps, key=lambda s: s["last_at"])
        return {"since": min(s["since"] for s in snaps if s["since"]),
                "attempts": max(s["attempts"] for s in snaps),
                "last_error": latest["last_error"],
                "affects": [s["what"] for s in snaps]}

    def _google_services_pending(self) -> bool:
        """True while auto-built services are waiting on a transient-failure
        retry (so an empty services dict means "not yet", not "never")."""
        return (not getattr(self, "_services_resolved", True)
                and self._google_backoff("_services_backoff", "sync services").pending)

    def _google_unavailable_status(self) -> dict | None:
        try:
            return self.google_unavailable()
        except Exception as exc:  # noqa: BLE001 — status must never raise
            log.debug("status: google_unavailable degraded: %s", exc)
            return None

    def _retry_backup_build(self) -> None:
        """Rebuild the backup config when the last build failed TRANSIENTLY and
        its retry is due. Builds off-lock (network), then assigns the
        (_backup, _backup_interval_s) pair under _config_lock -- only if no
        backup is configured by then, so a concurrent apply_config wins."""
        bo = self._google_backoff("_backup_backoff", "backup")
        if not bo.pending or not bo.due() or not bo.build_lock.acquire(blocking=False):
            return
        try:
            # Re-check under build_lock: a concurrent apply_config may have
            # rebuilt (and cleared the backoff) while we waited for it.
            if not (bo.pending and bo.due()):
                return
            res = _backup_from_config(str(app_dir()))
            transient = getattr(res, "transient", None)
            if transient is not None:
                bo.failed(transient)
                return
            backup_cfg, backup_interval = res
            with self._config_lock:
                if self._backup is None:
                    self._backup = backup_cfg
                    self._backup_interval_s = backup_interval
            if backup_cfg is not None:
                bo.succeeded()
            else:
                bo.clear()   # now failing permanently; the build logged why
        finally:
            bo.build_lock.release()

    def _retry_org_config_merge(self) -> None:
        """Re-run the org-config merge when the startup merge failed
        TRANSIENTLY and its retry is due. A merge stages org cadences into
        config.json, so a successful retry re-wires the cadence intervals the
        same way apply_config does."""
        bo = self._google_backoff("_org_config_backoff", "org-config merge")
        if not bo.pending or not bo.due() or not bo.build_lock.acquire(blocking=False):
            return
        try:
            if not (bo.pending and bo.due()):
                return
            home = str(app_dir())
            status, exc = _merge_org_config_classified(home)
            if status == "transient":
                bo.failed(exc)
                return
            if status != "merged":
                bo.clear()   # now failing permanently (or no token); logged
                return
            cadences = _cadences_from_config(home)
            with self._config_lock:
                self._assign_cadences(cadences)
            bo.succeeded()
        finally:
            bo.build_lock.release()

    # -- restore-on-first-run vs a transient outage ------------------------

    def _startup_restore(self) -> None:
        """Restore-on-first-run, retried through a transient outage BEFORE any
        writer starts (see RESTORE_STARTUP_WAIT_S). Runs on the loop thread at
        the top of run(); honours _stop.

        The store is NEVER overwritten once it has content. While a restore is
        pending, the daemon HOLDS (_restore_pending): no sync cycle, no
        content-writing maintenance pass, no backup -- so the store stays empty
        and the next start's ordinary empty-store restore can still run. A
        non-transient restore failure (wrong key, corrupt snapshot) does NOT
        hold: the hold is lifted and the error surfaces in /api/status/doctor,
        as before this change."""
        home = str(config.app_dir())
        bo = self._google_backoff("_restore_backoff", "restore")
        self._restore_pending = _read_restore_pending(home) is not None
        wait_s = getattr(self, "_restore_wait_s", RESTORE_STARTUP_WAIT_S)
        deadline = self._clock() + wait_s
        while True:
            try:
                status, exc = _restore_on_first_run_classified(
                    self._store, home, gate=self._restore_gate)
            except Exception as err:   # non-transient: report, do not hold
                self._restore_error = str(err)[:200]
                self._release_restore_hold(home)
                bo.clear()
                raise
            if status == "transient":
                self._hold_for_restore(home, exc)
                bo.failed(exc)
                wait = max(0.0, bo.retry_at - self._clock())
                if self._stop.is_set() or self._clock() + wait > deadline:
                    log.warning("restore-on-first-run still pending after the start-up "
                                "wait; holding sync and backups so the snapshot is not "
                                "overwritten")
                    return
                if self._stop.wait(wait):
                    return
                continue
            if status in ("restored", "no_snapshot"):
                self._release_restore_hold(home)
                bo.succeeded()
            elif status == "partial":
                # R24: the store is restored (it has content now), the records
                # repo / config.json are not. Never retried -- the empty-store
                # rule would refuse it -- so lift the hold and surface it.
                self._restore_error = ("store restored, but the records repo / "
                                       f"config.json were not: {exc}")[:200]
                self._release_restore_hold(home)
                bo.succeeded()
            elif status == "unconfigured":
                if self._restore_pending:
                    log.warning("restore pending, but backup is no longer configured: "
                                "nothing to protect, releasing the hold")
                self._release_restore_hold(home)
                bo.clear()
            else:   # not_empty
                if self._restore_pending:
                    log.warning("restore pending, but the store already has content: "
                                "not overwriting it; releasing the hold")
                self._release_restore_hold(home)
                bo.clear()
            return

    def _hold_for_restore(self, home, exc) -> None:
        """Enter (or stay in) the restore hold. If the marker cannot be written
        the hold still applies in memory: the store stays empty, so the next
        boot's ordinary empty-store restore still runs."""
        if not self._restore_pending:
            _write_restore_pending(home, str(exc))
        self._restore_pending = True

    def _release_restore_hold(self, home) -> None:
        """Lift the hold. If the marker cannot be removed, stay held and retry
        the unlink on the next loop iteration (_retry_pending_restore)."""
        if _clear_restore_pending(home):
            self._restore_pending = False
        else:
            self._restore_pending = True
            self._restore_unlink_pending = True

    @contextmanager
    def _restore_gate(self):
        """The `restoring` gate around the store swap: the control API answers
        503 (is_restoring) and every Store connection refuses until it drops."""
        self._restoring = True
        try:
            gate = getattr(self._store, "restoring_gate", None)
            with (gate() if gate is not None else nullcontext()):
                yield
        finally:
            self._restoring = False

    def swap_restored_store(self, stage) -> None:
        """Swap a restored staging file in as the live store, behind this
        daemon's restoring gate (the wizard's /api/backup/auto path)."""
        backup.swap_in_store(stage, self._store.path, gate=self._restore_gate)

    def note_restore_error(self, msg: str) -> None:
        """Record a restore problem for /api/status (`restore_error`) and doctor
        -- the wizard's partial restore (R24), as the start-up path does."""
        self._restore_error = str(msg)[:200]

    def is_restoring(self) -> bool:
        return bool(getattr(self, "_restoring", False))

    def _retry_pending_restore(self) -> None:
        """While the restore hold is on and its backoff is due: ask Drive
        whether a snapshot exists. None is the definitive answer that lifts the
        hold. One that exists is restored by RESTARTING the process: the next
        start's ordinary empty-store restore (the store stayed empty under the
        hold) performs it with the safe swap. With the watchdog's restart
        budget spent, the daemon stays held -- empty store, no sync, nothing to
        lose -- and tries again on the next start; doctor says so."""
        if not getattr(self, "_restore_pending", False):
            return
        home = str(app_dir())
        if getattr(self, "_restore_unlink_pending", False):
            if _clear_restore_pending(home):
                self._restore_unlink_pending = False
                self._restore_pending = False
            return
        bo = self._google_backoff("_restore_backoff", "restore")
        if not bo.pending or not bo.due() or not bo.build_lock.acquire(blocking=False):
            return
        try:
            if not (bo.pending and bo.due()) or self._stop.is_set():
                return
            status, exc = _probe_snapshot(home)
            if status == "transient":
                bo.failed(exc)
                return
            if status == "error":
                # Permanent, as at start-up: do not hold forever -- lift the
                # hold and surface the error (status/doctor).
                self._restore_error = str(exc)[:200]
                self._release_restore_hold(home)
                bo.clear()
                return
            if status in ("no_snapshot", "unconfigured"):
                if status == "unconfigured":
                    log.warning("restore pending, but backup is no longer configured: "
                                "nothing to protect, releasing the hold")
                self._release_restore_hold(home)
                bo.succeeded() if status == "no_snapshot" else bo.clear()
                return
            if not self._watchdog_may_exit():
                log.error("restore pending and a snapshot is reachable, but the restart "
                          "budget is spent; holding sync -- restart the daemon while online")
                self._restore_budget_spent = True
                bo.clear()
                return
            log.warning("restore pending and a snapshot is reachable: restarting so the "
                        "empty-store restore runs before anything writes")
            bo.succeeded()
            self._restart_process()
        finally:
            bo.build_lock.release()

    def restore_status(self) -> dict | None:
        """/api/status `restore_pending`: the marker plus whether the restart
        budget is spent, while the hold is on; else None."""
        if not getattr(self, "_restore_pending", False):
            return None
        marker = _read_restore_pending(str(app_dir())) or {}
        return {"since": marker.get("since"), "reason": marker.get("reason", ""),
                "restart_budget_spent": bool(getattr(self, "_restore_budget_spent", False))}

    def _retry_google_builds(self) -> None:
        """Called once per loop iteration on the cycle thread: retry whichever
        startup Google builds failed transiently, plus a pending restore. Never
        raises into the loop. (Services retry themselves inside ensure_services.)
        Each retry fires on the first iteration after its backoff time, so the
        effective spacing is max(backoff, interval_s)."""
        for retry in (self._retry_backup_build, self._retry_org_config_merge,
                      self._retry_pending_restore):
            try:
                retry()
            except Exception as exc:  # noqa: BLE001 — a retry must not break the loop
                log.warning("Google build retry failed: %s", exc, exc_info=True)

    # -- pause / resume -----------------------------------------------------

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        """Clear the pause flag, after re-stamping every tracked progress phase.

        Behaviour note (Task 7): cadence passes used to run inline every cycle
        regardless of pause state. Since Task 4 they live on their own
        maintenance thread, and `_maintenance_loop` skips its ENTIRE body while
        `_pause` is set -- not just the chunk-mutating cadence passes, but also
        the `_note_progress("maintenance")` stamp and the watchdog stall check
        that follow them. So a pause freezes every `_progress` KEY EXCEPT
        `"backup"` -- `run()`'s loop calls `_backup_under_bulk_lock()`
        unconditionally every interval, pause or not (a backup is a snapshot
        of current state, harmless while paused), and that method's first
        statement re-stamps `"backup"` before it even checks whether a backup
        is due. So `"backup"` alone keeps advancing throughout a pause; every
        other key (`cycle`/`sync`/`enrich`/`maintenance`) genuinely freezes.
        Re-stamping ALL of them here anyway, before clearing `_pause`, stops
        the first post-resume tick from reading those frozen non-backup
        timestamps as a stall (`STALL_S`) and self-restarting a daemon that
        was only ever idle on purpose, never actually wedged -- restamping
        "backup" too is a harmless no-op, not load-bearing.
        """
        # Re-stamp every tracked phase BEFORE unpausing -- see the docstring
        # above for why a pause longer than STALL_S would otherwise misread
        # pre-pause timestamps as a stall on the very first tick after resume.
        # See _restamp_all_progress -- the same helper backs the analogous
        # long-backup and long-backfill fixes.
        self._restamp_all_progress()
        self._pause.clear()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    # -- control API hooks (Task 2.2) ---------------------------------------

    def status(self) -> dict:
        """Snapshot the daemon's state for the control API / wizard.

        Keys: paused, chunk_count, google_connected, granted_scopes,
        enrich_enabled. Google fields are read from the token file directly,
        WITHOUT forcing a network refresh: the wizard polls /api/status every
        few seconds, so refreshing here would hammer Google's token endpoint and
        rewrite the token file on every poll, and a transient refresh error
        would wrongly flip google_connected to False. Google fields degrade
        gracefully — a missing or unreadable token resolves to
        google_connected=False / granted_scopes=[] and never raises.
        """
        from google.oauth2.credentials import Credentials

        token_file = auth.token_path()
        granted: list[str] = []
        google_connected = False
        google_account: str = ""
        google_name: str = ""
        try:
            if token_file.exists():
                creds = Credentials.from_authorized_user_file(str(token_file), auth.SCOPES)
                scopes = auth._granted_scopes(creds, token_file)
                granted = sorted(scopes) if scopes else []
                google_connected = bool(creds and (creds.valid or creds.refresh_token))
                # Resolve the connected account email. The token JSON has an
                # "account" field but the consent flow leaves it empty; fall
                # back to a one-shot Gmail getProfile call cached to a sidecar
                # so /api/status polls don't hit Google. start_auth removes the
                # sidecar on re-consent so a different account refreshes it.
                google_account = self._resolve_google_account(token_file) if google_connected else ""
                # Display name for the wizard prefill — only when the token was
                # actually granted the profile scope (older grants weren't), so we
                # never spam userinfo with 403s on every poll.
                if google_connected and scopes and \
                        "https://www.googleapis.com/auth/userinfo.profile" in scopes:
                    google_name = self._resolve_google_name(creds)
        except Exception as exc:  # noqa: BLE001 — no/invalid token degrades, never crashes
            log.debug("status: Google credentials unavailable: %s", exc)
        # Queue depth for the cowork extractor wizard step. Production writes
        # immutable work units to enrich_queue/units/*.json (prepare_units /
        # write_units) and the daemon drains pushed results from
        # enrich_inbox/<unit_id>.json, so we just count files in each. Errors
        # degrade to zero rather than failing the status poll.
        pending = 0
        inbox = 0
        try:
            home = config.app_dir()
            units_dir = home / "enrich_queue" / "units"
            if units_dir.exists():
                pending = sum(1 for p in units_dir.iterdir() if p.suffix == ".json")
            inbox_dir = home / "enrich_inbox"
            if inbox_dir.exists():
                inbox = sum(1 for p in inbox_dir.iterdir() if p.suffix == ".json")
        except OSError as exc:
            log.debug("status: spool counts unavailable: %s", exc)
        try:
            open_findings = self._store.open_findings_count()
        except Exception:  # noqa: BLE001 — degrade gracefully, never crash status poll
            open_findings = 0
        from mcpbrain import probes
        connections = probes.all_connections(str(app_dir()), self._store)
        from mcpbrain.sync import backfill_progress
        backfill = backfill_progress(self._store)
        # Org-baseline observability (spec Task 5): curator queue depth
        # (pending contributions + suppressed-merge pairs). Best-effort — a
        # missing table/meta key degrades to zeros rather than failing the
        # status poll.
        #
        # `cache_hits`/`cache_misses` (shared-drive ingest-cache hit/miss
        # counts) were REMOVED here (final-review fix): run_sync_cycle's
        # discover/work/publish split (this plan's Task 5) stopped producing
        # a `"shared_drive_cache"` summary key -- that hit/miss distinction
        # is now internal to handle_shared_drive_item/_cache_first_extract_one,
        # not surfaced back to the caller -- so these two fields had been
        # silently pinned at 0 forever, which reads as "the cache is
        # healthy" rather than "this metric no longer exists." Re-deriving
        # real counts would need threading a new counter through
        # work_queue's generic per-item processing for one source's benefit,
        # which is out of scope here.
        org = {
            "curator_version": 0,
            "contrib_staged": 0,
            "merge_suppressed": 0,
        }
        try:
            from mcpbrain import org_curate
            with self._store._connect() as db:
                org["curator_version"] = int(self._store.get_meta("org_curator_version") or 0)
                org["contrib_staged"] = db.execute(
                    "SELECT COUNT(*) c FROM org_contrib_staging").fetchone()["c"]
            org["merge_suppressed"] = len(org_curate._suppressed_pairs(self._store))
        except Exception as exc:  # noqa: BLE001 — status must never raise
            log.debug("status: org block degraded: %s", exc)
        # Watchdog restart-limiter state (spec: "exposed on /api/status for
        # doctor and the tray"). Reuses _recent_watchdog_exits so status can
        # never disagree with the limiter that actually decides. Best-effort —
        # status must never raise.
        try:
            watchdog_exits = len(self._recent_watchdog_exits())
        except Exception:  # noqa: BLE001
            watchdog_exits = 0
        # _progress is written by two threads (the cycle thread and
        # _maintenance_loop) under _progress_lock; reading it here without the
        # lock (as this used to) is a bare data race with dict mutation on the
        # other side. Snapshot under the lock, THEN call _stalled_phase()
        # (which takes the same lock itself) — never call it while already
        # holding the lock, _progress_lock is a plain Lock, not reentrant.
        with self._progress_lock:
            progress_snapshot = dict(self._progress)
        # A deliberately paused daemon freezes every timestamp in _progress
        # (see resume()'s docstring), so a pause longer than STALL_S makes
        # _stalled_phase() look wedged even though nothing is wrong — report
        # no stall at all while paused rather than a false positive.
        stalled = None if self.is_paused() else self._stalled_phase()
        # Surfaces the Task 3 backup/watchdog interaction: a "stalled: None
        # while paused"-style silent state would otherwise make "why isn't
        # the watchdog restarting a wedged daemon" undebuggable from status()
        # alone when the real answer is "a backup is in flight, deliberately".
        backup_in_progress = bool(getattr(self, "_backup_in_progress", None)
                                   and self._backup_in_progress.is_set())
        # Task 4: surfaces whether a gated cadence pass is CURRENTLY holding
        # _bulk_lock. Unlike backup_in_progress, this does NOT correspond to
        # a working watchdog deferral -- _recover_from_stall's own check of
        # this flag is dead code on the single (maintenance) thread that both
        # sets/clears it and would check it (see that method's docstring).
        # It genuinely IS useful here, though: status() is served by a
        # DIFFERENT thread (the control-API handler), which really can
        # observe this flag set while the maintenance thread is mid-pass --
        # so this is real "a gated pass is running right now" visibility for
        # a human/dashboard, just not a watchdog signal.
        bulk_pass_active = bool(getattr(self, "_bulk_pass_active", None)
                                 and self._bulk_pass_active.is_set())
        # Extraction-fidelity reflow (2026-09-24): progress + halt visibility.
        # Best-effort -- status must never raise.
        reflow = None
        try:
            reflow = self._store.reflow_status()
        except Exception as exc:  # noqa: BLE001
            log.debug("status: reflow block degraded: %s", exc)
        return {
            "paused": self.is_paused(),
            "chunk_count": self._store.chunk_count(),
            "enriched_count": self._store.enriched_count(),
            "google_connected": google_connected,
            "granted_scopes": granted,
            "google_account": google_account,
            "google_name": google_name,
            "enrich_enabled": self._enrich_client is not None,
            "spool": {"pending": pending, "inbox": inbox},
            "open_findings": open_findings,
            "is_configured": config.is_configured(str(app_dir())),
            "connections": connections,
            "backfill": backfill,
            "org": org,
            "progress": progress_snapshot,
            "stalled": stalled,
            "backup_in_progress": backup_in_progress,
            "bulk_pass_active": bulk_pass_active,
            "watchdog_exits": watchdog_exits,
            "watchdog_limit_reached":
                watchdog_exits >= getattr(self, "_watchdog_max_exits", WATCHDOG_MAX_EXITS),
            "version": __import__("mcpbrain", fromlist=["__version__"]).__version__,
            "reflow": reflow,
            # Non-null while any Google build is waiting on a transient-failure
            # retry -- so a multi-day "token fine, network was not" outage is
            # visible here and in doctor, not only in the log.
            "google_unavailable": self._google_unavailable_status(),
            # Non-null while the restore hold is on (sync + backups held).
            "restore_pending": self.restore_status(),
            # Non-null after a NON-transient restore failure (not held).
            "restore_error": getattr(self, "_restore_error", None),
            # Non-null while the backup prune is holding off (backup_prune).
            "prune_held": getattr(self, "_prune_held", None),
            # Non-null while a backup runs: {"phase": "capture" | "upload",
            # "started", and for upload "sent", "total", "item"} -- `mcpbrain
            # backup` polls this.
            "backup_running": getattr(self, "_backup_progress", None),
            "backup": self._backup_status(),
            "backup_requested": bool(getattr(self, "_backup_requested", None)
                                     and self._backup_requested.is_set()),
        }

    def _backup_status(self) -> dict | None:
        """The incremental backup at a glance (spec §6): the newest committed
        restore point (capture time), the outbox (items / bytes waiting to
        upload), the upload in flight, the chain being captured and its delta
        ratio (committed delta bytes / base bytes; a new base is cut past
        settings.base_threshold), and the last error. Local reads only --
        never a Drive call; never raises (a degraded field is None)."""
        home = str(app_dir())
        out = {"restore_point": None, "pending": None, "pending_bytes": None,
               "uploading": None, "chain": None, "delta_ratio": None,
               "last_error": None, "last_discard": None}
        try:
            state = json.loads((Path(home) / "backup_state.json").read_text())
            if isinstance(state, dict):
                out["restore_point"] = state.get("restore_point")
                out["last_error"] = state.get("last_error")
                out["last_discard"] = state.get("last_discard")
        except (OSError, ValueError):
            pass
        try:
            out["pending"] = len(backup_outbox.pending_items(home))
            out["pending_bytes"] = backup_outbox.outbox_bytes(home)
        except Exception as exc:  # noqa: BLE001 — status must never raise
            log.debug("status: backup outbox unreadable: %s", exc)
        try:
            cs = backup_outbox._load_state(home)
            if cs is not None:
                out["chain"] = cs.chain
                out["delta_ratio"] = (round(cs.delta_bytes / cs.base_bytes, 4)
                                      if cs.base_bytes else None)
        except Exception as exc:  # noqa: BLE001 — status must never raise
            log.debug("status: backup capture state unreadable: %s", exc)
        running = getattr(self, "_backup_progress", None)
        if running and running.get("phase") == "upload":
            out["uploading"] = dict(running)
        return out

    def config_profile(self) -> dict:
        """Saved profile for the settings form — never includes the ClickUp secret."""
        cfg = config.read_config(str(app_dir()))
        return {
            "owner_full_name": cfg.get("owner_full_name", "") or "",
            "owner_name": cfg.get("owner_name", "") or "",
            "owner_email": cfg.get("owner_email", "") or "",
            "owner_role": cfg.get("owner_role", "") or "",
            "orgs": cfg.get("orgs") or [],
            "clickup_list_id": cfg.get("clickup_list_id", "") or "",
            "clickup_api_key_set": bool(cfg.get("clickup_api_key")),
            "timezone": cfg.get("timezone", "") or "",
            "home_dir": str(app_dir()),
            "records_dir": config.records_dir(str(app_dir())),
            "project_instructions": config.render_project_instructions(cfg),
            "fleet": config.fleet_defaults(cfg),
            "tenant": _tenant_block(),
        }

    def _routed_tool_handlers(self) -> dict:
        """name -> callable(arguments) for every MCP tool executed in this process.

        The daemon's half of the thin adapter. The MCP server keeps the protocol
        (transport, tools/list, argument validation, result envelopes) and routes
        the Store-touching tools here, so this process is the only one holding a
        Store handle -- which removes the multiple-writable-handle class by
        construction rather than by measurement.

        Each entry takes the raw `arguments` dict and returns the handler's
        result, or a coroutine call_tool awaits. Keeping the argument->kwargs
        mapping here (rather than passing kwargs from the MCP side) means the
        wire payload stays exactly the client's validated `arguments` object,
        with no second serialisation contract to keep in step.

        Built lazily and cached. A concurrent double-build is harmless -- these
        are pure closure/factory constructions over the same store -- so this
        needs no lock, unlike the embedder's genuinely expensive lazy build.

        NOT every Store-touching tool is here. `brain_graph` and
        `brain_draft_context` are a documented exception and stay in the MCP
        server: both report progress through `ctx.session.send_progress_notification`
        on the live MCP ServerSession, and /api/tool is one blocking round trip
        with no channel back mid-call, so routing them would silently drop
        per-hop/per-stage progress (pinned by tests/test_mcp_progress.py). See
        docs/superpowers/specs/2026-08-10-tool-registry-thin-adapter-followups.md
        for the fine-grained routing that would fix that. The six filesystem-only
        capture tools are absent for a different reason: they deliberately keep
        working with the daemon down.
        """
        if self._tool_handlers is None:
            # Imported HERE (not at module scope) for the same reason call_tool
            # imports it: mcpbrain.tools is mcp-free precisely so this process can
            # read the tool layer without paying the protocol stack's import.
            from mcpbrain.tools import (
                make_brain_actions,
                make_brain_context,
                make_brain_draft_save,
                make_brain_finding_resolve,
                make_brain_gardener_apply,
                make_brain_graph_correct,
                make_brain_meeting_pack_get,
                make_brain_meeting_pack_upsert,
                make_brain_meetings_today,
                make_brain_proactive,
            )

            # self._store is assigned once in __init__ and never rebound, so
            # binding it into the factories below cannot go stale -- and this is
            # the daemon's single writable handle, the one Phase 4 makes the only
            # one in existence.
            store, home = self._store, str(app_dir())
            context = make_brain_context(store)
            actions = make_brain_actions(store)
            proactive = make_brain_proactive(store)
            finding_resolve = make_brain_finding_resolve(store)
            graph_correct = make_brain_graph_correct(store)
            gardener_apply = make_brain_gardener_apply(store)
            draft_save = make_brain_draft_save(store, home)
            meetings_today = make_brain_meetings_today(store, home)
            meeting_pack_get = make_brain_meeting_pack_get(store)
            meeting_pack_upsert = make_brain_meeting_pack_upsert(store)

            # The argument->kwargs mapping mirrors on_call_tool's dispatch for
            # each tool, and each `.get(...)` default is the SAME default the MCP
            # side uses -- the two must agree, which is what
            # test_tool_exec_routing's local-vs-routed agreement tests pin.
            # brain_read is the one routed tool with no make_brain_* factory: the
            # MCP server dispatches it inline as a bare store.read_doc (see the
            # declare(...) comment in mcpbrain/tools.py), so its handler is that
            # same call.
            self._tool_handlers = {
                "brain_read": lambda a: store.read_doc(a["doc_id"]),
                "brain_context": lambda a: context(
                    entity=a.get("entity", ""),
                    mode=a.get("mode", "profile"),
                    community_id=a.get("community_id"),
                ),
                # The MCP side resolves an empty owner to the configured install
                # owner before forwarding, so `owner` normally arrives filled in;
                # brain_actions' own empty-owner fallback still applies for a
                # direct /api/tool caller.
                "brain_actions": lambda a: actions(a.get("owner", ""),
                                                   a.get("status") or "open"),
                "brain_proactive": lambda a: proactive(a.get("finding_type", ""),
                                                        a.get("severity", "")),
                "brain_finding_resolve": lambda a: finding_resolve(
                    finding_id=a.get("finding_id", 0),
                    outcome=a.get("outcome", ""),
                    note=a.get("note", ""),
                ),
                "brain_graph_correct": lambda a, c=None: graph_correct(a, c),
                "brain_gardener_apply": lambda a: gardener_apply(
                    lane=a.get("lane", ""),
                    filename=a.get("filename", ""),
                    content=a.get("content", ""),
                    asserts_person_role=bool(a.get("asserts_person_role", False)),
                    attribution_source=a.get("attribution_source", ""),
                    attribution_quote=a.get("attribution_quote", ""),
                    attribution_doc_id=a.get("attribution_doc_id", ""),
                ),
                "brain_draft_save": lambda a: draft_save(
                    email_id=a.get("email_id", ""),
                    thread_id=a.get("thread_id", ""),
                    intent=a.get("intent", ""),
                    final_draft=a.get("final_draft", ""),
                    parent_draft_id=a.get("parent_draft_id"),
                ),
                "brain_meetings_today": lambda a: meetings_today(),
                "brain_meeting_pack_get": lambda a: meeting_pack_get(
                    a.get("event_id", "")),
                "brain_meeting_pack_upsert": lambda a: meeting_pack_upsert(
                    event_id=a.get("event_id", ""),
                    event_title=a.get("event_title", ""),
                    event_date=a.get("event_date", ""),
                    pack_text=a.get("pack_text", ""),
                    attendees=a.get("attendees") or [],
                    context_hash=a.get("context_hash", ""),
                ),
            }
        return self._tool_handlers

    def call_tool(self, name: str, arguments: dict, confirmation: dict | None = None):
        """Execute a routed MCP tool here and return its result (for /api/tool).

        Raises ValueError -- surfaced as a 400, not a 500 -- for a tool this
        daemon does not route or for arguments that fail the tool's declared
        inputSchema. Both mean the MCP server and the daemon disagree about the
        advertised surface, which is a bug in us and should be loud; a handler
        that genuinely raises propagates untouched so its traceback survives.

        The schema check is a DEFENSIVE second line: the MCP server validates at
        the protocol boundary (that is where a failure has to become an isError
        result the model can read), and this one exists so a mismatch fails here
        instead of arriving as a KeyError from deep inside a handler. Its message
        is deliberately terser -- it goes to the daemon log, not to the model.

        `confirmation` is the out-of-band channel `brain_graph_correct` reads
        (an elicitation the MCP server obtained, or a dashboard confirm) -- it
        travels beside `arguments`, never inside them, so the model cannot
        forge one. Ignored for every other tool.
        """
        if self.is_restoring():
            from mcpbrain.store import StoreRestoringError
            raise StoreRestoringError("a restored snapshot is being swapped in; retry shortly")
        import jsonschema

        # Importing mcpbrain.tools is what POPULATES the registry `spec()` reads
        # below. _routed_tool_handlers() imports it too (for the make_brain_*
        # factories), but this one is still load-bearing: a caller that pre-seeds
        # `_tool_handlers` (test_tool_exec_routing does) short-circuits that build
        # entirely, and `spec(name)` would then raise KeyError against an empty
        # registry. That module is deliberately mcp-free and AST-guarded, so the
        # daemon pays 143 modules / ~11ms for it rather than the ~601 / ~0.26s the
        # protocol stack would cost -- which is the whole reason the tools live
        # there and not in mcp_server.py.
        from mcpbrain import tools as _tools  # noqa: F401 -- populates the registry
        from mcpbrain.tool_registry import spec

        handlers = self._routed_tool_handlers()
        if name not in handlers:
            raise ValueError(f"tool not routed to the daemon: {name}")
        if getattr(self, "_restore_pending", False):
            # Restore hold: the store must stay empty so the pending restore can
            # still run -- and anything written now would be lost when it does.
            # Read-only tools (readOnlyHint) still answer.
            ann = spec(name).annotations
            if not (ann is not None and ann.read_only_hint is True):
                from mcpbrain.control_api import RestoreHoldRefused
                raise RestoreHoldRefused(
                    f"{name} is unavailable: mcpbrain is waiting to restore your backup "
                    "(Google was unreachable at start-up) and is holding all writes so "
                    "nothing is lost. Read-only tools still work; this clears once the "
                    "restore runs.")
        try:
            jsonschema.validate(arguments, spec(name).input_schema)
        except jsonschema.ValidationError as exc:
            raise ValueError(f"invalid arguments for {name}: {exc.message}") from exc

        handler = handlers[name]
        result = (handler(arguments, confirmation) if name in _CONFIRMABLE_TOOLS
                  else handler(arguments))
        # Every routed tool except brain_read is an `async def` handler, and the
        # control API's handler threads have no running event loop -- so a
        # coroutine result is driven to completion here. A fresh loop per call is
        # the right shape for these handlers: they hold no loop-affine state and
        # await nothing but their own store work.
        if asyncio.iscoroutine(result):
            result = asyncio.run(result)
        return result

    def search(self, query: str, limit: int = 5, *, expand: bool = False) -> list[dict]:
        """Semantic recall for the UserPromptSubmit hook (via /api/recall).

        Read-only and best-effort: returns compact {doc_id, score, distance,
        text} dicts, or [] on any failure — recall must never break a prompt.

        Absolute off-topic gate: embed the query once, take the nearest chunk's
        L2 distance, and if even that is past `recall_max_distance` the query is
        off-topic relative to the brain — return nothing. Unlike `score` (which
        is intra-query-normalised, so the top hit is always ~1.0 and can't tell
        an off-topic query apart), the raw distance is an absolute relevance
        signal. On-topic queries then get the normal hybrid ranking.
        """
        if self.is_restoring():
            return []        # a restored snapshot is being swapped in
        try:
            try:
                qv = self._embedder.embed_query(query)
            except RuntimeError:
                # Model not downloaded yet (lazy embedder). Recall degrades to empty
                # rather than crashing the control-API caller; the wizard drives the
                # download and recall works once it's cached.
                return []
            # Same depth hybrid_search reads from vec_knn, so every hit the
            # vector ranker contributed has a measured distance below.
            knn = self._store.vec_knn(qv, max(FUSION_DEPTH, limit * 2))
            if not knn or knn[0][1] > config.recall_max_distance(str(app_dir())):
                return []  # nothing close enough -> off-topic -> inject nothing
            dist = {doc: d for doc, d in knn}
            home = str(app_dir())
            # B3: three-axis weights (safe no-op when importance_recall is off)
            search_kwargs: dict = {"query_vec": qv}
            search_kwargs["collapse_documents"] = config.retrieval_collapse_documents_enabled(home)
            if config.importance_recall_enabled(home):
                w = config.importance_weights(home)
                search_kwargs.update({
                    "recency_weight":    w["recency_weight"],
                    "importance_weight": w["importance_weight"],
                    "decay_weight":      w["decay_weight"],
                    "recency_alpha":     w["recency_alpha"],
                })
            # Cold chunks stay searchable by default: the salience gate is an
            # enrichment-cost optimization (skip graph-extraction), NOT a retrieval
            # filter. Excluding cold from recall halved gold recall (0.75→0.35) once
            # the salience backfill grew the cold set, so exclusion is now decoupled
            # from tiered_memory and behind its own opt-in flag (default OFF).
            if config.recall_excludes_cold(home):
                search_kwargs["exclude_cold"] = True
            # Q6: route() wraps hybrid_search with routing/CRAG/rerank when flags on
            if any([config.retrieval_routing_enabled(home),
                    config.retrieval_crag_enabled(home),
                    config.retrieval_rerank_enabled(home)]):
                from mcpbrain.query_router import route as _route
                hits = _route(self._store, self._embedder, query, limit,
                              home=home, **search_kwargs)
            else:
                hits = hybrid_search(self._store, self._embedder, query, limit,
                                     **search_kwargs)
        except Exception:  # noqa: BLE001 — recall must never raise into the prompt path
            log.warning("recall search failed for %r", query, exc_info=True)
            return []
        # B5: strengthen recalled chunks (update memory_strength + last_accessed)
        recalled_ids = [c.get("doc_id") for c in hits if c.get("doc_id")]
        if recalled_ids and config.decay_enabled(str(app_dir())):
            try:
                from mcpbrain.decay import update_on_recall
                update_on_recall(self._store, recalled_ids)
            except Exception:  # noqa: BLE001
                pass
        # Shape the ranked hits into compact result dicts. Use the existing
        # distance field (set by router for synthetic results like community
        # summaries) when present; otherwise look it up from KNN. A hit outside
        # the KNN window (keyword-only: the window is the same FUSION_DEPTH
        # hybrid_search reads) has no measured distance: report None, never the BEST distance,
        # which would claim it is as close as the nearest chunk. No consumer
        # reads it as a float (prompt_recall/feedback rank on score;
        # maybe_expand passes it through).
        def _distance(c):
            if c.get("distance") is not None:
                return round(float(c["distance"]), 3)
            d = dist.get(c.get("doc_id"))
            return round(float(d), 3) if d is not None else None

        result_hits = [{"doc_id": c.get("doc_id"),
                        "score": round(float(c.get("score") or 0.0), 3),
                        "distance": _distance(c),
                        "text": c.get("text") or ""} for c in hits]
        from mcpbrain.retrieval_expand import maybe_expand
        try:
            return maybe_expand(self._store, result_hits, home=home, expand=expand)
        except Exception:  # noqa: BLE001 — recall must never raise
            return result_hits

    def _resolve_google_account(self, token_file) -> str:
        """Return the connected Google account email, resolving lazily.

        Reads ``MCPBRAIN_HOME/google_account`` first (cache). If missing, tries
        the consent-time-populated "account" field of the token JSON. If still
        missing AND a Gmail service is available, calls
        ``users().getProfile(userId='me')`` once and writes the email to the
        sidecar so subsequent polls stay offline. All errors degrade to "".
        """
        sidecar = app_dir() / "google_account"
        # Cache hit: trust the sidecar.
        try:
            cached = sidecar.read_text().strip()
            if cached:
                return cached
        except OSError:
            pass
        # Consent-time field (typically empty here, but cheap to check).
        try:
            from_token = (json.loads(token_file.read_text()).get("account") or "").strip()
        except (OSError, ValueError):
            from_token = ""
        if from_token:
            self._cache_google_account(sidecar, from_token)
            return from_token
        # Last resort: Gmail getProfile, but only if we already have a service.
        # Read what is resolved rather than calling ensure_services(): this is
        # the status-poll path, and while a transient-failure retry is pending
        # a poll must not be the thing that blocks on the token endpoint.
        try:
            gmail = (self._services.get("gmail_service")
                     if getattr(self, "_services_resolved", False) else None)
        except Exception:  # noqa: BLE001
            gmail = None
        if gmail is None:
            return ""
        try:
            profile = gmail.users().getProfile(
                userId="me").execute(num_retries=_NUM_RETRIES)
            email = (profile.get("emailAddress") or "").strip()
        except Exception as exc:  # noqa: BLE001 — must not break status polls
            log.debug("status: getProfile failed: %s", exc)
            return ""
        if email:
            self._cache_google_account(sidecar, email)
        return email

    def _resolve_google_name(self, creds) -> str:
        """The connected Google account's display name, cached to a sidecar
        (``MCPBRAIN_HOME/google_name``) so /api/status polls stay offline. Calls
        the userinfo API once on a cache miss; returns "" (and caches nothing) if
        the token lacks the profile scope. Errors degrade to "".
        """
        sidecar = app_dir() / "google_name"
        try:
            cached = sidecar.read_text().strip()
            if cached:
                return cached
        except OSError:
            pass
        name = auth.fetch_google_name(creds)
        if name:
            self._cache_google_account(sidecar, name)   # generic 0600 sidecar writer
        return name

    @staticmethod
    def _cache_google_account(sidecar, email: str) -> None:
        try:
            sidecar.write_text(email)
            os.chmod(sidecar, 0o600)
        except OSError as exc:
            log.debug("status: failed to write google_account sidecar: %s", exc)

    def _assign_cadences(self, cadences: dict) -> None:
        """Re-wire every cadence interval from a _cadences_from_config result.
        Caller holds _config_lock. Intervals only: the _last_* anchors persist
        across a re-wire so a cadence change does not reset the clock."""
        self._communities_interval_s = cadences["communities_interval_s"]
        self._lint_interval_s = cadences["lint_interval_s"]
        self._synthesise_interval_s = cadences["synthesise_interval_s"]
        self._proactive_interval_s = cadences["proactive_interval_s"]
        self._waiting_on_interval_s = cadences["waiting_on_interval_s"]
        self._blocks_interval_s = cadences["blocks_interval_s"]
        self._audit_interval_s = cadences["audit_interval_s"]
        self._stale_reextract_interval_s = cadences["stale_reextract_interval_s"]
        self._auto_update_interval_s = cadences["auto_update_interval_s"]
        self._verify_interval_s = cadences["verify_interval_s"]
        self._feedback_aggregate_interval_s = cadences["feedback_aggregate_interval_s"]
        self._org_backfill_interval_s = cadences["org_backfill_interval_s"]
        self._resolve_entities_interval_s = cadences["resolve_entities_interval_s"]
        self._review_interval_s = cadences["review_interval_s"]
        self._action_hygiene_interval_s = cadences["action_hygiene_interval_s"]
        self._mcp_heartbeat_sweep_interval_s = cadences["mcp_heartbeat_sweep_interval_s"]
        self._salience_score_interval_s = cadences["salience_score_interval_s"]
        self._decay_pass_interval_s = cadences["decay_pass_interval_s"]
        self._consolidation_interval_s = cadences["consolidation_interval_s"]
        self._voice_analyse_interval_s = cadences["voice_analyse_interval_s"]
        self._self_improve_interval_s = cadences["self_improve_interval_s"]
        self._auto_enable_interval_s = cadences["auto_enable_interval_s"]
        self._org_contrib_upload_interval_s = cadences["org_contrib_upload_interval_s"]
        self._org_import_interval_s = cadences["org_import_interval_s"]
        self._org_curate_interval_s = cadences["org_curate_interval_s"]
        self._ocr_setup_interval_s = cadences["ocr_setup_interval_s"]
        self._reflow_seed_interval_s = cadences["reflow_seed_interval_s"]

    def apply_config(self, body: dict) -> None:
        """Persist config updates, then re-wire enrich + backup from disk.

        Writes via config.write_config (atomic, 0600) then rebuilds the enrich
        client and backup config from the freshly-written config so a key change
        takes effect without a restart. Never logs the key.
        """
        home = str(app_dir())
        config.write_config(home, body)
        # Zero-touch enrichment: the first time an install becomes configured
        # (identity + >=1 org saved), turn enrichment on so the daemon starts
        # spooling the un-enriched backlog. Only auto-flip when the caller didn't
        # set enrich_mode itself (an explicit "off" is honored) and it's still the
        # "off" default — so a later save won't re-flip a deliberate choice.
        if ("enrich_mode" not in body
                and config.is_configured(home)
                and config.enrich_mode(home) == "off"):
            config.write_config(home, {"enrich_mode": "spool"})
        # Build both off-lock (network/IO work), then set all daemon-config
        # mutation under _config_lock so the loop thread never reads a new
        # _backup paired with a stale interval. Keep the lock hold time to
        # the assignments only.
        enrich_mode = config.enrich_mode(home)
        # build_lock (not _config_lock) serialises this backup build with a
        # concurrent _retry_backup_build, so an in-flight retry can never land
        # a stale config over the one just saved. Lock order matches the retry:
        # build_lock, then _config_lock.
        bo = self._google_backoff("_backup_backoff", "backup")
        with bo.build_lock:
            backup_build = _backup_from_config(home)
            backup_cfg, backup_interval = backup_build
            cadences = _cadences_from_config(home)  # IO off-lock; assign under lock below
            with self._config_lock:
                self._enrich_mode = enrich_mode
                self._backup = backup_cfg
                self._backup_interval_s = backup_interval
                self._assign_cadences(cadences)
            # A transient build failure schedules a retry (_retry_backup_build)
            # instead of leaving backup off for the rest of the run.
            transient = getattr(backup_build, "transient", None)
            if transient is not None:
                bo.failed(transient)
            elif backup_cfg is not None:
                bo.succeeded()
            else:
                bo.clear()
        # Best-effort: keep the records-repo scaffold current whenever settings
        # are saved. Failures never fail the POST.
        try:
            from mcpbrain import records
            records.scaffold_records(home)
        except Exception as exc:  # noqa: BLE001
            log.warning("apply_config materialise degraded: %s", exc)

    def start_auth(self) -> None:
        """Run the interactive Google OAuth consent flow (blocking).

        Opens a browser and writes the token file. The control API runs this on
        a background thread so the POST returns immediately.

        Single-flight: a non-blocking lock guards the flow so a double-click or
        wizard retry can't spawn a second redirect server + browser tab. If a
        flow is already running this returns immediately as a no-op.
        """
        if not self._auth_lock.acquire(blocking=False):
            log.info("auth flow already in progress; ignoring duplicate request")
            return
        try:
            auth.run_consent_flow()
            # Drop the cached account so a different Google identity is
            # re-resolved next /api/status poll instead of showing the old one.
            try:
                (app_dir() / "google_account").unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.debug("could not clear google_account sidecar: %s", exc)
        finally:
            self._auth_lock.release()

    def start_enrich_backfill(self) -> None:
        """One-shot enrich-backfill on a daemon thread. Single-flight; pauses the
        daemon's own write cycle for the duration so there is only one writer."""
        import threading
        from mcpbrain import enrich_backfill
        if not self._backfill_lock.acquire(blocking=False):
            log.info("enrich-backfill already running; ignoring duplicate start")
            return
        self._backfill_active.set()
        def _run():
            try:
                enrich_backfill.run_backfill(store=self._store, embedder=self._embedder)
            except Exception as exc:  # noqa: BLE001
                log.warning("enrich-backfill failed: %s", exc)
            finally:
                self._backfill_active.clear()
                self._backfill_lock.release()
        threading.Thread(target=_run, daemon=True).start()

    def cancel_enrich_backfill(self) -> None:
        """Write the cancel flag so the running enrich-backfill loop stops cleanly."""
        from mcpbrain import enrich_backfill
        enrich_backfill.request_cancel(str(app_dir()))

    def bootstrap_baseline_once(self, services=None, *, force=False) -> dict | None:
        """Import the org snapshot + shared-drive ingest caches before first sync.

        Idempotent: a no-op after it completes once (in-process latch + on-disk
        marker); re-runnable with force=True (doctor / `mcpbrain bootstrap`).
        Degrades cleanly (no fleet folder / snapshot / pin) and never raises into
        the sync cycle."""
        if getattr(self, "_restore_pending", False):
            # Restore hold: importing shared-drive caches would make the empty
            # store non-empty, the next start would then skip the restore, and
            # backups would resume over the good snapshot (the incident chain).
            return {"status": "skipped", "reason": "restore pending"}
        home = str(app_dir())
        if not force and self._baseline_bootstrap_done:
            return None
        if not force and not onboarding.should_bootstrap(home):
            return None
        # Serialise the loop thread and the force/control-API path: run_bootstrap
        # is a single-writer transaction + resume-marker read/write, not
        # concurrency-safe. Re-check the latch inside the lock (double-checked) so
        # a waiter that blocked behind a just-finished run doesn't redo the work.
        with self._bootstrap_lock:
            if not force and self._baseline_bootstrap_done:
                return None
            if services is None:
                services = self.ensure_services()
            try:
                result = onboarding.run_bootstrap(
                    home, self._store,
                    drive_service=services.get("drive_service"), force=force)
            except Exception as exc:  # noqa: BLE001 — bootstrap must never break sync
                log.warning("baseline bootstrap failed: %s", exc, exc_info=True)
                return {"status": "error", "error": str(exc)}
            if result.get("status") == "done":
                self._baseline_bootstrap_done = True
            return result

    # -- wake / stop --------------------------------------------------------

    def sync_now(self) -> None:
        """Wake the loop for an immediate cycle."""
        self._wake.set()

    def request_backup(self) -> bool:
        """Capture now and upload until the outbox is empty: makes the next
        maybe_backup due regardless of the interval, the failure backoff and
        the dark-wake deferral, lets uploads run on a dark-waking Mac until
        the outbox drains, and wakes the loop. `backup_requested` (status)
        stays true until the requested capture has been taken, found nothing
        to capture, or failed (the error is in backup_state.json); a capture
        skipped for a busy lock or a full outbox keeps it set. Returns False
        when backup is not configured (nothing will run)."""
        with self._config_lock:
            if self._backup is None:
                return False
        self._backup_requested.set()
        self._backup_drain_requested.set()
        self._wake.set()
        return True

    def stop(self) -> None:
        """Signal the loop to exit, and wake it so run() returns promptly.

        Also stops and joins the maintenance thread (_shutdown_maintenance),
        same as run()'s own _pending_update path: a deliberate stop() must
        leave no writer thread still running once the caller gets control
        back, for the same single-writer reason described there.
        """
        self._shutdown_maintenance()
        self._wake.set()

    def is_stopped(self) -> bool:
        """Return True if stop() has been called (the _stop event is set)."""
        return self._stop.is_set()

    def _shutdown_maintenance(self, timeout: float = 10.0) -> None:
        """Stop and join the maintenance thread.

        Must run before run() releases SingleWriterLock on the _pending_update
        path (and from stop()): otherwise update_from_index reinstalls (or a
        deliberate stop()'s caller proceeds) while the maintenance thread is
        still writing SQLite, and the successor process -- which now waits
        HANDOVER_LOCK_WAIT_S for the lock rather than failing outright --
        becomes a SECOND concurrent writer for that window, exactly what
        SingleWriterLock exists to prevent.

        Sets _stop unconditionally first (idempotent: run()'s own _stop.is_set()
        check and stop()'s previous direct `self._stop.set()` both relied on
        this happening), then joins the maintenance thread if one exists and is
        alive. getattr guards minimally-constructed Daemon doubles in tests that
        may not have started (or even set) _maintenance_thread.

        If the thread is STILL alive after the join times out, this logs an
        ERROR naming the risk and returns anyway rather than blocking forever
        (Important finding, final whole-branch review): gated-pass execution
        is deliberately unbounded elsewhere in this file (see
        `BULK_LOCK_ACQUIRE_S` / `_cycle_bulk_section`'s docstring -- a gated
        pass's ACQUIRE is bounded, its execution once acquired is not), so a
        mid-pass maintenance thread surviving this join is a real, reachable
        case, not a hypothetical. The caller (`run()`'s `_pending_update`
        path, and `stop()`) still proceeds to release `SingleWriterLock`
        right after this returns either way -- blocking here indefinitely
        would trade a visible, logged hazard for a daemon that can never
        restart/update at all, which is its own outage, and there is no safe
        finite bound to block on instead that would not sometimes cut off a
        legitimately-long pass. Making the failure loud (rather than blocking
        longer) matches how the rest of this file treats a lock hold that can
        legitimately outlast one tick's bound (`BULK_LOCK_ACQUIRE_S`,
        the backup capture's `_bounded_bulk_lock`): skip/log, don't block forever.
        """
        self._stop.set()
        t = getattr(self, "_maintenance_thread", None)
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
            if t.is_alive():
                log.error(
                    "maintenance thread did not stop within %.1fs of "
                    "shutdown (likely mid gated-pass, which runs unbounded "
                    "once acquired); proceeding anyway -- this risks a "
                    "second concurrent writer if an update/restart follows",
                    timeout)

    # -- one cycle ----------------------------------------------------------

    def _graph_cleanup_once(self) -> None:
        """One-shot graph hygiene on upgrade: drop self-loops + type-invalid
        relations and fold org-tag drift left by pre-0.7.34 enrichment. Guarded by a
        meta flag so it runs at most once per install. Best-effort; never raises.

        The mcpbrain.maintenance subpackage is dev-only tooling excluded from the
        wheel (pyproject `exclude = ["mcpbrain.maintenance*"]`), so a wheel install
        is expected to miss it; that's flagged done (silently) rather than retried
        and warned about every cycle."""
        flag = "graph_cleanup_v1"
        try:
            with self._store._connect() as db:
                if db.execute("SELECT 1 FROM meta WHERE k=?", (flag,)).fetchone():
                    return
            try:
                from mcpbrain.maintenance.graph_cleanup import cleanup_graph  # noqa: F401  (dev-only; excluded from the wheel)
            except ImportError:
                log.debug("maintenance module not installed (expected in a wheel install); skipping graph cleanup")
                with self._store._connect(write=True) as db:
                    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (flag, "1"))
                return
            counts = cleanup_graph(self._store)
            with self._store._connect(write=True) as db:
                db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (flag, "1"))
            log.info("graph cleanup (one-shot): %s", counts)
        except Exception as exc:  # noqa: BLE001
            log.warning("graph cleanup skipped: %s", exc)

    def _graph_recompute_once(self) -> None:
        """One-shot recency recompute on upgrade: make the newest-dated
        works_at/reports_to current per entity, correcting facts that backfill
        applied out of chronological order. Meta-flagged; best-effort.

        See _graph_cleanup_once: a missing mcpbrain.maintenance (wheel install)
        is expected and flagged done at debug level, not warned about."""
        flag = "singleton_recompute_v1"
        try:
            with self._store._connect() as db:
                if db.execute("SELECT 1 FROM meta WHERE k=?", (flag,)).fetchone():
                    return
            try:
                from mcpbrain.maintenance.graph_cleanup import recompute_singletons  # noqa: F401  (dev-only; excluded from the wheel)
            except ImportError:
                log.debug("maintenance module not installed (expected in a wheel install); skipping singleton recompute")
                with self._store._connect(write=True) as db:
                    db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (flag, "1"))
                return
            counts = recompute_singletons(self._store)
            with self._store._connect(write=True) as db:
                db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (flag, "1"))
            log.info("singleton recency recompute (one-shot): %s", counts)
        except Exception as exc:  # noqa: BLE001
            log.warning("singleton recompute skipped: %s", exc)

    # Generation key used for _pending_synthesis in _stash_generation. It is a
    # single list, not a dict of independently-keyed batches like
    # _pending_blocks/_pending_audit, so one fixed key (rather than a per-item
    # key) is enough to track its own rewrite history.
    _SYNTHESIS_GEN_KEY = "_pending_synthesis"

    def _bump_stash_gen(self, *keys: str) -> None:
        """Record that `keys` were just (re)written to a _pending_* stash.

        Call this while already holding _stash_lock, right alongside the write
        itself (a single multi-key write, e.g. _run_blocks's one `.update()`
        call for three keys, bumps all of them together in one lock hold).
        _stash_clear_drained compares this against the generation captured at
        snapshot time to tell "this key was rewritten mid-cycle" apart from
        "this key is unchanged since the snapshot, safe to delete."
        """
        for key in keys:
            self._stash_generation[key] = self._stash_generation.get(key, 0) + 1

    def _stash_snapshot(self) -> dict:
        """Atomically snapshot the three request stashes plus their generations.

        Does NOT clear anything — pairs with _stash_clear_drained, which only
        deletes the keys the caller's cycle actually confirmed were drained,
        and only if nothing rewrote them in the meantime. Replaces the old
        `with self._stash_lock: synthesis_requests = ...; merged = {...}` read
        in run_one with a single named snapshot both halves of that method now
        share.
        """
        with self._stash_lock:
            return {
                "blocks": dict(self._pending_blocks),
                "audit": dict(self._pending_audit),
                "synthesis": list(self._pending_synthesis),
                "gen": dict(self._stash_generation),
            }

    def _stash_clear_drained(self, drained: dict, taken: dict) -> None:
        """Delete only stash entries that were actually drained AND have NOT
        been rewritten since `taken` (from _stash_snapshot) was captured.

        run_one snapshots, the (possibly long) sync+enrich cycle runs, and only
        then does the drain result say what was actually applied. If a cadence
        pass on the maintenance thread rewrote a key in between (e.g.
        maybe_blocks firing again mid-cycle), the OLD read-then-delete would
        delete that fresh, never-yet-drained batch right along with the
        answered one it was really meant to clear -- a live silent-data-loss
        shape, same family as the 2026-06-05 stash-loss incident this whole
        _pending_* mechanism exists to avoid. Comparing the live generation
        against the one captured in `taken["gen"]` tells the two cases apart:
        unchanged generation means safe to delete; changed means keep the
        fresh write and let it ride to the next cycle.
        """
        taken_gen = taken.get("gen") or {}
        with self._stash_lock:
            for name in ("_pending_blocks", "_pending_audit"):
                store = getattr(self, name)
                for key in list(store):
                    if f"{key}_drained" not in drained:
                        continue
                    if self._stash_generation.get(key) != taken_gen.get(key):
                        continue      # rewritten mid-cycle; keep the fresh batch
                    log.info("block %s answers drained (%s); stash cleared",
                             key, drained[f"{key}_drained"])
                    del store[key]
            if drained.get("synthesis_written"):
                if self._stash_generation.get(self._SYNTHESIS_GEN_KEY) \
                        == taken_gen.get(self._SYNTHESIS_GEN_KEY):
                    self._pending_synthesis = []

    def run_one(self) -> dict | None:
        """Run a single cycle, unless paused.

        When paused, returns None and writes nothing to the store (the pause
        guarantee). Otherwise runs run_cycle with the configured services and
        returns its result dict.

        Also returns None (no store writes -- single-writer, yield to the
        backfill) while `_backfill_active` is set. Important I-1 (final
        whole-branch review): a backfill runs on its OWN thread and can
        legitimately take far longer than STALL_S (2000s+ observed live), but
        this early return used to do nothing -- leaving "cycle"/"sync"
        ageing for the backfill's whole duration with no restamp, unlike the
        `_pause` branch above (which resume() restamps on the way out).
        Restamping ALL progress here every time run() calls back in
        (~_interval_s) means a long, legitimate backfill reads as "busy on
        accounted-for work", not a wedge -- see `_restamp_all_progress`.
        """
        if self._pause.is_set():
            return None
        if self._backfill_active.is_set():
            self._restamp_all_progress()
            return None
        if getattr(self, "_restore_pending", False):
            # Restore hold: nothing is synced, so the store stays empty and the
            # pending restore can still run. Liveness is restamped exactly as
            # for a backfill, so the hold never reads as a stall.
            self._restamp_all_progress()
            return None
        self._graph_cleanup_once()
        self._graph_recompute_once()
        services = self.ensure_services()
        # Before the first real sync: seed the graph from the org snapshot and
        # bulk-import shared-drive caches, so run_cycle only extracts cache-misses.
        self.bootstrap_baseline_once(services)
        # Snapshot the enrich client + mode under the config lock so apply_config
        # (HTTP handler thread) can't swap them mid-cycle; use the locals for this
        # cycle.
        with self._config_lock:
            enrich_client = self._enrich_client
            enrich_mode = self._enrich_mode
        # Gate: no enrichment until the install is configured. Sync still runs.
        enrich_mode = _gated_enrich_mode(enrich_mode, str(app_dir()))
        # The spool prepare step folds in the merge-review block every cycle —
        # merge_review candidates are cheap to generate and the LLM adjudication
        # tier has been removed (§9A), so there is no longer a cadence to gate on.
        resolution_due = True
        # Stashed synthesis/block requests are RE-ATTACHED every cycle, not
        # consumed by one: prepare_units() writes a fresh batch of work units
        # each cycle, so a one-shot attach survives only until the next
        # production run (~one interval) unless the out-of-band extractor
        # happens to pull the unit in that window (live 2026-06-05 loss). Each
        # stash is cleared below, once the drain summary shows its answers
        # actually came back; until then every freshly-produced batch of units
        # carries the same requests.
        taken = self._stash_snapshot()
        synthesis_requests = taken["synthesis"]
        merged = {**taken["blocks"], **taken["audit"]}
        extra_blocks = {k: v for k, v in merged.items() if v} or None
        if extra_blocks:
            log.info("extra blocks attached: %s",
                     {k: len(v) for k, v in extra_blocks.items()})
        # Bound this cycle's bulk work (embed/sync/drain) to CYCLE_BUDGET_S so
        # the loop always reaches the maintenance passes and heartbeat below it,
        # even on a large backlog. See CYCLE_BUDGET_S for the incident this fixes.
        budget = Budget(getattr(self, "_cycle_budget_s", CYCLE_BUDGET_S), clock=self._clock)
        result = run_cycle(self._store, self._embedder,
                           enrich_client=enrich_client,
                           enrich_limit=self._enrich_batch,
                           enrich_mode=enrich_mode,
                           resolution_due=resolution_due,
                           synthesis_requests=synthesis_requests,
                           extra_blocks=extra_blocks,
                           budget=budget,
                           on_progress=self._note_progress,
                           bulk_section=self._cycle_bulk_section,
                           embed_max_items=getattr(self, "_embed_max_items", EMBED_MAX_ITEMS),
                           **services)
        drained = ((result or {}).get("enrich") or {}).get("drain") or {}
        self._stash_clear_drained(drained, taken)
        self._note_progress("cycle")
        return result

    # -- cadence helpers ----------------------------------------------------

    def _is_due(self, interval_attr: str, last_attr: str) -> bool:
        """Return True when the named cadence pass is overdue.

        False if the interval attribute is None (pass is OFF).
        True if last_attr is None (never run yet).
        True if clock() - last >= interval.
        """
        interval = getattr(self, interval_attr)
        if interval is None:
            return False
        last = getattr(self, last_attr)
        if last is None:
            return True
        return (self._clock() - last) >= interval

    # -- periodic backup ----------------------------------------------------

    def maybe_backup(self) -> dict | None:
        """CAPTURE a backup restore point into the local outbox, if one is due.

        Incremental backup (spec 2026-10-19 §2): capture copies the live store
        with SQLite's online backup API and writes only the pages that changed
        since the last capture (plus records/config when they changed) into
        `backup/outbox/` -- local work, seconds. Shipping it to Drive is
        `upload_backup_step`'s job, every loop iteration, outside the bulk lock.

        OFF unless a BackupConfig was supplied: returns None when self._backup
        is None. Otherwise gates on a time-based cadence using the injected
        clock -- due on the first call (self._last_backup is None) or once
        `backup_interval_s` (settings.capture_interval_s) has elapsed, counting
        sleep (max of monotonic and wall-clock elapsed). After a failure the
        interval shrinks to BACKUP_RETRY_BASE_S doubling per consecutive
        failure. `request_backup()` makes it due now and bypasses the interval,
        the failure backoff and the dark-wake deferral. Not due -> None.

        When due, on macOS: defers while the system is merely dark-waking
        (`_mac_user_is_active`), without stamping the cadence clock.

        Skips WITHOUT stamping the cadence clock or counting a failure (so the
        next iteration retries) when:
        - a restore is pending (a capture of a near-empty store must never
          become the newest restore point),
        - the live store holds no chunks (R28: same reason),
        - the bulk lock could not be acquired in `_bulk_lock_wait_s` (R3:
          `backup_outbox.LockBusy`),
        - the outbox is full (`OutboxFull`: recorded in backup_state.json as a
          failed attempt with a clear error; uploads keep draining it).

        The lock passed to capture is the bulk lock with the bounded acquire
        (`_bounded_bulk_lock`): capture holds it for the `backup()` copy only;
        hashing, compression and encryption run on the private copy outside
        it. `_backup_in_progress` is set around the capture only.

        Any other capture failure is logged and swallowed (the loop keeps
        running), recorded via write_backup_state(ok=False), and counts toward
        the failure backoff. The cadence clock is stamped on ATTEMPT (bounds a
        failing capture to one attempt per backoff step) and again on success.
        A successful capture records nothing in backup_state.json: only a
        COMMIT (the uploader's manifest upload) is a backup that landed.

        Returns {"captured": bool, ...}: "item" (the new item's name, None when
        nothing changed), or "skipped"/"deferred"/"error".
        """
        if self._backfill_active.is_set():
            return None  # single-writer: yield to the backfill
        # Snapshot the (backup, interval) pair atomically under the lock so a
        # concurrent apply_config can't hand us a new config with the old
        # interval. Use the locals for the rest of the method.
        with self._config_lock:
            backup_cfg, interval = self._backup, self._backup_interval_s

        if backup_cfg is None:
            return None

        # A restore that a transient outage skipped is still pending: capturing
        # now would put a near-empty store at the head of the history and the
        # prune would rotate the good ones out. Never capture, upload or prune
        # until the restore has run (see RESTORE_STARTUP_WAIT_S).
        if getattr(self, "_restore_pending", False):
            return {"captured": False, "skipped": "restore_pending"}

        # Outbox full: paused. Re-check CHEAPLY (outbox_bytes only -- no
        # sweep, no copy) at most every BACKUP_RETRY_BASE_S here; the upload
        # step re-checks after every step it runs, which is what normally
        # lifts the pause. Recorded in backup_state ONCE, on the transition
        # into full (see the OutboxFull handler), never per iteration.
        if getattr(self, "_outbox_full_since", None) is not None:
            checked = getattr(self, "_outbox_full_checked", None)
            if checked is not None and self._clock() - checked < BACKUP_RETRY_BASE_S:
                return {"captured": False, "skipped": "outbox_full"}
            if self._outbox_still_full(backup_cfg):
                return {"captured": False, "skipped": "outbox_full"}
            log.info("backup outbox drained below its cap; capture resumes")

        # getattr: an instance built without __init__ has neither (same
        # convention as _last_backup_wall below).
        requested_ev = getattr(self, "_backup_requested", None)
        requested = bool(requested_ev is not None and requested_ev.is_set())
        failures_so_far = getattr(self, "_backup_failures", 0)
        if failures_so_far:
            # A failed backup retries sooner than the interval.
            interval = min(interval, BACKUP_RETRY_BASE_S
                           * 2 ** min(failures_so_far - 1, 16))
        if self._last_backup is not None and not requested:
            elapsed = self._clock() - self._last_backup
            # getattr: an instance built without __init__ (or a path that sets
            # _last_backup alone) has no wall twin -- fall back to monotonic.
            last_wall = getattr(self, "_last_backup_wall", None)
            if last_wall is not None:
                # Wall-clock elapsed counts time asleep (see __init__). A
                # backwards wall-clock jump makes this small or negative, and
                # the max falls back to monotonic, so it can never suppress a
                # backup monotonic already considers due.
                wall_clock = getattr(self, "_wall_clock", time.time)
                elapsed = max(elapsed, wall_clock() - last_wall)
            if elapsed < interval:
                return None

        # A capture IS due. Before touching the cadence clock or doing any
        # work: on macOS, defer rather than attempt if the system is merely
        # dark-waking (see _mac_user_is_active's docstring for the live
        # incident this closes). Deliberately does NOT stamp _last_backup.
        if (not requested and sys.platform == "darwin"
                and not _mac_user_is_active()):
            log.info("backup capture due, but system is dark-waking "
                     "(not user-active) — deferring to next check")
            return {"captured": False, "deferred": True}

        # R28: an empty store must never become the newest restore point (a
        # reinstall before its restore ran, a store swapped mid-restore). Not
        # a failure and not an attempt: the cadence is untouched.
        try:
            empty = self._store.chunk_count() == 0
        except Exception as exc:  # noqa: BLE001 — unknown is not "empty"
            log.warning("backup capture: chunk count unavailable (%s)", exc)
            empty = False
        if empty:
            if requested_ev is not None:
                requested_ev.clear()   # nothing to capture: the request is answered
            log.info("backup capture skipped: the store holds no chunks")
            return {"captured": False, "skipped": "empty_store"}

        home = str(app_dir())
        started = self._wall_clock()
        attempt_mono, attempt_wall = self._clock(), self._wall_clock()
        self._backup_progress = {"phase": "capture", "started": started}
        # Set around the capture only (copy + local write), never the upload:
        # the watchdog defers recovery while it is set.
        self._backup_in_progress.set()
        try:
            item = backup_outbox.capture(
                home, self._store.path, backup_cfg.key,
                lock=self._bounded_bulk_lock(),
                settings=backup_cfg.settings, clock=self._wall_clock)
        except backup_outbox.LockBusy as exc:
            log.warning("backup capture skipped this cycle: %s; will retry next "
                        "cycle", exc)
            return {"captured": False, "skipped": "lock_busy"}
        except backup_outbox.OutboxFull as exc:
            try:
                mb = f"{backup_outbox.outbox_bytes(home) / 1_000_000:,.0f}"
            except Exception:  # noqa: BLE001 — the handler must never raise
                mb = "?"
            err = (f"outbox full: {mb} MB waiting to upload; capture "
                   "paused until the upload drains it")
            # The transition into "full" is recorded once; the pause then
            # re-checks cheaply (see the gate above) instead of re-raising,
            # re-logging and re-recording a failed attempt every iteration.
            self._outbox_full_since = self._wall_clock()
            self._outbox_full_checked = self._clock()
            log.warning("backup capture: %s (%s)", err, exc)
            write_backup_state(home, ok=False, error=err)
            return {"captured": False, "error": err, "skipped": "outbox_full"}
        except Exception as exc:  # noqa: BLE001 — backup must never crash the loop
            log.warning("backup capture failed: %s", exc, exc_info=True)
            # Stamped on ATTEMPT: a capture that can never succeed (disk full)
            # retries once per backoff step, not every cycle (2026-08-04).
            self._last_backup, self._last_backup_wall = attempt_mono, attempt_wall
            if requested_ev is not None:
                requested_ev.clear()   # the request got its attempt; error is surfaced
            bstate = write_backup_state(home, ok=False, error=f"capture failed: {exc}")
            self._backup_failures = bstate["consecutive_failures"]
            return {"captured": False, "error": str(exc)}
        finally:
            self._backup_in_progress.clear()
            self._backup_progress = None

        # Stamped again on completion so the interval spaces from the end.
        self._last_backup = self._clock()
        self._last_backup_wall = self._wall_clock()
        self._backup_failures = 0
        if requested_ev is not None:
            requested_ev.clear()
        return {"captured": item is not None,
                "item": item.name if item is not None else None}

    @contextmanager
    def _bounded_bulk_lock(self):
        """The bulk lock with the bounded, intent-marked acquire every other
        cycle-thread waiter uses (see _bulk_lock_intent, BULK_LOCK_ACQUIRE_S):
        a maintenance pass legitimately holding _bulk_lock longer than one
        tick must not park the cycle thread. Raises backup_outbox.LockBusy on
        timeout, which capture() lets propagate before writing anything."""
        with self._bulk_lock_intent():
            acquired = self._bulk_lock.acquire(timeout=self._bulk_lock_wait_s)
        if not acquired:
            raise backup_outbox.LockBusy(
                f"bulk lock held for more than {self._bulk_lock_wait_s:.1f}s "
                "(maintenance pass busy)")
        try:
            yield
        finally:
            self._bulk_lock.release()

    def _outbox_still_full(self, cfg) -> bool:
        """Cheap re-check of the outbox-full pause: one outbox_bytes read.
        Clears the pause (returns False) once the outbox is under the cap;
        an unreadable outbox counts as still full (re-checked later)."""
        self._outbox_full_checked = self._clock()
        try:
            full = backup_outbox.outbox_bytes(str(app_dir())) >= cfg.settings.outbox_max_bytes
        except Exception as exc:  # noqa: BLE001 — never raise out of the loop
            log.warning("backup outbox size unreadable: %s", exc)
            return True
        if not full:
            self._outbox_full_since = None
            self._outbox_full_checked = None
        return full

    def upload_backup_step(self) -> dict | None:
        """Ship outbox items to Drive for at most settings.upload_budget_s,
        then prune -- every loop iteration while backup is configured, whether
        or not a capture was due, and OUTSIDE the bulk lock (network I/O never
        holds it; spec §3).

        Returns None when there is nothing to do (backup off, restore pending,
        empty outbox, a dark-waking Mac with no outstanding request).
        Otherwise {"committed", "error", "pending", "pending_bytes"}.

        `backup_uploader.upload_step` records its own failure in
        backup_state.json (ok=False) and each commit (ok=True with the
        restore point); this method counts a failed step toward the failure
        backoff and, at two failed steps in a row, rebuilds the Drive service
        and the upload transport (same Layer-2 safety net as before). Items the
        step discarded are recorded as backup_state `last_discard`. Progress is
        stamped via _note_progress("backup") so the watchdog never reads a
        long upload as a stall; `_backup_progress` carries the upload's
        position for `/api/status` (`backup_running`).

        Prune (`backup_prune.prune_step`) runs with the remaining budget after
        a step that committed, while the journal holds pending deletions (an
        interrupted prune continues), or -- on an otherwise idle install --
        once per IDLE_PRUNE_INTERVAL_S (wall clock), so expired chains are
        removed even when nothing new commits. Other idle iterations make no
        Drive calls.

        The drain request (`_backup_drain_requested`) is cleared once the
        outbox is empty -- but never while the capture request
        (`_backup_requested`) is still outstanding (a capture skipped for a
        busy lock): the requested item has not even been written yet.
        """
        # One attribute read (atomic); the interval it is paired with is not
        # used here. getattr: a Daemon built without __init__ has none.
        cfg = getattr(self, "_backup", None)
        if cfg is None or getattr(self, "_restore_pending", False):
            return None
        home = str(app_dir())
        drain_ev = getattr(self, "_backup_drain_requested", None)
        try:
            pending = backup_outbox.pending_items(home)
        except Exception as exc:  # noqa: BLE001 — an unreadable outbox must not crash the loop
            log.warning("backup upload: outbox unreadable: %s", exc)
            return None
        capture_ev = getattr(self, "_backup_requested", None)
        capture_outstanding = bool(capture_ev is not None and capture_ev.is_set())
        journal_deletes = False
        last_prune = getattr(self, "_last_prune_wall", None)
        idle_prune_due = (last_prune is None
                          or self._wall_clock() - last_prune >= IDLE_PRUNE_INTERVAL_S)
        if not pending:
            if drain_ev is not None and not capture_outstanding:
                drain_ev.clear()
            try:
                journal_deletes = bool(backup_uploader.load_journal(home)["pending_deletes"])
            except Exception:  # noqa: BLE001 — best-effort
                journal_deletes = False
            if not journal_deletes and not idle_prune_due:
                return None
        requested = bool(drain_ev is not None and drain_ev.is_set())
        if (not requested and sys.platform == "darwin"
                and not _mac_user_is_active()):
            log.info("backup upload deferred: system is dark-waking")
            return None

        # Prune-only iterations need no upload transport.
        transport = self._backup_transport(cfg) if pending else None
        budget = float(cfg.settings.upload_budget_s)
        clock = getattr(self, "_upload_clock", time.monotonic)
        start = clock()
        started = self._wall_clock()

        def _progress(p):
            self._backup_progress = {"phase": "upload", "started": started,
                                     "sent": p.get("sent"), "total": p.get("total"),
                                     "item": p.get("item")}
            self._note_progress("backup")

        self._note_progress("backup")
        try:
            if pending and transport is None:
                err = "upload transport unavailable (Google credentials)"
                write_backup_state(home, ok=False, error=err)
                result = backup_uploader.StepResult(
                    committed=[], progress=None, error=err,
                    pending=len(pending), pending_bytes=backup_outbox.outbox_bytes(home))
            elif pending:
                result = backup_uploader.upload_step(
                    home, drive_service=cfg.drive_service, transport=transport,
                    key=cfg.key, budget_s=budget, clock=clock,
                    wall=self._wall_clock, on_progress=_progress)
            else:
                result = None
        except Exception as exc:  # noqa: BLE001 — upload_step should not raise; belt and braces
            log.warning("backup upload step raised: %s", exc, exc_info=True)
            write_backup_state(home, ok=False, error=f"{type(exc).__name__}: {exc}")
            result = backup_uploader.StepResult(
                committed=[], progress=None, error=str(exc), pending=len(pending),
                pending_bytes=0)
        finally:
            self._backup_progress = None
            self._note_progress("backup")

        for d in (getattr(result, "discarded", None) or []):
            record_discard(home, item=d.get("item"), reason=d.get("reason"),
                           at=self._wall_clock())

        if result is not None and result.error:
            self._upload_failures = getattr(self, "_upload_failures", 0) + 1
            self._backup_failures = backup_failures(home) or self._backup_failures + 1
            if self._upload_failures >= 2:
                self._rebuild_backup_clients(self._upload_failures)
        elif result is not None:
            self._upload_failures = 0
            if result.committed:
                self._backup_failures = 0

        # Every upload step re-checks the outbox-full pause (cheap), so a
        # drained outbox resumes capture on the next iteration.
        if getattr(self, "_outbox_full_since", None) is not None:
            self._outbox_still_full(cfg)

        if (result is not None and result.committed) or journal_deletes or idle_prune_due:
            remaining = budget - (clock() - start)
            if remaining > 0:
                self._last_prune_wall = self._wall_clock()
                try:
                    pr = backup_prune.prune_step(
                        home, drive_service=cfg.drive_service,
                        retain_days=cfg.settings.retain_days,
                        now=self._wall_clock(), budget_s=remaining)
                    held = pr.get("held")
                    # The doctor line's shape (newest vs largest kept bytes),
                    # plus the chains, for /api/status.
                    self._prune_held = ({
                        "newest_bytes": held["newest"]["base_bytes"],
                        "largest_kept_bytes": held["previous"]["base_bytes"],
                        "newest_chain": held["newest"]["chain"],
                        "previous_chain": held["previous"]["chain"],
                    } if held else None)
                except Exception as exc:  # noqa: BLE001 — prune is best-effort
                    log.warning("backup prune failed: %s", exc)
            self._note_progress("backup")

        if result is None:
            return None
        if drain_ev is not None and result.pending == 0 and not capture_outstanding:
            drain_ev.clear()
        return {"committed": list(result.committed), "error": result.error,
                "pending": result.pending, "pending_bytes": result.pending_bytes}

    def _backup_transport(self, cfg):
        """cfg.transport, building it (and storing it on a new BackupConfig)
        when the startup build could not. None when credentials are missing."""
        if cfg.transport is not None:
            return cfg.transport
        try:
            transport = _build_backup_transport()
        except Exception as exc:  # noqa: BLE001 — reported by the caller
            log.warning("backup upload transport unavailable: %s", exc)
            return None
        with self._config_lock:
            if self._backup is cfg:
                self._backup = replace(cfg, transport=transport)
        return transport

    def _rebuild_backup_clients(self, failures: int) -> None:
        """Layer-2 safety net: after repeated failed upload steps, build a
        fresh Drive service AND upload transport. Per-call retries already
        cover a flaky call; a failure that survives them and repeats is more
        likely a persistently broken client (a stale token) than a blip.
        Best-effort: a rebuild failure is logged, never raised. Swaps in a NEW
        BackupConfig (never mutates the caller-supplied one)."""
        log.warning("backup upload: %d consecutive failures, rebuilding the "
                    "Drive service and upload transport", failures)
        try:
            fresh = _build_drive_service()
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("backup upload: drive_service rebuild failed: %s", exc)
            return
        try:
            transport = _build_backup_transport()
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("backup upload: transport rebuild failed: %s", exc)
            transport = None
        with self._config_lock:
            if self._backup is not None:
                self._backup = replace(self._backup, drive_service=fresh,
                                       transport=transport)

    @contextmanager
    def _bulk_lock_intent(self):
        """Mark 'a thread is about to wait for _bulk_lock' for the duration of the
        wrapped block (normally just the bounded `.acquire(timeout=...)` call).

        Backed by a plain counter (`_bulk_lock_waiters`, guarded by
        `_bulk_lock_waiters_lock`) rather than a single `threading.Event`: TWO
        independent call sites can be waiting on `_bulk_lock` at once
        (`_run_periodic_passes` on the maintenance thread, and the backup
        capture's `_bounded_bulk_lock` on the cycle thread itself), and an Event
        would let one waiter's cleanup (`.clear()` in its own `finally`) erase
        the other's still-pending signal. The counter only reads "nobody is
        waiting" once every waiter that incremented it has also decremented.
        """
        with self._bulk_lock_waiters_lock:
            self._bulk_lock_waiters += 1
        try:
            yield
        finally:
            with self._bulk_lock_waiters_lock:
                self._bulk_lock_waiters -= 1

    def _bulk_lock_wanted(self) -> bool:
        """True while at least one thread is waiting to acquire _bulk_lock."""
        with self._bulk_lock_waiters_lock:
            return self._bulk_lock_waiters > 0

    @contextmanager
    def _cycle_bulk_section(self):
        """Hold _bulk_lock for ONE small chunk-mutating unit of work, yielding
        between units.

        The lock used to be held across the whole of run_one() (see run()'s
        history), which starved the four needs_bulk_lock cadence passes and
        _backup_under_bulk_lock entirely -- live: 183 consecutive "bulk lock
        held" skip warnings and not one gated pass run in over 8 minutes.
        Bounding the WHOLE-CALL hold with `budget` alone was not enough
        either: a soak test showed one lock hold per whole (even
        budget-bounded, <=60s) sync/drain call still starved the maintenance
        thread's 5s-bounded acquire almost every time on a sustained backlog
        -- this is a lock DUTY-CYCLE problem, not only a fairness problem. So
        every caller now enters this section around ONE message/event/file/
        embed-batch/inbox-file (see run_sync_cycle, index_pending, drain,
        work_queue's handle_gmail_item/handle_calendar_item/handle_drive_item,
        prepare_units's per-batch helpers), not once per whole call, releasing
        the lock in between so the maintenance thread has a real, frequent
        chance to acquire it.

        `_bulk_lock_wanted()` is an explicit fairness hand-off on top of that:
        a waiter marks intent (`_bulk_lock_intent`) before blocking on the
        lock, and this method pauses briefly after releasing when it sees that
        intent, so the waiter's pending acquire wins the next opportunity
        rather than racing a cycle thread that re-enters this section
        ~immediately.

        Note this hand-off is INSURANCE, not a measured fix, and it is not the
        thing that solved the live starvation -- the sectioning granularity
        above is. CPython's Lock is not FIFO-fair in principle, but measured on
        CPython/macOS it is fair enough in practice that deleting this pause
        changes nothing observable: with a cycle thread doing zero work per
        section (release, immediately re-acquire -- the adversarial shape) a
        single bounded waiter still won 20/20 acquires at 500ms/50ms/10ms
        timeouts and 19/20 at 2ms. Kept because it costs nothing when nobody is
        waiting and platforms differ; pinned by
        tests/test_bulk_lock_fairness.py::
        test_release_pauses_only_when_a_waiter_marked_intent (a deterministic
        contract test -- every THREADED test in that file passes with this
        block deleted, so the contract test is the only thing preventing a
        silent removal). See that file's module docstring for the measurements.

        The acquire here is deliberately UNBOUNDED (unlike
        the backup capture's bounded, skip-on-timeout `_bounded_bulk_lock`): the
        caller's chunk-mutating unit of work still needs to run, so there is
        nothing safe to skip to. A bounded-retry-with-progress-stamp variant
        was tried (Task 3 review) to address a related but DIFFERENT known
        gap -- a gated pass holding _bulk_lock past STALL_S can still make
        _stalled_phase() misreport a stall while this thread waits here, via
        "sync" (stamped once, at the end of run_sync_cycle, not per-phase)
        going stale, not via "cycle" -- and was reverted: re-stamping "cycle"
        between retry attempts does not touch "sync", so it did not actually
        fix that scenario (confirmed by direct instrumentation: identical
        `_stalled_phase() -> ('sync', ...)` outcome with or without the
        retry loop). See docs/superpowers/plans/2026-07-27-daemon-scheduling-
        remediation.md (Task 4 section) for the tracked gap and why fixing it
        belongs there, not here.
        """
        self._bulk_lock.acquire()
        try:
            yield
        finally:
            self._bulk_lock.release()
            if self._bulk_lock_wanted():
                # Give the waiter a scheduling window; without this the
                # re-acquire above beats it on an unfair lock.
                self._stop.wait(timeout=getattr(self, "_bulk_lock_yield_s", BULK_LOCK_YIELD_S))

    def _backup_under_bulk_lock(self) -> None:
        """The run loop's backup step: capture if due, then one time-boxed
        upload + prune step. Despite the (historical) name it no longer holds
        _bulk_lock itself: capture takes it inside, for the `backup()` copy
        only, through `_bounded_bulk_lock` (bounded acquire; a timeout skips
        the capture this cycle with the cadence untouched), and the upload and
        prune run with no lock at all -- network I/O never holds the lock the
        chunk-writing passes need (spec §2/§3).

        _note_progress("backup") is stamped UNCONDITIONALLY first (Task 3
        review fix): "we are here, trying" counts as liveness whether or not a
        capture or upload happens, so a skipped acquire never ages "backup"
        into a false stall.

        Re-stamps EVERY tracked progress key once the step returns
        (final-whole-branch-review Critical B): stamping only "backup" let
        "cycle"/"sync" age for the whole duration of a long backup and read as
        a stall on the next tick. In the `finally`, so it fires whether the
        step succeeded, failed internally (both swallow their own errors), or
        raised.

        Also re-sweeps orphaned mcpbrain-snap-* temp dirs left by the old
        full-snapshot path (pre-incremental daemons; a watchdog os._exit
        mid-snapshot), every iteration, not just at startup -- the startup
        sweep spares an orphan younger than SNAPSHOT_ORPHAN_MAX_AGE_S.
        """
        self._note_progress("backup")
        try:
            self.maybe_backup()
            self.upload_backup_step()
        finally:
            # Restamp ALL of _progress, not just "backup" -- see this
            # method's docstring (Critical B).
            self._restamp_all_progress()
            try:
                removed = backup.sweep_orphan_snapshots(
                    tempfile.gettempdir(), max_age_s=SNAPSHOT_ORPHAN_MAX_AGE_S)
                if removed:
                    log.info("swept %d orphaned snapshot temp dir(s)", removed)
            except Exception as exc:  # noqa: BLE001 — sweep must not crash the cycle
                log.warning("snapshot orphan sweep failed: %s", exc, exc_info=True)

    # -- silent auto-update ---------------------------------------------------

    def _run_auto_update(self) -> dict | None:
        """Cadence-gated auto-update check. Called by _run_periodic_passes via the
        dispatch table and directly by maybe_auto_update."""
        home = str(app_dir())
        with self._config_lock:
            interval = self._auto_update_interval_s
        if interval is None:
            interval = 86400.0 if config.is_configured(home) else None
        if interval is None:
            return None
        if self._last_auto_update is not None:
            elapsed = self._clock() - self._last_auto_update
            # getattr: an instance built without __init__ (or a path that sets
            # _last_auto_update alone) has no wall twin -- fall back to
            # monotonic. Wall-clock elapsed counts time asleep; a backwards
            # wall-clock jump makes it small or negative and the max falls back
            # to monotonic, so it can never suppress a check monotonic
            # already considers due.
            last_wall = getattr(self, "_last_auto_update_wall", None)
            if last_wall is not None:
                wall_clock = getattr(self, "_wall_clock", time.time)
                elapsed = max(elapsed, wall_clock() - last_wall)
            if elapsed < interval:
                return None
        # Stamped BEFORE the check (unchanged): a failing check backs off a
        # full interval on both clocks rather than retrying every tick.
        self._last_auto_update = self._clock()
        self._last_auto_update_wall = getattr(self, "_wall_clock", time.time)()
        try:
            from mcpbrain import update as upd
            idx = upd._index_url()
            if idx is None:
                log.warning(
                    "auto-update skipped: no update channel configured "
                    "(no tenant profile, no MCPBRAIN_INDEX_URL, no update_index_url)")
                return None
            latest = upd._latest_version(idx)
            if upd._should_update(upd._installed_version(), latest):
                self._pending_update = latest
                return {"update_available": True, "version": latest}
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-update check failed (loop continues): %s", exc)
        return None

    def maybe_auto_update(self) -> dict | None:
        """Detect a newer published version; signal run() to install it OUTSIDE the
        write lock. Default daily when configured; OFF when unconfigured. Never runs
        the install/restart here (that would happen under the held lock)."""
        return self._run_auto_update()

    # -- verify connections cadence -------------------------------------------

    def _run_log_cap(self) -> dict | None:
        """Truncate any launchd log that has grown past the cap.

        Never raises: this exists to prevent a disk-full outage, and taking the
        cycle down to do it would be the worse failure.
        """
        if not self._is_due("_log_cap_interval_s", "_last_log_cap"):
            return None
        self._last_log_cap = self._clock()
        try:
            from mcpbrain.log_cap import cap_agent_logs
            freed = cap_agent_logs(str(app_dir()))
        except Exception as exc:  # noqa: BLE001
            log.warning("log_cap pass failed: %s", exc)
            return None
        return {"freed_bytes": freed} if freed else None

    def _run_verify(self) -> dict | None:
        """Cadence-gated connection verification. Called by the dispatch table
        and directly by maybe_verify_connections."""
        home = str(app_dir())
        if not config.is_configured(home):
            return None
        with self._config_lock:
            interval = self._verify_interval_s
        if interval is None:
            interval = 3600.0
        if self._last_verify is not None and (self._clock() - self._last_verify) < interval:
            return None
        self._last_verify = self._clock()
        try:
            from mcpbrain import probes
            return probes.verify_connections(home, self._store)
        except Exception as exc:  # noqa: BLE001
            log.warning("verify_connections failed (loop continues): %s", exc)
            return None

    def maybe_verify_connections(self) -> dict | None:
        """Periodically verify connections (network) and cache the result.
        OFF unless configured; default hourly when configured without an explicit
        interval. Time-gated via self._clock."""
        return self._run_verify()

    # -- OCR binary install (once-ever, background) --------------------------

    def _run_ocr_setup(self) -> dict | None:
        """Once-ever background install of the tesseract OCR binary.

        Runs on a background thread, not the maintenance thread: the install
        (brew/winget) can block up to 600s, and a pass that long would park the
        stall-watchdog check that runs immediately after _run_periodic_passes
        returns (see the class docstring's note on why passes must stay well
        under MAINTENANCE_TICK_S).
        """
        if not self._is_due("_ocr_setup_interval_s", "_last_ocr_setup"):
            return None
        self._last_ocr_setup = self._clock()

        def _run():
            try:
                result = run_ocr_setup(str(app_dir()))
            except Exception as exc:  # noqa: BLE001 — OCR is optional, never fatal
                log.warning("ocr_setup failed: %s", exc, exc_info=True)
                result = {"status": "error", "detail": str(exc)}
            else:
                log.info("ocr_setup: %s", result)

        threading.Thread(target=_run, daemon=True).start()
        return {"ocr_setup": "started"}

    # -- periodic community detection ---------------------------------------

    # Re-cluster only after the graph grows by at least this much since the last
    # clustering (change-driven: backfill keeps clusters fresh, an idle graph is
    # not re-clustered every interval for nothing). Either threshold triggers.
    _CLUSTER_DELTA_ENTITIES = 25
    _CLUSTER_DELTA_RELATIONS = 100

    def _graph_counts(self) -> tuple[int, int]:
        with self._store._connect() as db:
            e = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            r = db.execute("SELECT COUNT(*) FROM entity_relations "
                           "WHERE invalidated_at IS NULL").fetchone()[0]
        return e, r

    def _communities_change_due(self) -> bool:
        """True if the graph grew materially since the last clustering (or never
        clustered with a marker). Cheap count check; defaults to True on error."""
        try:
            with self._store._connect() as db:
                le = db.execute("SELECT v FROM meta WHERE k='communities_clustered_entities'").fetchone()
                lr = db.execute("SELECT v FROM meta WHERE k='communities_clustered_relations'").fetchone()
            if le is None or lr is None:
                return True
            cur_e, cur_r = self._graph_counts()
            return ((cur_e - int(le[0])) >= self._CLUSTER_DELTA_ENTITIES
                    or (cur_r - int(lr[0])) >= self._CLUSTER_DELTA_RELATIONS)
        except Exception:  # noqa: BLE001
            return True

    def _mark_communities_clustered(self) -> None:
        try:
            cur_e, cur_r = self._graph_counts()
            with self._store._connect(write=True) as db:
                db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('communities_clustered_entities',?)", (str(cur_e),))
                db.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('communities_clustered_relations',?)", (str(cur_r),))
        except Exception:  # noqa: BLE001
            pass

    def _run_communities(self) -> dict | None:
        """Cadence-gated community detection. Called by the dispatch table
        and directly by maybe_communities. The interval is the floor; within it,
        re-clustering only happens when the graph changed materially."""
        if not self._is_due("_communities_interval_s", "_last_communities"):
            return None
        now = self._clock()
        if not self._communities_change_due():
            self._last_communities = now            # checked; graph idle → skip
            return {"communities": "skipped_no_change"}
        try:
            home = str(app_dir())
            # B6: use incremental extension when enabled; fall back to full run
            if config.incremental_communities_enabled(home):
                from mcpbrain.communities import extend_communities
                summary = extend_communities(self._store, home)
            else:
                from mcpbrain.communities import run
                summary = run(self._store)
        except Exception as exc:  # noqa: BLE001 — communities must never crash the loop
            log.warning(
                "community detection failed (will retry next due): %s", exc,
                exc_info=True,
            )
            return {"communities": False, "error": str(exc)}
        self._mark_communities_clustered()
        self._last_communities = now
        return summary

    def maybe_communities(self) -> dict | None:
        """Run community detection, if it is due.

        OFF unless communities_interval_s was supplied. Otherwise gates on a
        time-based cadence using the injected clock. Backfill guard: returns
        None while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_communities()

    # -- periodic ClickUp two-way sync --------------------------------------

    def _run_clickup_sync(self) -> dict | None:
        """Run ClickUp sync if key+list are configured and the fixed interval elapsed.
        Presence of clickup_api_key AND clickup_list_id is the single on/off switch."""
        from mcpbrain import config as _cfg
        home = str(app_dir())
        if not (_cfg.clickup_api_key(home) and _cfg.clickup_list_id(home)):
            return None
        if self._clickup_interval_s is None:
            self._clickup_interval_s = _CLICKUP_SYNC_INTERVAL_S
        if not self._is_due("_clickup_interval_s", "_last_clickup"):
            return None
        now = self._clock()
        try:
            from mcpbrain import clickup_sync
            summary = clickup_sync.sync(self._store, home)
        except Exception as exc:  # noqa: BLE001
            log.warning("clickup sync failed: %s", exc, exc_info=True)
            return {"clickup": False, "error": str(exc)}
        self._last_clickup = now
        return summary

    def maybe_clickup_sync(self) -> dict | None:
        if self._backfill_active.is_set():
            return None
        return self._run_clickup_sync()

    # -- periodic stale -> re-extraction trigger (Gap A) --------------------

    def _run_stale_reextract(self) -> dict | None:
        """Cadence-gated stale-reextract sweep. Called by the dispatch table
        and directly by maybe_stale_reextract."""
        if not self._is_due("_stale_reextract_interval_s", "_last_stale_reextract"):
            return None
        now = self._clock()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        try:
            from mcpbrain import stale_reextract
            summary = stale_reextract.sweep(self._store, now=now_iso)
        except Exception as exc:  # noqa: BLE001 — must never crash the loop
            log.warning("stale-reextract sweep failed (will retry next due): %s",
                        exc, exc_info=True)
            return {"stale_reextract": False, "error": str(exc)}
        self._last_stale_reextract = now
        return summary

    def maybe_stale_reextract(self) -> dict | None:
        """Reset stale, idle threads to enriched=0 so the normal cycle gives the
        LLM closer another at-bat, if due.

        OFF unless stale_reextract_interval_s is set (returns None). Backfill
        guard: returns None while a backfill is active.
        """
        if self._backfill_active.is_set():
            return None
        return self._run_stale_reextract()

    # -- S2 feedback aggregation (nightly) ------------------------------------

    def _run_feedback_aggregate(self) -> dict | None:
        """Nightly Bayesian-smoothed CTR → chunk_quality update."""
        if not self._is_due("_feedback_aggregate_interval_s",
                            "_last_feedback_aggregate"):
            return None
        now = self._clock()
        try:
            from mcpbrain.feedback import aggregate_feedback
            summary = aggregate_feedback(self._store)
            log.info("feedback_aggregate: updated=%d skipped=%d",
                     summary.get("updated", 0), summary.get("skipped", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("feedback_aggregate failed: %s", exc, exc_info=True)
            return {"feedback_aggregate": False, "error": str(exc)}
        self._last_feedback_aggregate = now
        return summary

    # -- B3 salience scoring (daily) ------------------------------------------

    def _run_salience_score(self) -> dict | None:
        """Score ALL unscored chunks with structural salience (B3).

        Structural scoring is deterministic and cheap (no LLM; ~sub-second per few
        thousand), so each run DRAINS the backlog (loop in 5000-chunk batches until
        none remain) rather than throttling. importance_recall is on by default and
        is only meaningful once salience is populated, so a fresh/upgraded store is
        fully scored on the first salience pass instead of ramping for weeks. The
        round bound is a runaway backstop, not an expected limit.
        """
        if not self._is_due("_salience_score_interval_s", "_last_salience_score"):
            return None
        now = self._clock()
        total = rounds = llm = 0
        try:
            from mcpbrain.importance import run_salience_pass
            while rounds < 500:   # backstop: 500 × 5000 = 2.5M chunks
                summary = run_salience_pass(self._store, str(app_dir()), cap=5000)
                n = summary.get("scored", 0)
                total += n
                llm += summary.get("llm_scored", 0)
                rounds += 1
                if n == 0:
                    break
            log.info("salience_score: scored=%d over %d round(s)", total, rounds)
        except Exception as exc:  # noqa: BLE001
            log.warning("salience_score failed: %s", exc, exc_info=True)
            return {"salience_score": False, "error": str(exc), "scored": total}
        self._last_salience_score = now
        return {"scored": total, "llm_scored": llm, "rounds": rounds}

    # -- B5 decay pass (nightly) -----------------------------------------------

    def _run_decay_pass(self) -> dict | None:
        """Evaluate decay for embedded chunks; demote stale low-salience to cold (B5)."""
        if not self._is_due("_decay_pass_interval_s", "_last_decay_pass"):
            return None
        now = self._clock()
        try:
            from mcpbrain.decay import apply_decay_pass
            summary = apply_decay_pass(self._store, str(app_dir()))
            log.info("decay_pass: evaluated=%d demoted=%d exempt=%d",
                     summary.get("evaluated", 0), summary.get("demoted", 0),
                     summary.get("exempt", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("decay_pass failed: %s", exc, exc_info=True)
            return {"decay_pass": False, "error": str(exc)}
        # Tier maintenance (B2): promote warm→hot on strength, demote low-salience,
        # and RECOMPUTE THE CORE TIER. Self-gates on tiered_memory; without this the
        # core tier is never populated and the always-injected block stays empty.
        try:
            from mcpbrain.memory_tier import run_tier_pass
            tier = run_tier_pass(self._store, str(app_dir()))
            if any(tier.values()):
                log.info("tier_pass: promoted=%d demoted=%d core=%d",
                         tier.get("promoted", 0), tier.get("demoted", 0), tier.get("core", 0))
                summary["tier"] = tier
        except Exception as exc:  # noqa: BLE001
            log.warning("tier_pass failed (decay still applied): %s", exc, exc_info=True)
        self._last_decay_pass = now
        return summary

    # -- B4 consolidation pass (nightly) ---------------------------------------

    def _run_consolidation(self) -> dict | None:
        """RAPTOR-style cluster+summarise of episodic chunks into semantic notes (B4)."""
        if not self._is_due("_consolidation_interval_s", "_last_consolidation"):
            return None
        now = self._clock()
        try:
            from mcpbrain.consolidation import consolidate
            # Pass the embedder so clustering is semantic (embedding-based), not lexical.
            summary = consolidate(self._store, str(app_dir()), embedder=self._embedder)
            log.info("consolidation: notes_written=%d clusters=%d",
                     summary.get("notes_written", 0), summary.get("clusters_found", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("consolidation failed: %s", exc, exc_info=True)
            return {"consolidation": False, "error": str(exc)}
        self._last_consolidation = now
        return summary

    # -- B6 voice analyser (weekly) --------------------------------------------

    def _run_voice_analyse(self) -> dict | None:
        """Weekly Phase A voice analysis: analyse drafts → propose voice.md updates (B6)."""
        if not self._is_due("_voice_analyse_interval_s", "_last_voice_analyse"):
            return None
        now = self._clock()
        home = str(app_dir())
        try:
            from mcpbrain.voice_analyser import maybe_run_analysis
            suggestions = maybe_run_analysis(self._store, home)
            log.info("voice_analyse: queued %d suggestions", len(suggestions))
            summary = {"suggestions": len(suggestions)}
        except Exception as exc:  # noqa: BLE001
            log.warning("voice_analyse failed: %s", exc, exc_info=True)
            return {"voice_analyse": False, "error": str(exc)}

        if config.voice_auto_apply_enabled(home):
            try:
                from mcpbrain.voice_apply import apply_suggestions
                apply_result = apply_suggestions(self._store, home)
                log.info("voice_auto_apply: applied=%d blocked=%s",
                         apply_result.get("applied", 0), apply_result.get("blocked"))
                summary["auto_applied"] = apply_result.get("applied", 0)
            except Exception as exc:  # noqa: BLE001
                log.warning("voice_auto_apply failed: %s", exc, exc_info=True)

        self._last_voice_analyse = now
        return summary

    # -- S4/S5 self-improvement (weekly) ---------------------------------------

    def _run_self_improve(self) -> dict | None:
        """Weekly self-improvement: embedding-drift check (S4), bandit reward
        update + advisory (S4), and outcome-grounded lessons (S5).

        Each step self-gates on its own flag and is isolated, so one failing does
        not abort the others. These modules were previously library-only with no
        caller — this is the cadence that actually runs them. Auto-apply stays off
        unless bandit_auto_apply is set; learning + lessons are fed by the real
        'used' accept signal recorded by the prompt-recall hook.
        """
        if not self._is_due("_self_improve_interval_s", "_last_self_improve"):
            return None
        now = self._clock()
        home = str(app_dir())
        summary: dict = {}

        # S4a: embedding-drift monitor vs the gold set.
        if config.drift_monitor_enabled(home):
            # Bounded acquire (Task 4): this pass is NOT bulk-lock-gated, so an
            # in-flight embedder build elsewhere (self._embedder's unbounded
            # acquire) would otherwise park this whole maintenance tick -- see
            # _embedder_bounded's docstring.
            embedder = self._embedder_bounded()
            if embedder is None:
                log.warning("self_improve: embedder busy building (cold "
                            "download in progress); skipping drift check "
                            "this tick, will retry when next due")
            else:
                try:
                    from mcpbrain.drift_monitor import init_drift_table, run_drift_check
                    init_drift_table(self._store)
                    summary["drift"] = run_drift_check(self._store, embedder, home)
                except Exception as exc:  # noqa: BLE001
                    log.warning("drift_monitor failed: %s", exc, exc_info=True)

        # S4b: feed the bandit real reward from recent accept signals, then advise.
        try:
            from mcpbrain import threshold_bandit as tb
            tb.init_bandit_table(self._store)
            # Attribute recent feedback to the threshold arm currently in effect.
            from mcpbrain.lessons import read_recent_outcomes
            used = len(read_recent_outcomes(self._store, days=7))
            if used:
                arm = min(tb.ARMS, key=lambda a: abs(a - config.recall_max_distance(home)))
                for _ in range(used):
                    tb.step(self._store, arm, outcome="used")
            summary["bandit"] = tb.advisory_report(self._store, home)
        except Exception as exc:  # noqa: BLE001
            log.warning("bandit advisory failed: %s", exc, exc_info=True)

        # S5: outcome-grounded lessons (only writes when 'used'/'edited' exist).
        if config.lessons_enabled(home):
            try:
                from mcpbrain.lessons import init_lessons_table, write_lessons
                init_lessons_table(self._store)
                summary["lessons"] = write_lessons(self._store, home)
            except Exception as exc:  # noqa: BLE001
                log.warning("lessons failed: %s", exc, exc_info=True)

        if summary:
            log.info("self_improve: %s", {k: (v if not isinstance(v, dict) else "ok")
                                          for k, v in summary.items()})
        self._last_self_improve = now
        return summary or None

    # -- Auto-graduation (flip data-gated flags ON when ready) ----------------

    def _run_auto_enable(self) -> dict | None:
        """Graduate data-gated flags (bandit/lessons/decay) once their readiness
        condition is genuinely met. Deterministic gates + a decay safety dry-run;
        only flips flags absent from config.json (never overrides the user)."""
        if not self._is_due("_auto_enable_interval_s", "_last_auto_enable"):
            return None
        now = self._clock()
        try:
            from mcpbrain.auto_enable import auto_enable_pass
            summary = auto_enable_pass(self._store, str(app_dir()))
            if summary.get("enabled"):
                log.info("auto_enable: graduated %s", summary["enabled"])
        except Exception as exc:  # noqa: BLE001
            log.warning("auto_enable failed: %s", exc, exc_info=True)
            return {"auto_enable": False, "error": str(exc)}
        self._last_auto_enable = now
        return summary

    # -- Q4 org backfill (deterministic) --------------------------------------

    def _run_org_backfill(self) -> dict | None:
        """Run org_from_email over org-less entities (deterministic, no LLM)."""
        if not self._is_due("_org_backfill_interval_s", "_last_org_backfill"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_backfill
            summary = org_backfill.run_backfill(self._store)
            log.info("org_backfill: updated=%d unknown_domains=%d",
                     summary.get("updated", 0), len(summary.get("unknown_domains", [])))
        except Exception as exc:  # noqa: BLE001
            log.warning("org_backfill failed: %s", exc, exc_info=True)
            return {"org_backfill": False, "error": str(exc)}
        self._last_org_backfill = now
        return summary

    # -- Task 3.3 entity resolution (deterministic) ----------------------------

    def _run_resolve_entities(self) -> dict | None:
        """Daily deterministic entity-dedup pass (Task 3.3, issue #23-fix validated)."""
        if not self._is_due("_resolve_entities_interval_s", "_last_resolve_entities"):
            return None
        now = self._clock()
        try:
            from mcpbrain import resolve
            from mcpbrain import config as _config
            summary = resolve.resolve_entities(self._store, home=str(_config.app_dir()))
            log.info("resolve_entities: auto_merges=%d", summary.get("auto_merges", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("resolve_entities failed: %s", exc, exc_info=True)
            return {"resolve_entities": False, "error": str(exc)}
        self._last_resolve_entities = now
        return summary

    def _run_action_hygiene(self) -> dict | None:
        """Daily sweep that keeps the actions table clean.

        Two reversible passes, both status-only (nothing is ever deleted):
          - archive_stale_actions: undated-by-age, plus dated deadlines so far
            past they are debris rather than follow-ups;
          - archive_duplicate_actions: collapse re-extracted copies of the same
            commitment, keeping the original.

        Until this cadence existed neither had an automatic caller — they ran
        only via a manual bin/consolidate.py invocation — so on a real install
        the actions table was never swept and accrued years of dead rows, which
        then crowded genuinely current work out of every capped actions surface.
        """
        if not self._is_due("_action_hygiene_interval_s", "_last_action_hygiene"):
            return None
        now = self._clock()
        try:
            stale = self._store.archive_stale_actions()
            dupes = self._store.archive_duplicate_actions()
        except Exception as exc:  # noqa: BLE001 — a sweep must never kill the cycle
            log.warning("action_hygiene failed: %s", exc, exc_info=True)
            return {"action_hygiene": False, "error": str(exc)}
        self._last_action_hygiene = now
        summary = {"actions_archived": stale.get("archived", 0),
                   "actions_deduped": dupes.get("archived", 0)}
        if summary["actions_archived"] or summary["actions_deduped"]:
            log.info("action_hygiene: archived=%d deduped=%d",
                     summary["actions_archived"], summary["actions_deduped"])
        return summary

    def _reflow_source_sets(self, home) -> tuple[set[str], set[str]]:
        """(workable_now, workable). `workable` is reflow.workable_reflow_sources
        -- what this install can work as configured. `workable_now` is the part
        of it whose service is built right now: the only sources seeded, since
        a queued row with no service would only defer and hold REFLOW_WINDOW.
        A source outside `workable` is permanently unavailable (scope not
        granted, not configured, anarlog disabled) and its queued rows are
        freed; one in `workable` but not `workable_now` failed transiently and
        its rows are left alone."""
        from mcpbrain.reflow import REFLOW_SOURCE_SERVICES, workable_reflow_sources
        try:
            services = self.ensure_services() or {}
        except Exception:  # noqa: BLE001 — treated as a transient build failure
            services = {}
        workable = workable_reflow_sources(home, services)
        now = {src for src, key in REFLOW_SOURCE_SERVICES.items()
               if services.get(key) is not None}
        return (now | {"reflow:anarlog"}) & workable, workable

    def _run_reflow_seed(self):
        """Top the reflow queue up to REFLOW_WINDOW (spec §4). Gated on the
        kill switch, the halt flag and a backup that succeeded in the last 24h.

        A reflow rewrites chunk rows in place (carry-over + re-enrich), so it
        must never run further ahead of a recovery point than one backup
        interval -- hence the freshness gate, checked every time this fires
        rather than once at startup, so a backup that later goes stale halts
        seeding without anyone having to notice and flip a switch by hand.

        Every run records its outcome in the `reflow:last_seed` cursor
        ({status, at, enqueued?, remaining?, held?}) so doctor and /api/status can say
        WHY nothing is moving, instead of reading a gated seed as idle.
        """
        if not self._is_due("_reflow_seed_interval_s", "_last_reflow_seed"):
            return None
        home = str(app_dir())
        self._last_reflow_seed = self._clock()
        prev = self._last_reflow_seed_record()
        try:
            now, workable = self._reflow_source_sets(home)
        except Exception as exc:  # noqa: BLE001 — a cadence pass must never kill the cycle
            # Return BEFORE the drop/backlog-empty logic: with no workable set
            # every source would look permanently unavailable, so its queued
            # rows would be freed and an empty backlog could be declared done.
            log.warning("reflow_seed: could not resolve sources: %s", exc)
            out = {"reflow_seed": False, "error": f"could not resolve reflow sources: {exc}"}
            self._record_reflow_seed(out, prev.get("remaining"), prev.get("sources"),
                                     held=prev.get("held"))
            return out
        out = self._reflow_seed_once(home, now, workable)
        if "remaining" in out:
            remaining, held = out["remaining"], out.get("held")
        elif out.get("reflow_seed") in ("disabled", "halted") and "remaining" in prev:
            # A disabled/halted seed ticks hourly for as long as it stays that
            # way and nothing it gates changes the backlog: reuse the last
            # known figure instead of re-running the chunk-table scans.
            remaining, held = prev["remaining"], prev.get("held")
        else:
            # Every other branch -- gated or failed included -- records what is
            # left, so the dashboard/doctor never read a blocked seed as idle.
            remaining, held = self._reflow_counts(workable)
        self._record_reflow_seed(out, remaining, sorted(workable), held=held)
        return {k: v for k, v in out.items() if k not in ("remaining", "held")}

    def _last_reflow_seed_record(self) -> dict:
        try:
            rec = json.loads(self._store.get_cursor("reflow:last_seed") or "{}")
            return rec if isinstance(rec, dict) else {}
        except Exception:  # noqa: BLE001 — a bad record is just "unknown"
            return {}

    def _record_reflow_seed(self, out: dict, remaining, sources, *, held=None) -> None:
        try:
            import datetime as _dt
            rec = {"status": out.get("reflow_seed") if out.get("reflow_seed") is not False
                   else "error", "at": _dt.datetime.now(_dt.timezone.utc).isoformat()}
            for k in ("enqueued", "error"):
                if k in out:
                    rec[k] = out[k]
            rec["remaining"] = remaining
            if held is not None:
                rec["held"] = held
            if sources is not None:
                rec["sources"] = sources
            self._store.set_cursor("reflow:last_seed", json.dumps(rec))
        except Exception as exc:  # noqa: BLE001 — visibility must never kill the cycle
            log.debug("reflow_seed: could not record last status: %s", exc)

    def _reflow_counts(self, sources) -> tuple[int | None, int | None]:
        """(remaining, held) via Store.reflow_live_counts: remaining INCLUDES
        the owners the selector's 24 h loop guard holds out of the seed, so a
        stuck owner never makes the backlog read empty."""
        if sources is None:
            return None, None
        try:
            return self._store.reflow_live_counts(REFLOW_REMAINING_CAP, sources=sources)
        except Exception:  # noqa: BLE001
            return None, None

    def _reflow_remaining(self, sources) -> int | None:
        return self._reflow_counts(sources)[0]

    def _reflow_seed_once(self, home, now: set[str], workable: set[str]) -> dict:
        if not config.reflow_enabled(home):
            return {"reflow_seed": "disabled"}
        if self._store.get_cursor("reflow:halted"):
            return {"reflow_seed": "halted"}
        from mcpbrain.probes import last_backup_success
        last = last_backup_success(home)
        fresh = last is not None and time.time() - last <= REFLOW_BACKUP_MAX_AGE_S
        if not fresh:
            return {"reflow_seed": "no_recent_backup"}
        try:
            from mcpbrain.reflow import REFLOW_SOURCES
            # Only a PERMANENTLY unavailable source's rows are freed; a source
            # whose service failed to build just now keeps its rows (and their
            # attempts) queued for when it comes back.
            gone = set(REFLOW_SOURCES) - workable
            dropped = self._store.drop_queued_reflow_rows(gone)
            if dropped:
                log.info("reflow_seed: freed %d queued row(s) of unavailable source(s) %s",
                         dropped, sorted(gone))
            # Size the window on DUE rows only: a row deferred with a delay
            # is not work, and counting it lets a long outage stall the rest.
            room = REFLOW_WINDOW - self._store.reflow_due_count()
            if room <= 0:
                remaining, held = self._reflow_counts(workable)
                return {"reflow_seed": "window_full", "enqueued": 0,
                        "remaining": remaining, "held": held}
            by_src: dict[str, list[dict]] = {}
            for src, owner in self._store.reflow_candidates(room, sources=now):
                by_src.setdefault(src, []).append(
                    {"ref_id": owner, "event": "reflow", "modified_at": "1970-01-01T00:00:00"})
            n = sum(self._store.enqueue_items(items, source=src) for src, items in by_src.items())
            remaining, held = self._reflow_counts(workable)
            if n:
                # New work: the backlog's end must be integrity-checked again
                # (e.g. after a future EXTRACTION_VERSIONS bump).
                self._store.set_cursor("reflow:integrity_checked", "")
            elif (self._store.reflow_stats()["queued"] == 0 and not remaining
                  and not held):
                # Truly done: nothing queued, nothing left for any workable
                # source (a source that failed transiently still counts), and
                # no owner merely held by the loop guard -- a held owner did
                # not converge, so the backlog is NOT done. (remaining already
                # includes held; `not held` states the rule outright.)
                self._reflow_backlog_empty()
            return {"reflow_seed": "ok", "enqueued": n, "remaining": remaining,
                    "held": held}
        except Exception as exc:  # noqa: BLE001 — a cadence pass must never kill the cycle
            log.warning("reflow_seed failed: %s", exc, exc_info=True)
            return {"reflow_seed": False, "error": str(exc)}

    def _reflow_backlog_empty(self):
        """Once per completion: run integrity_check and record the result.

        Cheap idempotence guard (`reflow:integrity_checked`) so a store that
        stays empty (nothing left to reflow) doesn't re-run a ~19s full-store
        check on every hourly tick forever.
        """
        if self._store.get_cursor("reflow:integrity_checked"):
            return
        from mcpbrain.doctor import _run_integrity_check
        problems = _run_integrity_check(str(app_dir()))
        self._store.set_cursor("reflow:integrity_checked",
                               "ok" if not problems else f"{len(problems)} problem(s)")
        if problems:
            log.error("reflow complete but integrity_check reported %d problem(s)", len(problems))

    def _run_enrich_payload_migration(self) -> None:
        """Drain one batch of the legacy doc_id-keyed enrich_payloads table.

        Self-terminating: once store.init() has renamed the old table aside and
        this has emptied and dropped it, every subsequent call is a cheap
        no-op returning done=True -- but _is_due still gates it hourly rather
        than every maintenance tick, matching every other needs_bulk_lock=True
        pass (decay_pass, salience_score, consolidation, stale_reextract) and
        the invariant test_maintenance_scheduler.py pins: the outer dispatch
        pre-check in _run_periodic_passes only skips contending for
        _bulk_lock on a not-due pass, it is not the authoritative gate.
        """
        if not self._is_due("_enrich_payload_migration_interval_s", "_last_enrich_payload_migration"):
            return
        now = self._clock()
        res = self._store.migrate_enrich_payloads_batch()
        self._last_enrich_payload_migration = now
        if res["migrated"] or res["deleted"]:
            log.info("enrich_payload_migration: migrated %d file(s), "
                     "deleted %d legacy row(s), done=%s",
                     res["migrated"], res["deleted"], res["done"])

    def _run_mcp_heartbeat_sweep(self) -> dict | None:
        """Hourly prune of dead-pid mcp_heartbeat/<pid>.json files.

        live_version_records(home) already prunes as a side effect of listing,
        so this pass is just calling it and reporting the surviving count. Before
        this cadence existed, that pruning only ever ran when a user manually
        invoked `mcpbrain doctor` -- so on a real install these tiny per-process
        files accumulated indefinitely, one per MCP server process lifetime.
        """
        if not self._is_due("_mcp_heartbeat_sweep_interval_s", "_last_mcp_heartbeat_sweep"):
            return None
        now = self._clock()
        try:
            from mcpbrain import config as _config
            from mcpbrain.mcp_server import live_version_records
            recs = live_version_records(str(_config.app_dir()))
        except Exception as exc:  # noqa: BLE001 — a sweep must never kill the cycle
            log.warning("mcp_heartbeat_sweep failed: %s", exc, exc_info=True)
            return {"mcp_heartbeat_sweep": False, "error": str(exc)}
        self._last_mcp_heartbeat_sweep = now
        return {"mcp_heartbeat_live": len(recs)}

    # -- Session-4 AI-adjudication review (graph-hygiene findings) ------------

    def _run_review(self) -> dict | None:
        """Daily AI-adjudication review cadence (Session-4). Builds review units
        from open graph-hygiene findings and stashes them as block units for the
        existing enrich pipeline to pick up — no new units/pull/push mechanism.
        """
        if not self._is_due("_review_interval_s", "_last_review"):
            return None
        now = self._clock()
        try:
            from mcpbrain import review
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            cap = _config.review_max_apply_per_run(home)
            kind_to_block_key = {
                "lint:orphan_entity": "review_orphan",
                "lint:missing_org": "review_missing_org",
                "lint:ownerless_action": "review_ownerless",
                "lint:ambiguous_org": "review_org",
                "lint:duplicate_org": "review_org",
                "org_unrecognised": "review_org",
            }
            units = review.build_review_units(
                self._store, kinds=list(kind_to_block_key), cap=cap)
            by_block: dict[str, list] = {}
            for u in units:
                block_key = kind_to_block_key.get(u["packet"].get("finding_type"))
                if block_key:
                    by_block.setdefault(block_key, []).append(u)
            with self._stash_lock:
                for key, items in by_block.items():
                    if items:
                        self._pending_blocks[key] = items
                        self._bump_stash_gen(key)
            counts = {k: len(v) for k, v in by_block.items()}
            log.info("review: stashed %s", counts)
        except Exception as exc:  # noqa: BLE001 — review must never crash the loop
            log.warning("review pass failed (will retry next due): %s", exc, exc_info=True)
            return {"review": False, "error": str(exc)}
        self._last_review = now
        return counts

    # -- Org-baseline cadences (Phase 0 stubs; bodies land in subsystem B) ----

    def _run_org_contrib_upload(self) -> dict | None:
        """Collect allowlisted deltas since the watermark and upload the outbox
        to the fleet folder. Both steps run here because Phase B may not add a
        drain-path hook — collect_from_drain stays reusable for a future one."""
        if not self._is_due("_org_contrib_upload_interval_s", "_last_org_contrib_upload"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_contrib
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            if not _config.org_contrib_enabled(home):
                self._last_org_contrib_upload = now
                return {"skipped": "disabled"}
            pin = _config.fleet_pin(home)
            if not pin.is_pinned:
                self._last_org_contrib_upload = now
                return {"skipped": "unpinned"}
            # FleetStorage is built by subsystem A (mcpbrain/fleet_storage.py). Guarded
            # import keeps B build-independent of A pre-convergence; the Drive service
            # lives in the services dict (ensure_services), not on self.
            try:
                from mcpbrain import fleet_storage
                fs = fleet_storage.fleet_folder_storage(
                    home, drive_service=self.ensure_services().get("drive_service"))
            except ImportError:
                fs = None
            if fs is None:
                # Services awaiting a transient-failure retry: stay due, so the
                # pass runs on the tick after they recover, not a day later.
                if not self._google_services_pending():
                    self._last_org_contrib_upload = now
                return {"skipped": "no_fleet_storage"}
            email = _config.owner_email(home)
            delta, wm = org_contrib._delta_since_watermark(self._store)
            n = org_contrib.collect_from_drain(self._store, delta, pin, email)
            self._store.set_meta("org_contrib_hwm", str(wm["hwm"]))
            self._store.set_meta("org_contrib_ts", wm["ts"])
            up = org_contrib.upload_pending(self._store, fs, email)
            log.info("org_contrib: collected=%d uploaded=%d", n, up["uploaded"])
        except Exception as exc:  # noqa: BLE001 — a cadence must never crash the loop
            log.warning("org_contrib pass failed: %s", exc, exc_info=True)
            return {"org_contrib": False, "error": str(exc)}
        self._last_org_contrib_upload = now
        return {"collected": n, **up}

    def _run_org_import(self) -> dict | None:
        """Import a newer org-graph snapshot into origin='org' rows."""
        if not self._is_due("_org_import_interval_s", "_last_org_import"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_import
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            if not _config.org_import_enabled(home):
                self._last_org_import = now
                return {"skipped": "disabled"}
            try:
                from mcpbrain import fleet_storage
                fs = fleet_storage.fleet_folder_storage(
                    home, drive_service=self.ensure_services().get("drive_service"))
            except ImportError:
                fs = None
            if fs is None:
                # Services awaiting a transient-failure retry: stay due, so the
                # pass runs on the tick after they recover, not a day later.
                if not self._google_services_pending():
                    self._last_org_import = now
                return {"skipped": "no_fleet_storage"}
            res = org_import.import_snapshot(self._store, fs)
            log.info("org_import: %s", res)
        except Exception as exc:  # noqa: BLE001
            log.warning("org_import pass failed: %s", exc, exc_info=True)
            return {"org_import": False, "error": str(exc)}
        self._last_org_import = now
        return res

    def _run_org_curate(self) -> dict | None:
        """Curator-only: ingest contributions, adjudicate, publish a snapshot."""
        if not self._is_due("_org_curate_interval_s", "_last_org_curate"):
            return None
        now = self._clock()
        try:
            from mcpbrain import org_curate
            from mcpbrain import config as _config
            home = str(_config.app_dir())
            if not _config.is_org_curator(home):
                self._last_org_curate = now
                return {"skipped": "not_curator"}
            try:
                from mcpbrain import fleet_storage
                fs = fleet_storage.fleet_folder_storage(
                    home, drive_service=self.ensure_services().get("drive_service"))
            except ImportError:
                fs = None
            if fs is None:
                # Services awaiting a transient-failure retry: stay due, so the
                # pass runs on the tick after they recover, not a day later.
                if not self._google_services_pending():
                    self._last_org_curate = now
                return {"skipped": "no_fleet_storage"}
            res = org_curate.run(self._store, fs, home)
            # Stash fuzzy-merge pairs as an enrich-spool block (mirrors _run_review):
            # a subagent judges them and drain -> apply_org_merge_answers applies the
            # merges on push. Async because there is no synchronous LLM client.
            units = res.pop("adjudication_units", [])
            if units:
                with self._stash_lock:
                    self._pending_blocks["org_merge_review"] = units
                    self._bump_stash_gen("org_merge_review")
            log.info("org_curate: %s",
                     {**{k: res[k] for k in ("version", "ingested") if k in res},
                      "merge_units": len(units)})
        except Exception as exc:  # noqa: BLE001
            log.warning("org_curate pass failed: %s", exc, exc_info=True)
            return {"org_curate": False, "error": str(exc)}
        self._last_org_curate = now
        return res

    # -- periodic graph lint ------------------------------------------------

    def _run_lint(self) -> dict | None:
        """Cadence-gated graph lint. Called by the dispatch table and
        directly by maybe_lint."""
        if not self._is_due("_lint_interval_s", "_last_lint"):
            return None
        now = self._clock()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        try:
            # Lazy import: keeps the daemon import light and lint an optional
            # path; also lets tests patch mcpbrain.lint_graph.run.
            from mcpbrain.lint_graph import run
            summary = run(self._store, now=now_iso)
        except Exception as exc:  # noqa: BLE001 — lint must never crash the loop
            log.warning(
                "lint pass failed (will retry next due): %s", exc, exc_info=True
            )
            return {"lint": False, "error": str(exc)}
        self._last_lint = now
        return summary

    def maybe_lint(self) -> dict | None:
        """Run the graph lint pass, if it is due.

        OFF unless lint_interval_s was supplied. Backfill guard: returns None
        while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_lint()

    # -- periodic thread synthesis ------------------------------------------

    def _run_synthesise(self) -> dict | None:
        """Cadence-gated synthesis-request build. Called by the dispatch table
        and directly by maybe_synthesise."""
        if not self._is_due("_synthesise_interval_s", "_last_synthesise"):
            return None
        now = self._clock()
        try:
            # Lazy import: keeps the daemon import light and synthesis an
            # optional path; also lets tests patch build_synthesis_requests.
            from mcpbrain.synthesise_threads import build_synthesis_requests
            requests = build_synthesis_requests(self._store)
            with self._stash_lock:
                self._pending_synthesis = requests
                self._bump_stash_gen(self._SYNTHESIS_GEN_KEY)
        except Exception as exc:  # noqa: BLE001 — synthesis must never crash the loop
            log.warning(
                "synthesis build failed (will retry next due): %s", exc,
                exc_info=True,
            )
            return {"synthesis_requested": 0, "error": str(exc)}
        self._last_synthesise = now
        return {"synthesis_requested": len(requests)}

    def maybe_synthesise(self) -> dict | None:
        """Build synthesis requests, if synthesis is due.

        OFF unless synthesise_interval_s was supplied. Backfill guard: returns
        None while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_synthesise()

    # -- periodic proactive detection pass ---------------------------------

    def _run_proactive(self) -> dict | None:
        """Cadence-gated proactive detection. Called by the dispatch table
        and directly by maybe_proactive."""
        if not self._is_due("_proactive_interval_s", "_last_proactive"):
            return None
        now = self._clock()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        try:
            # Lazy import: keeps the daemon import light and the proactive path
            # an optional dependency; also lets tests patch mcpbrain.proactive.run.
            from mcpbrain.proactive import run
            summary = run(self._store, now=now_iso)
        except Exception as exc:  # noqa: BLE001 — proactive must never crash the loop
            log.warning(
                "proactive detection failed (will retry next due): %s", exc,
                exc_info=True,
            )
            return {"proactive": False, "error": str(exc)}
        self._last_proactive = now
        return summary

    def maybe_proactive(self) -> dict | None:
        """Run the proactive detection pass, if it is due.

        OFF unless proactive_interval_s was supplied. Backfill guard: returns
        None while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_proactive()

    # -- periodic waiting-on reconciliation ---------------------------------

    def _run_waiting_on(self) -> dict | None:
        """Cadence-gated waiting-on reconciliation. Called by the dispatch table
        and directly by maybe_waiting_on."""
        if not self._is_due("_waiting_on_interval_s", "_last_waiting_on"):
            return None
        now = self._clock()
        import datetime as _dt
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        try:
            # Lazy import: keeps the daemon import light and lets tests patch
            # mcpbrain.waiting_on.run.
            from mcpbrain.waiting_on import run
            from mcpbrain import config as _cfg
            _identity = _cfg.owner_email(str(app_dir()))
            summary = run(self._store, now=now_iso, identity=_identity or None)
        except Exception as exc:  # noqa: BLE001 — waiting_on must never crash the loop
            log.warning(
                "waiting-on reconciliation failed (will retry next due): %s", exc,
                exc_info=True,
            )
            return {"waiting_on": False, "error": str(exc)}
        self._last_waiting_on = now
        return summary

    def maybe_waiting_on(self) -> dict | None:
        """Run the waiting-on reconciliation pass, if it is due.

        OFF unless waiting_on_interval_s was supplied. Backfill guard: returns
        None while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_waiting_on()

    # -- periodic block requests (profile_synthesis + community_synthesis + memory_distil) ---

    def _run_blocks(self) -> dict | None:
        """Cadence-gated block-request build. Called by the dispatch table
        and directly by maybe_blocks."""
        if not self._is_due("_blocks_interval_s", "_last_blocks"):
            return None
        now = self._clock()
        try:
            from mcpbrain.profile_synth import build_profile_requests
            from mcpbrain.community_synth import build_community_requests
            from mcpbrain.memory_distil import build_distil_requests

            profile_reqs = build_profile_requests(self._store)
            community_reqs = build_community_requests(self._store)
            distil_reqs = build_distil_requests(self._store)
            with self._stash_lock:
                self._pending_blocks.update({
                    "profile_synthesis": profile_reqs,
                    "community_synthesis": community_reqs,
                    "memory_distil": distil_reqs,
                })
                self._bump_stash_gen(
                    "profile_synthesis", "community_synthesis", "memory_distil")
        except Exception as exc:  # noqa: BLE001 — must never crash the loop
            log.warning(
                "blocks build failed (will retry next due): %s", exc, exc_info=True
            )
            return {
                "profile_synthesis_requested": 0,
                "community_synthesis_requested": 0,
                "memory_distil_requested": 0,
                "error": str(exc),
            }
        self._last_blocks = now
        log.info("blocks stashed: profiles=%d communities=%d distil=%d",
                 len(profile_reqs), len(community_reqs), len(distil_reqs))
        return {
            "profile_synthesis_requested": len(profile_reqs),
            "community_synthesis_requested": len(community_reqs),
            "memory_distil_requested": len(distil_reqs),
        }

    def maybe_blocks(self) -> dict | None:
        """Build block requests for profile/community/memory, if due.

        OFF unless blocks_interval_s was supplied. Backfill guard: returns None
        while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_blocks()

    # -- periodic profile audit ---------------------------------------------

    def _run_audit(self) -> dict | None:
        """Cadence-gated profile audit build. Called by the dispatch table
        and directly by maybe_audit."""
        if not self._is_due("_audit_interval_s", "_last_audit"):
            return None
        now = self._clock()
        try:
            from mcpbrain.profile_audit import build_audit_requests
            audit_reqs = build_audit_requests(self._store)
            with self._stash_lock:
                self._pending_audit = {"profile_audit": audit_reqs}
                self._bump_stash_gen("profile_audit")
        except Exception as exc:  # noqa: BLE001 — must never crash the loop
            log.warning(
                "audit build failed (will retry next due): %s", exc, exc_info=True
            )
            return {"audit_requested": 0, "error": str(exc)}
        self._last_audit = now
        return {"audit_requested": len(audit_reqs)}

    def maybe_audit(self) -> dict | None:
        """Build profile audit requests, if due.

        OFF unless audit_interval_s was supplied. Backfill guard: returns None
        while a backfill is active (single-writer).
        """
        if self._backfill_active.is_set():
            return None
        return self._run_audit()

    # -- periodic pass orchestration ----------------------------------------

    def _run_gated_pass(self, cp: "CadencePass") -> None:
        """Acquire _bulk_lock (bounded) and run ONE gated cadence pass to completion.

        Called INLINE by _run_periodic_passes, on the maintenance thread itself
        (see that method's docstring for why a worker-thread version of this was
        tried and reverted). Owns the whole acquire -> run -> release lifecycle,
        including setting/clearing _bulk_pass_active around the pass's real
        execution. That flag is kept for status() observability only -- on
        this single thread, _recover_from_stall()'s check of it is dead code
        (always sees it already clear by the time it runs); see
        _recover_from_stall's docstring for why, and for the still-open gap
        this leaves.

        Signals intent BEFORE blocking on the acquire so the cycle thread's
        _cycle_bulk_section sees it and yields between units of work (CPython's
        Lock is not FIFO-fair, so without this explicit hand-off the cycle
        thread wins nearly every race), and is skipped for this tick — logged,
        not raised — if the cycle thread still holds the lock past
        BULK_LOCK_ACQUIRE_S.
        """
        with self._bulk_lock_intent():
            acquired = self._bulk_lock.acquire(timeout=self._bulk_lock_wait_s)
        if not acquired:
            log.warning(
                "periodic pass %s skipped this tick: bulk lock held for "
                "more than %.1fs (cycle busy); will retry", cp.name,
                self._bulk_lock_wait_s)
            return
        active = getattr(self, "_bulk_pass_active", None)
        if active is not None:
            active.set()
        try:
            getattr(self, cp.fn_name)()
        except Exception as exc:  # noqa: BLE001
            log.warning("periodic pass %s failed: %s", cp.name, exc, exc_info=True)
        finally:
            if active is not None:
                active.clear()
            self._bulk_lock.release()

    def _run_periodic_passes(self) -> None:
        """Iterate _CADENCE_PASSES; each entry self-gates on its cadence.

        The dispatch table in _CADENCE_PASSES drives the order (communities
        first so lint reads fresh entity_communities and blocks reads fresh
        communities via community_synth.py:54). needs_configured gates
        graph-writing passes on config.is_configured so they never write blank
        attribution into the graph on an unconfigured install. Each _run_X call
        is individually wrapped so an unexpected raise from one pass never
        blocks the remaining passes. The four passes that also write `chunks`
        take the coarse _bulk_lock so they never race the cycle thread's own
        chunk-mutating phases (_cycle_bulk_section).

        A gated pass's own cadence is checked BEFORE the lock is attempted, so a
        not-due pass never contends for it at all (each _run_X keeps its own
        _is_due call as the authoritative gate; this is only a pre-filter, and
        only for the gated passes — auto_update/verify resolve a default
        interval internally and would be wrongly disabled by an outer check).

        A due gated pass's acquire-and-run (_run_gated_pass) runs INLINE, on
        this thread, exactly as before this task. A worker-thread variant
        (bounded by a join timeout, so a genuinely-hung pass could not make
        this method itself unreachable) was built and reviewed as part of this
        task's known-gap fix, and was REVERTED — found, by actually running
        the composed system (199 simulated maintenance ticks against a real
        hung pass), to be worse than the plain inline shape it replaced, not
        better:

          1. It made _recover_from_stall's _bulk_pass_active defer UNBOUNDED
             for exactly the case it was meant to help with. A pass that
             genuinely never returns holds _bulk_lock forever either way; with
             the worker-thread variant, _bulk_pass_active also stays set
             forever (it is only cleared when the pass's own thread finishes),
             so _recover_from_stall deferred recovery FOREVER too -- an
             end-to-end run confirmed 199 ticks / 3.3 simulated hours with no
             restart and _stalled_phase() reporting a stale "cycle" the whole
             time. That is the identical "daemon silently dead, nothing to
             page anyone" end-state this whole task exists to prevent, reached
             by a subtler path (the watchdog is technically reachable now, but
             permanently muzzled by its own deferral) instead of the original
             one (the watchdog thread itself was unreachable) -- the same
             "behaviorally safe, operationally pointless" pattern that got
             Task 3's _cycle_bulk_section retry-loop attempt reverted.
          2. _shutdown_maintenance only joins _maintenance_thread. The
             worker-thread variant left the four gated passes running on
             separate, untracked mcpbrain-pass-* threads, so on the
             _pending_update restart path a stray pass thread in the dying
             process could still be writing chunks while the successor
             started writing too -- the exact two-concurrent-writers hazard
             _shutdown_maintenance exists to prevent. _run_periodic_passes
             also never re-checked _stop, so it kept spawning new
             chunk-mutating threads even after shutdown was requested.

        Fixing both properly (a time-bounded defer with an escalation path --
        e.g. deferring only up to STALL_S, then letting recovery proceed
        anyway, since a pass hung that long needs a restart the same as any
        other stall -- plus a real thread registry _shutdown_maintenance
        joins or explicitly abandons-and-logs) is real additional concurrency
        surface in code that has already been through 4 (Task 2) and 3
        (Task 3) review rounds for similar correctness issues. Per the brief's
        explicit "fine to leave this open" allowance, this task instead
        reverted to plain inline dispatch (this method's current shape).

        IMPORTANT — correcting an earlier version of this docstring: reverting
        to inline dispatch does NOT also fix cause 1. _bulk_pass_active is
        kept and still set/cleared around the pass's real (inline) execution,
        but on a SINGLE thread that both sets/clears it AND is the only thing
        that ever checks it, _run_gated_pass's own `finally: active.clear()`
        runs strictly BEFORE this method returns, which is strictly before
        _maintenance_loop reaches _note_progress("maintenance") /
        _stalled_phase() / _recover_from_stall() on that SAME thread. So by
        the time _recover_from_stall() ever runs, _bulk_pass_active is ALWAYS
        already clear -- there is no concurrent second thread left to ever
        observe it set. A full-timeline simulation of the real, single-
        threaded production call chain (this method -> _run_gated_pass ->
        _stalled_phase -> _recover_from_stall, a pass holding the lock for
        2100 simulated seconds) confirmed this directly: 69 out of 69
        pass-completions produced a spurious restart. _bulk_pass_active is
        retained purely for status() observability (a human/dashboard can see
        "a gated pass is running" mid-pass); it is NOT a working watchdog
        mitigation in the current shape. Cause 1 (the false-restart problem)
        is therefore STILL OPEN, behaviorally identical to Task 3's
        end-state (cd172ce) -- this revert only restored the topology, not a
        fix for the underlying gap. Both cause 1 and cause 2 are left as
        explicit, tracked known gaps. See
        docs/superpowers/plans/2026-07-27-daemon-scheduling-remediation.md,
        Task 4 section, for the fuller writeup, including a smaller suggested
        future-fix direction (recording the pass's hold window as a
        timestamp pair instead of a live flag).
        """
        configured = config.is_configured(str(app_dir()))
        held = getattr(self, "_restore_pending", False)
        for cp in _CADENCE_PASSES:
            if cp.needs_configured and not configured:
                continue
            if held and cp.name not in _RESTORE_HOLD_PASSES:
                continue   # restore hold: stays due, runs once the hold lifts
            try:
                if cp.needs_bulk_lock:
                    if not self._is_due(cp.interval_attr, cp.last_attr):
                        continue
                    self._run_gated_pass(cp)
                else:
                    getattr(self, cp.fn_name)()
            except Exception as exc:  # noqa: BLE001
                log.warning("periodic pass %s failed: %s", cp.name, exc, exc_info=True)

    # -- progress heartbeat + watchdog --------------------------------------

    def _note_progress(self, phase: str) -> None:
        """Record that `phase` advanced. The old heartbeat was written only after
        the cadence passes, so a mid-cycle stall was invisible by construction."""
        with self._progress_lock:
            self._progress[phase] = self._clock()

    def _restamp_all_progress(self) -> None:
        """Re-stamp EVERY tracked `_progress` key to "now" in one shot.

        `_stalled_phase()`'s `min()`-over-all-keys has no way to express "this
        thread just spent a long time on a KNOWN, accounted-for deferral, not
        a wedge" -- a single-key `_note_progress` call leaves every OTHER key
        ageing for the whole deferral. `resume()` was the first place this
        pattern was needed (a long pause froze every key except "backup"); it
        is also the fix for a long backup (`_backup_under_bulk_lock`'s
        `finally`, Critical B from the final whole-branch review) and a long
        backfill (`run_one`'s `_backfill_active` early return, Important
        I-1) -- both are threads legitimately busy doing accounted-for work,
        not stalled, for a duration that can exceed STALL_S.
        """
        with self._progress_lock:
            now = self._clock()
            for phase in self._progress:
                self._progress[phase] = now

    def _stalled_phase(self) -> tuple[str, float] | None:
        """(phase, seconds_since) for the STALEST recorded phase, if it is too old.

        Must be the minimum timestamp, not the maximum: the maintenance thread
        (Task 4) calls _note_progress("maintenance") every ~60s independent of
        the bulk sync/cycle thread, so "maintenance" is always the freshest key
        once the daemon is up. Picking the freshest phase would mean this never
        fires no matter how long "sync"/"cycle" have been wedged -- exactly the
        failure mode STALL_S exists to catch.
        """
        with self._progress_lock:
            if not self._progress:
                return None            # nothing started yet is not a stall
            phase, ts = min(self._progress.items(), key=lambda kv: kv[1])
        age = self._clock() - ts
        return (phase, age) if age > getattr(self, "_stall_s", STALL_S) else None

    def _watchdog_exits_path(self):
        # Daemon has no _home attribute — the app dir is resolved on demand,
        # as everywhere else in this module.
        return app_dir() / "watchdog_exits.json"

    def _recent_watchdog_exits(self) -> list[float]:
        """Watchdog-triggered exit timestamps still inside WATCHDOG_WINDOW_S.

        The single reader of watchdog_exits.json — the limiter, the recorder and
        status() all go through here so they can never disagree about what
        "recent" means. A missing/corrupt file reads as no history.

        Persisted and compared on WALL-CLOCK time (time.time()), not
        self._clock() (time.monotonic() by default): monotonic's epoch is
        arbitrary per-process and resets across a reboot, which would make
        every historical entry look like it just happened — disabling the
        restart limiter permanently right after the reboot it's most needed
        for. _progress stays on self._clock(): monotonic is correct there
        because it only measures in-process durations, never persisted.
        """
        import json as _json
        path = self._watchdog_exits_path()
        try:
            recent = [float(t) for t in _json.loads(path.read_text())]
        except (OSError, ValueError, TypeError):
            recent = []
        cutoff = time.time() - getattr(self, "_watchdog_window_s", WATCHDOG_WINDOW_S)
        return [t for t in recent if t >= cutoff]

    def _watchdog_may_exit(self) -> bool:
        """False once WATCHDOG_MAX_EXITS restarts have happened in the window.

        A persistently broken install should end up visibly stuck rather than
        restarting forever.
        """
        return len(self._recent_watchdog_exits()) < getattr(
            self, "_watchdog_max_exits", WATCHDOG_MAX_EXITS)

    def _record_watchdog_exit(self) -> None:
        import json as _json
        recent = self._recent_watchdog_exits() + [time.time()]
        try:
            self._watchdog_exits_path().write_text(_json.dumps(recent))
        except OSError:
            pass

    def _exit_for_restart(self) -> None:
        os._exit(1)   # noqa: SLF001 — bypass atexit; the supervisor restarts us

    def _spawn_replacement(self) -> None:
        """Start a detached successor before exiting (unsupervised Windows only).

        close_fds is NOT detachment: without DETACHED_PROCESS the successor
        inherits our console, and without CREATE_NEW_PROCESS_GROUP a Ctrl-Break
        or a console teardown aimed at the dying parent reaches it too. Both
        flags are Windows-only names, so they are resolved only on win32 (they
        do not exist in POSIX `subprocess`); elsewhere Popen keeps its current
        behaviour, which matters because tests exercise this path on macOS.
        """
        import subprocess
        kwargs: dict = {"close_fds": True}
        if sys.platform == "win32":  # pragma: no cover - Windows-only flags
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                       | subprocess.CREATE_NEW_PROCESS_GROUP)
        subprocess.Popen([sys.executable, "-m", "mcpbrain.daemon"],  # noqa: S603
                         **kwargs)
        os._exit(1)  # noqa: SLF001

    def _recover_from_stall(self) -> None:
        """Exit (or, unsupervised, spawn+exit) so the supervisor restarts us.

        Two cases genuinely, currently prevent a restart even though
        _stalled_phase() looks wedged (Task 3):

        - A backup is in flight (_backup_in_progress). os._exit bypasses
          `finally`, so firing mid-snapshot orphans the temp dir
          make_encrypted_snapshot is still writing to -- the mechanism that
          left ~24GB of mcpbrain-snap-* on disk and froze the host on
          2026-07-27. Recovery is deferred, not skipped: the next tick that
          finds the backup finished re-evaluates normally.
        - The cycle is repeatedly raising, not hanging
          (_cycle_error_streak > 3). A deterministic exception looks
          identical to a wedge from _stalled_phase's point of view -- "cycle"
          simply stops advancing either way -- but a restart cannot fix a
          code/config bug that fails the same way every time; doing it anyway
          just burns the 3-exit watchdog budget on something restart-looping
          can't solve. The daemon stays up, visibly failing (already logged
          loudly in run()'s except handler), instead.

        A THIRD check below (_bulk_pass_active, Task 4) is present in code but
        is CURRENTLY DEAD -- it can never actually defer anything in
        production, and must not be read as a working mitigation. The intent
        was: a gated cadence pass actively holding _bulk_lock (e.g.
        _run_salience_score's `while rounds < 500` loop, legitimately running
        well past STALL_S) makes "sync"/"cycle"/"maintenance" all go stale
        together (the cycle thread's own _cycle_bulk_section blocks on the
        SAME lock, and because _run_gated_pass runs INLINE on the maintenance
        thread, that thread's own tick can't reach _note_progress either),
        which _stalled_phase() cannot tell apart from a genuine wedge from
        the raw timestamps alone -- so defer while the flag is set, same
        shape as _backup_in_progress.

        The problem: on a single thread that both sets/clears the flag AND is
        the only thing that ever checks it, _run_gated_pass's own
        `finally: active.clear()` runs strictly BEFORE _run_periodic_passes()
        returns, which is strictly before THIS method is ever reached (via
        _maintenance_loop's _note_progress -> _stalled_phase ->
        _recover_from_stall, all on that same thread). So by the time this
        method runs, _bulk_pass_active is ALWAYS already clear -- there is no
        concurrent second thread left to observe it set. A full-timeline
        simulation of the real, single-threaded production call chain (a
        pass holding the lock for 2100 simulated seconds) confirmed this
        directly: 69 out of 69 pass-completions produced a spurious restart.
        _bulk_pass_active is kept only for status() observability (so a
        human/dashboard polling mid-pass can see one is running); the check
        below is inert in the current shape. This is an explicit, tracked
        known gap (cause 1 of the Task 4 known-gap note), not fixed here --
        see docs/superpowers/plans/2026-07-27-daemon-scheduling-remediation.md,
        Task 4 section, for the fuller writeup and a suggested smaller future
        fix (recording the pass's hold window instead of a live flag).

        getattr(..., default) guards all three checks: minimally-constructed
        Daemon doubles in tests (Daemon.__new__ + hand-set attributes) may
        not set any of these attributes, and that must read as "no, not
        deferring" rather than raise.

        Windows supervision (Task 5 review gap, FIXED 2026-07-28): `supervised`
        below asks `win_supervised()`, not merely "is Task Scheduler usable".
        install_agent falls back to the plain /TR CLI form when XML registration
        is rejected, which keeps an on-logon task but loses RestartOnFailure; on
        that fallback a clean exit would leave the daemon dead until next logon
        -- worse than the unsupervised path it thinks it is avoiding, which at
        least self-spawns. win_supervised() distinguishes them by inspecting the
        artefact that actually determines the behaviour: only the XML path
        installs the WAITING shim whose WScript.Quit propagates the daemon's exit
        code, and only a propagated exit code can ever trigger RestartOnFailure.
        Deriving it from the installed shim avoids a second piece of state (a
        marker file) that could drift out of sync with what is really registered.
        """
        if getattr(self, "_backup_in_progress", None) is not None \
                and self._backup_in_progress.is_set():
            log.warning("watchdog: stall detected but a backup is in "
                        "progress; deferring recovery")
            return
        if getattr(self, "_bulk_pass_active", None) is not None \
                and self._bulk_pass_active.is_set():
            log.warning("watchdog: stall detected but a gated maintenance "
                        "pass is still actively holding the bulk lock (e.g. "
                        "a long salience-scoring or stale-reextract sweep); "
                        "deferring recovery")
            return
        if getattr(self, "_cycle_error_streak", 0) > 3:
            log.error("watchdog: cycle is repeatedly failing, not hanging "
                       "(%d consecutive errors); a restart cannot fix a "
                       "deterministic failure, so recovery is skipped -- "
                       "staying up for diagnosis",
                       self._cycle_error_streak)
            return
        supervised = True
        if sys.platform == "win32":
            # NOT just "is the mechanism schtasks": install_agent falls back to
            # the plain /TR form when XML registration is rejected, which keeps
            # the on-logon task but loses RestartOnFailure. Treating that as
            # supervised takes the bare os._exit branch with nothing to restart
            # us -- dead until next logon, strictly worse than the stall being
            # recovered from. win_supervised() checks what was actually
            # installed (the waiting, exit-code-propagating shim).
            supervised = win_supervised(str(app_dir()))
        self._restart_process(supervised)

    def _restart_process(self, supervised: bool | None = None) -> None:
        """Exit so the supervisor restarts us (or, unsupervised on Windows,
        spawn a successor first), counted against the watchdog's restart
        budget. Shared by the stall watchdog and the pending-restore retry."""
        if supervised is None:
            supervised = (win_supervised(str(app_dir()))
                          if sys.platform == "win32" else True)
        self._record_watchdog_exit()
        if supervised:
            self._exit_for_restart()
        else:
            self._spawn_replacement()

    def _maintenance_loop(self) -> None:
        """Run due cadence passes on our own clock, independent of the bulk cycle.

        Each pass still self-gates via _is_due, so a tick is cheap. This thread
        exists because the passes used to run only after run_one() returned, and
        an unbounded cycle therefore starved every one of them.
        """
        while not self._stop.is_set():
            if not self._pause.is_set():
                try:
                    self._run_periodic_passes()
                    self._note_progress("maintenance")
                    stalled = self._stalled_phase()
                    if stalled is not None:
                        phase, age = stalled
                        if self._watchdog_may_exit():
                            log.error("watchdog: no progress in %.0fs (last phase=%s) "
                                      "— invoking recovery", age, phase)
                            # _recover_from_stall may itself defer (backup in
                            # flight, or the cycle repeatedly failing rather
                            # than hanging — see its docstring) rather than
                            # actually restarting; it logs that outcome itself.
                            self._recover_from_stall()
                        else:
                            log.error("watchdog: no progress in %.0fs (last phase=%s); "
                                      "restart limit reached, staying up for diagnosis",
                                      age, phase)
                except AssertionError:
                    # Never swallow: this is how tests' _no_real_exit safety net
                    # (an os._exit call raises AssertionError instead of really
                    # exiting) reports an illegitimate watchdog trip. Catching
                    # it here as just another "bad pass" would print a WARNING
                    # and keep looping — silently hiding the exact regression
                    # that fixture exists to catch. Anything else genuinely
                    # unexpected from _run_periodic_passes/_note_progress/
                    # _stalled_phase/_recover_from_stall falls through to the
                    # broad except below and keeps this thread alive; a real
                    # assertion failure should not.
                    #
                    # Scope note: this only closes the gap for a SYNCHRONOUS
                    # call to _maintenance_loop (exactly what
                    # test_maintenance_loop_does_not_swallow_assertion_error
                    # exercises). In real operation this method runs on its
                    # own background thread, so an AssertionError escaping it
                    # there does NOT fail a synchronous test assertion by
                    # itself -- threading.excepthook prints it and the thread
                    # dies; pytest turns that into a
                    # PytestUnhandledThreadExceptionWarning, which is not
                    # configured here to fail the suite (no matching
                    # filterwarnings entry). Closing that remaining gap would
                    # mean either adding that filterwarnings entry (suite-wide,
                    # unverified against tests outside this task's scope) or a
                    # dedicated threading.excepthook capture in a future task.
                    raise
                except Exception:  # noqa: BLE001 — a bad pass must not kill the thread
                    log.warning("maintenance loop iteration failed", exc_info=True)
            self._stop.wait(timeout=self._maintenance_interval_s)

    def _start_maintenance_thread(self) -> None:
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="mcpbrain-maintenance", daemon=True)
        self._maintenance_thread.start()

    # -- the loop -----------------------------------------------------------

    def migrate_embed_backend(self, backend: str = EMBED_BACKEND) -> int:
        """Re-embed the whole corpus once if the embedding backend changed.

        No-op when the stored marker already matches `backend`. Returns the
        number of chunks re-embedded (0 on a no-op).
        """
        from mcpbrain.index import index_pending

        if self._store.get_meta("embed_backend") == backend:
            return 0
        self._store.mark_all_unembedded()
        # Announce before the (potentially long) re-embed so the run isn't
        # silent. Every chunk was just marked unembedded, so the total chunk
        # count is exactly what we're about to re-embed.
        pending = self._store.chunk_count()
        log.info(
            "embedding backend changed to %s; re-embedding %d chunks",
            backend, pending,
        )
        # Serialise the re-embed's chunk writes like every other bulk write.
        # This runs at startup before _start_maintenance_thread(), so the
        # race is narrow today -- but an unlocked full-corpus write is the
        # one shape that must not be left to ordering luck.
        count = index_pending(self._store, self._embedder,
                              bulk_section=self._cycle_bulk_section)
        log.info("re-embedded %d chunks for backend change", count)
        self._store.set_meta("embed_backend", backend)
        return count

    def _migrate_embed_backend_safe(self) -> None:
        """Re-embed on a backend change, but never let a not-ready/undownloadable
        model stop the daemon from starting its loop. RuntimeError = embedder has
        no factory (lazy, not built); any other exception = model build/download
        failed (e.g. offline). Both degrade to 'skip migrate, continue' — recall
        stays best-effort until the model is available; the loop still runs."""
        try:
            self.migrate_embed_backend()
        except Exception as exc:  # noqa: BLE001 — must not stop the daemon starting
            log.info("embed-backend migrate skipped (model not ready): %s", exc)

    def run(self) -> None:
        """Acquire the single-writer lock and loop until stopped.

        Each iteration: clear _wake BEFORE run_one() so any sync_now() that
        arrives during the cycle re-sets it and causes the following wait to
        return immediately (rather than waiting the full interval). After
        run_one(), wait up to interval_s on _wake. Releases the lock on exit
        via the context manager.
        """
        with self._lock:
            # Sweep orphaned mcpbrain-snap-* work dirs FIRST, before anything
            # else in this block: make_encrypted_snapshot cleans up in a
            # `finally` that cannot fire when the process is killed mid-
            # snapshot -- exactly what the watchdog does deliberately
            # (os._exit). ~24GB of these accumulated live in the OS temp dir
            # (tempfile.gettempdir() -- /var/folders/... on macOS, NOT $HOME;
            # that is where the 24GB was actually found) and filled the disk.
            # Swept against the SAME parent make_encrypted_snapshot's own
            # tempfile.mkdtemp(prefix="mcpbrain-snap-") call uses (no `dir=`
            # there, so it resolves to gettempdir() too). Best-effort: a sweep
            # failure must not block startup. This is a once-per-process-
            # lifetime safety net, not the only sweep: _backup_under_bulk_lock
            # re-runs the same sweep every loop iteration (see its docstring)
            # so an orphan created by an immediate watchdog-triggered restart
            # -- too fresh for THIS sweep to touch -- still gets cleaned up
            # well within a day, not just at the next full restart.
            try:
                removed = backup.sweep_orphan_snapshots(
                    tempfile.gettempdir(), max_age_s=SNAPSHOT_ORPHAN_MAX_AGE_S)
                if removed:
                    log.info("startup: swept %d orphaned snapshot temp dir(s)", removed)
            except Exception as exc:  # noqa: BLE001 — sweep must not crash startup
                log.warning("snapshot orphan sweep failed: %s", exc, exc_info=True)
            # Reinstall recovery FIRST: if the store is empty and a backup is
            # configured, pull+restore the latest Drive snapshot before the
            # loop, so the first normal cycle delta-syncs from the snapshot
            # point. A restore failure is logged and swallowed — startup must
            # not crash, consistent with how _backup_from_config degrades.
            #
            # Restore must run BEFORE migrate_embed_backend(). Restore overwrites
            # the whole store file with the snapshot, including its embed-backend
            # marker. If migrate ran first it would write the current marker into
            # the (empty) store, restore would then clobber it with the snapshot's
            # (possibly older) marker, and the next migrate check would force a
            # full unconditional re-embed of the restored corpus. Restoring first
            # means migrate's check runs against the restored data and only
            # re-embeds when the backend genuinely changed. The chunk_count()==0
            # guard inside maybe_restore still works: it runs here on the empty
            # store, before migrate.
            #
            # A transient outage is retried here, before any writer starts --
            # see _startup_restore and RESTORE_STARTUP_WAIT_S.
            try:
                self._startup_restore()
            except Exception as exc:  # noqa: BLE001 — restore must not crash startup
                log.warning("restore-on-first-run failed; continuing empty: %s",
                            exc, exc_info=True)
            # Re-embed the whole corpus once if the embedding backend changed
            # since the last run. No-op (and silent) when the marker matches.
            # Runs against the restored data (see above). Guarded: the lazy
            # embedder may not be built yet (model still downloading), or
            # building it may fail outright (e.g. offline, model download
            # unreachable) — either way migration is skipped rather than
            # crashing startup; the guarded run_one() loop below will retry
            # each cycle.
            self._migrate_embed_backend_safe()
            # Resolve services once at startup so they are available from the
            # first cycle, regardless of pause state. ensure_services() is
            # idempotent: the subsequent call inside run_one() becomes a no-op
            # -- unless this one failed TRANSIENTLY, in which case those calls
            # retry it on the _GoogleBackoff schedule.
            self.ensure_services()
            # Start the watchdog's clock for the cycle phase HERE, not at
            # construction: startup (restore, embed migration, service
            # resolution) can legitimately take a while, and the 30-minute
            # stall budget should be measured from the loop actually starting.
            # Stamping it before the maintenance thread exists means a first
            # cycle that never returns is visible to the watchdog.
            self._note_progress("cycle")
            # Maintenance now ticks on its own thread/clock (Task 4) instead of
            # running inline after run_one() — an unbounded cycle used to
            # starve every cadence pass behind it for four days.
            self._start_maintenance_thread()
            while not self._stop.is_set():
                self._wake.clear()          # clear before the cycle; a sync_now during the cycle re-sets it
                cycle_result = None
                try:
                    # _bulk_lock is no longer held across the whole cycle here --
                    # run_one() -> run_cycle() now acquires it only around each
                    # chunk-mutating phase via _cycle_bulk_section, releasing it
                    # between phases so the maintenance thread's four
                    # chunk-writing passes get a real turn. Holding it for the
                    # whole of run_one() is exactly what starved them (live:
                    # 183 consecutive skip warnings, no gated pass run in 8+
                    # minutes).
                    cycle_result = self.run_one()
                    # A cycle that returns (even with a degraded/partial
                    # result) is not the repeated-failure case Task 3's streak
                    # tracks — only a raise is. Reset here, not just at the top
                    # of the loop, so a run of failures immediately followed by
                    # one success clears the streak before the NEXT failure.
                    self._cycle_error_streak = 0
                except Exception as exc:  # noqa: BLE001 — a transient cycle error must not kill the daemon
                    # Crashing here would hand the failure to launchd, whose
                    # restart resets every cadence anchor and drops stashed
                    # block/synthesis requests (live 2026-06-05 Gmail-timeout
                    # crash loop). Log and retry on the next interval; the
                    # skipped _pending_* resets in run_one preserve the stash.
                    #
                    # Re-stamp "cycle" here too, NOT a separate "cycle_error"
                    # key (Task 3 review fix, reproduced directly): a cycle
                    # that raised but whose loop thread is still turning IS
                    # liveness — the thread isn't wedged, it's actively
                    # retrying — so "cycle" is the correct key to refresh.
                    # _stalled_phase() takes the MINIMUM timestamp over EVERY
                    # key in _progress and nothing ever prunes a key, so a
                    # distinct key written only here and never touched again
                    # would go permanently stale after a single transient
                    # error (one Gmail timeout is literally the motivating
                    # case) and falsely trip a restart 30 minutes later on an
                    # otherwise perfectly healthy daemon — the exact failure
                    # mode this whole task exists to prevent, just moved to a
                    # new key. _cycle_error_streak (a plain counter, not in
                    # _progress) is the separate diagnostic signal instead;
                    # _recover_from_stall consults it directly.
                    self._note_progress("cycle")
                    self._cycle_error_streak += 1
                    if self._cycle_error_streak > 3:
                        log.error(
                            "cycle has failed %d times in a row (%s: %s); a "
                            "restart cannot fix a deterministic failure, so "
                            "the watchdog will not restart for this — "
                            "staying up, visibly failing",
                            self._cycle_error_streak, type(exc).__name__, exc,
                            exc_info=True)
                    else:
                        log.error("cycle failed; retrying next interval: %s",
                                  exc, exc_info=True)
                # Retry any Google build (backup config, org-config merge, a
                # pending restore) that failed transiently and is due -- off
                # _bulk_lock, since it is network I/O. Never raises. Checked
                # once per iteration, so these retries land at the first
                # iteration after their retry time (~interval_s), not at it.
                self._retry_google_builds()
                # Backup: capture when due (self-gates on configured + due;
                # harmless when paused -- a restore point of current state),
                # then one time-boxed upload + prune step. Capture takes
                # _bulk_lock only for the online-backup copy, with a BOUNDED
                # acquire (a gated maintenance pass may legitimately hold the
                # lock past one tick); the upload and prune never hold it.
                self._backup_under_bulk_lock()
                # Maintenance-thread liveness (Task 3): the watchdog itself
                # lives inside _maintenance_loop (see _start_maintenance_thread),
                # so if that thread dies (an uncaught exception past the pass-
                # isolation try/except, or the AssertionError re-raise above)
                # the daemon would keep looping cycles forever with no one
                # watching for a wedge ever again — a dead scheduler silently
                # took the watchdog down with it. Checked here, on the cycle
                # thread, once per iteration; restarting is safe because
                # _maintenance_loop is idempotent to (re)start (fresh thread,
                # same self). Skipped while stopping so a deliberate shutdown
                # never resurrects the thread.
                if not self._stop.is_set() and self._maintenance_thread is not None \
                        and not self._maintenance_thread.is_alive():
                    log.error("maintenance thread died; restarting it "
                              "(the watchdog lives there)")
                    self._start_maintenance_thread()
                # Stamp the daemon's own liveness so the fleet beacon (written by
                # a separate process) reports real daemon health, not cached probes.
                write_daemon_heartbeat(str(config.app_dir()))
                if self._pending_update or self._stop.is_set():
                    # Stop the maintenance thread BEFORE breaking out of the
                    # `with self._lock:` block below releases SingleWriterLock.
                    # On the _pending_update path this is the ONLY place that
                    # happens (a plain stop() also calls this itself, making
                    # this a harmless no-op here) -- without it,
                    # update_from_index reinstalls and restarts while the
                    # maintenance thread is still writing SQLite, and the
                    # successor (which now waits HANDOVER_LOCK_WAIT_S for the
                    # lock rather than failing outright) becomes a second
                    # concurrent writer for that window. See
                    # _shutdown_maintenance's docstring.
                    self._shutdown_maintenance()
                    break
                # Re-wake promptly when the cycle yielded mid-work, so a large
                # backlog still drains at close to full speed while the loop
                # keeps reaching the maintenance/heartbeat section every minute.
                more = bool((cycle_result or {}).get("more_work"))
                self._wake.wait(timeout=1.0 if more else self._interval_s)

        if self._pending_update:
            try:
                from mcpbrain import update as upd
                upd.update_from_index(upd._index_url())  # uv install + restart, lock released
            except Exception as exc:  # noqa: BLE001
                log.error("auto-update install failed: %s", exc)


def _tenant_block() -> dict | None:
    """The tenant identity the wizard renders in its fleet section, or None."""
    from mcpbrain import tenant
    prof = tenant.profile()
    return None if prof is None else {"tenant_id": prof.tenant_id,
                                      "display_name": prof.display_name}


def last_backup_attempt_epoch(home) -> float | None:
    """Wall-clock time of the last backup ATTEMPT, or None if unknown.

    Seeds ``Daemon``'s in-memory cadence clock at startup so a restart does not
    read as a first run (see the constructor).

    Reads ``last_attempt``, NOT ``last_success``: the cadence clock advances on
    attempt, and seeding from the last success would let a failure storm resume
    across restarts — precisely what the attempt-based cadence exists to stop.
    A missing or unreadable file returns None, which correctly means "back up
    now" for a fresh install.
    """
    try:
        state = json.loads((Path(home) / "backup_state.json").read_text())
        return float(state["last_attempt"])
    except (OSError, ValueError, TypeError, KeyError):
        return None


def backup_failures(home) -> int:
    """Consecutive failed backups recorded in ``backup_state.json`` (0 when
    unknown). Seeds ``Daemon``'s failure backoff at start-up, the same way
    ``last_backup_attempt_epoch`` seeds its clock."""
    try:
        state = json.loads((Path(home) / "backup_state.json").read_text())
        return max(0, int(state.get("consecutive_failures") or 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def write_daemon_heartbeat(home) -> None:
    """Persist the daemon's last-cycle timestamp to ``daemon_heartbeat.json``.

    The fleet beacon is written by a SEPARATE process (the hourly
    ``mcpbrain fleet-report --beacon`` job), so without this it could only report
    cached probe state — a dead daemon would still look healthy. This file is the
    daemon's own liveness signal: the beacon reads it so the fleet report can
    distinguish "daemon alive" from "beacon job alive" from "offboarded".
    """
    from datetime import datetime, timezone
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (Path(home) / "daemon_heartbeat.json").write_text(
            json.dumps({"last_cycle": stamp}))
    except OSError as exc:
        log.warning("daemon heartbeat write failed (continuing): %s", exc)


def maybe_restore_on_first_run(store, home) -> bool:
    """If the store is empty and backup is configured, restore the newest
    restore point from Drive (store + records repo + config.json). The
    subsequent normal daemon cycle performs the delta sync. Returns True if a
    restore ran (a partial one included: the store has content now).
    The daemon itself uses _restore_on_first_run_classified, which also
    reports a transient skip.
    """
    status, _exc = _restore_on_first_run_classified(store, home)
    return status in ("restored", "partial")


def _restore_on_first_run_classified(store, home, *, gate=None):
    """restore-on-first-run, classified. Returns (status, exc):
      ("not_empty", None)    store has chunks -- never overwritten
      ("unconfigured", None) backup not configured / credentials permanently bad
      ("transient", exc)     Google unreachable (credentials, listing, download),
                             or the live store was busy at the swap
      ("no_snapshot", None)  Drive definitively holds no restore point
      ("restored", None)
      ("partial", exc)       the store was swapped in, but the records repo /
                             config.json could not be placed after it (R24):
                             the store has content now, so this is reported,
                             never retried
    Any other failure (wrong key, every restore point broken) is raised, as
    before. `gate` (the daemon's restoring gate; default the store's own)
    brackets only the final swap (backup.swap_in_store).

    Goes through backup_restore.restore_latest -- the one restore function
    every entry point uses -- referenced via the module attribute so tests can
    patch it.
    """
    if store.chunk_count() != 0:
        return ("not_empty", None)
    build = _backup_from_config(home)
    bc, _interval = build
    if bc is None:
        transient = getattr(build, "transient", None)
        if transient is not None:
            log.warning("restore-on-first-run: Google unreachable (transient): %s", transient)
            return ("transient", transient)
        return ("unconfigured", None)
    swap_gate = gate if gate is not None else getattr(store, "restoring_gate", None)
    swapped: list[bool] = []

    def swap(stage, dest):
        backup.swap_in_store(stage, dest, gate=swap_gate)
        swapped.append(True)

    try:
        result = backup_restore.restore_latest(
            home, bc.drive_service, bc.key, dest_store=store.path,
            records_dir=config.records_dir(home),
            config_path=str(Path(home) / "config.json"), swap=swap)
    except backup_restore.NoRestorePoint as exc:
        if not exc.fallbacks:
            return ("no_snapshot", None)
        raise
    except Exception as exc:  # noqa: BLE001 — classified, then re-raised
        if swapped:
            log.error("restore-on-first-run: the store was restored but the records "
                      "repo / config.json could not be placed: %s", exc)
            return ("partial", exc)
        if _is_transient_google_error(exc):
            log.warning("restore-on-first-run: not restored yet (transient): %s", exc)
            return ("transient", exc)
        raise
    log.info("restored store from %s %s (captured %s)%s", result.used.kind,
             result.used.label, result.used.captured_at,
             f" after {len(result.fallbacks)} fallback(s)" if result.fallbacks else "")
    return ("restored", None)


def _is_transient_probe_error(exc) -> bool:
    """The restore probe's classifier: _is_transient_google_error, plus ANY
    HTTP 5xx from Drive (anywhere in the cause chain). A probe that reads a
    one-off Drive 500 as permanent would lift the hold and let sync write
    into the empty store over the backup. Reflow keeps its own stricter rule
    (a file that always 500s must end); this widening is the probe's only."""
    if _is_transient_google_error(exc):
        return True
    from mcpbrain.sync.reflow_handler import _http_status
    seen: set[int] = set()
    todo = [exc]
    while todo and len(seen) < 8:
        e = todo.pop(0)
        if e is None or id(e) in seen:
            continue
        seen.add(id(e))
        status = _http_status(e)
        if isinstance(status, int) and 500 <= status <= 599:
            return True
        todo.extend([e.__cause__, e.__context__])
    return False


def _probe_snapshot(home):
    """Does Drive hold a restore point for this user (backup_restore.candidates)? ("snapshot" | "no_snapshot" |
    "unconfigured" | "transient" | "error", exc). Read-only; never raises.
    Any Drive 5xx counts as transient here (_is_transient_probe_error)."""
    build = _backup_from_config(home)
    bc, _interval = build
    if bc is None:
        transient = getattr(build, "transient", None)
        return ("transient", transient) if transient is not None else ("unconfigured", None)
    try:
        found = backup_restore.candidates(home, bc.drive_service, bc.key)
    except Exception as exc:  # noqa: BLE001 — a probe must never raise into the loop
        if _is_transient_probe_error(exc):
            return ("transient", exc)
        log.warning("restore pending: snapshot lookup failed permanently: %s", exc)
        return ("error", exc)
    return ("snapshot", None) if found else ("no_snapshot", None)


def _restore_pending_path(home) -> Path:
    return Path(home) / RESTORE_PENDING_MARKER


def _read_restore_pending(home) -> dict | None:
    """The persisted restore-pending marker, or None. Never raises."""
    try:
        data = json.loads(_restore_pending_path(home).read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"since": None, "reason": "unreadable marker"}
    return data if isinstance(data, dict) else {"since": None, "reason": ""}


def _write_restore_pending(home, reason: str) -> bool:
    from datetime import datetime, timezone
    try:
        _restore_pending_path(home).write_text(json.dumps({
            "since": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": reason[:200]}))
        return True
    except OSError as exc:
        log.warning("could not persist the restore-pending marker (holding in memory): %s",
                    exc)
        return False


def _clear_restore_pending(home) -> bool:
    """Remove the marker. True when it is gone (or never existed)."""
    try:
        _restore_pending_path(home).unlink()
        log.info("restore-pending cleared")
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not clear the restore-pending marker (will retry): %s", exc)
        return False
    return True


class _BackupBuild(tuple):
    """`_backup_from_config`'s result: a plain (BackupConfig | None,
    interval | None) 2-tuple to every caller that unpacks or compares it, plus
    `.transient` -- the exception when the build failed TRANSIENTLY (network),
    so the daemon schedules a retry instead of leaving backup off for the run.
    A tuple subclass rather than a third element keeps the function's long-
    standing (bc, interval) contract, including every test that stubs it."""

    def __new__(cls, bc=None, interval=None, transient=None):
        self = super().__new__(cls, (bc, interval))
        self.transient = transient
        return self


def _backup_from_config(home):
    """Build a Drive-backed BackupConfig from config.json, or (None, None) if
    backup is not fully configured / credentials are unavailable.

    Returns a _BackupBuild: when the credential build failed TRANSIENTLY
    (_is_transient_google_error) it is (None, None) with `.transient` set, and
    the CALLER owns reporting and retrying it (Daemon._backup_backoff).

    Backup stays OFF (returns (None, None)) when: there is no `backup` block, a
    required field (escrow_key/shared_drive_id/user_id) is missing, Google
    credentials can't be loaded, or the token lacks Drive scope (no
    drive_service). Failure degrades gracefully and is logged — it never crashes
    daemon startup.
    """
    cfg = config.read_config(home).get("backup") or {}
    escrow_key = cfg.get("escrow_key")
    shared_drive_id = cfg.get("shared_drive_id")
    user_id = cfg.get("user_id")
    if not (escrow_key and shared_drive_id and user_id):
        return _BackupBuild()
    try:
        # Backup uploads are ~750MB and need the long timeout; routine reads use
        # DEFAULT_READ_TIMEOUT_S (60s). Pass the 600s timeout explicitly.
        services = auth.build_google_services(token_file=Path(home) / "google_token.json",
                                              drive_timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — backup must not crash startup
        # NOTE: this also catches programming errors (e.g. a bad call signature).
        # If auth.build_google_services' signature changes, re-verify this call.
        if _is_transient_google_error(exc):
            return _BackupBuild(transient=exc)
        log.warning("backup configured but Google credentials unavailable; backup disabled: %s", exc)
        return _BackupBuild()
    drive = services.get("drive_service")
    if not drive:
        log.warning("backup configured but the token lacks Drive scope; backup disabled")
        return _BackupBuild()
    key = escrow_key.encode() if isinstance(escrow_key, str) else escrow_key
    settings = _backup_settings(cfg)
    # The resumable-upload transport, from the same credentials as `drive`.
    # Built eagerly when the token loads; otherwise None and built on first
    # upload (`Daemon._backup_transport`) -- never a reason to turn backup off.
    try:
        transport = _build_backup_transport(Path(home) / "google_token.json")
    except Exception as exc:  # noqa: BLE001 — retried lazily at upload time
        log.info("backup upload transport not built yet (%s); will retry on upload", exc)
        transport = None
    bc = BackupConfig(key=key, drive_service=drive,
                      shared_drive_id=shared_drive_id, user_id=user_id,
                      settings=settings, transport=transport)
    return _BackupBuild(bc, settings.capture_interval_s)


def _backup_settings(cfg: dict) -> "backup_outbox.Settings":
    """The `backup` block as Settings, each value validated: a bad or
    non-positive value falls back to its default (logged), never disables
    backup. `interval_s` is ignored -- it was the full-upload cadence; capture
    runs every `capture_interval_s` (default DEFAULT_BACKUP_INTERVAL_S)."""
    d = backup_outbox.Settings()
    raw = dict(cfg or {})
    retain_key = "retain_days" if "retain_days" in raw else "retain"
    for key, attr in (("capture_interval_s", "capture_interval_s"),
                      (retain_key, "retain_days"),
                      ("base_threshold", "base_threshold"),
                      ("upload_budget_s", "upload_budget_s"),
                      ("outbox_max_bytes", "outbox_max_bytes")):
        if key not in raw:
            continue
        try:
            v = float(raw[key])
            if not v > 0 or v != v or v == float("inf"):
                raise ValueError("must be positive")
        except (TypeError, ValueError) as exc:
            log.warning("backup.%s invalid (%r); using default %s: %s",
                        key, raw[key], getattr(d, attr), exc)
            raw.pop(key)
    return backup_outbox.Settings.from_config(raw)


def _build_backup_transport(token_file=None):
    """The resumable-upload Transport for backup, from the user's OAuth token
    (the same credentials the backup drive_service is built from), with the
    long upload timeout. Raises when the credentials cannot be loaded."""
    creds = (auth.load_credentials(token_file=Path(token_file)) if token_file
             else auth.load_credentials())
    return drive_resumable.authorized_transport(creds, auth.DEFAULT_HTTP_TIMEOUT_S)


def _build_drive_service():
    """Build a Drive v3 service from the user's OAuth token, or raise."""
    from mcpbrain import auth
    creds = auth.load_credentials()
    return auth.build_service("drive", "v3", creds)


def _maybe_merge_org_config(home):
    """Merge org-config into local config. Best-effort.

    Returns the exception when the merge failed TRANSIENTLY (the caller
    schedules a retry -- Daemon._org_config_backoff), else None.

    Never raises: a Drive failure leaves local config intact. The daemon
    NEVER calls an LLM here — this is pure Drive I/O.

    Gated on a stored Google OAuth token: with no credentials there is no
    Drive access to attempt, so skip before touching auth/Drive at all —
    this avoids a per-boot "org-config merge skipped" warning on every
    install that hasn't connected Google yet (the common state before
    onboarding completes). Folder resolution (explicit fleet.folder_id, else
    the baked-in org default) is NOT re-derived here — merge_org_config
    already owns that fallback (0.7.90); re-deriving it here was a redundant
    duplicate of that logic.
    """
    status, exc = _merge_org_config_classified(home)
    return exc if status == "transient" else None


def _merge_org_config_classified(home) -> tuple[str, BaseException | None]:
    """The org-config merge, classified: ("merged", None), ("no_token", None),
    ("permanent", exc) or ("transient", exc). Never raises. Only a permanent
    failure is logged here; a transient one is reported by the retry backoff."""
    from mcpbrain import auth, fleet
    if not auth.token_path().exists():
        return ("no_token", None)
    try:
        svc = _build_drive_service()
        fleet.merge_org_config(home, svc)
    except Exception as exc:  # noqa: BLE001 — org-config is best-effort
        if _is_transient_google_error(exc):
            return ("transient", exc)
        log.warning("org-config merge skipped: %s", exc)
        return ("permanent", exc)
    return ("merged", None)


_CADENCE_DEFAULTS: dict[str, float] = {
    "communities_interval_s":         86400.0,
    "blocks_interval_s":              86400.0,
    "proactive_interval_s":           86400.0,
    "waiting_on_interval_s":          86400.0,
    "lint_interval_s":                86400.0,
    "stale_reextract_interval_s":     86400.0,
    "feedback_aggregate_interval_s":  86400.0,   # S2: nightly aggregate
    "org_backfill_interval_s":        86400.0,   # Q4: daily deterministic backfill
    "resolve_entities_interval_s":    86400.0,   # Task 3.3: daily deterministic entity dedup (issue #23-fix validated)
    "review_interval_s":              86400.0,   # Session-4: daily AI-adjudicated graph-hygiene review
    "action_hygiene_interval_s":      86400.0,   # daily actions sweep: TTL + duplicate collapse
    "mcp_heartbeat_sweep_interval_s": 3600.0,    # hourly: prune dead-pid mcp_heartbeat/*.json
    "salience_score_interval_s":      86400.0,   # B3: daily structural salience
    "decay_pass_interval_s":          86400.0,   # B5: nightly decay pass
    "consolidation_interval_s":       86400.0,   # B4: nightly consolidation
    "voice_analyse_interval_s":       604800.0,  # B6: weekly voice analysis
    "self_improve_interval_s":        604800.0,  # S4/S5: weekly drift+bandit+lessons
    "auto_enable_interval_s":         86400.0,    # auto-graduation: daily readiness check
    "synthesise_interval_s":          604800.0,
    "audit_interval_s":               604800.0,
    "verify_interval_s":              3600.0,
    "auto_update_interval_s":         86400.0,
    "org_contrib_upload_interval_s":  86400.0,   # Phase 0 stub: daily contribution upload
    "org_import_interval_s":          86400.0,   # Phase 0 stub: daily snapshot import
    "org_curate_interval_s":          86400.0,   # Phase 0 stub: daily curator adjudication
    "ocr_setup_interval_s":           86400.0,   # once-ever OCR binary install (marker-gated)
    "reflow_seed_interval_s":         3600.0,    # extraction-fidelity: hourly reflow queue top-up
}

_CADENCE_KEYS = (
    "communities_interval_s",
    "lint_interval_s",
    "synthesise_interval_s",
    "proactive_interval_s",
    "waiting_on_interval_s",
    "blocks_interval_s",
    "audit_interval_s",
    "stale_reextract_interval_s",
    "auto_update_interval_s",
    "verify_interval_s",
    "feedback_aggregate_interval_s",
    "org_backfill_interval_s",
    "resolve_entities_interval_s",
    "review_interval_s",
    "action_hygiene_interval_s",
    "mcp_heartbeat_sweep_interval_s",
    "salience_score_interval_s",
    "decay_pass_interval_s",
    "consolidation_interval_s",
    "voice_analyse_interval_s",
    "self_improve_interval_s",
    "auto_enable_interval_s",
    "org_contrib_upload_interval_s",
    "org_import_interval_s",
    "org_curate_interval_s",
    "ocr_setup_interval_s",
    "reflow_seed_interval_s",
)


def _cadences_from_config(home) -> dict:
    """Read the cadences block. Absent keys use _CADENCE_DEFAULTS (so a fresh
    install is fully functional); an explicit entry overrides, and an explicit
    0/negative maps to None (OFF) as a power-user kill-switch. clickup is NOT
    here — it is gated on api_key+list_id (C3).
    """
    cfg = config.read_config(home)
    cadences_block = cfg.get("cadences") or {}
    # Org-config overlay (staged by fleet.merge_org_config under "org_config")
    # wins over the user's local cadences — this is how an admin pushes a
    # cadence change org-wide. Confined to cadences by the org-config allowlist.
    org_cadences = (cfg.get("org_config") or {}).get("cadences") or {}
    if org_cadences:
        cadences_block = {**cadences_block, **org_cadences}
    result = {}
    for key in _CADENCE_KEYS:
        if key not in cadences_block:
            result[key] = _CADENCE_DEFAULTS.get(key)
            continue
        raw = cadences_block[key]
        try:
            val = float(raw)
            if val <= 0:
                raise ValueError("must be positive")
            result[key] = val
        except (TypeError, ValueError) as exc:
            log.warning("cadences.%s invalid (%r); disabling: %s", key, raw, exc)
            result[key] = None
    return result


# Task 7: module-tuning-constant defaults, keyed by the config['tuning'] name
# each reads through. Built FROM the module constants (not duplicated as
# separate literals) so this dict can never drift from what a Daemon actually
# falls back to when the config block is absent or invalid.
_TUNING_DEFAULTS: dict[str, float | int] = {
    "cycle_budget_s": CYCLE_BUDGET_S,
    "maintenance_tick_s": MAINTENANCE_TICK_S,
    "stall_s": STALL_S,
    "bulk_lock_acquire_s": BULK_LOCK_ACQUIRE_S,
    "bulk_lock_yield_s": BULK_LOCK_YIELD_S,
    "watchdog_max_exits": WATCHDOG_MAX_EXITS,
    "watchdog_window_s": WATCHDOG_WINDOW_S,
    "handover_lock_wait_s": HANDOVER_LOCK_WAIT_S,
    "embed_max_items": EMBED_MAX_ITEMS,
}

# Keys that coerce via int() rather than the default float() -- a restart
# count and an item count, not a duration.
_TUNING_INT_KEYS = frozenset({"watchdog_max_exits", "embed_max_items"})


def _tuning_from_config(home) -> dict:
    """Read config['tuning'], overriding _TUNING_DEFAULTS. Thin wrapper around
    config.daemon_tuning so this module's own defaults dict is the single
    source of truth (see _TUNING_DEFAULTS). Unlike _cadences_from_config,
    none of these knobs has an OFF state, so an invalid override falls back to
    the default rather than mapping to None. Lets a wedged install be tuned
    (e.g. raise the cycle budget, lower the watchdog restart ceiling) without
    a release.
    """
    return config.daemon_tuning(home, _TUNING_DEFAULTS, int_keys=_TUNING_INT_KEYS)


def main(argv=None) -> None:
    """CLI entry point: `python -m mcpbrain.daemon [--once] [--interval N]`.

    Wires a real embedder + store + enrich client, then runs either a single
    cycle (--once) or the interval loop. Google services auto-build from the
    user's token inside the daemon (services=None); a missing token degrades to
    no sync rather than crashing — authorise via `python -m mcpbrain.auth`.

    The embedder itself is built lazily (see `Daemon._embedder`): only its
    dimension (`embedder_dim`, a fixed constant — no onnxruntime import) is
    needed up front to size the store, so the control server / setup wizard
    can start and become reachable even before the model is downloaded.
    """
    import argparse

    from mcpbrain.embed import embedder_dim, get_embedder
    from mcpbrain.store import Store

    ap = argparse.ArgumentParser(prog="mcpbrain.daemon")
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    ap.add_argument("--interval", type=float, default=300.0, help="sync interval seconds")
    args = ap.parse_args(argv)

    _configure_logging()

    from mcpbrain import vcruntime
    if sys.platform == "win32":
        vcruntime.ensure_vcruntime_dlls(str(config.app_dir()))
    vcruntime.add_search_dir(str(config.app_dir()))

    dim = embedder_dim("bge-small")
    store = Store(config.store_path(), dim=dim)
    store.init()
    org_transient = _maybe_merge_org_config(str(config.app_dir()))
    enrich_mode = config.enrich_mode(str(config.app_dir()))
    backup_build = _backup_from_config(str(config.app_dir()))
    backup_cfg, backup_interval = backup_build
    cadences = _cadences_from_config(str(config.app_dir()))
    tuning = _tuning_from_config(str(config.app_dir()))
    daemon = Daemon(store, embedder=None, interval_s=args.interval,
                    enrich_mode=enrich_mode,
                    backup=backup_cfg, backup_interval_s=backup_interval,
                    last_backup_attempt_epoch=last_backup_attempt_epoch(
                        str(config.app_dir())),
                    backup_failures=backup_failures(str(config.app_dir())),
                    communities_interval_s=cadences["communities_interval_s"],
                    lint_interval_s=cadences["lint_interval_s"],
                    synthesise_interval_s=cadences["synthesise_interval_s"],
                    proactive_interval_s=cadences["proactive_interval_s"],
                    waiting_on_interval_s=cadences["waiting_on_interval_s"],
                    blocks_interval_s=cadences["blocks_interval_s"],
                    audit_interval_s=cadences["audit_interval_s"],
                    stale_reextract_interval_s=cadences["stale_reextract_interval_s"],
                    auto_update_interval_s=cadences["auto_update_interval_s"],
                    verify_interval_s=cadences["verify_interval_s"])
    # Lazy embedder: the real model loads on first use of self._embedder
    # (e.g. inside run_one()/search()), not here — see Daemon._embedder.
    daemon._embedder_factory = lambda: get_embedder("bge-small")
    # S2/Q4/B3/B5/B4/B6 cadences: not constructor params; wire after construction.
    daemon._feedback_aggregate_interval_s = cadences["feedback_aggregate_interval_s"]
    daemon._org_backfill_interval_s = cadences["org_backfill_interval_s"]
    daemon._resolve_entities_interval_s = cadences["resolve_entities_interval_s"]
    daemon._action_hygiene_interval_s = cadences["action_hygiene_interval_s"]
    daemon._mcp_heartbeat_sweep_interval_s = cadences["mcp_heartbeat_sweep_interval_s"]
    daemon._review_interval_s = cadences["review_interval_s"]
    daemon._salience_score_interval_s = cadences["salience_score_interval_s"]
    daemon._decay_pass_interval_s = cadences["decay_pass_interval_s"]
    daemon._consolidation_interval_s = cadences["consolidation_interval_s"]
    daemon._voice_analyse_interval_s = cadences["voice_analyse_interval_s"]
    daemon._self_improve_interval_s = cadences["self_improve_interval_s"]
    daemon._auto_enable_interval_s = cadences["auto_enable_interval_s"]
    daemon._org_contrib_upload_interval_s = cadences["org_contrib_upload_interval_s"]
    daemon._org_import_interval_s = cadences["org_import_interval_s"]
    daemon._org_curate_interval_s = cadences["org_curate_interval_s"]
    daemon._ocr_setup_interval_s = cadences["ocr_setup_interval_s"]
    daemon._reflow_seed_interval_s = cadences["reflow_seed_interval_s"]
    # Task 7 tuning knobs: not constructor params either, wired the same way
    # as the cadences just above (see _tuning_from_config / Daemon.__init__).
    daemon._cycle_budget_s = tuning["cycle_budget_s"]
    daemon._maintenance_interval_s = tuning["maintenance_tick_s"]
    daemon._stall_s = tuning["stall_s"]
    daemon._bulk_lock_wait_s = tuning["bulk_lock_acquire_s"]
    daemon._bulk_lock_yield_s = tuning["bulk_lock_yield_s"]
    daemon._watchdog_max_exits = tuning["watchdog_max_exits"]
    daemon._watchdog_window_s = tuning["watchdog_window_s"]
    daemon._embed_max_items = tuning["embed_max_items"]
    # A startup Google build that failed TRANSIENTLY retries on a backoff from
    # the run loop (_retry_google_builds) rather than staying off for the run.
    if org_transient is not None:
        daemon._org_config_backoff.failed(org_transient)
    if getattr(backup_build, "transient", None) is not None:
        daemon._backup_backoff.failed(backup_build.transient)

    if args.once:
        daemon.ensure_services()   # resolve services before the single cycle
        result = daemon.run_one()
        print("cycle:", result)
    else:
        # Loop mode serves the token-guarded loopback control API + browser
        # wizard alongside the sync loop. ControlServer.start() writes the
        # control_port/control_token files `mcpbrain setup` reads. A one-shot
        # --once cycle needs no control server, so it stays unwired above.
        #
        # Order matters: probe the single-writer lock BEFORE ControlServer.start()
        # so a second instance (e.g. a launchd retry racing the running daemon)
        # exits cleanly without clobbering the live daemon's on-disk
        # control_port/control_token. Otherwise the tray, which reads those
        # files, would be pointed at a dead port. The probe acquires-then-releases
        # so daemon.run()'s own `with self._lock:` can re-acquire normally; the
        # TOCTOU window is microseconds vs. launchd's 10-second minimum-runtime
        # retry cadence.
        #
        # The probe waits HANDOVER_LOCK_WAIT_S rather than giving up on the
        # first attempt: when the watchdog self-heals an unsupervised Windows
        # install it spawns this process while the parent is still exiting, so
        # a strictly non-blocking acquire would kill the successor and leave
        # nothing running. A genuinely-running second daemon still loses, just
        # a couple of seconds later.
        try:
            probe = SingleWriterLock()
            probe.acquire(timeout_s=tuning["handover_lock_wait_s"])
            probe.release()
        except AlreadyRunningError:
            log.error("another mcpbrain daemon is already running; exiting")
            raise SystemExit(1)
        ctrl = control_api.ControlServer(daemon, home=str(config.app_dir()), store=store)
        ctrl.start()
        log.info("control API + wizard on http://127.0.0.1:%d/", ctrl.port)
        try:
            daemon.run()           # loop until Ctrl-C / stop
        finally:
            ctrl.stop()


if __name__ == "__main__":
    main()
