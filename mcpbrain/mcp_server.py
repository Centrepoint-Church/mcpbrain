import logging
from pathlib import Path

from mcpbrain import config

from mcpbrain.retrieval import annotate_action_freshness, hybrid_search

_log = logging.getLogger("mcpbrain.mcp_server")


def write_heartbeat(home: str, *, now=None) -> None:
    """Record that Claude Desktop launched this MCP server (the verified-connected
    signal the status layer reads). Best-effort: never raise into startup."""
    import json
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    try:
        (Path(home) / "mcp_heartbeat.json").write_text(
            json.dumps({"last_seen": now.isoformat()})
        )
    except OSError:
        pass


def _default_owner() -> str:
    """The install owner for MCP-initiated writes, from config (empty if unset)."""
    return config.owner_name(str(config.app_dir()))


def _resource_entries() -> list[tuple[str, Path]]:
    """(name, resolved_path) for every context resource we expose.

    Two roots: the app-dir context (the daemon-maintained note index, e.g.
    memory.md) and the per-user records repo (identity, voice, preferences,
    reference, decisions, MEMORY.md, CLAUDE.md) so the working Cowork project can
    read standing context through the MCP server without any filesystem paths.
    Only existing files are returned; a missing file or repo is simply absent.
    """
    entries: list[tuple[str, Path]] = []
    app_ctx = config.app_dir() / "context"
    if app_ctx.is_dir():
        for md in sorted(app_ctx.glob("*.md")):
            entries.append((md.name, md.resolve()))
    records = Path(config.records_dir(str(config.app_dir())))
    candidates: list[Path] = [records / "CLAUDE.md", records / "MEMORY.md",
                              records / "state" / "decisions.md"]
    for sub in ("context", "reference"):
        sub_dir = records / sub
        if sub_dir.is_dir():  # guard: never raise if the repo isn't scaffolded yet
            candidates.extend(sorted(sub_dir.glob("*.md")))
    for p in candidates:
        if p.is_file():
            entries.append((str(p.relative_to(records)), p.resolve()))
    return entries


async def list_context_resources():
    """Return types.Resource entries for the app-dir context + the records repo."""
    from mcp import types
    return [
        types.Resource(uri=f"file://{path}", name=name, mimeType="text/markdown")
        for name, path in _resource_entries()
    ]


async def read_context_resource(uri) -> str:
    """Return a resource's text, rejecting any uri not in the advertised allowlist.

    Exact membership against _resource_entries() is the containment guard: only a
    path we actually expose can be read, so no traversal or arbitrary-file read is
    possible regardless of the uri given.
    """
    from urllib.parse import unquote, urlparse
    # urlparse handles both file:///abs and file://localhost/abs forms a client
    # might send; unquote decodes %20 etc. (the allowlist is the real guard).
    path = Path(unquote(urlparse(str(uri)).path)).resolve()
    allowed = {p for _, p in _resource_entries()}
    if path not in allowed:
        raise ValueError(f"resource not in allowlist: {uri}")
    return path.read_text(encoding="utf-8")


def make_brain_search(store, embedder):
    async def brain_search(query: str, limit: int = 10) -> list[dict]:
        try:
            return hybrid_search(store, embedder, query, limit)
        except Exception:
            _log.exception("brain_search failed for query %r", query)
            return []
    return brain_search


def make_brain_context(store):
    async def brain_context(entity: str = "", mode: str = "profile",
                            community_id: int | None = None) -> dict | list:
        """Profile an entity or list community clusters.

        mode="profile" (default): entity is required. Returns the entity record,
            its relations (in + out), the actions it owns, and the projects/areas
            it owns. Returns {} when the entity is unknown.

        mode="communities": entity is ignored.
            - If community_id is given: returns the list of entity dicts that
              are members of that community.
            - Otherwise: returns all community_summaries rows (list of dicts).
        """
        try:
            if mode == "communities":
                if community_id is not None:
                    return store.community_members(community_id)
                return store.list_communities()

            # mode == "profile" (default)
            if not entity:
                return {}
            ent = store.find_entity(entity)
            if not ent:
                return {}
            rels = store.relations_for(ent["id"])
            relations = []
            for r in rels:
                if r["entity_a"] == ent["id"]:
                    relations.append({"relation": r["relation"], "other": r["entity_b"],
                                      "direction": "out", "source_doc_id": r["source_doc_id"]})
                else:
                    relations.append({"relation": r["relation"], "other": r["entity_a"],
                                      "direction": "in", "source_doc_id": r["source_doc_id"]})
            # owner must match ent["name"] exactly (case-insensitive); Gemini-extracted owners may use short forms and won't match.
            # Actions now come from the unified actions table, not graph_actions_legacy.
            # annotate_action_freshness is read-time only (no DB writes); keeps the MCP tool read-only.
            actions = annotate_action_freshness(store, store.unified_actions(owner=ent["name"]))
            return {"entity": {**ent, "profile": ent.get("profile", "")},
                    "relations": relations, "actions": actions}
        except Exception:
            _log.exception("brain_context failed for entity=%r mode=%r", entity, mode)
            return {}
    return brain_context


def make_brain_actions(store):
    async def brain_actions(owner: str = "", status: str = "open") -> list[dict]:
        """Action items from the unified actions table, filtered by owner and
        status, with read-time freshness annotation. Empty owner defaults to
        the configured install owner. Returns [] on error."""
        try:
            if not owner:  # explicit None/empty must not widen to all owners
                owner = config.owner_name(str(config.app_dir()))
            status = status or "open"
            actions = store.unified_actions(owner=owner, status=status)
            return annotate_action_freshness(store, actions)
        except Exception:
            _log.exception("brain_actions failed for owner=%r status=%r", owner, status)
            return []
    return brain_actions


