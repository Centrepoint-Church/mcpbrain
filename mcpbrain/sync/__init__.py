import logging
from datetime import datetime, timedelta, timezone

# Module-level (not lazy-inside-the-function) so tests can monkeypatch these
# as attributes of THIS module (mcpbrain.sync) -- e.g.
# monkeypatch.setattr(sync_mod, "discover_drive", fake) -- and have
# run_sync_cycle's bare-name calls resolve the patched version through this
# module's own __dict__ at call time. A lazy `from mcpbrain.sync.drive import
# discover_drive` inside the function body would instead re-fetch the
# unpatched original from mcpbrain.sync.drive every call.
from mcpbrain.sync.calendar import discover_calendar, handle_calendar_item
from mcpbrain.sync.drive import (
    discover_drive, discover_shared_drives, handle_drive_item, handle_shared_drive_item,
)
from mcpbrain.sync.gmail import discover_gmail, handle_gmail_item

log = logging.getLogger(__name__)


# Progressive-backfill defaults. Tuned so backfill drains without blocking the
# live delta-sync that shares the same loop iteration:
#   - 90-day windows: small enough that one window fits in a sync cycle even
#     for high-volume mailboxes; large enough that a decade finishes in ~40 cycles
#   - 200 items per source per step: bounds Google API spend per cycle
# Stops only when a source has produced ZERO items for STOP_AFTER_EMPTY_WINDOWS
# consecutive windows — i.e. the daemon has walked past the start of that
# account's history. No fixed horizon: a 20-year-old Gmail account backfills
# in full, and once the floor passes the user's earliest message the empty-
# window counter trips and that source goes idle.
_BACKFILL_WINDOW_DAYS = 90
_BACKFILL_MAX_PER_SOURCE = 200
_STOP_AFTER_EMPTY_WINDOWS = 4   # 4 × 90d = ~1 year of empty before declaring done


