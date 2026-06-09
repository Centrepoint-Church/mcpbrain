"""Drain step: consume enrich_inbox/*.json, apply, mark, resolve, delete.

The daemon runs this. A stateless extractor session writes one batch file per
cycle into MCPBRAIN_HOME/enrich_inbox/<batch>.json. drain reads each file,
validates it against the contract, applies every extraction through Phase 1's
apply(), marks that thread's chunks enriched, feeds merge-review answers into
entity resolution, and deletes the file once every part of it succeeded. A
malformed or contract-violating file is moved to enrich_inbox/bad/ rather than
crashing the daemon. One bad item never aborts the batch.

Home resolution mirrors prepare.py / extractor_driver.py: spool paths resolve
under config.app_dir() (which reads MCPBRAIN_HOME), with an optional home=
override taking precedence.

The apply() seam
----------------
apply is Phase 1's graph_write.apply, injected as a parameter rather than
imported so this module stays importable before Phase 1 lands. The real
signature is:

    apply(store, extraction, *, doc_ids, identity="...", clock=None) -> summary_dict

It upserts entities, relations, role observations, topics and the email_context
row for one clean contract envelope. Idempotency rests on its upsert/dedup, so
re-applying the same extraction is safe.

drain keeps an embedder= parameter because the daemon passes one, but it does
NOT forward it to apply: the structural apply does not consume an embedder
(action/topic embedding is a later Phase 1 task). The parameter is reserved for
when that work lands and drain needs to hand an embedder to a writer that takes
one.
"""

import json
import logging
import os
from pathlib import Path

from mcpbrain import config, orgs
from mcpbrain.contract import (
    normalise_org, sanitize_batch, validate_batch_wrapper, validate_extraction,
)
from mcpbrain.resolve import _pick_winner

log = logging.getLogger(__name__)

# Per-key drainers for optional inbox blocks. Each drainer(store, inbox_obj) called
# when the key is present; failures are isolated (log + continue). Registered by
# block modules at import time.
BLOCK_DRAINERS: dict = {}


def _home(home) -> Path:
    """Resolve the spool root: explicit override first, else config.app_dir()."""
    return Path(home) if home is not None else config.app_dir()


def _iter_inbox(home_dir: Path):
    """Yield enrich_inbox/*.json files, skipping the bad/ quarantine subdir."""
    inbox_dir = home_dir / "enrich_inbox"
    if not inbox_dir.is_dir():
        return
    for path in sorted(inbox_dir.glob("*.json")):
        if path.is_file():
            yield path


def _quarantine(path: Path) -> Path:
    """Move a malformed file to enrich_inbox/bad/, creating the dir as needed."""
    bad_dir = path.parent / "bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    target = bad_dir / path.name
    # Path.replace overwrites an existing target atomically, which would erase
    # an earlier bad file of the same name (forensic loss). Find the first free
    # -N suffix instead. Deterministic so quarantine tests stay stable; pid is
    # only there to avoid clobbering across concurrent drain processes.
    if target.exists():
        n = 1
        while True:
            candidate = bad_dir / f"{path.stem}-{os.getpid()}-{n}{path.suffix}"
            if not candidate.exists():
                target = candidate
                break
            n += 1
    path.replace(target)
    return target


def _regroup_parts(extractions: list) -> list:
    """Recombine split long threads into one extraction per thread_id.

    The prepare step splits an over-long thread into sub-parts that share a
    thread_id and carry {"part": i, "of": k} (see prepare._split_long_thread).
    This is the inverse: group by thread_id, and for any group with more than
    one part, sort by part and concatenate their messages in order into a single
    extraction. The other fields are taken from the first part, and the part/of
    keys are stripped so apply consumes a clean contract envelope. Order of the
    first appearance of each thread_id is preserved.
    """
    order = []
    groups: dict[str, list] = {}
    for ext in extractions:
        tid = ext.get("thread_id")
        if tid not in groups:
            groups[tid] = []
            order.append(tid)
        groups[tid].append(ext)

    recombined = []
    for tid in order:
        parts = groups[tid]
        if len(parts) == 1 and "part" not in parts[0]:
            recombined.append(parts[0])
            continue
        if any("part" not in p for p in parts):
            # An extractor dropped a part key on at least one member. Sorting
            # would float the no-part member to position 0 and silently misorder
            # the concatenated messages. Apply each member as-is instead;
            # apply() upserts/dedups so separate application is safe.
            log.warning("drain: thread %s has mixed part/no-part extractions; "
                        "applying parts as-is", tid)
            recombined.extend(parts)
            continue
        ordered = sorted(parts, key=lambda e: e.get("part", 0))
        of = ordered[0].get("of")
        if of and len(ordered) != of:
            # The extractor dropped or truncated a part. Apply what we have
            # (better than nothing, and a real partial would otherwise retry
            # forever), but log so a truncated batch is observable.
            log.warning("drain: thread %s received %d parts but declared of=%d; "
                        "applying incomplete thread", tid, len(ordered), of)
        merged = dict(ordered[0])
        messages = []
        for p in ordered:
            messages.extend(p.get("messages", []))
        merged["messages"] = messages
        merged.pop("part", None)
        merged.pop("of", None)
        recombined.append(merged)
    return recombined