def make_brain_graph(store):
    async def brain_graph(entity: str, hops: int = 1, *, at_time: str | None = None,
                          include_invalidated: bool = False) -> dict:
        """Traverse the relationship graph from an entity up to `hops` (capped at 3).
        at_time scopes the traversal to relations valid at that ISO date;
        include_invalidated also follows superseded edges.
        Returns {center, nodes:[entity dicts], edges:[{entity_a,relation,entity_b}]}; {} if unknown."""
        try:
            center = store.find_entity(entity)
            if not center:
                return {}
            depth = max(0, min(hops, 3))  # cap; guard against runaway traversal
            visited = {center["id"]}
            edges = {}  # (entity_a, relation, entity_b) -> dict, dedup
            frontier = {center["id"]}
            for _ in range(depth):
                next_frontier = set()
                for ent_id in frontier:
                    for r in store.relations_for(ent_id, at_time=at_time,
                                                 include_invalidated=include_invalidated):
                        key = (r["entity_a"], r["relation"], r["entity_b"])
                        if key not in edges:
                            edges[key] = {"entity_a": r["entity_a"], "relation": r["relation"],
                                          "entity_b": r["entity_b"]}
                        for nbr in (r["entity_a"], r["entity_b"]):
                            if nbr not in visited:
                                visited.add(nbr)
                                next_frontier.add(nbr)
                frontier = next_frontier
                if not frontier:
                    break
            nodes = [n for n in (store.get_entity(i) for i in visited) if n]
            return {"center": center, "nodes": nodes, "edges": list(edges.values())}
        except Exception:
            _log.exception("brain_graph failed for %r", entity)
            return {}
    return brain_graph


def make_brain_proactive(store):
    async def brain_proactive(finding_type: str = "", severity: str = "") -> list:
        """Return open proactive findings, optionally filtered by type and/or severity."""
        try:
            findings = store.open_findings(finding_type or None)
            if severity:
                findings = [f for f in findings if f.get("severity") == severity]
            return findings
        except Exception:
            _log.exception("brain_proactive failed")
            return []
    return brain_proactive


def _capture_envelope(kind: str, source: str = "mcp", **fields) -> dict:
    from datetime import datetime, timezone
    return {"kind": kind, "source": source,
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            **fields}


def make_brain_ingest():
    async def brain_ingest(title: str, content: str, tags: str = "",
                           observation_type: str = "note", org: str = "") -> dict:
        """Save a note/decision/memory. QUEUED: searchable after the next
        sync cycle (~5 min), not immediately. Returns {queued, path|error}."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "ingest", title=title, content=content, tags=tags,
                observation_type=observation_type or "note", org=org))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_ingest


def make_brain_action_create():
    async def brain_action_create(text: str, owner: str = "", deadline: str = "",
                                  org: str = "", project_id: str = "",
                                  area_id: str = "") -> dict:
        """Create an action item. QUEUED: appears after the next sync cycle
        (~5 min). Empty owner defaults to the configured install owner."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "action_create", text=text, owner=owner, deadline=deadline,
                org=org, project_id=project_id, area_id=area_id))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_action_create


def make_brain_action_update():
    async def brain_action_update(action_id: int, status: str) -> dict:
        """Mark an action done or reopen it ('done'|'open'). QUEUED: applies
        on the next sync cycle (~5 min)."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "action_update", action_id=action_id, status=status))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_action_update


def make_brain_decision():
    async def brain_decision(text: str, rationale: str = "", owner: str = "",
                             supersedes: str = "", org: str = "") -> dict:
        """Record a decision. QUEUED: the daemon appends a row to state/decisions.md
        in your records repo and commits (one daemon cycle, ~seconds-minutes), not instantly."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "decision", text=text, rationale=rationale, owner=owner,
                supersedes=supersedes, org=org))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_decision


