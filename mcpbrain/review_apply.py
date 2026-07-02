"""Applies an AI adjudicator's verdicts on orphan-entity and missing-org
findings unattended on a daily cadence — a capped, conservative loop: only
'suppress'/'assign' mutate the graph (reversibly, via store.suppress_entity /
store.update_entity_org), any unrecognised verdict is a no-op 'skip' so
ambiguity never turns into an unattended mutation."""

from mcpbrain import orgs
from mcpbrain.resolve import _NAME_MERGEABLE_TYPES


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


def apply_ownerless_verdicts(store, verdicts: list[dict], *, cap: int) -> dict:
    """Apply a batch of ownerless-action-finding verdicts.

    Each verdict: {"finding_id": int, "ref_id": <action id>, "verdict":
    "owner"|"waiting_on"|"unowned"|"skip", "owner"?: str, "owner_entity_id"?: str}.

    - "owner": only applied when `owner` (a name string) is present. When
      present: store.assign_action_owner(ref_id, owner, owner_entity_id=...)
      then resolve the finding. Capped: once `cap` owner assignments have been
      applied in this call, further owner verdicts are left untouched (owner
      not set, finding left open) so they're picked up again next run.
      If assign_action_owner returns False (ref_id no longer names a real
      action — e.g. resolved/deleted between detection and verdict), the
      finding is left open (not resolved) rather than counted as a success;
      it's tallied under "missing" instead.
      An "owner" verdict with no `owner` field falls through to the same
      "skip" handling as an unrecognised verdict — a bad adjudicator output
      (no name) must never masquerade as a successful assignment.
    - "waiting_on" / "unowned" / "skip": resolve the finding with no owner
      change. Each is counted under its own key so the verdict mix stays
      visible (mirrors Task 2.2's `external`-gets-its-own-counter precedent).
    - Any other/unrecognised verdict string, or an "owner" verdict missing
      the `owner` field: treated as "skip" (conservative default) — resolve,
      count under "skipped".

    Returns {"owner_assigned": n, "waiting_on": n, "unowned": n, "skipped": n,
    "capped": n, "missing": n}.
    """
    result = {"owner_assigned": 0, "waiting_on": 0, "unowned": 0, "skipped": 0, "capped": 0, "missing": 0}
    for verdict in verdicts:
        finding_id = verdict.get("finding_id")
        ref_id = verdict.get("ref_id")
        verdict_str = verdict.get("verdict")
        owner = verdict.get("owner")

        if verdict_str == "owner" and owner:
            if result["owner_assigned"] >= cap:
                result["capped"] += 1
                continue
            if store.assign_action_owner(ref_id, owner, owner_entity_id=verdict.get("owner_entity_id", "")):
                store.resolve_finding(finding_id)
                result["owner_assigned"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "waiting_on":
            store.resolve_finding(finding_id)
            result["waiting_on"] += 1
        elif verdict_str == "unowned":
            store.resolve_finding(finding_id)
            result["unowned"] += 1
        else:
            # "skip", an "owner" verdict with a missing `owner` field, and any
            # unrecognised verdict string.
            store.resolve_finding(finding_id)
            result["skipped"] += 1

    return result


def apply_org_verdicts(
    store, verdicts: list[dict], *, cap: int, home: str | None = None
) -> dict:
    """Apply a batch of org-hygiene-finding verdicts, bundled across THREE
    finding kinds that have DIFFERENT `ref_id` semantics (see mcpbrain/
    lint_graph.py's check_ambiguous_org/check_duplicate_orgs and
    mcpbrain/drain.py's org-drift gate around line ~416):

    - lint:ambiguous_org: `ref_id` is a real ENTITY id (an entity tagged
      org='external' whose email domain actually maps to a configured org).
    - lint:duplicate_org: `ref_id` is the variant ORG STRING itself, not an
      entity id.
    - org_unrecognised: `ref_id` is the raw unrecognised org string; there is
      no "canonical" target for this kind, only a suggestion to record it.

    Each verdict: {"finding_id": int, "finding_type": "lint:ambiguous_org"|
    "lint:duplicate_org"|"org_unrecognised", "ref_id": str, "verdict":
    "canonicalize"|"add_to_config"|"skip", "canonical_org"?: str}.

    - "canonicalize" on lint:ambiguous_org: only applied when `canonical_org`
      is present AND is one of this install's configured taxonomy names
      (`orgs.taxonomy_from_config(home).valid_orgs` — same pattern as Task
      2.2's apply_missing_org_verdicts). When valid:
      store.update_entity_org(ref_id, canonical_org) then resolve the
      finding, count "canonicalized". If update_entity_org returns False
      (ref_id no longer names a real entity — merged/renamed away between
      detection and verdict), the finding is left open and tallied under
      "missing" instead.
    - "canonicalize" on lint:duplicate_org: only applied when `canonical_org`
      is present AND in the taxonomy. `ref_id` is the variant org string, not
      an entity id, so this looks up an ORG-TYPED entity by name for BOTH
      `ref_id` (the variant) and `canonical_org` (the target). store.
      merge_entities has no return value and silently no-ops on a missing or
      equal id, so existence and distinctness are verified here BEFORE
      calling it — merge_entities' silent no-op can never be mistaken for a
      successful merge. If both org-typed entities exist, have different
      ids, and "org" is one of resolve._NAME_MERGEABLE_TYPES (org-typed rows
      trivially satisfy this since the lookup already filtered type='org';
      checked explicitly anyway as a defensive guard matching this
      codebase's established safety pattern for any entity merge): store.
      merge_entities(loser_id=<variant>, winner_id=<canonical>,
      canonical_name=canonical_org, method="review_org_canonicalize"), then
      resolve the finding, count "canonicalized". Otherwise (either entity
      missing, or the two ids are the same) there is nothing safe to do yet:
      the finding is left open, tallied under "missing".
    - "add_to_config" on org_unrecognised: store.suggest_org_mapping(ref_id,
      reason=...) then resolve the finding, count "suggested". This writes
      only to the org_suggestions table — NEVER to config.json — a
      suggestion record only, inspectable later, never auto-applied.
      "add_to_config" on any other finding_type is not a defined transition
      and falls through to "skip" handling below.
    - "skip" (any finding_type): resolve the finding, no mutation, count
      "skipped".
    - Any other/unrecognised verdict string, or a "canonicalize" with a
      missing/invalid `canonical_org`: treated as "skip" — resolve, count
      "skipped". A bad adjudicator output (hallucinated org) must never
      masquerade as a successful canonicalization.

    Capped: the three mutating outcomes (ambiguous_org canonicalize,
    duplicate_org canonicalize, org_unrecognised add_to_config) all draw from
    ONE shared budget of `cap` per call — mirroring the single `capped`
    counter in the return shape and the single `cap=50` this whole bundled
    "review_org" block is registered with in drain.py's BLOCK_DRAINERS. Once
    `result["canonicalized"] + result["suggested"]` reaches `cap`, further
    mutating verdicts are left untouched (finding left open) so they're
    picked up again next run; "missing" and "skipped" outcomes never consume
    budget.

    Returns {"canonicalized": n, "suggested": n, "skipped": n, "capped": n,
    "missing": n}.
    """
    valid_orgs = orgs.taxonomy_from_config(home).valid_orgs
    result = {"canonicalized": 0, "suggested": 0, "skipped": 0, "capped": 0, "missing": 0}

    def _budget_used() -> int:
        return result["canonicalized"] + result["suggested"]

    for verdict in verdicts:
        finding_id = verdict.get("finding_id")
        finding_type = verdict.get("finding_type")
        ref_id = verdict.get("ref_id")
        verdict_str = verdict.get("verdict")
        canonical_org = verdict.get("canonical_org")

        if verdict_str == "canonicalize" and canonical_org and canonical_org in valid_orgs \
                and finding_type == "lint:ambiguous_org":
            if _budget_used() >= cap:
                result["capped"] += 1
                continue
            if store.update_entity_org(ref_id, canonical_org):
                store.resolve_finding(finding_id)
                result["canonicalized"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "canonicalize" and canonical_org and canonical_org in valid_orgs \
                and finding_type == "lint:duplicate_org":
            if _budget_used() >= cap:
                result["capped"] += 1
                continue
            with store._connect() as db:
                variant_row = db.execute(
                    "SELECT id FROM entities WHERE type='org' AND name=?", (ref_id,)
                ).fetchone()
                canon_row = db.execute(
                    "SELECT id FROM entities WHERE type='org' AND name=?", (canonical_org,)
                ).fetchone()
            merged = False
            if (variant_row and canon_row and variant_row["id"] != canon_row["id"]
                    and "org" in _NAME_MERGEABLE_TYPES):
                store.merge_entities(
                    loser_id=variant_row["id"], winner_id=canon_row["id"],
                    canonical_name=canonical_org, method="review_org_canonicalize")
                merged = True
            if merged:
                store.resolve_finding(finding_id)
                result["canonicalized"] += 1
            else:
                result["missing"] += 1
        elif verdict_str == "add_to_config" and finding_type == "org_unrecognised":
            if _budget_used() >= cap:
                result["capped"] += 1
                continue
            store.suggest_org_mapping(ref_id, reason=verdict.get("reason", ""))
            store.resolve_finding(finding_id)
            result["suggested"] += 1
        else:
            # "skip", "add_to_config" on a finding_type it isn't defined for,
            # a "canonicalize" with a missing/invalid `canonical_org`, and any
            # unrecognised verdict string.
            store.resolve_finding(finding_id)
            result["skipped"] += 1

    return result
