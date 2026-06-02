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
  reuses make_encrypted_snapshot/upload_snapshot; a backup failure is logged and
  swallowed so it never crashes the loop.
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

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mcpbrain import auth, backup, config, control_api, enrich
from mcpbrain.backup import make_encrypted_snapshot, upload_snapshot
from mcpbrain.config import app_dir
from mcpbrain.enrich import run_enrichment
from mcpbrain.sync import run_sync_cycle

log = logging.getLogger(__name__)

EMBED_BACKEND = "fastembed:bge-small:v1"
DEFAULT_BACKUP_INTERVAL_S = 3600


@dataclass
class BackupConfig:
    """Config for the daemon's periodic encrypted backup (Task H2).

    Supplying a BackupConfig is what TURNS BACKUP ON — the daemon never backs up
    when backup is None. Holds the escrow key, an INJECTED Drive service (so
    tests mock it; no real network), the Shared Drive id and per-user folder
    name, and the local encrypted artifact path. out_path defaults to
    app_dir()/"snapshot.enc" — a stable local encrypted artifact that is also
    uploaded. __post_init__ resolves a None out_path to that default, so the
    field is always a Path after construction.
    """

    key: bytes
    drive_service: object
    shared_drive_id: str
    user_id: str
    out_path: Path | None = None

    def __post_init__(self):
        self.out_path = (
            Path(self.out_path) if self.out_path is not None
            else app_dir() / "snapshot.enc"
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

    def acquire(self) -> None:
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


def run_cycle(store, embedder, *, gmail_service=None, calendar_service=None,
              drive_service=None, enrich_client=None,
              enrich_limit: int | None = None) -> dict:
    """One sync -> embed -> enrich cycle.

    Sync each provided source and embed via run_sync_cycle (the tested core),
    then enrich the un-enriched chunks. Enrichment is tiered: with an
    enrich_client it extracts into the graph and marks chunks enriched; without
    one (None) it defers (no graph writes, no marking, mode flag set).

    enrich_limit caps how many un-enriched chunks are processed this cycle so a
    large post-migration backlog drains progressively over multiple cycles
    rather than enriching the entire corpus in one tight, lock-holding loop.
    None enriches every un-enriched chunk.

    Returns the sync result dict ({"gmail","calendar","drive","embedded"}) plus
    an "enrich" key holding run_enrichment's summary.
    """
    result = run_sync_cycle(
        store, embedder,
        gmail_service=gmail_service,
        calendar_service=calendar_service,
        drive_service=drive_service,
    )
    docs = [
        (c["doc_id"], c["text"], c["metadata"])
        for c in store.unenriched_chunks(limit=enrich_limit)
    ]
    result["enrich"] = run_enrichment(store, docs, client=enrich_client)
    return result


class Daemon:
    """Owns the store-writing loop: sync -> embed on an interval, with
    pause/resume and a single-writer lock.

    Orchestration scope: wires sync -> embed -> enrich -> maybe_backup.
    Enrichment is tiered via enrich_client (None -> defer no-op). Periodic backup
    is tiered via backup (None -> OFF); when configured it self-gates on a
    time-based cadence using the injected clock.

    Threading model: pause/stop/wake are threading.Event objects so the tray
    (Task 3.2) and tests can drive the daemon without real timers. run() blocks
    on _wake.wait(interval_s) so pause/sync_now/stop are responsive.
    """

    def __init__(self, store, embedder, *, services: dict | None = None,
                 interval_s: float = 300.0,
                 lock=None, enrich_client=None, enrich_batch: int = 100, backup=None,
                 backup_interval_s: float | None = None,
                 resolve_interval_s: float | None = None, clock=time.monotonic):
        self._store = store
        self._embedder = embedder
        self._enrich_client = enrich_client  # None -> enrichment defers (no-op)
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
        # interleave reading a new config with the old interval. _config_lock
        # also guards _enrich_client (set by apply_config, read by run_one).
        self._config_lock = threading.Lock()
        self._backup = backup
        self._backup_interval_s = backup_interval_s
        if self._backup is not None and self._backup_interval_s is None:
            raise ValueError("backup_interval_s is required when backup is configured")
        # Periodic entity resolution is OFF unless resolve_interval_s is set.
        # Tiered like enrichment: reuses self._enrich_client (None -> resolve_entities
        # does deterministic-only resolution). Time-based cadence via self._clock.
        self._resolve_interval_s = resolve_interval_s
        self._clock = clock
        self._last_backup = None
        self._last_resolve = None
        self._pause = threading.Event()   # set == paused
        self._stop = threading.Event()    # set == stop the loop
        self._wake = threading.Event()    # set == run a cycle now

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
        logged and degrades to empty services (no sync, no crash).
        """
        if self._services_resolved:
            return self._services

        from mcpbrain import auth
        try:
            built = auth.build_google_services()
        except Exception as exc:  # noqa: BLE001 — no/invalid token, etc.
            log.warning(
                "no Google credentials — running without sync "
                "(authorise: python -m mcpbrain.auth): %s", exc
            )
            built = {}
        self._services = self._filter_services(built)
        self._services_resolved = True
        return self._services

    # -- pause / resume -----------------------------------------------------

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
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
        try:
            if token_file.exists():
                creds = Credentials.from_authorized_user_file(str(token_file), auth.SCOPES)
                scopes = auth._granted_scopes(creds, token_file)
                granted = sorted(scopes) if scopes else []
                google_connected = bool(creds and (creds.valid or creds.refresh_token))
        except Exception as exc:  # noqa: BLE001 — no/invalid token degrades, never crashes
            log.debug("status: Google credentials unavailable: %s", exc)
        return {
            "paused": self.is_paused(),
            "chunk_count": self._store.chunk_count(),
            "google_connected": google_connected,
            "granted_scopes": granted,
            "enrich_enabled": self._enrich_client is not None,
        }

    def apply_config(self, body: dict) -> None:
        """Persist config updates, then re-wire enrich + backup from disk.

        Writes via config.write_config (atomic, 0600) then rebuilds the enrich
        client and backup config from the freshly-written config so a key change
        takes effect without a restart. Never logs the key.
        """
        home = str(app_dir())
        config.write_config(home, body)
        # Build both off-lock (network/IO work), then set all daemon-config
        # mutation under _config_lock so the loop thread never reads a new
        # _enrich_client or a _backup paired with a stale interval. Keep the
        # lock hold time to the assignments only.
        enrich_client = _enrich_client_from_config(home)
        backup_cfg, backup_interval = _backup_from_config(home)
        with self._config_lock:
            self._enrich_client = enrich_client
            self._backup = backup_cfg
            self._backup_interval_s = backup_interval

    def register(self) -> str:
        """Register mcpbrain with Claude Desktop and return the config path."""
        # Lazy import to avoid an import cycle (wizard imports daemon-adjacent code).
        from mcpbrain.wizard.register import register_mcpbrain
        return str(register_mcpbrain(mcpbrain_home=str(app_dir())))

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
        finally:
            self._auth_lock.release()

    # -- wake / stop --------------------------------------------------------

    def sync_now(self) -> None:
        """Wake the loop for an immediate cycle."""
        self._wake.set()

    def stop(self) -> None:
        """Signal the loop to exit, and wake it so run() returns promptly."""
        self._stop.set()
        self._wake.set()

    def is_stopped(self) -> bool:
        """Return True if stop() has been called (the _stop event is set)."""
        return self._stop.is_set()

    # -- one cycle ----------------------------------------------------------

    def run_one(self) -> dict | None:
        """Run a single cycle, unless paused.

        When paused, returns None and writes nothing to the store (the pause
        guarantee). Otherwise runs run_cycle with the configured services and
        returns its result dict.
        """
        if self._pause.is_set():
            return None
        services = self.ensure_services()
        # Snapshot the enrich client under the config lock so apply_config (HTTP
        # handler thread) can't swap it mid-cycle; use the local for this cycle.
        with self._config_lock:
            enrich_client = self._enrich_client
        return run_cycle(self._store, self._embedder,
                         enrich_client=enrich_client,
                         enrich_limit=self._enrich_batch, **services)

    # -- periodic backup ----------------------------------------------------

    def maybe_backup(self) -> dict | None:
        """Take an encrypted snapshot and upload it, if backup is due.

        OFF unless a BackupConfig was supplied: returns None when self._backup
        is None (never backs up an unconfigured daemon). Otherwise gates on a
        time-based cadence using the injected clock — due on the first call
        (self._last_backup is None) or once backup_interval_s has elapsed since
        the last backup. Not due -> returns None and does nothing.

        When due: reuses backup.py's primitives — make_encrypted_snapshot
        produces the encrypted artifact (the only artifact; no cleartext leaves
        the machine) and upload_snapshot ships it to the per-user Shared Drive
        folder. Returns a summary dict.

        A backup failure (e.g. a Drive error) is logged and swallowed so the
        daemon loop keeps running — it returns {"backed_up": False, "error": ...}
        rather than propagating. _last_backup advances only on a clean run, so a
        failed attempt retries on the next due tick.
        """
        # Snapshot the (backup, interval) pair atomically under the lock so a
        # concurrent apply_config can't hand us a new config with the old
        # interval. Use the locals for the rest of the method.
        with self._config_lock:
            backup, interval = self._backup, self._backup_interval_s

        if backup is None:
            return None

        if self._last_backup is not None:
            elapsed = self._clock() - self._last_backup
            if elapsed < interval:
                return None

        cfg = backup
        try:
            path = make_encrypted_snapshot(self._store.path, cfg.out_path, cfg.key)
            file_id = upload_snapshot(
                cfg.drive_service, path, cfg.shared_drive_id, cfg.user_id
            )
        except Exception as exc:  # noqa: BLE001 — backup must never crash the loop
            log.warning("periodic backup failed: %s", exc, exc_info=True)
            return {"backed_up": False, "error": str(exc)}

        # Advance the cadence clock only after a clean backup.
        self._last_backup = self._clock()
        return {"backed_up": True, "file_id": file_id, "path": str(path)}

    # -- periodic entity resolution -----------------------------------------

    def maybe_resolve(self) -> dict | None:
        """Run entity resolution, if it is due.

        OFF unless resolve_interval_s was supplied: returns None when
        self._resolve_interval_s is None (never resolves an unconfigured
        daemon). Otherwise gates on a time-based cadence using the injected
        clock — due on the first call (self._last_resolve is None) or once
        resolve_interval_s has elapsed since the last resolve. Not due ->
        returns None and does nothing.

        Tiered: reuses self._enrich_client. With a client, resolve_entities
        also LLM-adjudicates fuzzy candidates; without one (None) it does
        deterministic-only resolution, which is safe.

        A resolve failure is logged and swallowed so the daemon loop keeps
        running — it returns {"resolved": False, "error": ...} rather than
        propagating. _last_resolve advances only on a clean run, so a failed
        attempt retries on the next due tick.

        Cost note: with an enrich_client set, resolution LLM-adjudicates fuzzy
        candidates (up to resolve_entities' max_adjudications per run). A very
        small resolve_interval_s therefore drives frequent LLM calls — pick an
        interval well above the sync interval (resolution is cheap to defer).
        """
        if self._resolve_interval_s is None:
            return None

        # Record the START time as the cadence anchor (unlike maybe_backup's
        # end-time): a slow LLM-adjudicated resolve then doesn't eat into the
        # next interval. _last_resolve is committed only on a clean run below.
        now = self._clock()
        if self._last_resolve is not None and (now - self._last_resolve) < self._resolve_interval_s:
            return None

        try:
            # Lazy import: keeps the daemon import light and resolution an
            # optional path; also lets tests patch mcpbrain.resolve.resolve_entities.
            from mcpbrain.resolve import resolve_entities
            summary = resolve_entities(self._store, client=self._enrich_client)
        except Exception as exc:  # noqa: BLE001 — resolve must never crash the loop
            log.warning("resolve failed (will retry next due): %s", exc, exc_info=True)
            return {"resolved": False, "error": str(exc)}

        # Advance the cadence clock only after a clean resolve.
        self._last_resolve = now
        return summary

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
        count = index_pending(self._store, self._embedder)
        log.info("re-embedded %d chunks for backend change", count)
        self._store.set_meta("embed_backend", backend)
        return count

    def run(self) -> None:
        """Acquire the single-writer lock and loop until stopped.

        Each iteration: clear _wake BEFORE run_one() so any sync_now() that
        arrives during the cycle re-sets it and causes the following wait to
        return immediately (rather than waiting the full interval). After
        run_one(), wait up to interval_s on _wake. Releases the lock on exit
        via the context manager.
        """
        with self._lock:
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
            try:
                maybe_restore_on_first_run(self._store, str(config.app_dir()))
            except Exception as exc:  # noqa: BLE001 — restore must not crash startup
                log.warning("restore-on-first-run failed; continuing empty: %s",
                            exc, exc_info=True)
            # Re-embed the whole corpus once if the embedding backend changed
            # since the last run. No-op (and silent) when the marker matches.
            # Runs against the restored data (see above).
            self.migrate_embed_backend()
            # Resolve services once at startup so they are available from the
            # first cycle, regardless of pause state. ensure_services() is
            # idempotent: the subsequent call inside run_one() becomes a no-op.
            self.ensure_services()
            while not self._stop.is_set():
                self._wake.clear()          # clear before the cycle; a sync_now during the cycle re-sets it
                self.run_one()
                # Backup self-gates on configured + due; harmless when paused
                # (a snapshot of current state). Runs in this loop thread, so it
                # shares the single-writer lock the daemon already holds.
                self.maybe_backup()
                # Resolution self-gates on configured + due; reuses the
                # single-writer lock this loop thread already holds.
                self.maybe_resolve()
                if self._stop.is_set():
                    break
                # Block until woken (sync_now/stop) or the interval elapses.
                self._wake.wait(timeout=self._interval_s)


def _enrich_client_from_config(home):
    """Build the enrich client, config-first with env fallback.

    Reads config['gemini_key'] from the app dir; falls back to GEMINI_API_KEY
    for the existing dev path. Tiered: an empty key yields no client (enrichment
    deferred). The key is never logged.
    """
    cfg = config.read_config(home)
    return enrich.resolve_client(cfg.get("gemini_key") or os.getenv("GEMINI_API_KEY"))


def maybe_restore_on_first_run(store, home) -> bool:
    """Restore the latest encrypted snapshot when starting with an empty store.

    No-op unless: the store is empty (chunk_count() == 0), a backup is fully
    configured, and Drive has at least one snapshot. The subsequent normal
    daemon cycle performs the delta sync. Returns True if a restore ran.

    References backup functions via the module attribute (backup.find_latest_snapshot,
    backup.download_and_restore) so they remain patchable in tests.
    """
    if store.chunk_count() != 0:
        return False
    bc, _interval = _backup_from_config(home)
    if bc is None:
        return False
    file_id = backup.find_latest_snapshot(bc.drive_service, bc.shared_drive_id, bc.user_id)
    if not file_id:
        return False
    backup.download_and_restore(bc, store, file_id)
    log.info("restored store from latest snapshot %s", file_id)
    return True


def _backup_from_config(home):
    """Build a Drive-backed BackupConfig from config.json, or (None, None) if
    backup is not fully configured / credentials are unavailable.

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
        return (None, None)
    try:
        services = auth.build_google_services(token_file=Path(home) / "google_token.json")
    except Exception as exc:  # noqa: BLE001 — backup must not crash startup
        # NOTE: this also catches programming errors (e.g. a bad call signature).
        # If auth.build_google_services' signature changes, re-verify this call.
        log.warning("backup configured but Google credentials unavailable; backup disabled: %s", exc)
        return (None, None)
    drive = services.get("drive_service")
    if not drive:
        log.warning("backup configured but the token lacks Drive scope; backup disabled")
        return (None, None)
    key = escrow_key.encode() if isinstance(escrow_key, str) else escrow_key
    raw_interval = cfg.get("interval_s", DEFAULT_BACKUP_INTERVAL_S)
    try:
        interval_s = float(raw_interval)
        if interval_s <= 0:
            raise ValueError("must be positive")
    except (TypeError, ValueError) as exc:
        log.warning("backup.interval_s invalid (%r); using default %ss: %s",
                    raw_interval, DEFAULT_BACKUP_INTERVAL_S, exc)
        interval_s = float(DEFAULT_BACKUP_INTERVAL_S)
    bc = BackupConfig(key=key, drive_service=drive,
                      shared_drive_id=shared_drive_id, user_id=user_id)
    return (bc, interval_s)


def main(argv=None) -> None:
    """CLI entry point: `python -m mcpbrain.daemon [--once] [--interval N]`.

    Wires a real embedder + store + enrich client, then runs either a single
    cycle (--once) or the interval loop. Google services auto-build from the
    user's token inside the daemon (services=None); a missing token degrades to
    no sync rather than crashing — authorise via `python -m mcpbrain.auth`.
    """
    import argparse

    from mcpbrain.embed import get_embedder
    from mcpbrain.store import Store

    ap = argparse.ArgumentParser(prog="mcpbrain.daemon")
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    ap.add_argument("--interval", type=float, default=300.0, help="sync interval seconds")
    args = ap.parse_args(argv)

    emb = get_embedder(config.EMBEDDER)
    store = Store(config.store_path(), dim=emb.dim)
    store.init()
    enrich_client = _enrich_client_from_config(str(config.app_dir()))
    backup_cfg, backup_interval = _backup_from_config(str(config.app_dir()))
    daemon = Daemon(store, emb, interval_s=args.interval, enrich_client=enrich_client,
                    backup=backup_cfg, backup_interval_s=backup_interval)  # services=None -> auto-build from token

    if args.once:
        daemon.ensure_services()   # resolve services before the single cycle
        result = daemon.run_one()
        print("cycle:", result)
    else:
        # Loop mode serves the token-guarded loopback control API + browser
        # wizard alongside the sync loop. ControlServer.start() writes the
        # control_port/control_token files `mcpbrain setup` reads. A one-shot
        # --once cycle needs no control server, so it stays unwired above.
        ctrl = control_api.ControlServer(daemon, home=str(config.app_dir()))
        ctrl.start()
        log.info("control API + wizard on http://127.0.0.1:%d/", ctrl.port)
        try:
            daemon.run()           # loop until Ctrl-C / stop
        finally:
            ctrl.stop()


if __name__ == "__main__":
    main()