def make_brain_note():
    async def brain_note(text: str) -> dict:
        """Record a continuity note. QUEUED: the daemon prepends a dated entry to
        state/hot.md in your records repo and commits (one daemon cycle), not instantly."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "continuity", text=text))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_note


def make_brain_memory_write():
    async def brain_memory_write(slug: str, description: str, body: str,
                                 memory_type: str = "project") -> dict:
        """Write a durable auto-memory file. QUEUED: the daemon writes memory/<slug>.md
        + a MEMORY.md pointer in your records repo and commits (one daemon cycle), not instantly."""
        from mcpbrain.capture import write_capture
        try:
            p = write_capture(str(config.app_dir()), _capture_envelope(
                "memory", slug=slug, description=description, body=body,
                memory_type=memory_type))
            return {"queued": True, "path": str(p)}
        except (ValueError, OSError) as exc:
            return {"queued": False, "error": str(exc)}
    return brain_memory_write


def make_brain_gardener_apply(store):
    async def brain_gardener_apply(lane: str, filename: str, content: str,
                                   asserts_person_role: bool = False,
                                   attribution_source: str = "",
                                   attribution_quote: str = "",
                                   attribution_doc_id: str = "") -> dict:
        """Apply a reference-gardener change directly to the records repo, through the
        deterministic guard. NOT queued — the write + commit happen synchronously so the
        gardener gets immediate enforcement feedback.

        lane: 'reference' (drift) or 'context' (constitution). filename: basename of an
        existing file in that dir. content: full new file content.

        Person-role claims are VERIFIED, not just labelled. When asserts_person_role is
        True: attribution_source must be 'owner_statement' | 'signature' |
        'owner_confirmation', and attribution_quote (verbatim supporting text) is
        required. For owner_statement/signature, attribution_doc_id must point at a stored
        chunk and the quote is checked to actually appear in it — a fabricated or inferred
        attribution is rejected. owner_confirmation needs only the confirmed quote.
        Returns {"applied": bool, "committed": bool} or {"applied": False, "error": ...}.
        """
        import subprocess
        from mcpbrain import records_write, config as _cfg
        home = str(config.app_dir())
        repo = _cfg.records_dir(home)
        cap = _cfg.gardener_max_changed_lines(home)
        try:
            if lane == "reference":
                committed = records_write.write_gardener_reference(
                    repo, filename, content, max_changed_lines=cap)
            elif lane == "context":
                if asserts_person_role:
                    err = _verify_role_attribution(
                        store, attribution_source, attribution_quote, attribution_doc_id)
                    if err:
                        return {"applied": False, "error": err}
                committed = records_write.write_gardener_context(
                    repo, filename, content,
                    asserts_person_role=asserts_person_role,
                    attribution_source=(attribution_source or None),
                    attribution_doc_id=(attribution_doc_id or None),
                    max_changed_lines=cap)
            else:
                return {"applied": False, "error": f"unknown lane {lane!r} "
                        "(expected 'reference' or 'context')"}
            return {"applied": True, "committed": committed}
        except (ValueError, FileNotFoundError, OSError) as exc:
            return {"applied": False, "error": str(exc)}
        except subprocess.CalledProcessError as exc:
            # The daemon is the usual records-repo writer; a concurrent commit can
            # hold .git/index.lock. Surface it cleanly so the gardener retries next
            # run rather than crashing the tool call.
            return {"applied": False, "error": f"git busy (retry next run): {exc}"}
    return brain_gardener_apply


def _verify_role_attribution(store, source: str, quote: str, doc_id: str) -> str | None:
    """Verify a person-role attribution before it is written. Returns an error string
    to reject, or None to allow. Enforces the enum (defence-in-depth — the writer
    checks too) and verifies the quote against the cited stored source."""
    from mcpbrain import records_write
    if source not in records_write._APPROVED_ATTRIBUTION_SOURCES:
        return (f"Role attribution source {source!r} is not permitted. Approved: "
                f"{sorted(records_write._APPROVED_ATTRIBUTION_SOURCES)}.")
    if source in records_write._STORE_BACKED_SOURCES:
        if not doc_id:
            return (f"attribution_doc_id is required for a {source} role claim — cite the "
                    "stored chunk that contains the supporting text")
        chunk = store.get_chunk(doc_id)
        if not chunk:
            return f"attribution_doc_id {doc_id!r} not found in the store"
        return records_write.verify_attribution_quote(quote, chunk.get("text", ""))
    # owner_confirmation: live human-in-the-loop; require the confirmed text, no store doc.
    if not quote.strip():
        return "attribution_quote (the confirmed statement) is required for owner_confirmation"
    return None


def make_brain_draft_context(store, home: str):
    async def brain_draft_context(email_id: str, intent: str = "") -> dict:
        """Return context for drafting a reply (subject, body, sender, voice_rules, samples).

        email_id: message_id from email_context.
        intent: optional — 'reply' | 'acknowledge' | 'decline' | 'decide' | 'inform'.
        Returns context dict or {"error": "email not found"}.
        """
        from mcpbrain import draft as _draft
        return _draft.draft_context(store, home, email_id, intent=intent)
    return brain_draft_context


def make_brain_draft_save(store, home: str):
    async def brain_draft_save(email_id: str, thread_id: str, intent: str,
                                final_draft: str, parent_draft_id: int | None = None) -> dict:
        """Persist a completed draft to draft history.

        Call after the Cowork skill has finished drafting.
        Returns {"draft_record_id": <id>} or {"error": ...}.
        """
        try:
            record_id = store.save_draft(
                email_id=email_id, thread_id=thread_id, intent=intent,
                audience_tier="", draft_text=final_draft, critique="",
                voice_issues=[], samples_used=0, model="cowork",
                parent_draft_id=parent_draft_id,
            )
            return {"draft_record_id": record_id}
        except Exception as exc:
            _log.exception("brain_draft_save failed for email_id=%r", email_id)
            return {"error": str(exc)}
    return brain_draft_save


# --- Autonomous-loop tools (host-native; VM-proof) --------------------------
# The Cowork enrich + meeting-packs scheduled tasks must reach the host's app
# data and store. Per the Cowork desktop architecture, shell commands and curl
# run in an isolated VM, but local plugin MCP servers run natively on the host —
# so these tools are the reliable channel. The enrich tools are pure file I/O on
# the app-data dir (mirroring brain_ingest); the meeting tools wrap the store +
# dashboard the control API also uses.

_ENRICH_RULES_CACHE = None


def _enrich_rules() -> str:
    """The canonical extraction rules — the SHARED-EXTRACTION-RULES block of the
    bundled ``enrich_prompt.md`` (shipped inside the wheel). brain_enrich_pull
    returns this so the response is self-contained: the enrichment caller needs
    no plugin/skill file and no source repo to know the extraction protocol.
    Returns '' if the bundled file is somehow missing (never raises)."""
    global _ENRICH_RULES_CACHE
    if _ENRICH_RULES_CACHE is not None:
        return _ENRICH_RULES_CACHE
    from pathlib import Path
    begin, end = "<!-- SHARED-EXTRACTION-RULES:BEGIN -->", "<!-- SHARED-EXTRACTION-RULES:END -->"
    try:
        text = (Path(__file__).parent / "enrich_prompt.md").read_text()
        _ENRICH_RULES_CACHE = text[text.index(begin) + len(begin):text.index(end)].strip()
    except (OSError, ValueError):
        _ENRICH_RULES_CACHE = ""
    return _ENRICH_RULES_CACHE


# Bounds the FULL serialized pull/unit response (work + rules + context). Kept under
# Claude Code's consumer limits: a result above ~50KB is persisted to a file the
# caller must Read back. Sourced from config.unit_pull_cap() (default 60_000 as of
# Task 5.1 — raised from 40_000 to pack more threads per Haiku call). Must stay in
# lockstep with prepare._UNIT_PULL_CAP (the producer sizes units against this).
_PULL_MAX_CHARS = config.unit_pull_cap()


_ROUTINES = ("enrich", "meeting-packs", "gardener", "reference-gardener")


def _routine_instructions(name: str) -> str | None:
    """The bundled protocol markdown for a recurring routine, served via MCP so a
    scheduled task is self-contained — no plugin command/skill resolution (which the
    Cowork/scheduled-task runtime does not reliably do) and no source repo. Returns
    None for an unknown name. The name is validated against a fixed allowlist, so
    there is no path traversal."""
    if name not in _ROUTINES:
        return None
    from pathlib import Path
    try:
        return (Path(__file__).parent / "routines" / f"{name}.md").read_text()
    except OSError:
        return None


# The optional answer blocks brain_enrich_pull may ask for, beyond extractions +
# merge_answers. Each is drained by the daemon from the inbox object under this
# exact key (see drain.py BLOCK_DRAINERS + synthesise_threads). Without forwarding
# them, the synthesis/profile/community/memory/audit work the batch requested is
# silently dropped on the MCP path.
_ENRICH_ANSWER_BLOCKS = ("synthesis", "profile_synthesis", "community_synthesis",
                         "memory_distil", "profile_audit")


_LEASE_TTL_S = 15 * 60  # a claimed unit is re-listable after this (covers crashed subagents)
_UNITS_BATCH_DEFAULT = 12  # max units handed out (and claimed) per brain_enrich_units call


def _units_batch() -> int:
    """Max ready units returned — and claimed — per brain_enrich_units call.

    Without a cap, one call globs and leases the WHOLE queue, so any overlapping
    caller (the hourly cycle running alongside the backfill loop, or the next
    wave within a run) gets {"empty": true} until the 15-min lease expires. Cap
    each call to a wave's worth so callers share the queue wave-by-wave instead.
    Override with MCPBRAIN_ENRICH_UNITS_BATCH.
    """
    import os
    try:
        return max(1, int(os.environ.get("MCPBRAIN_ENRICH_UNITS_BATCH", _UNITS_BATCH_DEFAULT)))
    except (TypeError, ValueError):
        return _UNITS_BATCH_DEFAULT


def _units_dir(home):
    from pathlib import Path
    return Path(home) / "enrich_queue" / "units"


def _claims_dir(home):
    from pathlib import Path
    return Path(home) / "enrich_queue" / "claims"


def make_brain_enrich_units(home: str):
    async def brain_enrich_units() -> dict:
        """List up to a wave's worth of ready work units and CLAIM each with a short
        lease. Returns descriptors only — `unit_id`, `kind`, `block`, `count` — NO
        payloads, so the orchestrator stays context-flat. Spawn one subagent per
        returned `unit_id`; each calls brain_enrich_pull(unit_id), extracts, and
        brain_enrich_push(unit_id, …). At most `_units_batch()` units are returned per
        call (the rest stay unclaimed for the next call / an overlapping caller), so no
        single call leases the whole queue. Units claimed within the lease are skipped,
        so overlapping runs and the backfill loop never re-hand-out in-flight work; a
        stale claim (crashed subagent) becomes re-listable. Call again for the next
        wave; returns {"empty": true} when no unclaimed units remain."""
        import json as _json
        import time as _time
        try:
            files = sorted(_units_dir(home).glob("*.json"))
        except OSError:
            return {"empty": True}
        claims = _claims_dir(home)
        batch = _units_batch()
        ready, now = [], _time.time()
        for f in files:
            uid = f.stem
            claim = claims / uid
            try:
                if claim.exists() and now - claim.stat().st_mtime < _LEASE_TTL_S:
                    continue                          # still leased to another worker
            except OSError:
                pass
            try:
                d = _json.loads(f.read_text())
            except (OSError, ValueError):
                continue                              # skip a half-written/garbage unit
            try:
                claims.mkdir(parents=True, exist_ok=True)
                (claims / uid).touch()                # claim (mtime = now)
            except OSError:
                pass
            ready.append({"unit_id": uid, "kind": d.get("kind"), "block": d.get("block"),
                          "count": len(d.get("threads") or d.get("items") or [])})
            if len(ready) >= batch:
                break                                 # cap one call to a wave; leave the rest unclaimed
        return {"units": ready} if ready else {"empty": True}
    return brain_enrich_units


def make_brain_enrich_pull(home: str):
    async def brain_enrich_pull(unit_id: str, with_rules: bool = True) -> dict:
        """Return one work unit's payload (from brain_enrich_units) with the current
        context attached, or {"empty": true} if the unit is gone. A `kind` "thread"
        unit returns `threads`; a `kind` "block" unit returns `block` + `items`.

        `with_rules` (default True) attaches the FULL extraction protocol so a
        general-purpose caller is self-contained (no plugin/skill file or source
        repo). The `enrich-batch` subagent passes ``with_rules=False``: it already
        carries the rules in its SYSTEM PROMPT (kept byte-identical to `_enrich_rules`
        by test_enrich_agent_rules_in_sync), so every sibling subagent shares one
        cacheable prefix — re-sending the rules here, in the uncached tool result,
        would pay for them a second time and defeat the caching."""
        import json as _json
        from pathlib import Path
        if not unit_id:
            return {"empty": True}
        try:
            d = _json.loads((_units_dir(home) / f"{unit_id}.json").read_text())
        except (OSError, ValueError):
            return {"empty": True}
        try:
            ctx = _json.loads((Path(home) / "enrich_queue" / "context.json").read_text())
        except (OSError, ValueError):
            ctx = {}
        # When rules are sent, they lead (byte-stable across units) then context, so a
        # general-purpose caller's serialized prefix is reusable; the variable per-unit
        # fields (unit_id, work) trail. The enrich-batch agent omits them entirely —
        # its rules live in the cacheable system-prompt prefix instead.
        out = {}
        if with_rules:
            out["rules"] = _enrich_rules()
        out["context"] = ctx
        out["kind"] = d.get("kind")
        out["unit_id"] = unit_id
        if d.get("kind") == "block":
            out["block"] = d.get("block")
            out["items"] = d.get("items") or []
        else:
            out["threads"] = d.get("threads") or []
        # Safety net: the producer sized the unit to fit, but if a since-grown context
        # tips it over, trim context to the few fields an answer needs.
        if len(_json.dumps(out)) > _PULL_MAX_CHARS:
            out["context"] = {k: ctx[k] for k in ("owner_name", "valid_orgs",
                                                  "org_domain_map") if k in ctx}
        return out
    return brain_enrich_pull


def make_brain_enrich_push(home: str):
    async def brain_enrich_push(unit_id: str = "", extractions: list | None = None,
                                merge_answers: list | None = None,
                                **blocks) -> dict:
        """Write a unit's enrichment result to enrich_inbox/<unit_id>.json for the
        daemon to drain (it applies the result, marks chunks enriched, and deletes the
        unit). Besides `extractions` and `merge_answers`, accepts the optional answer
        blocks (synthesis, profile_synthesis, community_synthesis, memory_distil,
        profile_audit) and forwards each. Returns {"written": bool, path|error}.

        Schema rules:
          - `extractions` must be a list when provided; passing a non-list (string,
            dict, number) is rejected with a clear error so a subagent that narrates
            its result instead of producing a proper tool call is caught at the
            boundary rather than silently consuming the unit with zero extractions.
          - `extractions` may be None/omitted ONLY for block units that carry their
            answer in a block field (merge_answers, synthesis, profile_synthesis,
            community_synthesis, memory_distil, profile_audit).  A push with no
            extractions AND no block answers is rejected — it indicates a derailed
            subagent that produced prose instead of a real extraction payload.
        """
        import json as _json
        from pathlib import Path
        if not unit_id:
            return {"written": False, "error": "unit_id required"}
        # Type check before the or-coercion so a dict/string/number is never silently
        # treated as an empty list.  extractions=None is handled separately below.
        if extractions is not None and not isinstance(extractions, list):
            return {"written": False, "error": "extractions must be a list"}
        # A push with extractions=None/missing is only valid for block units — when at
        # least one block-answer field carries the real payload.  Without block answers
        # a missing extractions means the subagent derailed (produced prose, timed out,
        # or skipped the tool call entirely) and must not silently drain the unit.
        has_block_answer = (merge_answers is not None and merge_answers != []) or any(
            blocks.get(k) for k in _ENRICH_ANSWER_BLOCKS
        )
        if extractions is None and not has_block_answer:
            return {"written": False,
                    "error": "extractions is required for thread units (must be a list of "
                             "extraction objects); omit or pass [] only for block units that "
                             "provide a block-answer field (merge_answers, synthesis, etc.)"}
        extractions = extractions if extractions is not None else []
        try:
            inbox = Path(home) / "enrich_inbox"
            inbox.mkdir(parents=True, exist_ok=True)
            payload = {"unit_id": unit_id, "extractions": extractions,
                       "merge_answers": merge_answers or []}
            for _k in _ENRICH_ANSWER_BLOCKS:
                if blocks.get(_k):
                    payload[_k] = blocks[_k]
            target = inbox / f"{unit_id}.json"
            tmp = inbox / f".{unit_id}.json.tmp"
            tmp.write_text(_json.dumps(payload, ensure_ascii=False))
            tmp.replace(target)  # atomic; the daemon never sees a half-written file
            return {"written": True, "path": str(target)}
        except (OSError, ValueError) as exc:
            return {"written": False, "error": str(exc)}
    return brain_enrich_push


def make_brain_enrich_advance(home: str):
    async def brain_enrich_advance() -> dict:
        """Nudge the daemon to run an immediate drain + prepare cycle, so newly
        enriched units are applied and the next units are produced in seconds instead
        of after the normal interval. Use between backfill rounds, then call
        brain_enrich_units again. Returns {"woken": true} or {"error": ...} when the
        daemon isn't reachable."""
        from mcpbrain.control_client import ControlClient, DaemonUnavailable
        try:
            return ControlClient(home).sync_now()
        except DaemonUnavailable as exc:
            return {"error": f"daemon not reachable: {exc}"}
    return brain_enrich_advance


