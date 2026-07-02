"""Applies an AI adjudicator's verdicts on orphan-entity and missing-org
findings unattended on a daily cadence — a capped, conservative loop: only
'suppress'/'assign' mutate the graph (reversibly, via store.suppress_entity /
store.update_entity_org), any unrecognised verdict is a no-op 'skip' so
ambiguity never turns into an unattended mutation."""

from mcpbrain import orgs


def apply_orphan_verdicts(store, verdicts: list[dict], *, cap: int) -> dict:
    """Apply a batch of orphan-finding verdicts.

    Each verdict: {"finding_id": int, "ref_id": <entity id>, "verdict":
    "suppress"|"keep"|"skip", "reason"?: str}.

    - "suppress": store.suppress_entity(ref_id, reason) then resolve the
      finding. Capped: once `cap` suppressions have been applied in this call,
      further suppress verdicts are left untouched (entity not suppressed,
      finding left open) so they're picked up again next run. If
      suppress_entity returns False (ref_id no longer names a real entity —
      e.g. merged/renamed away between detection and verdict), the finding is
      left open (not resolved) rather than counted as a success; it'll stop
      being re-detected as an orphan on its own once the entity is truly gone,
      and resolve via the normal resolve_findings_not_in cleanup.
    - "keep" / "skip" / anything unrecognised: resolve the finding with no
      graph mutation. Unrecognised strings are treated as "skip" — the safe
      default when this loop can't tell what the adjudicator meant.

    Returns {"suppressed": n, "kept": n, "skipped": n, "capped": n, "missing": n}.
    """
    result = {"suppressed": 0, "kept": 0, "skipped": 0, "capped": 0, "missing": 0}
    for verdict in verdicts:
        finding_id = verdict.get("finding_id")
        ref_id = verdict.get("ref_id")
        verdict_str = verdict.get("verdict")

        if verdict_str == "suppress":
            if result["suppressed"] >= cap:
                result["capped"] += 1
                continue
            if store.suppress_entity(ref_id, reason=verdict.get("reason", "")):
                store.resolve_finding(finding_id)
                result["suppressed"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "keep":
            store.resolve_finding(finding_id)
            result["kept"] += 1
        else:
            # "skip" and any unrecognised verdict string.
            store.resolve_finding(finding_id)
            result["skipped"] += 1

    return result


def apply_missing_org_verdicts(
    store, verdicts: list[dict], *, cap: int, home: str | None = None
) -> dict:
    """Apply a batch of missing-org-finding verdicts.

    Each verdict: {"finding_id": int, "ref_id": <entity id>, "verdict":
    "assign"|"external"|"skip", "org"?: str}.

    - "assign": only applied when `org` is present AND is one of this
      install's configured taxonomy names (`orgs.taxonomy_from_config(home)
      .valid_orgs`). When valid: store.update_entity_org(ref_id, org) then
      resolve the finding. Capped: once `cap` assignments have been applied in
      this call, further assign verdicts are left untouched (org not set,
      finding left open) so they're picked up again next run.
      When `org` is missing or not in the taxonomy, the assign is NOT applied
      — a bad adjudicator output (hallucinated org) must never masquerade as
      a successful assignment — and it falls through to the same "skip"
      handling as an unrecognised verdict: the finding is resolved (it's a
      no-op either way) but counted under "skipped", not "assigned".
      If store.update_entity_org returns False (ref_id no longer names a real
      entity — e.g. merged/renamed away between detection and verdict), the
      finding is left open (not resolved) rather than counted as a success;
      it's tallied under "missing" instead.
    - "external" / "skip" / anything unrecognised: resolve the finding with
      no graph mutation. Unrecognised strings are treated as "skip" — the
      safe default when this loop can't tell what the adjudicator meant.

    Returns {"assigned": n, "external": n, "skipped": n, "capped": n, "missing": n}.
    """
    valid_orgs = orgs.taxonomy_from_config(home).valid_orgs
    result = {"assigned": 0, "external": 0, "skipped": 0, "capped": 0, "missing": 0}
    for verdict in verdicts:
        finding_id = verdict.get("finding_id")
        ref_id = verdict.get("ref_id")
        verdict_str = verdict.get("verdict")
        org = verdict.get("org")

        if verdict_str == "assign" and org and org in valid_orgs:
            if result["assigned"] >= cap:
                result["capped"] += 1
                continue
            if store.update_entity_org(ref_id, org):
                store.resolve_finding(finding_id)
                result["assigned"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "external":
            store.resolve_finding(finding_id)
            result["external"] += 1
        else:
            # "skip", an "assign" with a missing/invalid org, and any
            # unrecognised verdict string.
            store.resolve_finding(finding_id)
            result["skipped"] += 1

    return result
