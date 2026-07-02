"""Applies an AI adjudicator's verdicts on orphan-entity findings unattended
on a daily cadence — a capped, conservative loop: only 'suppress' mutates the
graph (reversibly, via store.suppress_entity), any unrecognised verdict is a
no-op 'skip' so ambiguity never turns into an unattended mutation."""


def apply_orphan_verdicts(store, verdicts: list[dict], *, cap: int) -> dict:
    """Apply a batch of orphan-finding verdicts.

    Each verdict: {"finding_id": int, "ref_id": <entity id>, "verdict":
    "suppress"|"keep"|"skip", "reason"?: str}.

    - "suppress": store.suppress_entity(ref_id, reason) then resolve the
      finding. Capped: once `cap` suppressions have been applied in this call,
      further suppress verdicts are left untouched (entity not suppressed,
      finding left open) so they're picked up again next run.
    - "keep" / "skip" / anything unrecognised: resolve the finding with no
      graph mutation. Unrecognised strings are treated as "skip" — the safe
      default when this loop can't tell what the adjudicator meant.

    Returns {"suppressed": n, "kept": n, "skipped": n, "capped": n}.
    """
    result = {"suppressed": 0, "kept": 0, "skipped": 0, "capped": 0}
    for verdict in verdicts:
        finding_id = verdict.get("finding_id")
        ref_id = verdict.get("ref_id")
        verdict_str = verdict.get("verdict")

        if verdict_str == "suppress":
            if result["suppressed"] >= cap:
                result["capped"] += 1
                continue
            store.suppress_entity(ref_id, reason=verdict.get("reason", ""))
            store.resolve_finding(finding_id)
            result["suppressed"] += 1
        elif verdict_str == "keep":
            store.resolve_finding(finding_id)
            result["kept"] += 1
        else:
            # "skip" and any unrecognised verdict string.
            store.resolve_finding(finding_id)
            result["skipped"] += 1

    return result
