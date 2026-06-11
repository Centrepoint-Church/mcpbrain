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

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mcpbrain import auth, backup, config, control_api, drain, enrich, graph_write, prepare
from mcpbrain.backup import make_encrypted_snapshot, upload_snapshot
from mcpbrain.config import app_dir
from mcpbrain.enrich import run_enrichment
from mcpbrain.sync import run_sync_cycle

# Import block modules at startup so their BLOCK_DRAINERS entries are registered
# before the first drain pass. All four imports are intentional side effects.
import mcpbrain.profile_synth   # noqa: F401 — registers BLOCK_DRAINERS["profile_synthesis"]
import mcpbrain.community_synth  # noqa: F401 — registers BLOCK_DRAINERS["community_synthesis"]
import mcpbrain.memory_distil    # noqa: F401 — registers BLOCK_DRAINERS["memory_distil"]
import mcpbrain.profile_audit    # noqa: F401 — registers BLOCK_DRAINERS["profile_audit"]

log = logging.getLogger(__name__)

EMBED_BACKEND = "fastembed:bge-small:v1"
DEFAULT_BACKUP_INTERVAL_S = 3600

# Spool prepare bounds. thread_cap is a belt-and-braces ceiling on top of the
# cap group_unenriched_threads already applies; char_budget splits an over-long
# thread before the extractor sees it. Conservative starting points; tune later.
SPOOL_THREAD_CAP = 100
SPOOL_CHAR_BUDGET = 24000


def _graph_apply():
    """Resolve Phase 1's graph_write.apply through an indirection seam.

    graph_write has landed (imported at module top), so this returns the real
    apply directly. The seam is kept as the monkeypatch surface that
    tests/test_run_cycle_modes.py patches to a stub.
    """
    return graph_write.apply


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
    retain: int = 7   # keep the newest N uploaded snapshots; older are pruned

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


def _gated_enrich_mode(mode: str, home: str) -> str:
    """Force enrichment OFF until the install is configured (identity + ≥1 org).

    Sync/index are identity-agnostic and still run every cycle; only enrichment —
    which writes owner identity and org taxonomy into the graph — is gated. "off"
    stays "off"; any other mode passes through only once config.is_configured.
    """
    if mode == "off":
        return "off"
    return mode if config.is_configured(home) else "off"