def make_brain_meetings_today(store, home: str):
    async def brain_meetings_today() -> list:
        """Today's calendar events, each annotated with has_pack. Same data the
        meeting-packs task used to read via curl /api/dashboard/today."""
        from mcpbrain import dashboard
        try:
            return dashboard.annotate_meeting_packs(store, dashboard.calendar_today(home))
        except Exception as exc:  # noqa: BLE001
            _log.exception("brain_meetings_today failed")
            return [{"error": str(exc)}]
    return brain_meetings_today


def make_brain_meeting_pack_get(store):
    async def brain_meeting_pack_get(event_id: str) -> dict:
        """Return the stored pack for event_id (incl. context_hash), or
        {"found": false} when none exists."""
        try:
            return store.get_meeting_pack(event_id) or {"found": False}
        except Exception as exc:  # noqa: BLE001
            _log.exception("brain_meeting_pack_get failed")
            return {"found": False, "error": str(exc)}
    return brain_meeting_pack_get


def make_brain_meeting_pack_upsert(store):
    async def brain_meeting_pack_upsert(event_id: str, event_title: str,
                                        event_date: str, pack_text: str,
                                        attendees: list | None = None,
                                        context_hash: str = "",
                                        cowork_session: str = "meeting-packs") -> dict:
        """Create or update a meeting pack, storing context_hash so the next
        hourly run can skip it when unchanged. Returns {"ok": bool, error?}."""
        if not event_id:
            return {"ok": False, "error": "event_id required"}
        try:
            store.upsert_meeting_pack(
                event_id=event_id, event_title=event_title, event_date=event_date,
                pack_text=pack_text, attendees=attendees or [],
                cowork_session=cowork_session, context_hash=context_hash)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            _log.exception("brain_meeting_pack_upsert failed")
            return {"ok": False, "error": str(exc)}
    return brain_meeting_pack_upsert


