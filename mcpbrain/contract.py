"""The extraction JSON contract: one validator both halves of the enrich loop import.

The prepare/extractor side produces the envelope; the drain/apply side consumes
it. This module is the single seam that pins the shape, so the two cannot drift.

The validator is strict on structure (required keys, correct types, enum
membership) and lenient on optionality: fields that the contract describes as
"or empty" are allowed to be absent or empty. Validation only: the input dict
is never mutated.

Org handling is split in two:
  - validate_extraction checks org is a non-empty STRING (structural). The org
    value set is config-driven (orgs.OrgTaxonomy), and an extractor returning
    an unconfigured org is recoverable drift, not a structural violation — a
    whole thread's entities and actions should not be quarantined over a label.
  - normalise_org (called by drain after validation) canonicalises the value
    through the taxonomy's aliases and coerces anything still unrecognised to
    "unknown", returning the raw value so drain can record a proactive finding
    ("org X keeps appearing — add it to config?"). That is the taxonomy-growth
    loop: drift is absorbed, observed, and feeds config evolution.

`_VALID_CONTENT_TYPES` is imported from `enrich` rather than re-declared so the
enum has a single owner and the gate can't diverge from it.
"""

from mcpbrain import orgs
from mcpbrain.chunking import _VALID_CONTENT_TYPES

# Closed entity type set. Extractors must return one of these; sanitize_extraction
# drops any entity whose type is not in this set. Kept as a frozenset so callers
# can use 'in' without a function call.
ENTITY_TYPES: frozenset[str] = frozenset({"person", "org", "project"})

# Closed relation type set. Matches VALID_RELATION_TYPES in graph_write.py; defined
# here so contract.py can sanitize off-schema relation types before apply() ever
# sees them — earlier rejection than the apply-time filter.
RELATION_TYPES: frozenset[str] = frozenset({
    "works_at", "reports_to", "manages", "coordinates_with", "mentioned_with",
})


def validate_extraction(d: object) -> list[str]:
    """Validate one extraction envelope. Returns a list of human-readable problems.

    An empty list means valid. The input dict is not mutated.
    """
    problems: list[str] = []

    if not isinstance(d, dict):
        return ["extraction must be a JSON object"]

    # thread_id: required, non-empty str.
    thread_id = d.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        problems.append("thread_id must be a non-empty string")

    # org: required, non-empty string. Enum membership is NOT checked here —
    # see the module head: normalise_org coerces unconfigured values after
    # validation instead of quarantining the extraction.
    org = d.get("org")
    if not isinstance(org, str) or not org.strip():
        problems.append(f"org must be a non-empty string, got {org!r}")

    # content_type: required, in enum.
    content_type = d.get("content_type")
    if content_type not in _VALID_CONTENT_TYPES:
        problems.append(
            f"content_type must be one of {sorted(_VALID_CONTENT_TYPES)}, got {content_type!r}"
        )

    # summary: required str (may be empty string, but must be present and a str).
    if not isinstance(d.get("summary"), str):
        problems.append("summary must be a string")

    # messages: OPTIONAL — the daemon attaches the canonical messages from the unit
    # it built (sender/date/message_id are system-owned, not model output). Validate
    # shape only if the model included them.
    messages = d.get("messages")
    if messages is not None:
        if not isinstance(messages, list):
            problems.append("messages, when present, must be a list")
        else:
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    problems.append(f"messages[{i}] must be an object")

    # entities / actions / relations / topics: must be lists (may be empty).
    for field in ("entities", "actions", "relations", "topics"):
        if not isinstance(d.get(field), list):
            problems.append(f"{field} must be a list")

    # Each action needs a non-empty description.
    actions = d.get("actions")
    if isinstance(actions, list):
        for i, action in enumerate(actions):
            if not isinstance(action, dict):
                problems.append(f"actions[{i}] must be an object")
                continue
            description = action.get("description")
            if not isinstance(description, str) or not description.strip():
                problems.append(f"actions[{i}].description must be a non-empty string")

    # Each relation needs source_name / type / target_name.
    relations = d.get("relations")
    if isinstance(relations, list):
        for i, relation in enumerate(relations):
            if not isinstance(relation, dict):
                problems.append(f"relations[{i}] must be an object")
                continue
            for field in ("source_name", "type", "target_name"):
                value = relation.get(field)
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"relations[{i}].{field} must be a non-empty string")

    # resolved_action_ids: optional list; when present, every entry must be int.
    # bool is a subclass of int, so reject it explicitly.
    resolved = d.get("resolved_action_ids")
    if resolved is not None:
        if not isinstance(resolved, list):
            problems.append("resolved_action_ids must be a list")
        else:
            for i, rid in enumerate(resolved):
                if not isinstance(rid, int) or isinstance(rid, bool):
                    problems.append(f"resolved_action_ids[{i}] must be an integer")

    # Empty extraction guard (Q8): an extraction where entities, relations,
    # actions, and topics are all empty AND summary is blank/trivial gives the
    # graph nothing. Treat it as invalid so drain skips it without calling
    # mark_enriched — the unit stays re-queueable for a better extraction.
    if isinstance(d.get("entities"), list) and isinstance(d.get("relations"), list) \
            and isinstance(d.get("actions"), list) and isinstance(d.get("topics"), list):
        all_empty = (not d["entities"] and not d["relations"]
                     and not d["actions"] and not d["topics"])
        trivial_summary = not (d.get("summary") or "").strip()
        if all_empty and trivial_summary:
            problems.append("extraction has no content (all lists empty and summary blank)")

    # Optional fields (contextual_summary, updated_actions, reply_*) are intentionally not deep-validated -- lenient on optionality.
    return problems


