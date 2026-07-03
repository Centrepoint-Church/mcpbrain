"""Applies an AI adjudicator's verdicts on orphan-entity and missing-org
findings unattended on a daily cadence — a capped, conservative loop: only
'suppress'/'assign' mutate the graph (reversibly, via store.suppress_entity /
store.update_entity_org), any unrecognised verdict is a no-op 'skip' so
ambiguity never turns into an unattended mutation."""

import logging

from mcpbrain import orgs
from mcpbrain.resolve import _NAME_MERGEABLE_TYPES, _pick_winner, is_role_address

log = logging.getLogger(__name__)


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
        verdict_str = verdict.get("verdict")
        # M1: target the finding's OWN stored ref_id, not the verdict's — a
        # malformed verdict must not redirect an unattended mutation onto an
        # arbitrary entity or resolve an unrelated finding.
        f = store.get_finding(finding_id)
        if not f or f["resolved_at"] or f["finding_type"] != "lint:orphan_entity":
            result["skipped"] += 1
            continue
        ref_id = f["ref_id"]

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
        verdict_str = verdict.get("verdict")
        org = verdict.get("org")
        # M1: authoritative target from the finding, not the verdict.
        f = store.get_finding(finding_id)
        if not f or f["resolved_at"] or f["finding_type"] != "lint:missing_org":
            result["skipped"] += 1
            continue
        ref_id = f["ref_id"]

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
        verdict_str = verdict.get("verdict")
        owner = verdict.get("owner")
        # M1: authoritative target (action id) from the finding, not the verdict.
        f = store.get_finding(finding_id)
        if not f or f["resolved_at"] or f["finding_type"] != "lint:ownerless_action":
            result["skipped"] += 1
            continue
        ref_id = f["ref_id"]

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
      an entity id. This is a bulk, non-destructive TEXT FIELD correction —
      NOT an entity merge: store.rewrite_org_field(ref_id, canonical_org)
      relabels every entity currently tagged org=<ref_id> to org=
      <canonical_org> in one UPDATE, returning the row count. If the count is
      > 0, resolve the finding, count "canonicalized". If it's 0 (no entity
      currently carries that org value — a stale finding), the finding is
      left open, tallied under "missing".
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

    _ORG_KINDS = ("lint:ambiguous_org", "lint:duplicate_org", "org_unrecognised")
    for verdict in verdicts:
        finding_id = verdict.get("finding_id")
        verdict_str = verdict.get("verdict")
        canonical_org = verdict.get("canonical_org")
        # M1: route on the finding's OWN stored type + ref_id, not the verdict's —
        # so a verdict can't spoof the kind or redirect the target.
        f = store.get_finding(finding_id)
        if not f or f["resolved_at"] or f["finding_type"] not in _ORG_KINDS:
            result["skipped"] += 1
            continue
        finding_type = f["finding_type"]
        ref_id = f["ref_id"]

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
            updated = store.rewrite_org_field(ref_id, canonical_org)
            if updated > 0:
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


def apply_duplicate_verdicts(store, answers: list[dict], *, cap: int) -> dict:
    """Apply the LLM-adjudicated entity-merge answers from the merge_review
    spool block — the LIVE, unattended tier that folds a `same=true` pair
    into one entity via resolve._pick_winner + store.merge_entities. Ported
    from drain._apply_merge_answers with the two safety guards this
    codebase's other review-adjudication appliers (above) already have and
    this tier was missing:

    - Type guard: only entities of a _NAME_MERGEABLE_TYPES type (person/org/
      project) may be merged. _candidate_pairs only blocks on matching
      `type`, not on that allowlist, so a same-type pair of e.g. two
      `document` entities could in theory reach here; merging on generic
      titles/ids is exactly the structural-collapse failure mode
      _deterministic_merges was restricted to guard against (issue #23).
    - Role-address guard (the C1 fix, retrofitted to this tier): if either
      entity's email_addr is a shared/role mailbox (office@, info@, ...),
      the pair is never merged — a role inbox must not key an identity
      merge, since distinct people commonly share one.

    Each answer: {"pair_id": "a-id|b-id", "same": bool, "canonical": str}.
    pair_id is the two entity ids sorted and joined by '|' (see
    prepare._merge_pair). For same:true on a mergeable, non-role pair: look
    both up, pick the winner with resolve._pick_winner (winner, loser) and
    fold the loser in via merge_entities with method='llm'. Malformed
    pair_id, a missing entity, or `same` not strictly True are all skipped
    (unchanged from the ported behaviour) — this is the LLM tier of
    resolution, adjudicated in the spool session and applied here by the
    daemon: no second Claude call, no Gemini.

    Capped: once `cap` merges have been applied in this call, further
    same=true verdicts are left un-applied (entities untouched) so they're
    picked up again next cycle — this pipeline regenerates candidate pairs
    fresh every cycle, so no "already tried" state needs to persist.

    Returns {"merged": n, "guarded": n, "capped": n, "skipped": n}.
    "skipped" covers the pre-existing skip reasons (malformed pair_id,
    missing entity, `same` not strictly True). "guarded" covers the two
    NEW safety-guard rejections (non-mergeable type, role address).
    """
    result = {"merged": 0, "guarded": 0, "capped": 0, "skipped": 0}
    for ans in answers or []:
        # Strict bool: validate_batch_file already rejects non-bool `same`,
        # but require True here too so no truthy non-bool can ever drive a
        # merge.
        if ans.get("same") is not True:
            result["skipped"] += 1
            continue
        pair_id = ans.get("pair_id", "")
        ids = pair_id.split("|")
        if len(ids) != 2 or not all(ids):
            log.warning("review_apply: malformed merge pair_id %r, skipping", pair_id)
            result["skipped"] += 1
            continue
        if ids[0] == ids[1]:
            # M2: a self-pair ("x|x") is a no-op merge — skip, don't count it merged.
            result["skipped"] += 1
            continue
        a = store.get_entity(ids[0])
        b = store.get_entity(ids[1])
        if a is None or b is None:
            log.info("review_apply: merge pair %s has a missing entity, skipping", pair_id)
            result["skipped"] += 1
            continue

        if a["type"] not in _NAME_MERGEABLE_TYPES or b["type"] not in _NAME_MERGEABLE_TYPES:
            log.warning(
                "review_apply: merge pair %s has a non-mergeable type (%s, %s), guarding",
                pair_id, a["type"], b["type"])
            result["guarded"] += 1
            continue
        if is_role_address(a.get("email_addr", "")) or is_role_address(b.get("email_addr", "")):
            log.warning(
                "review_apply: merge pair %s has a role-address entity, guarding", pair_id)
            result["guarded"] += 1
            continue

        if result["merged"] >= cap:
            result["capped"] += 1
            continue

        winner, loser = _pick_winner(a, b)
        try:
            store.merge_entities(loser["id"], winner["id"],
                                 canonical_name=ans.get("canonical") or None,
                                 method="llm")
        except Exception as exc:
            log.error("review_apply: merge failed for %s <- %s: %s",
                      winner["id"], loser["id"], exc)
            continue
        result["merged"] += 1

    return result