def run_cycle(store, embedder, *, gmail_service=None, calendar_service=None,
              drive_service=None, enrich_client=None,
              enrich_limit: int | None = None,
              enrich_mode: str = "off", resolution_due: bool = False,
              synthesis_requests: list | None = None,
              extra_blocks: dict | None = None) -> dict:
    """One sync -> embed -> enrich cycle.

    Sync each provided source and embed via run_sync_cycle (the tested core),
    then enrich according to enrich_mode:

      - "gemini": the existing per-chunk path. run_enrichment over the un-enriched
        chunks. Tiered on enrich_client: with a client it extracts into the graph
        and marks chunks enriched; without one (None) it defers (no graph writes,
        no marking, mode flag set).
      - "spool": prepare.prepare writes pending.json from the un-enriched threads,
        then drain.drain applies whatever the out-of-band extractor session has
        produced since last cycle. run_cycle does NOT call the extractor itself.
        resolution_due gates the merge-review block in prepare, so it is appended
        exactly when the deterministic resolve tier would also fire.
      - "off": skip enrichment entirely.

    enrich_mode defaults to "off" (matching config.enrich_mode's default), so a
    direct caller that forgets to pass a mode does NOT silently run the legacy
    gemini path. The live daemon resolves the real mode from config in run_one
    and passes it in explicitly.

    enrich_limit caps how many un-enriched chunks the gemini path processes this
    cycle so a large post-migration backlog drains progressively rather than
    enriching the entire corpus in one tight, lock-holding loop. None enriches
    every un-enriched chunk.

    Returns the sync result dict ({"gmail","calendar","drive","embedded"}) plus
    an "enrich" key holding the chosen path's summary.
    """
    result = run_sync_cycle(
        store, embedder,
        gmail_service=gmail_service,
        calendar_service=calendar_service,
        drive_service=drive_service,
    )
    try:
        drain_caps = drain.drain_captures(store)
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
        prep = prepare.prepare(store, thread_cap=SPOOL_THREAD_CAP,
                               char_budget=SPOOL_CHAR_BUDGET,
                               resolution_due=resolution_due,
                               synthesis_requests=synthesis_requests,
                               extra_blocks=extra_blocks)
        drained = drain.drain(store, apply=_graph_apply(), embedder=embedder)
        result["enrich"] = {"mode": "spool", "prepare": prep, "drain": drained}
    elif enrich_mode == "off":
        result["enrich"] = {"mode": "off"}
    else:  # "gemini": the existing path, unchanged.
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
                 resolve_interval_s: float | None = None,
                 communities_interval_s: float | None = None,
                 lint_interval_s: float | None = None,
                 synthesise_interval_s: float | None = None,
                 proactive_interval_s: float | None = None,
                 waiting_on_interval_s: float | None = None,
                 blocks_interval_s: float | None = None,
                 audit_interval_s: float | None = None,
                 clickup_interval_s: float | None = None,
                 stale_reextract_interval_s: float | None = None,
                 auto_update_interval_s: float | None = None,
                 verify_interval_s: float | None = None,
                 clock=time.monotonic,
                 enrich_mode: str = "off"):
        self._store = store
        self._embedder = embedder
        self._enrich_client = enrich_client  # None -> enrichment defers (no-op)
        # Enrichment source: spool | gemini | off. Defaults to "off" so a
        # newly-constructed daemon enriches nothing until explicitly configured.
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
        # can pass them to prepare.prepare() in the next spool cycle.
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
        # requests and stashes them so run_one() can pass them to prepare.prepare().
        self._blocks_interval_s: float | None = blocks_interval_s
        self._last_blocks = None
        self._pending_blocks: dict = {}
        # Periodic profile audit is OFF unless audit_interval_s is set.
        # Same cadence pattern: builds audit requests and stashes for run_one().
        self._audit_interval_s: float | None = audit_interval_s
        self._last_audit = None
        self._pending_audit: dict = {}
        # Periodic ClickUp two-way sync is OFF unless clickup_interval_s is set.
        # Same three-shape contract as the other passes; runs in this loop
        # thread so it shares the single-writer lock.
        self._clickup_interval_s: float | None = clickup_interval_s
        self._last_clickup = None
        # Periodic stale -> re-extraction trigger (Gap A) is OFF unless
        # stale_reextract_interval_s is set. Same three-shape cadence contract.
        self._stale_reextract_interval_s: float | None = stale_reextract_interval_s
        self._last_stale_reextract = None
        # Silent auto-update cadence: OFF unless auto_update_interval_s is set.
        self._auto_update_interval_s: float | None = auto_update_interval_s
        self._last_auto_update = None
        # Pending update version: set by maybe_auto_update (detect-only); consumed
        # by run() AFTER the write lock is released so uv install + restart never
        # happen under the held lock.
        self._pending_update: str | None = None
        # Periodic connection verification (network) is OFF unless verify_interval_s
        # is set. Defaults to hourly when configured without an explicit interval.
        # Writes connections.json which all_connections() overlays.
        self._verify_interval_s: float | None = verify_interval_s
        self._last_verify = None
        self._pause = threading.Event()   # set == paused
        self._stop = threading.Event()    # set == stop the loop
        self._wake = threading.Event()    # set == run a cycle now
        # Single-flight guard for enrich-backfill: non-blocking acquire means a
        # duplicate start_enrich_backfill call is a no-op. _backfill_active
        # signals run_one to yield its write cycle while the backfill is live.
        self._backfill_active = threading.Event()
        self._backfill_lock = threading.Lock()

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
        google_account: str = ""
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
        except Exception as exc:  # noqa: BLE001 — no/invalid token degrades, never crashes
            log.debug("status: Google credentials unavailable: %s", exc)
        # Spool depth for the cowork extractor wizard step. The on-disk layout
        # is owned by prepare.py (writes enrich_queue/pending.json) and
        # extractor_driver.py (writes enrich_inbox/<batch>.json), so we just
        # count files. Errors degrade to zero rather than failing the status
        # poll.
        pending = 0
        inbox = 0
        try:
            home = config.app_dir()
            if (home / "enrich_queue" / "pending.json").exists():
                pending = 1
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
        return {
            "paused": self.is_paused(),
            "chunk_count": self._store.chunk_count(),
            "enriched_count": self._store.enriched_count(),
            "google_connected": google_connected,
            "granted_scopes": granted,
            "google_account": google_account,
            "enrich_enabled": self._enrich_client is not None,
            "spool": {"pending": pending, "inbox": inbox},
            "open_findings": open_findings,
            "is_configured": config.is_configured(str(app_dir())),
            "connections": connections,
            "backfill": backfill,
            "version": __import__("mcpbrain", fromlist=["__version__"]).__version__,
        }

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
        }

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
        try:
            gmail = self.ensure_services().get("gmail_service")
        except Exception:  # noqa: BLE001
            gmail = None
        if gmail is None:
            return ""
        try:
            profile = gmail.users().getProfile(userId="me").execute()
            email = (profile.get("emailAddress") or "").strip()
        except Exception as exc:  # noqa: BLE001 — must not break status polls
            log.debug("status: getProfile failed: %s", exc)
            return ""
        if email:
            self._cache_google_account(sidecar, email)
        return email

    @staticmethod
    def _cache_google_account(sidecar, email: str) -> None:
        try:
            sidecar.write_text(email)
            os.chmod(sidecar, 0o600)
        except OSError as exc:
            log.debug("status: failed to write google_account sidecar: %s", exc)

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
        enrich_mode = config.enrich_mode(home)
        backup_cfg, backup_interval = _backup_from_config(home)
        cadences = _cadences_from_config(home)  # IO off-lock; assign under lock below
        with self._config_lock:
            self._enrich_client = enrich_client
            self._enrich_mode = enrich_mode
            self._backup = backup_cfg
            self._backup_interval_s = backup_interval
            # Cadence re-wire: intervals only; _last_* anchors persist across
            # re-wire so a cadence change doesn't reset the clock.
            self._communities_interval_s = cadences["communities_interval_s"]
            self._lint_interval_s = cadences["lint_interval_s"]
            self._synthesise_interval_s = cadences["synthesise_interval_s"]
            self._proactive_interval_s = cadences["proactive_interval_s"]
            self._waiting_on_interval_s = cadences["waiting_on_interval_s"]
            self._blocks_interval_s = cadences["blocks_interval_s"]
            self._audit_interval_s = cadences["audit_interval_s"]
            self._clickup_interval_s = cadences["clickup_interval_s"]
            self._stale_reextract_interval_s = cadences["stale_reextract_interval_s"]
            self._auto_update_interval_s = cadences["auto_update_interval_s"]
            self._verify_interval_s = cadences["verify_interval_s"]
        # Best-effort: keep the personal skills + records-repo scaffold current
        # whenever settings are saved. Failures never fail the POST.
        try:
            from mcpbrain import records, skills
            skills.write_personal_skills()
            records.scaffold_records(home)
        except Exception as exc:  # noqa: BLE001
            log.warning("apply_config materialise degraded: %s", exc)

    def register(self) -> str:
        """Register mcpbrain with Claude Desktop and return the config path."""
        import sys
        from pathlib import Path
        # Lazy import to avoid an import cycle (wizard imports daemon-adjacent code).
        from mcpbrain.wizard.register import register_mcpbrain
        # Resolve the mcpbrain executable WITHOUT relying on PATH. Under launchd
        # (and systemd) the daemon's PATH usually omits ~/.local/bin, so a bare
        # shutil.which("mcpbrain") returns None and registration fails. The
        # console script sits next to this interpreter in the tool venv, and
        # argv[0] is the launching binary; prefer those, fall back to which().
        names = ("mcpbrain", "mcpbrain.exe")
        candidates = [
            Path(sys.executable).with_name("mcpbrain"),
            Path(sys.executable).with_name("mcpbrain.exe"),
            Path(sys.argv[0]),
        ]
        mcpbrain_bin = next(
            (str(c) for c in candidates if c.name in names and c.exists()), None
        )
        return str(register_mcpbrain(mcpbrain_home=str(app_dir()), mcpbrain_bin=mcpbrain_bin))

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
        if self._pause.is_set() or self._backfill_active.is_set():
            return None
        services = self.ensure_services()
        # Snapshot the enrich client + mode under the config lock so apply_config
        # (HTTP handler thread) can't swap them mid-cycle; use the locals for this
        # cycle.
        with self._config_lock:
            enrich_client = self._enrich_client
            enrich_mode = self._enrich_mode
        # Gate: no enrichment until the install is configured. Sync still runs.
        enrich_mode = _gated_enrich_mode(enrich_mode, str(app_dir()))
        # The spool prepare step folds in the merge-review block on the same
        # cadence the deterministic resolve tier fires. Compute it here (without
        # consuming the cadence) and pass it through; maybe_resolve still runs
        # after this cycle and advances the clock.
        resolution_due = self._resolve_due()
        # Stashed synthesis/block requests are RE-ATTACHED every cycle, not
        # consumed by one: prepare rewrites pending.json each cycle, so a
        # one-shot attach survives only until the next rewrite (~one interval)
        # unless the out-of-band extractor happens to read the file in that
        # window (live 2026-06-05 loss). Each stash is cleared below, once the
        # drain summary shows its answers actually came back; until then every
        # rewritten pending.json carries the same requests.
        synthesis_requests = self._pending_synthesis
        merged = {**self._pending_blocks, **self._pending_audit}
        extra_blocks = {k: v for k, v in merged.items() if v} or None
        if extra_blocks:
            log.info("extra blocks attached: %s",
                     {k: len(v) for k, v in extra_blocks.items()})
        result = run_cycle(self._store, self._embedder,
                           enrich_client=enrich_client,
                           enrich_limit=self._enrich_batch,
                           enrich_mode=enrich_mode,
                           resolution_due=resolution_due,
                           synthesis_requests=synthesis_requests,
                           extra_blocks=extra_blocks,
                           **services)
        drained = ((result or {}).get("enrich") or {}).get("drain") or {}
        if drained.get("synthesis_written"):
            self._pending_synthesis = []
        for key in list(self._pending_blocks):
            if f"{key}_drained" in drained:
                log.info("block %s answers drained (%s); stash cleared",
                         key, drained[f"{key}_drained"])
                del self._pending_blocks[key]
        for key in list(self._pending_audit):
            if f"{key}_drained" in drained:
                log.info("block %s answers drained (%s); stash cleared",
                         key, drained[f"{key}_drained"])
                del self._pending_audit[key]
        return result

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
        if self._backfill_active.is_set():
            return None  # single-writer: yield to the backfill
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
            # Bound history: keep the newest `retain` snapshots, prune older.
            # Best-effort — a prune failure must not fail the (successful) backup.
            from mcpbrain.backup import prune_snapshots
            prune_snapshots(cfg.drive_service, cfg.shared_drive_id, cfg.user_id,
                            keep=cfg.retain)
        except Exception as exc:  # noqa: BLE001 — backup must never crash the loop
            log.warning("periodic backup failed: %s", exc, exc_info=True)
            return {"backed_up": False, "error": str(exc)}

        # Advance the cadence clock only after a clean backup.
        self._last_backup = self._clock()
        return {"backed_up": True, "file_id": file_id, "path": str(path)}

    # -- silent auto-update ---------------------------------------------------

    def maybe_auto_update(self) -> dict | None:
        """Detect a newer published version; signal run() to install it OUTSIDE the
        write lock. Default daily when configured; OFF when unconfigured. Never runs
        the install/restart here (that would happen under the held lock)."""
        home = str(app_dir())
        with self._config_lock:
            interval = self._auto_update_interval_s
        if interval is None:
            interval = 86400.0 if config.is_configured(home) else None
        if interval is None:
            return None
        if self._last_auto_update is not None and (self._clock() - self._last_auto_update) < interval:
            return None
        self._last_auto_update = self._clock()
        try:
            from mcpbrain import update as upd
            idx = upd._index_url()
            if "CHANGE-ME" in idx:
                log.warning("auto-update skipped: update channel not configured (index URL is the placeholder)")
                return None
            latest = upd._latest_version(idx)
            if upd._should_update(upd._installed_version(), latest):
                self._pending_update = latest
                return {"update_available": True, "version": latest}
        except Exception as exc:  # noqa: BLE001
            log.warning("auto-update check failed (loop continues): %s", exc)
        return None

    # -- verify connections cadence -------------------------------------------

    def maybe_verify_connections(self) -> dict | None:
        """Periodically verify connections (network) and cache the result.
        OFF unless configured; default hourly when configured without an explicit
        interval. Time-gated via self._clock."""
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

    # -- periodic entity resolution -----------------------------------------

    def _resolve_due(self) -> bool:
        """Whether an entity resolve is due this cycle, without consuming it.

        OFF (False) unless resolve_interval_s was supplied. Otherwise due on the
        first call (self._last_resolve is None) or once resolve_interval_s has
        elapsed since the last resolve. Read-only: it does not advance the
        cadence clock. maybe_resolve uses this as its gate, and run_one reuses it
        to set the spool merge-review cadence (resolution_due), so the LLM merge
        block is appended exactly when the deterministic resolve tier fires.
        """
        if self._resolve_interval_s is None:
            return False
        if self._last_resolve is None:
            return True
        return (self._clock() - self._last_resolve) >= self._resolve_interval_s

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
        if self._backfill_active.is_set():
            return None  # single-writer: yield to the backfill
        if not self._resolve_due():
            return None

        # Record the START time as the cadence anchor (unlike maybe_backup's
        # end-time): a slow LLM-adjudicated resolve then doesn't eat into the
        # next interval. _last_resolve is committed only on a clean run below.
        now = self._clock()

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

    # -- periodic community detection ---------------------------------------

    def maybe_communities(self) -> dict | None:
        """Run community detection, if it is due.

        OFF unless communities_interval_s was supplied: returns None when
        self._communities_interval_s is None (never runs on an unconfigured
        daemon). Otherwise gates on a time-based cadence using the injected
        clock — due on the first call (self._last_communities is None) or once
        communities_interval_s has elapsed since the last run. Not due ->
        returns None and does nothing.

        Records START time as the cadence anchor (same pattern as maybe_resolve)
        so a long detection run doesn't eat into the next interval.
        _last_communities advances only on a clean run.

        A communities failure is logged and swallowed so the daemon loop keeps
        running — it returns {"communities": False, "error": ...} rather than
        propagating.
        """
        if self._communities_interval_s is None:
            return None

        if self._last_communities is not None:
            elapsed = self._clock() - self._last_communities
            if elapsed < self._communities_interval_s:
                return None

        # Record START time as cadence anchor before the (potentially slow) pass.
        now = self._clock()

        try:
            from mcpbrain.communities import run
            summary = run(self._store)
        except Exception as exc:  # noqa: BLE001 — communities must never crash the loop
            log.warning(
                "community detection failed (will retry next due): %s", exc,
                exc_info=True,
            )
            return {"communities": False, "error": str(exc)}

        # Advance the cadence clock only after a clean run.
        self._last_communities = now
        return summary

    # -- periodic ClickUp two-way sync --------------------------------------

    def maybe_clickup_sync(self) -> dict | None:
        """Run the ClickUp ⇄ actions sync, if due.

        OFF unless clickup_interval_s is set (returns None). Otherwise gates on a
        time-based cadence like maybe_communities. Runs in the loop thread so it
        shares the single-writer lock. A failure is logged and swallowed so the
        daemon loop keeps running.
        """
        if self._clickup_interval_s is None:
            return None
        if self._last_clickup is not None:
            if (self._clock() - self._last_clickup) < self._clickup_interval_s:
                return None
        now = self._clock()
        try:
            from mcpbrain import clickup_sync
            from mcpbrain import config as _config
            summary = clickup_sync.sync(self._store, str(_config.app_dir()))
        except Exception as exc:  # noqa: BLE001 — sync must never crash the loop
            log.warning("clickup sync failed (will retry next due): %s", exc,
                        exc_info=True)
            return {"clickup": False, "error": str(exc)}
        self._last_clickup = now
        return summary

    # -- periodic stale -> re-extraction trigger (Gap A) --------------------

    def maybe_stale_reextract(self) -> dict | None:
        """Reset stale, idle threads to enriched=0 so the normal cycle gives the
        LLM closer another at-bat, if due.

        OFF unless stale_reextract_interval_s is set (returns None). Does no LLM
        work itself; the re-extraction happens in the normal enrichment cycle. A
        failure is logged and swallowed so the loop keeps running.
        """
        if self._stale_reextract_interval_s is None:
            return None
        if self._last_stale_reextract is not None:
            if (self._clock() - self._last_stale_reextract) < self._stale_reextract_interval_s:
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

    # -- periodic graph lint ------------------------------------------------

    def maybe_lint(self) -> dict | None:
        """Run the graph lint pass, if it is due.

        OFF unless lint_interval_s was supplied: returns None when
        self._lint_interval_s is None (never runs on an unconfigured daemon).
        Otherwise gates on a time-based cadence using the injected clock —
        due on the first call (self._last_lint is None) or once
        lint_interval_s has elapsed since the last run. Not due -> returns
        None and does nothing.

        Records START time as the cadence anchor (same pattern as
        maybe_communities) so a long lint run doesn't eat into the next
        interval. _last_lint advances only on a clean run.

        A lint failure is logged and swallowed so the daemon loop keeps
        running — it returns {"lint": False, "error": ...} rather than
        propagating.
        """
        if self._lint_interval_s is None:
            return None

        if self._last_lint is not None:
            elapsed = self._clock() - self._last_lint
            if elapsed < self._lint_interval_s:
                return None

        # Record START time as cadence anchor before the (potentially slow) pass.
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

        # Advance the cadence clock only after a clean run.
        self._last_lint = now
        return summary

    # -- periodic thread synthesis ------------------------------------------

    def maybe_synthesise(self) -> dict | None:
        """Build synthesis requests, if synthesis is due.

        OFF unless synthesise_interval_s was supplied: returns None when
        self._synthesise_interval_s is None (never synthesises an unconfigured
        daemon). Otherwise gates on a time-based cadence using the injected
        clock — due on the first call (self._last_synthesise is None) or once
        synthesise_interval_s has elapsed since the last run. Not due ->
        returns None and does nothing.

        When due: calls build_synthesis_requests(store) and stashes the result
        on self._pending_synthesis so run_one() can forward it to
        prepare.prepare() in the next spool cycle. Returns a summary dict with
        synthesis_requested=N.

        Records START time as the cadence anchor (same pattern as maybe_resolve)
        so a slow build doesn't eat into the next interval. _last_synthesise
        advances only on a clean run.

        A synthesis failure is logged and swallowed so the daemon loop keeps
        running — it returns {"synthesis_requested": 0, "error": ...} rather
        than propagating. _last_synthesise is NOT advanced on failure, so the
        next call retries.
        """
        if self._synthesise_interval_s is None:
            return None

        if self._last_synthesise is not None:
            elapsed = self._clock() - self._last_synthesise
            if elapsed < self._synthesise_interval_s:
                return None

        # Record START time as cadence anchor before the build pass.
        now = self._clock()

        try:
            # Lazy import: keeps the daemon import light and synthesis an
            # optional path; also lets tests patch build_synthesis_requests.
            from mcpbrain.synthesise_threads import build_synthesis_requests
            requests = build_synthesis_requests(self._store)
            self._pending_synthesis = requests
        except Exception as exc:  # noqa: BLE001 — synthesis must never crash the loop
            log.warning(
                "synthesis build failed (will retry next due): %s", exc,
                exc_info=True,
            )
            return {"synthesis_requested": 0, "error": str(exc)}

        # Advance the cadence clock only after a clean build.
        self._last_synthesise = now
        return {"synthesis_requested": len(requests)}

    # -- periodic proactive detection pass ---------------------------------

    def maybe_proactive(self) -> dict | None:
        """Run the proactive detection pass, if it is due.

        OFF unless proactive_interval_s was supplied: returns None when
        self._proactive_interval_s is None (never runs on an unconfigured
        daemon). Otherwise gates on a time-based cadence using the injected
        clock — due on the first call (self._last_proactive is None) or once
        proactive_interval_s has elapsed since the last run. Not due ->
        returns None and does nothing.

        Records START time as the cadence anchor (same pattern as
        maybe_resolve) so a long detection run doesn't eat into the next
        interval. _last_proactive advances only on a clean run.

        A proactive failure is logged and swallowed so the daemon loop keeps
        running — it returns {"proactive": False, "error": ...} rather than
        propagating.
        """
        if self._proactive_interval_s is None:
            return None

        if self._last_proactive is not None:
            elapsed = self._clock() - self._last_proactive
            if elapsed < self._proactive_interval_s:
                return None

        # Record START time as cadence anchor before the (potentially slow) pass.
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

        # Advance the cadence clock only after a clean run.
        self._last_proactive = now
        return summary

    # -- periodic waiting-on reconciliation ---------------------------------

    def maybe_waiting_on(self) -> dict | None:
        """Run the waiting-on reconciliation pass, if it is due.

        OFF unless waiting_on_interval_s was supplied: returns None when
        self._waiting_on_interval_s is None (never runs on an unconfigured
        daemon). Otherwise gates on a time-based cadence using the injected
        clock — due on the first call (self._last_waiting_on is None) or once
        waiting_on_interval_s has elapsed since the last run. Not due ->
        returns None and does nothing.

        Records START time as the cadence anchor (same pattern as maybe_proactive)
        so a long pass doesn't eat into the next interval. _last_waiting_on
        advances only on a clean run.

        A waiting_on failure is logged and swallowed so the daemon loop keeps
        running — it returns {"waiting_on": False, "error": ...} rather than
        propagating.
        """
        if self._waiting_on_interval_s is None:
            return None

        if self._last_waiting_on is not None:
            elapsed = self._clock() - self._last_waiting_on
            if elapsed < self._waiting_on_interval_s:
                return None

        # Record START time as cadence anchor before the pass.
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

        # Advance the cadence clock only after a clean run.
        self._last_waiting_on = now
        return summary

    # -- periodic block requests (profile_synthesis + community_synthesis + memory_distil) ---

    def maybe_blocks(self) -> dict | None:
        """Build block requests for profile/community/memory, if due.

        OFF unless blocks_interval_s was supplied: returns None when
        self._blocks_interval_s is None (never builds on an unconfigured daemon).
        Otherwise gates on a time-based cadence using the injected clock —
        due on the first call (self._last_blocks is None) or once
        blocks_interval_s has elapsed since the last run.

        When due: calls build_profile_requests, build_community_requests, and
        build_distil_requests; stashes the results in self._pending_blocks so
        run_one() can forward them to prepare.prepare() as extra_blocks in the
        next spool cycle. Returns a summary dict.

        Records START time as cadence anchor. _last_blocks advances only on a
        clean run. Failures are logged and swallowed.
        """
        if self._blocks_interval_s is None:
            return None

        if self._last_blocks is not None:
            elapsed = self._clock() - self._last_blocks
            if elapsed < self._blocks_interval_s:
                return None

        now = self._clock()

        try:
            from mcpbrain.profile_synth import build_profile_requests
            from mcpbrain.community_synth import build_community_requests
            from mcpbrain.memory_distil import build_distil_requests

            profile_reqs = build_profile_requests(self._store)
            community_reqs = build_community_requests(self._store)
            distil_reqs = build_distil_requests(self._store)
            self._pending_blocks = {
                "profile_synthesis": profile_reqs,
                "community_synthesis": community_reqs,
                "memory_distil": distil_reqs,
            }
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

    # -- periodic profile audit ---------------------------------------------

    def maybe_audit(self) -> dict | None:
        """Build profile audit requests, if due.

        OFF unless audit_interval_s was supplied: returns None when
        self._audit_interval_s is None (never builds on an unconfigured daemon).
        Otherwise gates on a time-based cadence using the injected clock —
        due on the first call (self._last_audit is None) or once
        audit_interval_s has elapsed since the last run.

        When due: calls build_audit_requests; stashes the results in
        self._pending_audit so run_one() can forward them to prepare.prepare()
        as extra_blocks in the next spool cycle. Returns a summary dict.

        Records START time as cadence anchor. _last_audit advances only on a
        clean run. Failures are logged and swallowed.
        """
        if self._audit_interval_s is None:
            return None

        if self._last_audit is not None:
            elapsed = self._clock() - self._last_audit
            if elapsed < self._audit_interval_s:
                return None

        now = self._clock()

        try:
            from mcpbrain.profile_audit import build_audit_requests
            audit_reqs = build_audit_requests(self._store)
            self._pending_audit = {"profile_audit": audit_reqs}
        except Exception as exc:  # noqa: BLE001 — must never crash the loop
            log.warning(
                "audit build failed (will retry next due): %s", exc, exc_info=True
            )
            return {"audit_requested": 0, "error": str(exc)}

        self._last_audit = now
        return {"audit_requested": len(audit_reqs)}

    # -- periodic pass orchestration ----------------------------------------

    def _run_periodic_passes(self) -> None:
        """Call the periodic maintenance passes in spec order (§54, §165).

        communities first so lint's duplicate-detection reads fresh
        entity_communities. Each pass self-gates on its cadence and swallows
        its own errors, but this method also wraps each call individually so
        an unexpected raise (e.g. a mocked exception in tests) from one pass
        never blocks the remaining passes.

        auto_update and verify_connections are identity-independent and always
        run.  All graph-writers are gated on is_configured so they never write
        blank/empty attribution into the graph on an unconfigured install.
        """
        if self._backfill_active.is_set():
            return  # single-writer: yield the whole cycle to the backfill
        configured = config.is_configured(str(app_dir()))

        # Always run (independent of identity): updates + connection verification.
        for pass_fn in (
            self.maybe_auto_update,
            self.maybe_verify_connections,
        ):
            try:
                pass_fn()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "periodic pass %s failed unexpectedly: %s",
                    getattr(pass_fn, "__name__", repr(pass_fn)), exc, exc_info=True,
                )

        if not configured:
            return  # graph-writers need identity + orgs; skip until configured

        # Graph-writing passes: communities first (lint reads fresh entity_communities).
        for pass_fn in (
            self.maybe_communities,
            self.maybe_lint,
            self.maybe_synthesise,
            self.maybe_proactive,
            self.maybe_waiting_on,
            self.maybe_blocks,
            self.maybe_audit,
            self.maybe_clickup_sync,
            self.maybe_stale_reextract,
        ):
            try:
                pass_fn()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "periodic pass %s failed unexpectedly: %s",
                    getattr(pass_fn, "__name__", repr(pass_fn)), exc, exc_info=True,
                )

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
                try:
                    self.run_one()
                except Exception as exc:  # noqa: BLE001 — a transient cycle error must not kill the daemon
                    # Crashing here would hand the failure to launchd, whose
                    # restart resets every cadence anchor and drops stashed
                    # block/synthesis requests (live 2026-06-05 Gmail-timeout
                    # crash loop). Log and retry on the next interval; the
                    # skipped _pending_* resets in run_one preserve the stash.
                    log.error("cycle failed; retrying next interval: %s",
                              exc, exc_info=True)
                # Backup self-gates on configured + due; harmless when paused
                # (a snapshot of current state). Runs in this loop thread, so it
                # shares the single-writer lock the daemon already holds.
                self.maybe_backup()
                # Resolution self-gates on configured + due; reuses the
                # single-writer lock this loop thread already holds.
                if config.is_configured(str(app_dir())):
                    self.maybe_resolve()
                # Five periodic maintenance passes in spec order (§54, §165).
                # communities first (lint reads fresh entity_communities).
                self._run_periodic_passes()
                if self._pending_update or self._stop.is_set():
                    break
                # Block until woken (sync_now/stop) or the interval elapses.
                self._wake.wait(timeout=self._interval_s)

        if self._pending_update:
            try:
                from mcpbrain import update as upd
                upd.update_from_index(upd._index_url())  # uv install + restart, lock released
            except Exception as exc:  # noqa: BLE001
                log.error("auto-update install failed: %s", exc)


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
    try:
        retain = int(cfg.get("retain", 7))
        if retain <= 0:
            raise ValueError("must be positive")
    except (TypeError, ValueError) as exc:
        log.warning("backup.retain invalid (%r); using default 7: %s",
                    cfg.get("retain"), exc)
        retain = 7
    bc = BackupConfig(key=key, drive_service=drive,
                      shared_drive_id=shared_drive_id, user_id=user_id,
                      retain=retain)
    return (bc, interval_s)