def main() -> None:  # stdio entry point, exercised manually + in P3 integration
    import mcp.server.stdio
    from mcp.server import Server
    from mcp import types
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    emb = get_embedder("bge-small")
    _store_path, _store_dim = config.store_path(), emb.dim
    store = Store(_store_path, dim=_store_dim, read_only=True)   # read path: index/graph/email
    search = make_brain_search(store, emb)
    context = make_brain_context(store)
    actions = make_brain_actions(store)
    graph = make_brain_graph(store)
    proactive = make_brain_proactive(store)
    ingest = make_brain_ingest()
    action_create = make_brain_action_create()
    action_update = make_brain_action_update()
    decision = make_brain_decision()
    note = make_brain_note()
    memory_write = make_brain_memory_write()
    gardener_apply = make_brain_gardener_apply(store)
    # Draft tools write to draft_records, so they need a writable store handle.
    # the read-only store cannot INSERT; this writable handle is scoped to draft_records
    # writes by the MCP server (serialised via WAL + busy_timeout).
    draft_store = Store(_store_path, dim=_store_dim, read_only=False)  # draft_records writes
    home = str(config.app_dir())
    write_heartbeat(home)
    draft_context_fn = make_brain_draft_context(draft_store, home)
    draft_save_fn = make_brain_draft_save(draft_store, home)
    # Autonomous-loop tools (host-native). Reads use the RO store; pack upsert
    # needs the writable handle (same one the draft tools use).
    enrich_units = make_brain_enrich_units(home)
    enrich_pull = make_brain_enrich_pull(home)
    enrich_push = make_brain_enrich_push(home)
    enrich_advance = make_brain_enrich_advance(home)
    meetings_today = make_brain_meetings_today(store, home)
    meeting_pack_get = make_brain_meeting_pack_get(store)
    meeting_pack_upsert = make_brain_meeting_pack_upsert(draft_store)
    # Standing instructions read by every session that connects this server —
    # the owner's identity/role/orgs + the brain tools + the capture loop. Rendered
    # from saved config at connect time (so it's never a stale paste), then captured
    # for the life of the connection; a config change is picked up on reconnect.
    server = Server(
        "mcpbrain",
        instructions=config.render_project_instructions(config.read_config(home)),
    )

    @server.list_resources()
    async def _list_resources():
        return await list_context_resources()

    @server.read_resource()
    async def _read_resource(uri):
        from mcp.server.lowlevel.helper_types import ReadResourceContents
        text = await read_context_resource(uri)
        return [ReadResourceContents(content=text, mime_type="text/markdown")]

    @server.list_tools()
    async def _tools():
        return [
            types.Tool(
                name="brain_search",
                description="Search your Gmail/Calendar/Drive index.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="brain_read",
                description="Fetch the full text + metadata of a chunk by doc_id.",
                inputSchema={
                    "type": "object",
                    "properties": {"doc_id": {"type": "string"}},
                    "required": ["doc_id"],
                },
            ),
            types.Tool(
                name="brain_context",
                description=(
                    "Profile an entity or list community clusters. "
                    "mode='profile' (default): entity is required — returns record, relations, "
                    "actions, projects, and areas. "
                    "mode='communities': returns all community summaries, or the member entities "
                    "for a specific community when community_id is supplied."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "mode": {
                            "type": "string",
                            "default": "profile",
                            "enum": ["profile", "communities"],
                        },
                        "community_id": {"type": "integer"},
                    },
                },
            ),
            types.Tool(
                name="brain_actions",
                description="Action items from the unified actions table, filtered by owner + status, with freshness.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string", "default": "",
                                  "description": "Empty defaults to the configured install owner."},
                        "status": {"type": "string", "default": "open"},
                    },
                },
            ),
            types.Tool(
                name="brain_graph",
                description="Traverse the relationship graph from an entity up to `hops` (max 3).",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string"},
                        "hops": {"type": "integer", "default": 1},
                        "at_time": {"type": "string"},
                        "include_invalidated": {"type": "boolean", "default": False},
                    },
                    "required": ["entity"],
                },
            ),
            types.Tool(
                name="brain_proactive",
                description="Open proactive findings: projects without next actions, areas overdue, lint issues.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "finding_type": {
                            "type": "string",
                            "description": "Filter by type (e.g. 'project_no_next_action', 'lint:missing_org')",
                        },
                        "severity": {"type": "string"},
                    },
                },
            ),
            types.Tool(
                name="brain_ingest",
                description=(
                    "Save a note, decision, or memory to your knowledge base. "
                    "QUEUED: the item is searchable after the next sync cycle (~5 min), "
                    "not immediately."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "tags": {"type": "string", "default": ""},
                        "observation_type": {
                            "type": "string",
                            "default": "note",
                            "enum": ["note", "decision", "memory", "reference"],
                        },
                        "org": {"type": "string", "default": ""},
                    },
                    "required": ["title", "content"],
                },
            ),
            types.Tool(
                name="brain_action_create",
                description=(
                    "Create a new action item. "
                    "QUEUED: appears in brain_actions after the next sync cycle (~5 min). "
                    "Empty owner defaults to the configured install owner."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "owner": {"type": "string", "default": ""},
                        "deadline": {"type": "string", "default": ""},
                        "org": {"type": "string", "default": ""},
                        "project_id": {"type": "string", "default": ""},
                        "area_id": {"type": "string", "default": ""},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="brain_action_update",
                description=(
                    "Mark an action done or reopen it. "
                    "QUEUED: applies on the next sync cycle (~5 min)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action_id": {"type": "integer"},
                        "status": {"type": "string", "enum": ["done", "open"]},
                    },
                    "required": ["action_id", "status"],
                },
            ),
            types.Tool(
                name="brain_decision",
                description=(
                    "Record a decision. "
                    "QUEUED: the daemon appends a row to state/decisions.md in your records repo "
                    "and commits (one daemon cycle, ~seconds-minutes), not instantly."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "rationale": {"type": "string", "default": ""},
                        "owner": {"type": "string", "default": ""},
                        "supersedes": {"type": "string", "default": ""},
                        "org": {"type": "string", "default": ""},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="brain_note",
                description=(
                    "Record a continuity note. "
                    "QUEUED: the daemon prepends a dated entry to state/hot.md in your records repo "
                    "and commits (one daemon cycle), not instantly."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                    },
                    "required": ["text"],
                },
            ),
            types.Tool(
                name="brain_memory_write",
                description=(
                    "Write a durable auto-memory file. "
                    "QUEUED: the daemon writes memory/<slug>.md + a MEMORY.md pointer "
                    "in your records repo and commits (one daemon cycle), not instantly."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "description": {"type": "string"},
                        "body": {"type": "string"},
                        "memory_type": {"type": "string", "default": "project"},
                    },
                    "required": ["slug", "description", "body"],
                },
            ),
            types.Tool(
                name="brain_gardener_apply",
                description=(
                    "Apply a reference-gardener change directly to the records repo through "
                    "the role-attribution guard and per-run change cap. Synchronous (not "
                    "queued): commits immediately and returns the result so the gardener gets "
                    "enforcement feedback. Use only from the reference-gardener routine in "
                    "auto-apply mode."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "lane": {"type": "string", "enum": ["reference", "context"],
                                 "description": "'reference' (drift) or 'context' (constitution)"},
                        "filename": {"type": "string", "description": "basename of an existing file in that dir"},
                        "content": {"type": "string", "description": "full new file content"},
                        "asserts_person_role": {"type": "boolean", "default": False,
                                                "description": "True only if assigning a role/title to a person"},
                        "attribution_source": {"type": "string",
                                               "enum": ["owner_statement", "signature", "owner_confirmation"],
                                               "description": "required when asserts_person_role"},
                        "attribution_quote": {"type": "string",
                                              "description": "verbatim supporting text; required for a role claim and verified against the cited source"},
                        "attribution_doc_id": {"type": "string",
                                               "description": "stored chunk id the quote lives in; required for owner_statement/signature"},
                    },
                    "required": ["lane", "filename", "content"],
                },
            ),
            types.Tool(
                name="brain_draft_context",
                description="Get email context for drafting a reply (subject, body, sender, voice rules, thread samples). Returns context dict to use in the draft-reply skill.",
                inputSchema={"type": "object", "properties": {
                    "email_id": {"type": "string", "description": "message_id from email_context"},
                    "intent": {"type": "string", "description": "optional intent override"},
                }, "required": ["email_id"]},
            ),
            types.Tool(
                name="brain_draft_save",
                description="Persist a completed draft to draft history. Call after the Cowork draft-reply skill has finished. Returns draft_record_id.",
                inputSchema={"type": "object", "properties": {
                    "email_id": {"type": "string"},
                    "thread_id": {"type": "string"},
                    "intent": {"type": "string"},
                    "final_draft": {"type": "string", "description": "The finished draft text to save"},
                    "parent_draft_id": {"type": "integer", "description": "optional: id of prior draft being replaced"},
                }, "required": ["email_id", "thread_id", "intent", "final_draft"]},
            ),
            types.Tool(
                name="brain_routine",
                description="Return the full instructions for a recurring mcpbrain routine, to follow verbatim. Use this as the FIRST step of a scheduled task: call it, then do exactly what it returns. name is one of: enrich, meeting-packs, gardener, reference-gardener. Self-contained — do not look for a skill or command or read files.",
                inputSchema={"type": "object", "properties": {
                    "name": {"type": "string", "enum": list(_ROUTINES),
                             "description": "the routine to run"},
                }, "required": ["name"]},
            ),
            types.Tool(
                name="brain_enrich_units",
                description="List ready enrichment work units (descriptors only — unit_id, kind, block, count; NO payloads, so the orchestrator stays context-flat) and claim each with a short lease. FIRST step of the enrich task: call this, then spawn one subagent per unit_id — each calls brain_enrich_pull(unit_id), extracts, and brain_enrich_push(unit_id, …). Returns {\"empty\": true} when the queue is dry. Loop it (with brain_enrich_advance) to drain a backlog.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="brain_enrich_pull",
                description="Fetch one work unit's payload by unit_id (from brain_enrich_units), with a `rules` field carrying the FULL extraction protocol to follow (envelope schema, entity/relation/merge rules) and the standing `context`. A `kind` \"thread\" unit returns `threads`; a `kind` \"block\" unit returns `block` + `items`. Returns {\"empty\": true} if the unit is gone. Follow `rules` from this response; do not read skill files or source.",
                inputSchema={"type": "object", "properties": {
                    "unit_id": {"type": "string",
                                "description": "the unit to fetch (from brain_enrich_units)"},
                }, "required": ["unit_id"]},
            ),
            types.Tool(
                name="brain_enrich_push",
                description="Submit a unit's enrichment result by unit_id → enrich_inbox/<unit_id>.json; the daemon applies it, marks chunks enriched, and deletes the unit. Pass `extractions` (one per thread, for a thread unit) and/or the block answer field for a block unit: merge_answers (merge_review), synthesis / profile_synthesis / community_synthesis / memory_distil / profile_audit.",
                inputSchema={"type": "object", "properties": {
                    "unit_id": {"type": "string", "description": "the unit you pulled (writes enrich_inbox/<unit_id>.json)"},
                    "extractions": {"type": "array", "items": {"type": "object"},
                                    "description": "one extraction object per thread (thread unit)"},
                    "merge_answers": {"type": "array", "items": {"type": "object"},
                                      "description": "answers for a merge_review block unit"},
                    "synthesis": {"type": "array", "items": {"type": "object"},
                                  "description": "answers for a synthesis block unit"},
                    "profile_synthesis": {"type": "array", "items": {"type": "object"},
                                          "description": "answers for a profile_synthesis block unit"},
                    "community_synthesis": {"type": "array", "items": {"type": "object"},
                                            "description": "answers for a community_synthesis block unit"},
                    "memory_distil": {"type": "array", "items": {"type": "object"},
                                      "description": "answers for a memory_distil block unit"},
                    "profile_audit": {"type": "array", "items": {"type": "object"},
                                      "description": "answers for a profile_audit block unit"},
                }, "required": ["unit_id"]},
            ),
            types.Tool(
                name="brain_enrich_advance",
                description="Nudge the daemon to apply pushed unit results and produce the next units immediately (instead of waiting for its normal cycle). Use between backfill rounds, then call brain_enrich_units again.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="brain_meetings_today",
                description="Today's calendar events, each with has_pack. Use in the meeting-packs task instead of curl /api/dashboard/today.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="brain_meeting_pack_get",
                description="Get the stored meeting pack for an event (incl. context_hash for change detection), or {\"found\": false}.",
                inputSchema={"type": "object", "properties": {
                    "event_id": {"type": "string"},
                }, "required": ["event_id"]},
            ),
            types.Tool(
                name="brain_meeting_pack_upsert",
                description="Create or update a meeting pack. Always pass context_hash so the next hourly run can skip it when unchanged.",
                inputSchema={"type": "object", "properties": {
                    "event_id": {"type": "string"},
                    "event_title": {"type": "string"},
                    "event_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "pack_text": {"type": "string", "description": "the markdown pack"},
                    "attendees": {"type": "array", "items": {"type": "string"}},
                    "context_hash": {"type": "string", "description": "fingerprint of the pack's inputs"},
                }, "required": ["event_id", "event_title", "event_date", "pack_text"]},
            ),
        ]

    @server.call_tool()
    async def _call(name, arguments):
        import json
        if name == "brain_read":
            chunk = store.get_chunk(arguments["doc_id"])
            return [types.TextContent(type="text", text=json.dumps(chunk))]
        if name == "brain_context":
            out = await context(
                entity=arguments.get("entity", ""),
                mode=arguments.get("mode", "profile"),
                community_id=arguments.get("community_id"),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_actions":
            # null-coalesce: explicit None/empty defaults to the configured owner
            owner = arguments.get("owner") or _default_owner()
            if not owner:
                return [types.TextContent(type="text", text='[{"error": "Install not configured: set owner_name in config.json"}]')]
            status = arguments.get("status") or "open"
            out = await actions(owner, status)
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_graph":
            out = await graph(arguments["entity"], arguments.get("hops", 1),
                              at_time=arguments.get("at_time"),
                              include_invalidated=arguments.get("include_invalidated", False))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_proactive":
            out = await proactive(arguments.get("finding_type", ""), arguments.get("severity", ""))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_ingest":
            out = await ingest(
                title=arguments.get("title", ""),
                content=arguments.get("content", ""),
                tags=arguments.get("tags", ""),
                observation_type=arguments.get("observation_type", "note"),
                org=arguments.get("org", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_action_create":
            out = await action_create(
                text=arguments.get("text", ""),
                owner=arguments.get("owner") or _default_owner(),
                deadline=arguments.get("deadline", ""),
                org=arguments.get("org", ""),
                project_id=arguments.get("project_id", ""),
                area_id=arguments.get("area_id", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_action_update":
            out = await action_update(
                action_id=arguments.get("action_id", 0),
                status=arguments.get("status", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_decision":
            out = await decision(
                text=arguments.get("text", ""),
                rationale=arguments.get("rationale", ""),
                owner=arguments.get("owner") or _default_owner(),
                supersedes=arguments.get("supersedes", ""),
                org=arguments.get("org", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_note":
            out = await note(
                text=arguments.get("text", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_memory_write":
            out = await memory_write(
                slug=arguments.get("slug", ""),
                description=arguments.get("description", ""),
                body=arguments.get("body", ""),
                memory_type=arguments.get("memory_type", "project"),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_gardener_apply":
            out = await gardener_apply(
                lane=arguments.get("lane", ""),
                filename=arguments.get("filename", ""),
                content=arguments.get("content", ""),
                asserts_person_role=bool(arguments.get("asserts_person_role", False)),
                attribution_source=arguments.get("attribution_source", ""),
                attribution_quote=arguments.get("attribution_quote", ""),
                attribution_doc_id=arguments.get("attribution_doc_id", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_draft_context":
            out = await draft_context_fn(
                email_id=arguments.get("email_id", ""),
                intent=arguments.get("intent", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_draft_save":
            out = await draft_save_fn(
                email_id=arguments.get("email_id", ""),
                thread_id=arguments.get("thread_id", ""),
                intent=arguments.get("intent", ""),
                final_draft=arguments.get("final_draft", ""),
                parent_draft_id=arguments.get("parent_draft_id"),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_routine":
            rname = (arguments or {}).get("name", "")
            instructions = _routine_instructions(rname)
            out = ({"name": rname, "instructions": instructions} if instructions
                   else {"error": f"unknown routine {rname!r}", "available": list(_ROUTINES)})
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_enrich_units":
            out = await enrich_units()
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_enrich_pull":
            out = await enrich_pull(unit_id=arguments.get("unit_id", ""))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_enrich_push":
            # Do NOT coerce extractions=None to [] here — the handler must see None
            # when the field is absent so the block-unit vs thread-unit guard works.
            out = await enrich_push(
                unit_id=arguments.get("unit_id", ""),
                extractions=arguments.get("extractions"),  # None if absent; validated in handler
                merge_answers=arguments.get("merge_answers") or [],
                **{k: arguments[k] for k in _ENRICH_ANSWER_BLOCKS if arguments.get(k)},
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_enrich_advance":
            out = await enrich_advance()
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_meetings_today":
            out = await meetings_today()
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_meeting_pack_get":
            out = await meeting_pack_get(arguments.get("event_id", ""))
            return [types.TextContent(type="text", text=json.dumps(out))]
        if name == "brain_meeting_pack_upsert":
            out = await meeting_pack_upsert(
                event_id=arguments.get("event_id", ""),
                event_title=arguments.get("event_title", ""),
                event_date=arguments.get("event_date", ""),
                pack_text=arguments.get("pack_text", ""),
                attendees=arguments.get("attendees") or [],
                context_hash=arguments.get("context_hash", ""),
            )
            return [types.TextContent(type="text", text=json.dumps(out))]
        results = await search(arguments["query"], arguments.get("limit", 10))
        return [types.TextContent(type="text", text=json.dumps(results))]

    async def _run():
        async with mcp.server.stdio.stdio_server() as (r, w):
            await server.run(r, w, server.create_initialization_options())

    import asyncio
    asyncio.run(_run())


if __name__ == "__main__":  # spawnable: python -m mcpbrain.mcp_server
    main()