def run_sync_cycle(store, embedder, *, gmail_service=None,
                   calendar_service=None, drive_service=None, home=None,
                   budget=None, embed_max_items: int = 2000,
                   bulk_section=None) -> dict:
    """Run a sync+embed cycle over whichever services are provided.

    Discovery/work split (Task 8): for each provided service, `discover_*`
    lists that source's changes and enqueues them (cheap, list-only, bounded
    by its own small `DISCOVERY_BUDGET_S` slice); ONE shared `work_queue` call
    then drains up to `config.sync_work_limit(home)` due items across every
    source via `handle_*_item`, and index_pending embeds whatever it wrote.
    Splitting listing from fetching is what makes a budget cutoff free: a
    round that could not finish still leaves durable, resumable rows in
    `sync_queue` rather than losing or repeating work. After that, run one
    progressive-backfill step that walks one historical window per source
    (newest-to-oldest) so the corpus eventually contains everything. Live
    discovery+work runs FIRST every cycle so anything new always reaches the
    store before older history is processed. Returns `"discovered"`
    (per-source enqueued counts), `"worked"` (`{"processed", "failed"}` from
    `work_queue`), backfill counts, and total chunks embedded this cycle.

    When `drive_service` and `home` are both given AND `config.ingest_cache_enabled(home)`
    AND `config.fleet_pin(home).is_pinned`, also runs the Shared Drive ingest-cache
    path (spec §A) through the SAME discover/work/embed split every other
    source uses, rather than the old monolithic `sync_shared_drives`:
    `discover_shared_drives` lists each pinned drive's changes into the SAME
    `sync_queue` `discover_*`/`handle_*` sources use (as `"drive:<id>"`), a
    per-drive `FleetStorage` is built once via `cache_storage_factory`, and a
    `"drive"`-keyed handler dispatches each queued item to `handle_drive_item`
    (My Drive, `source == "drive"`) or `handle_shared_drive_item` (a pinned
    Shared Drive, `source == "drive:<id>"`) — added to the SAME `handlers` dict
    and drained by the SAME single `work_queue` call as gmail/calendar/My-Drive.
    A cache MISS records a durable `shared_drive_pending_publish` row (rather
    than publishing inline, since publishing needs the chunk EMBEDDED first);
    after `work_queue`+`_embed()` run, `publish_pending_shared_drive_artifacts`
    publishes each drive's pending backlog (batch-GC'd per drive). One
    progressive-backfill window per pinned drive then runs via
    `_shared_drive_backfill_step` (so a newly-pinned drive's PRE-EXISTING
    documents get ingested too, not just files touched after the pin) — its
    misses are recorded into the SAME pending-publish table (not the old
    inline `_publish_drive_misses` path), embedded, and published with a
    SECOND `publish_pending_shared_drive_artifacts` call so they don't wait a
    full extra cycle. Adds shared-drive entries to `"discovered"` (keyed
    `"drive:<id>"`), `"shared_drives_published"` (`{drive_id: count}`,
    accumulated across both publish calls), and `"shared_drives_backfill"`
    (per-drive backfill-processed counts) to the result. Drive-presence
    revocation (`"revoked_drives"`) and the `"shared_drive_cache"` hit/miss
    summary are NOT reproduced by this discover/work split — see this
    function's implementation comments for why.

    Strictly additive AND non-fatal: with `home=None` (every caller before this
    feature) shared-drive discovery/publish never run and existing behaviour
    for gmail/calendar/My-Drive sync is unchanged. Because shared-drive
    discovery must run BEFORE the single `work_queue` call (to register its
    handler) while publish/backfill must run AFTER it (publish reads back
    vectors that only exist once `_embed()` has run), the old single
    try/except around one contiguous block is now TWO try/excepts — one
    around discovery, one around publish+backfill — each independently
    guaranteeing the same invariant the one block used to: a Drive-API outage
    anywhere in the shared-drive path (including `list_shared_drives`) is
    caught, logged, and skipped for this cycle, and can never abort the
    gmail/calendar/My-Drive discovery/work/embed that already ran, nor the
    steps that follow.

    Bounded: `budget` (a `Budget`, or None for unbounded) and `embed_max_items`
    are threaded into every `index_pending` call so embedding one cycle's slice
    never holds the loop for hours on a large backlog. Once `budget` expires,
    the function returns early with `result["budget_spent"] = True` after
    finishing the source block it was in — subsequent sources/backfill are
    skipped this cycle and picked up on the next tick (the underlying work is
    predicate-driven, so nothing is lost).

    `bulk_section` (Task 2 duty-cycle fix: a zero-arg context-manager factory,
    default `contextlib.nullcontext`) is threaded into `discover_calendar`
    (its window-hygiene eviction), each `handle_*_item` call (via the
    `handlers` closures below, including `handle_shared_drive_item`),
    `_shared_drive_backfill_step`, and `index_pending`,
    each of which brackets its OWN small units of work (one message/event/
    file/embed-batch) with it, rather than this function holding one
    `_bulk_lock` for its entire body. A soak test showed the latter shape (one
    lock hold per whole call, even budget-bounded) still starves the
    maintenance thread's 5s acquire almost every time on a sustained backlog;
    per-item sections give it a real chance throughout the cycle instead of
    once.
    """
    from mcpbrain import config
    from mcpbrain.budget import Budget
    from mcpbrain.daemon import DISCOVERY_BUDGET_S
    from mcpbrain.index import index_pending
    from mcpbrain.sync.queue import work_queue

    result = {"embedded": 0}

    def _embed() -> None:
        """One bounded embed pass, accumulating count AND the cap signal.

        `embed_capped` means index_pending stopped on embed_max_items with more
        chunks still pending — the cycle has more work RIGHT NOW even though the
        budget never expired. Without it the daemon loop slept a full interval
        (300 s) between 2000-chunk slices of a live backlog.
        """
        st: dict = {}
        result["embedded"] += index_pending(store, embedder, home=home, budget=budget,
                                            max_items=embed_max_items, stats=st,
                                            bulk_section=bulk_section)
        if st.get("capped"):
            result["embed_capped"] = True

    # Discovery only LISTS (no fetch/extract/upsert), so it is cheap and
    # bounded by its own small DISCOVERY_BUDGET_S slice (a fairness knob, not
    # a correctness mechanism -- see that constant's docstring) rather than
    # the cycle's own `budget`, so a large work queue can never starve
    # discovery of new changes. Drive discovery keeps the SAME try/except
    # protection sync_drive used to have here — a live Drive/TLS SSL failure
    # observed in production must not abort the work loop below or the
    # shared-drive/backfill steps that follow; gmail/calendar discovery are
    # deliberately NOT wrapped, matching that same pre-existing precedent
    # (only Drive ever needed it).
    discovered = {}
    disc_budget = Budget(DISCOVERY_BUDGET_S)
    if gmail_service is not None:
        discovered["gmail"] = discover_gmail(gmail_service, store, budget=disc_budget)
    if calendar_service is not None:
        discovered["calendar"] = discover_calendar(calendar_service, store,
                                                   budget=disc_budget,
                                                   bulk_section=bulk_section)
    if drive_service is not None:
        try:
            discovered["drive"] = discover_drive(drive_service, store, budget=disc_budget)
        except Exception as exc:  # noqa: BLE001 — a Drive/TLS blip must not abort the cycle
            log.warning("sync: Drive discovery failed (cycle continues, retries next cycle): %s", exc)
    # Shared Drive discovery (spec §A) is folded into this SAME discovery
    # phase, not run as its own later block, so its handler can be registered
    # into the SAME `handlers` dict below and drained by the ONE `work_queue`
    # call that follows -- there is exactly one work_queue invocation per
    # cycle. Gated: needs a drive service, a home to read config from, the
    # cache enabled, and a fleet pin present; without a pin this is a no-op
    # and drive sync behaves exactly as before. Wrapped in its own try/except
    # (see the module docstring for why this is now split from the
    # publish/backfill try/except further below) so a Drive-API outage here
    # (e.g. `list_shared_drives`) can never abort the gmail/calendar/My-Drive
    # discovery above, or the work/embed/publish/backfill steps that follow.
    pin = None
    cr = False
    drives_fs: dict[str, object] = {}
    if drive_service is not None and home is not None:
        try:
            # Cheapest check first: ingest_cache_enabled is a single config-dict
            # read; fleet_pin additionally constructs a FleetPin object, so it's
            # only built once the cheaper check passes. is_pinned is checked last.
            ingest_cache_on = config.ingest_cache_enabled(home)
            pin = config.fleet_pin(home) if ingest_cache_on else None
            if ingest_cache_on and pin.is_pinned:
                # CR (Q6 contextual-retrieval prefix) materially changes the
                # embedding vector and is a LOCAL flag not in pipeline_fingerprint,
                # so it must be threaded to both the import guard and the publish
                # stamp — otherwise a CR-on install could import a CR-off install's
                # vectors (or vice-versa) as if interchangeable.
                cr = config.contextual_retrieval_enabled(home)
                discovered_sd = discover_shared_drives(
                    drive_service, store, pin=pin, budget=disc_budget)
                discovered.update(
                    {f"drive:{did}": n for did, n in discovered_sd.items()})
                from mcpbrain.fleet_storage import cache_storage_factory
                storage_factory = cache_storage_factory(home, drive_service)
                drives_fs = {did: storage_factory(did) for did in discovered_sd}
        except Exception as exc:  # noqa: BLE001 — optional feature; must never
            # abort gmail/calendar/My-Drive discovery above, or anything below.
            log.warning("sync: shared-drive discovery failed (skipped this cycle): %s", exc)
    result["discovered"] = discovered

    # folder_cache and fetch_attachments are hoisted ONCE per cycle and closed
    # over below -- NOT rebuilt per item. folder_path's own docstring says its
    # cache is "owned by the CALLER for a whole sync round" (5,000 files in 40
    # folders costs 40 lookups, not 5,000); gmail's fetch_attachments flag has
    # the identical per-call-config-read cost the 0.7.105 fix removed
    # elsewhere. Building either fresh inside the lambda would silently defeat
    # them -- a lambda called once per item would rebuild the "cache" on every
    # call.
    folder_cache: dict = {}
    fetch_attachments = config.gmail_attachments(home) if home else False

    handlers = {}
    if drive_service is not None:
        def _drive_handler(it):
            """One handler slot serves BOTH My Drive and every pinned Shared
            Drive: work_queue resolves handlers by
            `source.split(":", 1)[0]`, so a My-Drive item (source=="drive")
            and a Shared-Drive item (source=="drive:<id>") both resolve to
            this SAME "drive" key -- registering a distinct
            handlers["drive:<id>"] entry per drive (as a first draft of this
            wiring did) would silently never be looked up. The item's OWN
            source string is what actually carries which drive it belongs
            to, so this dispatches on that instead of on the dict key.
            """
            src = it["source"]
            if ":" in src:
                drive_id = src.split(":", 1)[1]
                return handle_shared_drive_item(
                    drive_service, store, it, fleet_storage=drives_fs[drive_id],
                    pin=pin, drive_id=drive_id, contextual_retrieval=cr,
                    folder_cache=folder_cache, bulk_section=bulk_section)
            return handle_drive_item(
                drive_service, store, it, folder_cache=folder_cache,
                bulk_section=bulk_section)
        handlers["drive"] = _drive_handler
    if gmail_service is not None:
        handlers["gmail"] = lambda it: handle_gmail_item(
            gmail_service, store, it, fetch_attachments=fetch_attachments,
            bulk_section=bulk_section)
    if calendar_service is not None:
        handlers["calendar"] = lambda it: handle_calendar_item(
            calendar_service, store, it, bulk_section=bulk_section)
    result["worked"] = work_queue(
        store, handlers=handlers,
        limit=config.sync_work_limit(home) if home else 50,
        budget=budget)
    _embed()
    if budget is not None and budget.expired():
        result["budget_spent"] = True
        return result

    # Shared Drive publish + backfill (spec §A continued) -- runs only when
    # discovery above actually built at least one drive's storage (cache
    # enabled + pinned). Split from the discovery try/except above (see the
    # module docstring) because publish reads back vectors that only exist
    # once `_embed()` has run. `budget`, not `disc_budget`, is threaded
    # through both `publish_pending_shared_drive_artifacts` calls and
    # `_shared_drive_backfill_step` below -- publish is a network write and
    # backfill's own extraction is real work, the same expensive class of
    # work `disc_budget` deliberately does NOT bound (see its own docstring).
    # Wrapped in its own try/except so a failure here can never abort the
    # progressive-backfill step/return that follows.
    #
    # Per-drive budget-check placement (deliberately NOT reproducing the old
    # per-drive check that used to sit inside this loop): that check existed
    # because the old `sync_shared_drives` did a whole drive's extraction AND
    # publish inline, in one iteration, with no finer-grained budget check of
    # its own -- a many-shared-drives fleet needed a check between drives or
    # one slow drive could exhaust the whole cycle. That work is now split
    # across three places that each already check `budget` at a FINER grain
    # than per-drive: `work_queue` checks before every queued item (covers
    # the extraction `handle_shared_drive_item` used to do inline),
    # `publish_pending_shared_drive_artifacts` checks before every file
    # across every drive (Task 4, unmodified here), and `discover_shared_drives`
    # checks its own `disc_budget` per drive during listing (Task 2,
    # unmodified here). Adding a fourth, coarser per-drive check here would be
    # redundant with all three -- the phase-boundary checks below (matching
    # the same pattern already used after every other phase in this function)
    # are the only ones this step needs.
    if drive_service is not None and home is not None and drives_fs:
        try:
            from mcpbrain import ingest_cache
            # config.owner_email can return "" when unconfigured. Rather than
            # stamp published artifacts with an empty published_by, skip
            # publishing this cycle — files are still synced/embedded locally
            # either way, so nothing is lost, just not shared to the fleet yet.
            # Logged once per cycle here, not once per file/drive.
            published_by = config.owner_email(home)
            if published_by:
                result["shared_drives_published"] = publish_pending_shared_drive_artifacts(
                    store, ingest_cache, drives_fs=drives_fs, pin=pin,
                    published_by=published_by, contextual_retrieval=cr, budget=budget)
            else:
                log.warning(
                    "sync: owner_email unconfigured; shared-drive artifacts "
                    "will not be published to the fleet cache this cycle "
                    "(files still synced and embedded locally)")
            if budget is not None and budget.expired():
                result["budget_spent"] = True
                return result

            # One progressive-backfill window per pinned drive so a newly-
            # pinned drive's PRE-EXISTING documents (everything before the
            # pin) eventually get ingested too, not just files touched after
            # the pin (which the live delta discovery+work above already
            # covers). Reuses this cycle's storage instances — no second
            # storage_factory call.
            bf_sd = _shared_drive_backfill_step(store, drive_service, pin, drives_fs,
                                                contextual_retrieval=cr, budget=budget,
                                                bulk_section=bulk_section)
            backfill_counts: dict[str, int] = {}
            any_backfill_processed = False
            any_backfill_miss = False
            for drive_id, res in bf_sd.items():
                backfill_counts[drive_id] = res["processed"]
                if res["processed"]:
                    any_backfill_processed = True
                # Route backfill's misses through the SAME durable
                # shared_drive_pending_publish table the live-delta path
                # (handle_shared_drive_item, above) already records into,
                # instead of the old inline `_publish_drive_misses` call --
                # one mechanism, published by the one call site below, rather
                # than two living side by side in the same function.
                for file_id, content_hash in res["miss"]:
                    store.record_pending_publish(drive_id, file_id, content_hash)
                    any_backfill_miss = True
            result["shared_drives_backfill"] = backfill_counts
            if any_backfill_processed:
                _embed()
            # A second publish pass, AFTER backfill's own embed, covers the
            # pending rows backfill just recorded (they didn't exist yet
            # during the first publish call above) so they go out THIS cycle
            # instead of waiting a full extra one. Only run when backfill
            # actually recorded something to publish -- an unconditional
            # second call would otherwise hit every drive's (usually empty)
            # pending table on every cycle, network round-trips included via
            # `ingest_cache`, for no work. Folds into the same drives_fs/pin
            # -- no second storage_factory call -- and merges into the counts
            # the first call already produced.
            if published_by and any_backfill_miss:
                pub2 = publish_pending_shared_drive_artifacts(
                    store, ingest_cache, drives_fs=drives_fs, pin=pin,
                    published_by=published_by, contextual_retrieval=cr, budget=budget)
                merged = dict(result.get("shared_drives_published", {}))
                for did, n in pub2.items():
                    merged[did] = merged.get(did, 0) + n
                result["shared_drives_published"] = merged

            # One line per pass, matching daemon.py's cadence-log convention
            # (e.g. "feedback_aggregate: updated=%d skipped=%d"). Drive-
            # presence revocation is no longer tracked by this discover/work
            # split (see the module docstring), so unlike the old
            # sync_shared_drives-based summary this carries no "revoked="
            # field -- only what this step can actually observe: how many
            # pinned drives it touched and how many artifacts it published
            # this cycle (across both publish calls).
            total_published = sum(result.get("shared_drives_published", {}).values())
            log.info("shared_drives: drives=%d published=%d",
                     len(drives_fs), total_published)
        except Exception as exc:  # noqa: BLE001 — optional feature; must never
            # abort the progressive-backfill step/return that follows.
            log.warning("sync: shared-drive publish/backfill step failed (skipped): %s", exc)
        if budget is not None and budget.expired():
            result["budget_spent"] = True
            return result

    # Gate entry on the budget UNCONDITIONALLY (not nested inside the
    # shared-drive `if` above, which only ran when drive_service+home were
    # both set) — previously a gmail-only or calendar-only cycle (or one where
    # `home` wasn't passed) fell through to progressive_backfill_step with NO
    # budget check at all. progressive_backfill_step's own per-source calls
    # are already item-bounded (max_per_source, default 200/source), so it
    # doesn't need internal per-item budget checks the way the delta-sync
    # sources do — but a cycle that already spent its whole budget on the
    # deltas above must not ALSO do this bounded-but-nonzero backfill work
    # unconditionally.
    if budget is not None and budget.expired():
        result["budget_spent"] = True
        return result

    # One backfill step per cycle, AFTER the live deltas. Bounded by
    # max_per_source so a slow cycle never starves new items.
    bf = progressive_backfill_step(
        store,
        gmail_service=gmail_service,
        drive_service=drive_service,
        calendar_service=calendar_service,
        bulk_section=bulk_section,
    )
    result["backfill"] = bf
    if any(bf.get(k, 0) for k in ("gmail", "drive", "calendar")):
        _embed()
    if budget is not None and budget.expired():
        result["budget_spent"] = True
    return result