def _apply_merge_answers(store, answers) -> int:
    """Apply the LLM-adjudicated merge answers. Returns the number of merges done.

    Each answer is {pair_id, same, canonical}. pair_id is the two entity ids
    sorted and joined by '|' (see prepare._merge_pair), so split on '|' to
    recover them. For same:true, look both up, pick the winner with
    resolve._pick_winner (winner, loser) and fold the loser in via merge_entities
    with method='llm'. If either entity is gone (a prior cycle already merged
    it), skip and log. This is the LLM tier of resolution, adjudicated in the
    spool session and applied here by the daemon: no second Claude call, no
    Gemini.
    """
    merges = 0
    for ans in answers or []:
        # Strict bool: validate_batch_file already rejects non-bool `same`, but
        # require True here too so no truthy non-bool can ever drive a merge.
        if ans.get("same") is not True:
            continue
        pair_id = ans.get("pair_id", "")
        ids = pair_id.split("|")
        if len(ids) != 2 or not all(ids):
            log.warning("drain: malformed merge pair_id %r, skipping", pair_id)
            continue
        a = store.get_entity(ids[0])
        b = store.get_entity(ids[1])
        if a is None or b is None:
            log.info("drain: merge pair %s has a missing entity, skipping", pair_id)
            continue
        winner, loser = _pick_winner(a, b)
        try:
            store.merge_entities(loser["id"], winner["id"],
                                 canonical_name=ans.get("canonical") or None,
                                 method="llm")
        except Exception as exc:
            log.error("drain: merge failed for %s <- %s: %s",
                      winner["id"], loser["id"], exc)
            continue
        merges += 1
    return merges