def normalise_org(extraction: dict, taxonomy: "orgs.OrgTaxonomy | None" = None) -> str | None:
    """Canonicalise the thread org in place; coerce unrecognised values to "unknown".

    Runs AFTER validate_extraction (org is known to be a non-empty string).
    Returns the raw unrecognised value when a coercion happened (so the caller
    can record a proactive finding), or None when the org was already valid.
    """
    if taxonomy is None:
        taxonomy = orgs.taxonomy_from_config()
    raw = extraction.get("org", "")
    resolved = taxonomy.canonical(raw)
    if resolved in taxonomy.valid_orgs:
        if resolved != raw:
            extraction["org"] = resolved
        return None
    extraction["org"] = "unknown"
    return raw


def validate_batch_wrapper(d: object) -> list[str]:
    """Validate only the batch-file *wrapper* — everything validate_batch_file
    checks except the per-extraction contents.

    Used by the tolerant drain path: a wrapper or merge_answers problem is
    structural (or risks an irreversible mis-merge) and quarantines the whole
    file, but a single malformed extraction is dropped individually rather than
    failing the batch. Returns a list of human-readable problems; empty = valid.
    """
    problems: list[str] = []

    if not isinstance(d, dict):
        return ["batch file must be a JSON object"]

    # The file is identified by EITHER a batch_id (legacy fan-out) or a unit_id
    # (work-queue). Exactly one must be a non-empty string.
    ident = d.get("unit_id") if d.get("unit_id") is not None else d.get("batch_id")
    if not isinstance(ident, str) or not ident.strip():
        problems.append("a non-empty batch_id or unit_id is required")

    if not isinstance(d.get("extractions"), list):
        problems.append("extractions must be a list")

    # merge_answers: optional list. Each entry is validated here, not downstream:
    # a merge collapses two entities irreversibly, so a malformed answer must
    # quarantine the whole file rather than slip through a truthiness check and
    # mis-merge (e.g. "same": "false", a truthy string).
    merge_answers = d.get("merge_answers")
    if merge_answers is not None:
        if not isinstance(merge_answers, list):
            problems.append("merge_answers must be a list")
        else:
            for i, ans in enumerate(merge_answers):
                if not isinstance(ans, dict):
                    problems.append(f"merge_answers[{i}]: must be an object")
                    continue
                if not isinstance(ans.get("pair_id"), str) or not ans["pair_id"].strip():
                    problems.append(f"merge_answers[{i}]: pair_id must be a non-empty string")
                if not isinstance(ans.get("same"), bool):
                    problems.append(f"merge_answers[{i}]: same must be a boolean (true/false)")
                if "canonical" in ans and not isinstance(ans["canonical"], str):
                    problems.append(f"merge_answers[{i}]: canonical must be a string")

    # synthesis: optional list of {thread_id, contextual_summary|summary}.
    synthesis = d.get("synthesis")
    if synthesis is not None:
        if not isinstance(synthesis, list):
            problems.append("synthesis must be a list")
        else:
            for i, item in enumerate(synthesis):
                if not isinstance(item, dict):
                    problems.append(f"synthesis[{i}]: must be an object")
                    continue
                if not isinstance(item.get("thread_id"), str) or not item["thread_id"].strip():
                    problems.append(f"synthesis[{i}]: thread_id must be a non-empty string")

    return problems


def validate_batch_file(d: object) -> list[str]:
    """Validate the inbox batch-file wrapper AND every extraction by index.

    Returns a list of human-readable problems; empty means valid. Input is not
    mutated. The strict whole-file view; the drain consumer uses the tolerant
    wrapper + per-extraction path instead (sanitize_batch + validate_extraction).
    """
    problems = validate_batch_wrapper(d)
    if isinstance(d, dict) and isinstance(d.get("extractions"), list):
        for i, extraction in enumerate(d["extractions"]):
            for problem in validate_extraction(extraction):
                problems.append(f"extractions[{i}]: {problem}")
    return problems