def _publish_one_miss(store, ingest_cache, fs, drive_id, file_id, content_hash,
                      pin, published_by, *, contextual_retrieval: bool,
                      skip_gc: bool, on_error=None) -> bool:
    """Publish one (file_id, content_hash) miss. Returns whether it actually
    published (False on a no-op -- e.g. not yet embedded -- or a caught
    per-file failure, never raises).

    `on_error`, if given, is called with the caught exception. This lets a
    caller that needs to distinguish a genuine per-file failure from a clean,
    benign no-op (both of which return False here) keep doing so -- e.g.
    `_publish_drive_misses`'s "every publish in this drive failed" warning
    must not fire just because a batch of chunks isn't embedded yet."""
    try:
        return bool(ingest_cache.publish_file(
            store, fs, drive_id, file_id, content_hash, pin,
            published_by=published_by, skip_gc=skip_gc,
            contextual_retrieval=contextual_retrieval))
    except Exception as exc:  # noqa: BLE001 — publish is best-effort
        log.info("sync: publish_file skipped for drive %s file %s: %s",
                 drive_id, file_id, exc)
        if on_error is not None:
            on_error(exc)
        return False


def _publish_drive_misses(store, ingest_cache, fs, drive_id, misses, pin, published_by,
                          *, contextual_retrieval: bool = False) -> int:
    """Publish each (file_id, content_hash) miss for one drive to the shared
    cache, isolating per-file failures — a transient error on one file must
    not abort the rest of that drive's misses or any other drive's — then
    batch-GC superseded same-pipeline artifacts for the WHOLE miss set in ONE
    cache-folder listing (`ingest_cache.gc_superseded_batch`) instead of the
    O(n) per-file listing `publish_file`'s own internal `gc_superseded` call
    would otherwise do for each file individually.

    `publish_file`/`publish` take `skip_gc=True` here so their internal
    per-file `gc_superseded` call is skipped entirely for every file in this
    loop — the batched call below already covers exactly the same keep set
    (same delete rule, one listing for the whole drive instead of one per
    file), so the per-file GC would otherwise be pure redundant O(n) work on
    top of the O(1) batch. Returns the count of misses successfully
    published.
    """
    published = 0
    failed = 0

    def _count_failure(exc):
        nonlocal failed
        failed += 1

    keep_map: dict[str, str] = {}
    for file_id, content_hash in misses:
        keep_map[file_id] = content_hash
        if _publish_one_miss(store, ingest_cache, fs, drive_id, file_id, content_hash,
                             pin, published_by, contextual_retrieval=contextual_retrieval,
                             skip_gc=True, on_error=_count_failure):
            published += 1
    # A SYSTEMATIC failure (every attempted publish failed — e.g. a missing
    # drive.file write scope or an uncreatable cache folder) means the cache is
    # silently not populating for the whole fleet. Surface it at WARNING once per
    # drive, not buried in per-file info noise.
    if failed and published == 0 and failed == len(misses):
        log.warning("sync: ALL %d shared-cache publishes failed for drive %s — "
                    "cache is not populating (check drive.file scope / cache folder access)",
                    failed, drive_id)
    if keep_map:
        try:
            ingest_cache.gc_superseded_batch(fs, drive_id, keep_map, pin)
        except Exception as exc:  # noqa: BLE001 — GC failure must not fail publish
            log.info("sync: batched GC skipped for drive %s: %s", drive_id, exc)
    return published