def drain(store, *, home=None, apply=None, embedder=None) -> dict:
    """Process every inbox file. Returns a summary dict.

    Summary keys: files, applied, marked, merges, quarantined, entities,
    relations.

    For each valid file, every extraction is applied through the injected
    apply() then its thread's chunks are marked enriched. apply runs BEFORE
    mark so a crash leaves the chunks unmarked and the thread is reprocessed
    next cycle. A failure on any extraction is logged and isolated: its chunks
    are not marked and the file is kept (not deleted) so unfinished threads
    retry. A file is only fully successful when every extraction applied.
    """
    home_dir = _home(home)
    summary = {"files": 0, "applied": 0, "marked": 0, "merges": 0,
               "quarantined": 0, "entities": 0, "relations": 0,
               "skipped": 0, "dropped_items": 0}

    for path in _iter_inbox(home_dir):
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            log.warning("drain: malformed inbox file %s, quarantining: %s", path.name, exc)
            _quarantine(path)
            summary["quarantined"] += 1
            continue

        # Tolerant validation: only wrapper / merge_answers problems (structural,
        # or irreversible-merge risk) quarantine the whole file. A single bad
        # extraction is sanitised (droppable noise removed) and, if still
        # invalid, skipped individually — one malformed relation from the LLM
        # must not discard an entire batch of good extractions.
        problems = validate_batch_wrapper(data)
        if problems:
            log.warning("drain: wrapper contract violation in %s, quarantining: %s",
                        path.name, "; ".join(problems[:5]))
            _quarantine(path)
            summary["quarantined"] += 1
            continue

        data, dropped_noise = sanitize_batch(data)
        if dropped_noise:
            log.warning("drain: dropped %d malformed relation/action item(s) in %s",
                        dropped_noise, path.name)
            summary["dropped_items"] = summary.get("dropped_items", 0) + dropped_noise

        summary["files"] += 1
        file_ok = True

        extractions = _regroup_parts(data["extractions"])
        taxonomy = orgs.taxonomy_from_config(home)
        # Fail loudly on a misconfigured call rather than letting the per-
        # extraction handler swallow the TypeError, set file_ok=False, keep the
        # file, and loop forever. Raised before the loop so it is never caught.
        if extractions and apply is None:
            raise TypeError("drain() requires an apply callable; inject graph_write.apply")

        for extraction in extractions:
            # Per-extraction contract check (post-sanitise). A structurally
            # invalid extraction (missing thread_id, bad messages, unknown
            # content_type…) is dropped and logged, NOT quarantined with the
            # batch — its chunks stay enriched=0 and re-queue next prepare. This
            # does not flip file_ok: the file is still consumed (we did our best
            # with the salvageable extractions).
            ext_problems = validate_extraction(extraction)
            if ext_problems:
                log.warning("drain: skipping invalid extraction (thread %s) in %s: %s",
                            extraction.get("thread_id", "?"), path.name,
                            "; ".join(ext_problems[:3]))
                summary["skipped"] = summary.get("skipped", 0) + 1
                continue
            thread_id = extraction["thread_id"]
            # Org drift gate: canonicalise; coerce an unconfigured org to
            # "unknown" and record it, so repeated sightings of a real org
            # surface as a "add it to config orgs?" finding instead of either
            # quarantining the thread or vanishing silently.
            raw_org = normalise_org(extraction, taxonomy)
            if raw_org is not None:
                log.info("drain: unconfigured org %r on thread %s coerced to "
                         "'unknown'", raw_org, thread_id)
                store.record_finding(
                    "org_unrecognised", ref_id=raw_org.strip().lower(),
                    org="unknown",
                    summary=f"Extractor returned unconfigured org '{raw_org}'",
                    detail=f"Last seen on thread {thread_id}; coerced to "
                           f"'unknown'. If this is a real organisation, add it "
                           f"to the orgs list in config.json.",
                    severity="info")
            # Recover the chunks this extraction covers by message id, NOT by a
            # thread-wide query. Marking only the messages that were actually
            # extracted means a late-arriving message (synced after prepare) or a
            # dropped long-thread part stays enriched=0 and re-queues next cycle,
            # instead of being silently marked done without ever being enriched.
            msg_ids = [m.get("message_id") for m in extraction.get("messages", [])
                       if m.get("message_id")]
            doc_ids = store.doc_ids_for_messages(msg_ids)
            if not doc_ids:
                log.warning("drain: no chunks matched the messages of thread %s "
                            "in %s; applying but marking nothing", thread_id, path.name)
            try:
                res = apply(store, extraction, doc_ids=doc_ids)
            except Exception as exc:
                log.error("drain: apply failed for thread %s in %s: %s",
                          thread_id, path.name, exc)
                file_ok = False
                continue
            summary["applied"] += 1
            # Surface apply()'s own counts: entities counts entities LINKED to
            # the thread (including upserts of already-known people), so it is
            # > 0 even when no net store rows are added. (res or {}).get(...)
            # so a minimal apply returning None or a dict without these keys
            # never crashes drain.
            summary["entities"] += (res or {}).get("entities", 0)
            summary["relations"] += (res or {}).get("relations", 0)
            store.mark_enriched(doc_ids)
            summary["marked"] += len(doc_ids)

        try:
            summary["merges"] += _apply_merge_answers(store, data.get("merge_answers"))
        except Exception as exc:
            log.error("drain: merge-answer processing failed in %s: %s", path.name, exc)
            file_ok = False

        try:
            from mcpbrain.synthesise_threads import drain_synthesis
            synth = drain_synthesis(store, data)
            if synth.get("thread_context_written", 0):
                summary["synthesis_written"] = summary.get("synthesis_written", 0) + synth["thread_context_written"]
        except Exception as exc:
            log.error("drain: synthesis drain failed in %s: %s", path.name, exc)
            file_ok = False

        for _key, _drainer in BLOCK_DRAINERS.items():
            if _key not in data:
                continue
            try:
                res = _drainer(store, data)
                # Always report the key on success, even for a falsy result, so
                # the daemon clears its stash for this block (it keys off the
                # presence of f"{_key}_drained", not its value).
                summary[f"{_key}_drained"] = sum(
                    v for v in res.values() if isinstance(v, int)) if res else 0
            except Exception as exc:
                log.error("drain: %s drain failed in %s: %s", _key, path.name, exc)
                # Retain the file for retry — matches the synthesis-drain failure
                # path above. Without this the inbox file is deleted with the
                # block's answers unapplied while the daemon's stash re-attaches
                # the same requests every cycle (silent infinite retry loop).
                file_ok = False

        # Delete only when every extraction applied and merge-answers ran. A
        # partial failure leaves the file for retry next cycle. Idempotency on a
        # re-applied extraction rests on apply()'s upsert/dedup (Phase 1); drain
        # itself just does not double-delete -- a gone file is skipped by the
        # glob in _iter_inbox.
        if file_ok:
            try:
                path.unlink()
            except OSError as exc:
                log.error("drain: could not delete completed file %s: %s", path.name, exc)

            # The batch this file answers is consumed: remove a matching
            # pending.json so the extractor can't re-run a stale batch.
            pending = home_dir / "enrich_queue" / "pending.json"
            try:
                batch_id = data.get("batch_id")
                if batch_id and pending.exists() and \
                        json.loads(pending.read_text()).get("batch_id") == batch_id:
                    pending.unlink()
                    log.info("drain: consumed pending.json for %s", data.get("batch_id"))
            except (ValueError, OSError) as exc:
                log.warning("drain: pending.json check failed: %s", exc)

    return summary