_CADENCE_KEYS = (
    "communities_interval_s",
    "lint_interval_s",
    "synthesise_interval_s",
    "proactive_interval_s",
    "waiting_on_interval_s",
    "blocks_interval_s",
    "audit_interval_s",
    "clickup_interval_s",
    "stale_reextract_interval_s",
    "auto_update_interval_s",
    "verify_interval_s",
)


def _cadences_from_config(home) -> dict:
    """Read the cadences block from config.json. Returns a dict of the
    interval keys in _CADENCE_KEYS; absent keys map to None (OFF). Invalid
    values log a warning and map to None, mirroring the backup interval
    validation in daemon.py.
    """
    cfg = config.read_config(home)
    cadences_block = cfg.get("cadences") or {}
    result = {}
    for key in _CADENCE_KEYS:
        raw = cadences_block.get(key)
        if raw is None:
            result[key] = None
            continue
        try:
            val = float(raw)
            if val <= 0:
                raise ValueError("must be positive")
            result[key] = val
        except (TypeError, ValueError) as exc:
            log.warning("cadences.%s invalid (%r); disabling: %s", key, raw, exc)
            result[key] = None
    return result


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

    # Configure root logging so warnings/errors reach stdout/stderr. Under
    # launchd these are routed to the plist's StandardOutPath/StandardErrorPath;
    # in a terminal they appear inline. Without this, every `log.info/warning`
    # in the daemon is silently dropped, which is why a healthy daemon looks
    # "hung" in the foreground and a crashing launchd job leaves no trace.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    emb = get_embedder(config.EMBEDDER)
    store = Store(config.store_path(), dim=emb.dim)
    store.init()
    enrich_client = _enrich_client_from_config(str(config.app_dir()))
    enrich_mode = config.enrich_mode(str(config.app_dir()))
    backup_cfg, backup_interval = _backup_from_config(str(config.app_dir()))
    cadences = _cadences_from_config(str(config.app_dir()))
    daemon = Daemon(store, emb, interval_s=args.interval, enrich_client=enrich_client,
                    enrich_mode=enrich_mode,
                    backup=backup_cfg, backup_interval_s=backup_interval,
                    communities_interval_s=cadences["communities_interval_s"],
                    lint_interval_s=cadences["lint_interval_s"],
                    synthesise_interval_s=cadences["synthesise_interval_s"],
                    proactive_interval_s=cadences["proactive_interval_s"],
                    waiting_on_interval_s=cadences["waiting_on_interval_s"],
                    blocks_interval_s=cadences["blocks_interval_s"],
                    audit_interval_s=cadences["audit_interval_s"],
                    clickup_interval_s=cadences["clickup_interval_s"],
                    stale_reextract_interval_s=cadences["stale_reextract_interval_s"],
                    auto_update_interval_s=cadences["auto_update_interval_s"],
                    verify_interval_s=cadences["verify_interval_s"])  # services=None -> auto-build from token

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
        try:
            probe = SingleWriterLock()
            probe.acquire()
            probe.release()
        except AlreadyRunningError:
            log.error("another mcpbrain daemon is already running; exiting")
            raise SystemExit(1)
        ctrl = control_api.ControlServer(daemon, home=str(config.app_dir()), store=store)
        ctrl.start()
        log.info("control API + wizard on http://127.0.0.1:%d/", ctrl.port)
        # Materialise the personal skills at startup so a fresh install has the
        # mcpbrain-enrichment + mcpbrain-setup skills before the user opens Cowork.
        try:
            from mcpbrain import skills
            skills.write_personal_skills()
        except Exception as exc:  # noqa: BLE001 — best-effort, never block startup
            log.warning("skill materialise at startup degraded: %s", exc)
        try:
            daemon.run()           # loop until Ctrl-C / stop
        finally:
            ctrl.stop()


if __name__ == "__main__":
    main()