def publish_pending_shared_drive_artifacts(store, ingest_cache, *, drives_fs: dict,
                                           pin, published_by: str,
                                           contextual_retrieval: bool = False,
                                           budget=None) -> dict:
    """Publish every drive's durable pending-publish backlog, clearing only
    the files that actually succeeded THIS call. A file that no-ops (not yet
    embedded) stays pending and is retried next time this runs -- never
    silently dropped, matching this plan's retry-forever posture elsewhere.

    `budget` is checked before EACH file's publish, not just between drives:
    publish_file is itself a network write to fleet storage, the same shape
    of work that caused a live 1h44m SSL hang in the code this replaces (see
    run_sync_cycle's shared-drive block comment). A cutoff here is free --
    whatever wasn't reached is still in shared_drive_pending_publish and is
    retried next cycle.

    `skip_gc=True` here for the same reason `_publish_drive_misses` uses it
    -- a per-file `gc_superseded` listing would be redundant next to a
    batched one. This function does not itself call `gc_superseded_batch`;
    which call site owns that batch (this step vs. `_publish_drive_misses`,
    once both exist in the same cycle) is a wiring decision left to the
    caller that assembles the per-cycle flow (see the follow-up task that
    wires this in).
    """
    out = {}
    for drive_id, fs in drives_fs.items():
        pending = store.pending_publishes(drive_id)
        count = 0
        for file_id, content_hash in pending:
            if budget is not None and budget.expired():
                out[drive_id] = count
                return out
            if _publish_one_miss(store, ingest_cache, fs, drive_id, file_id,
                                 content_hash, pin, published_by,
                                 contextual_retrieval=contextual_retrieval,
                                 skip_gc=True):
                store.clear_pending_publish(drive_id, file_id)
                count += 1
        out[drive_id] = count
    return out


