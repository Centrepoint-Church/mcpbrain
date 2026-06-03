"""The extraction JSON contract: one validator both halves of the enrich loop import.

The prepare/extractor side produces the envelope; the drain/apply side consumes
it. This module is the single seam that pins the shape, so the two cannot drift.

The validator is strict on structure (required keys, correct types, enum
membership) and lenient on optionality: fields that the contract describes as
"or empty" are allowed to be absent or empty. Validation only: the input dict
is never mutated. Pure stdlib, no third-party imports.

`_VALID_ORGS` and `_VALID_CONTENT_TYPES` are imported from `enrich` rather than
re-declared so each enum has a single owner and the gate can't diverge from it.
"""

from mcpbrain.enrich import _VALID_CONTENT_TYPES, _VALID_ORGS


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

    # org: required, in enum.
    org = d.get("org")
    if org not in _VALID_ORGS:
        problems.append(f"org must be one of {sorted(_VALID_ORGS)}, got {org!r}")

    # content_type: required, in enum.
    content_type = d.get("content_type")
    if content_type not in _VALID_CONTENT_TYPES:
        problems.append(
            f"content_type must be one of {sorted(_VALID_CONTENT_TYPES)}, got {content_type!r}"
        )

    # summary: required str (may be empty string, but must be present and a str).
    if not isinstance(d.get("summary"), str):
        problems.append("summary must be a string")

    # messages: required, non-empty list; each with message_id/sender/date.
    messages = d.get("messages")
    if not isinstance(messages, list):
        problems.append("messages must be a list")
    elif not messages:
        problems.append("messages must be a non-empty list")
    else:
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                problems.append(f"messages[{i}] must be an object")
                continue
            for field in ("message_id", "sender", "date"):
                value = msg.get(field)
                if not isinstance(value, str) or not value.strip():
                    problems.append(f"messages[{i}].{field} must be a non-empty string")

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

    # Optional fields (contextual_summary, updated_actions, reply_*) are intentionally not deep-validated -- lenient on optionality.
    return problems


def validate_batch_file(d: object) -> list[str]:
    """Validate the inbox batch-file wrapper.

    Shape: {"batch_id": str, "extractions": [<envelope>, ...], "merge_answers": [...]}.
    Validates the wrapper, then each extraction by index so a failure names which
    one. Returns a list of human-readable problems; empty means valid. Input is
    not mutated.
    """
    problems: list[str] = []

    if not isinstance(d, dict):
        return ["batch file must be a JSON object"]

    batch_id = d.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        problems.append("batch_id must be a non-empty string")

    extractions = d.get("extractions")
    if not isinstance(extractions, list):
        problems.append("extractions must be a list")
    else:
        for i, extraction in enumerate(extractions):
            for problem in validate_extraction(extraction):
                problems.append(f"extractions[{i}]: {problem}")

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