def sanitize_extraction(d: object) -> tuple[object, int]:
    """Drop droppable LLM noise from one extraction; return (cleaned, dropped).

    Removes:
    - relations missing a non-empty source_name/type/target_name
    - relations whose type is not in RELATION_TYPES (off-schema)
    - actions missing a non-empty description
    - entities whose type is not in ENTITY_TYPES (off-schema)

    These are list items the contract treats as individually invalid; dropping
    them lets the rest of an otherwise-good extraction apply. Non-dict input and
    structural fields (thread_id, messages, org…) are left untouched. Returns a
    shallow copy; the input is not mutated.
    """
    if not isinstance(d, dict):
        return d, 0
    out = dict(d)
    dropped = 0

    ents = out.get("entities")
    if isinstance(ents, list):
        kept = [e for e in ents if not (isinstance(e, dict) and
                e.get("type") and e["type"] not in ENTITY_TYPES)]
        dropped += len(ents) - len(kept)
        out["entities"] = kept

    rels = out.get("relations")
    if isinstance(rels, list):
        kept = [r for r in rels if isinstance(r, dict) and all(
            isinstance(r.get(f), str) and r.get(f).strip()
            for f in ("source_name", "type", "target_name"))
            and r.get("type") in RELATION_TYPES]
        dropped += len(rels) - len(kept)
        out["relations"] = kept

    acts = out.get("actions")
    if isinstance(acts, list):
        kept = [a for a in acts if isinstance(a, dict)
                and isinstance(a.get("description"), str) and a["description"].strip()]
        dropped += len(acts) - len(kept)
        out["actions"] = kept

    return out, dropped


def sanitize_batch(d: object) -> tuple[object, int]:
    """Apply sanitize_extraction to every extraction; return (cleaned, dropped).

    Non-dict input or a missing/!list extractions field is returned unchanged
    (the wrapper validator will catch those structural problems).
    """
    if not isinstance(d, dict) or not isinstance(d.get("extractions"), list):
        return d, 0
    out = dict(d)
    total = 0
    cleaned = []
    for extraction in d["extractions"]:
        ce, n = sanitize_extraction(extraction)
        cleaned.append(ce)
        total += n
    out["extractions"] = cleaned
    return out, total


_CAPTURE_KINDS = {"ingest", "action_create", "action_update", "decision", "continuity", "memory"}
_OBSERVATION_TYPES = {"memory", "decision", "note", "reference"}
_ACTION_STATUSES = {"open", "done"}


def validate_capture(d: object) -> list[str]:
    """Validate one capture envelope (the MCP write tools' spool format).

    Structural only, same philosophy as validate_extraction: an empty list
    means valid. The daemon drain quarantines envelopes that fail.
    captured_at and source are intentionally not validated — the MCP write
    tools always stamp them via _capture_envelope; the drain treats them as
    optional metadata.
    """
    if not isinstance(d, dict):
        return ["capture envelope must be a JSON object with a valid kind"]
    problems: list[str] = []
    kind = d.get("kind")
    if kind not in _CAPTURE_KINDS:
        problems.append(f"kind must be one of {sorted(_CAPTURE_KINDS)}, got {kind!r}")
        return problems
    if kind == "ingest":
        for field in ("title", "content"):
            v = d.get(field)
            if not isinstance(v, str) or not v.strip():
                problems.append(f"{field} must be a non-empty string")
        ot = d.get("observation_type", "note") or "note"
        if ot not in _OBSERVATION_TYPES:
            problems.append(
                f"observation_type must be one of {sorted(_OBSERVATION_TYPES)}, got {ot!r}")
    elif kind == "action_create":
        v = d.get("text")
        if not isinstance(v, str) or not v.strip():
            problems.append("text must be a non-empty string")
    elif kind == "action_update":
        aid = d.get("action_id")
        if not isinstance(aid, int) or isinstance(aid, bool):
            problems.append("action_id must be an integer")
        elif aid <= 0:
            problems.append("action_id must be a positive integer")
        if d.get("status") not in _ACTION_STATUSES:
            problems.append(f"status must be one of {sorted(_ACTION_STATUSES)}")
    elif kind == "decision":
        v = d.get("text")
        if not isinstance(v, str) or not v.strip():
            problems.append("text must be a non-empty string")
    elif kind == "continuity":
        v = d.get("text")
        if not isinstance(v, str) or not v.strip():
            problems.append("text must be a non-empty string")
    elif kind == "memory":
        for field in ("slug", "body"):
            v = d.get(field)
            if not isinstance(v, str) or not v.strip():
                problems.append(f"{field} must be a non-empty string")
    # Cross-kind contamination: action_id on a non-action_update envelope is
    # almost certainly a client bug (misrouted envelope). Quarantine rather than
    # silently create a note when the intent was to update an action.
    if kind != "action_update" and "action_id" in d:
        problems.append("action_id is only valid on action_update envelopes")
    return problems