def _floor_dt(store, key: str, default: datetime) -> datetime:
    """Read a backfill-floor cursor as a tz-aware UTC datetime, or default."""
    raw = store.get_cursor(key)
    if not raw:
        return default
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return default
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def progressive_backfill_step(
    store,
    *,
    gmail_service=None,
    drive_service=None,
    calendar_service=None,
    window_days: int = _BACKFILL_WINDOW_DAYS,
    max_per_source: int = _BACKFILL_MAX_PER_SOURCE,
    stop_after_empty_windows: int = _STOP_AFTER_EMPTY_WINDOWS,
    now: datetime | None = None,
    bulk_section=None,
) -> dict:
    """Run ONE backfill window per source, walking newest -> oldest. No horizon.

    Each source maintains a "floor" cursor (`<source>_backfill_until`) holding
    the start-of-window for the next step (initial state: floor = now). Each
    call processes [floor - window_days, floor], advances the floor backward,
    and tracks consecutive empty windows in `<source>_backfill_empty`. When the
    empty counter reaches `stop_after_empty_windows` (default ~1 year), that
    source's `*_done` flag flips and subsequent calls are no-ops. This lets a
    20-year-old account backfill in full and naturally stop once the daemon
    has walked past the earliest message.

    Side effects: only `upsert_chunk` and `set_cursor` on backfill keys. Does
    NOT touch the live delta cursors (gmail historyId, drive pageToken,
    calendar syncToken). Errors per source are isolated — a failed window
    leaves that source's floor untouched so the next cycle retries it.

    Each per-source call is already item-bounded (`max_per_source`, default
    200), so no budget/checkpoint logic is added here — but `bulk_section` is
    threaded into each so even this bounded window releases `_bulk_lock`
    between items rather than holding it for up to 200 items straight.
    """
    from mcpbrain.sync.gmail import backfill_gmail
    from mcpbrain.sync.drive import backfill_drive
    from mcpbrain.sync.calendar import backfill_calendar_window

    if now is None:
        now = datetime.now(timezone.utc)
    result = {"gmail": 0, "drive": 0, "calendar": 0,
              "gmail_done": False, "drive_done": False, "calendar_done": False}

    def _empty_count(key: str) -> int:
        raw = store.get_cursor(key)
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    def _step(floor_key: str, empty_key: str, run, done_key: str) -> int:
        if _empty_count(empty_key) >= stop_after_empty_windows:
            result[done_key] = True
            return 0
        end = _floor_dt(store, floor_key, default=now)
        start = end - timedelta(days=window_days)
        try:
            n = run(start, end)
        except Exception:  # noqa: BLE001 — one source's failure must not stall others
            return 0
        store.set_cursor(floor_key, start.isoformat())
        # Reset the empty counter on any hit; otherwise increment so a long
        # empty tail eventually trips the done flag.
        if n > 0:
            store.set_cursor(empty_key, "0")
        else:
            store.set_cursor(empty_key, str(_empty_count(empty_key) + 1))
            if _empty_count(empty_key) >= stop_after_empty_windows:
                result[done_key] = True
        return n

    if gmail_service is not None:
        def _gmail(start, end):
            return backfill_gmail(
                gmail_service, store,
                after=start.strftime("%Y/%m/%d"),
                before=end.strftime("%Y/%m/%d"),
                max_messages=max_per_source,
                bulk_section=bulk_section,
            )
        result["gmail"] = _step("gmail_backfill_until", "gmail_backfill_empty",
                                _gmail, "gmail_done")

    if drive_service is not None:
        def _drive(start, end):
            return backfill_drive(
                drive_service, store,
                modified_after=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                modified_before=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_files=max_per_source,
                bulk_section=bulk_section,
            )
        result["drive"] = _step("drive_backfill_until", "drive_backfill_empty",
                                _drive, "drive_done")

    if calendar_service is not None:
        def _cal(start, end):
            return backfill_calendar_window(
                calendar_service, store,
                time_min=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                time_max=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_events=max_per_source,
                bulk_section=bulk_section,
            )
        result["calendar"] = _step("calendar_backfill_until", "calendar_backfill_empty",
                                   _cal, "calendar_done")

    return result