def drain_captures(store, *, home=None) -> int:
    """Apply every capture_inbox envelope. Returns the number applied.

    validate -> dedupe -> apply -> change_log -> delete. Invalid or unparseable
    envelopes quarantine to capture_inbox/bad/. The daemon calls this each
    cycle; it is the ONLY consumer of the spool the MCP write tools feed.
    """
    from mcpbrain.chunking import action_fingerprint, content_hash
    from mcpbrain.contract import validate_capture

    home_dir = _home(home)
    inbox = home_dir / "capture_inbox"
    if not inbox.exists():
        return 0
    applied = 0
    for path in sorted(inbox.glob("*.json")):
        try:
            env = json.loads(path.read_text())
        except (ValueError, OSError) as exc:
            log.warning("capture: unparseable %s, quarantining: %s", path.name, exc)
            _quarantine(path)
            continue
        problems = validate_capture(env)
        if problems:
            log.warning("capture: invalid %s, quarantining: %s",
                        path.name, "; ".join(problems[:3]))
            _quarantine(path)
            continue
        kind = env["kind"]
        file_ok = True
        if kind == "ingest":
            text = f"{env['title'].strip()}\n\n{env['content'].strip()}"
            chash = content_hash(text)
            doc_id = f"note-{chash[:32]}"
            try:
                changed = store.upsert_chunk(doc_id, text, chash,
                                   {"source": "note", "title": env["title"],
                                    "observation_type": env.get("observation_type", "note"),
                                    # tags stored for future FTS indexing (not yet live)
                                    "tags": env.get("tags", ""),
                                    "org": env.get("org", ""),
                                    "captured_at": env.get("captured_at", "")})
                if changed:
                    store.record_change("capture_ingest", ref_id=doc_id,
                                        summary=f"Saved note '{env['title'][:60]}'")
                    applied += 1
            except Exception as exc:
                log.error("capture: ingest failed for %s: %s", path.name, exc)
                file_ok = False
        elif kind == "action_create":
            fp = action_fingerprint(env["text"])
            if store.find_open_action_by_fingerprint(fp) is not None:
                log.info("capture: duplicate action skipped: %r", env["text"][:60])
            else:
                try:
                    owner = env.get("owner") or config.owner_name(str(home_dir))
                    aid = store.add_unified_action(
                        text=env["text"], owner=owner, deadline=env.get("deadline", ""),
                        org=env.get("org", ""), project_id=env.get("project_id", ""),
                        area_id=env.get("area_id", ""), source="capture",
                        text_fingerprint=fp)
                    store.record_change("capture_action", ref_id=str(aid),
                                        summary=f"Created action '{env['text'][:60]}'")
                    applied += 1
                except Exception as exc:
                    log.error("capture: action_create failed for %s: %s", path.name, exc)
                    file_ok = False
        elif kind == "action_update":
            try:
                changed = store.set_action_status(
                    env["action_id"], env["status"],
                    resolved_by=f"capture:{env.get('source', 'mcp')}",
                    only_if_open=(env["status"] == "done"))
                if changed:
                    store.record_change(
                        "capture_action_update", ref_id=str(env["action_id"]),
                        summary=f"Action {env['action_id']} -> {env['status']}")
                    applied += 1
                else:
                    log.info("capture: action_update %s no-op (not open / not found)",
                             env["action_id"])
            except Exception as exc:
                log.error("capture: action_update failed for %s: %s", path.name, exc)
                file_ok = False
        elif kind in ("decision", "continuity", "memory"):
            try:
                from mcpbrain import joshbrain_write as jw
                repo = config.joshbrain_dir(str(home_dir))
                if kind == "decision":
                    committed = jw.append_decision(repo, text=env["text"], rationale=env.get("rationale", ""),
                                       owner=env.get("owner", ""), supersedes=env.get("supersedes", ""),
                                       org=env.get("org", ""))
                elif kind == "continuity":
                    committed = jw.append_continuity(repo, text=env["text"])
                else:  # memory
                    committed = jw.write_memory(repo, slug=env["slug"], description=env.get("description", ""),
                                    body=env["body"], memory_type=env.get("memory_type", "project"))
                if committed:
                    applied += 1
            except Exception as exc:
                log.error("capture: %s write failed for %s: %s", kind, path.name, exc)
                file_ok = False
        if file_ok:
            path.unlink(missing_ok=True)
    return applied