def _shared_drive_backfill_step(
    store, drive_service, pin, drives: dict,
    *, window_days: int = _BACKFILL_WINDOW_DAYS,
    max_per_source: int = _BACKFILL_MAX_PER_SOURCE,
    stop_after_empty_windows: int = _STOP_AFTER_EMPTY_WINDOWS,
    now: datetime | None = None,
    contextual_retrieval: bool = False,
    budget=None,
    bulk_section=None,
) -> dict:
    """One backfill window per pinned Shared Drive, walking newest -> oldest.

    Mirrors `progressive_backfill_step`'s per-source floor-cursor mechanism (see
    its docstring for the full rationale) but keyed PER-DRIVE —
    `drive:<id>_backfill_until` / `drive:<id>_backfill_empty` — so N shared
    drives backfill independently of each other and of the My-Drive backfill.
    `drives` is `{drive_id: fleet_storage}`, reusing the storage instances
    `sync_shared_drives` already built this cycle via `storage_factory` (no
    second factory call, no extra Drive-API auth round-trip). A drive whose
    window raises is isolated (its floor cursor is left untouched so the next
    cycle retries it) and never stalls the others. Returns `{drive_id:
    {"processed": int, "miss": [(file_id, content_hash), ...]}}`.

    Design note: this is a parallel, per-drive-keyed SIBLING to
    `progressive_backfill_step` rather than a generalisation of it. The
    existing function's single-source-per-key shape (one `drive_backfill_until`
    cursor for the one My-Drive source) doesn't cleanly parametrise over an
    arbitrary, dynamic set of Shared Drives without a larger signature/cursor-
    key redesign, so this reuses the exact same windowing constants and
    floor-cursor idiom in a dedicated function instead — the smallest change
    that is still a real, correct, per-drive-windowed periodic backfill (not a
    stub), wired into `run_sync_cycle`'s shared-drive block (and therefore
    into every daemon cycle) as its actual production caller.

    `budget` (a `Budget`, or None for unbounded) is checked once per drive
    (this loop's "page"), immediately after that drive's window is processed,
    same as the between-sources checks in `run_sync_cycle` -- a fleet with
    many pinned shared drives must not let one cycle's backfill loop run
    unbounded across all of them. A drive not yet reached this cycle keeps its
    existing floor cursor and is retried next cycle.
    """
    from mcpbrain.sync.drive import backfill_shared_drive

    if now is None:
        now = datetime.now(timezone.utc)

    def _empty_count(key: str) -> int:
        raw = store.get_cursor(key)
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    out: dict = {}
    for drive_id, fs in drives.items():
        floor_key = f"drive:{drive_id}_backfill_until"
        empty_key = f"drive:{drive_id}_backfill_empty"
        if _empty_count(empty_key) >= stop_after_empty_windows:
            out[drive_id] = {"processed": 0, "miss": []}
            continue
        end = _floor_dt(store, floor_key, default=now)
        start = end - timedelta(days=window_days)
        try:
            res = backfill_shared_drive(
                drive_service, store, drive_id,
                start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                fleet_storage=fs, pin=pin,
                modified_before=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                max_files=max_per_source,
                contextual_retrieval=contextual_retrieval,
                bulk_section=bulk_section,
            )
        except Exception:  # noqa: BLE001 — one drive's failure must not stall others
            out[drive_id] = {"processed": 0, "miss": []}
            continue
        store.set_cursor(floor_key, start.isoformat())
        # Reset the empty counter on any hit; otherwise increment so a long
        # empty tail eventually trips the done state (mirrors _step above).
        if res["processed"] > 0:
            store.set_cursor(empty_key, "0")
        else:
            store.set_cursor(empty_key, str(_empty_count(empty_key) + 1))
        out[drive_id] = res
        if budget is not None and budget.expired():
            log.info("shared-drive backfill: budget spent after %d/%d drive(s)",
                     len(out), len(drives))
            break
    return out


def backfill_progress(store) -> dict:
    """Per-source indexing-backfill progress for the status UI.

    `reached` is the floor cursor (how far back this source has indexed; None if
    not started). `done` is True once the empty-window counter hit the stop
    threshold (the source has walked past its earliest item)."""
    out = {}
    for src in ("gmail", "drive", "calendar"):
        reached = store.get_cursor(f"{src}_backfill_until")
        try:
            empty = int(store.get_cursor(f"{src}_backfill_empty") or 0)
        except ValueError:
            empty = 0
        out[src] = {"reached": reached, "done": empty >= _STOP_AFTER_EMPTY_WINDOWS}
    return out
