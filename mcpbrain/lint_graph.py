"""Knowledge graph lint — daily structural integrity checks.

Port of src/lint_graph.py (Nexus) adapted for mcpbrain's store.

Connection pattern: each check receives an open sqlite3.Connection,
opened via ``with store._connect() as db:``.

DROPPED checks (tables/columns not present in mcpbrain):
- check_unsynthesised  — mcpbrain entities has no summary / summary_updated columns
- check_stale_summaries — same reason

KEPT checks (all tables exist from Phase 1 / Phase 3 Task 0):
- check_missing_org
- check_orphan_entities
- check_ambiguous_org
- check_duplicate_orgs
- check_possible_duplicates (entity_communities join works — Task 1 populates it)
- check_ownerless_actions (adapted to unified actions table)
- check_community_singletons (community_summaries from Task 0.1)
- check_threads_without_summary (thread_context from Task 0.2)
- check_unenriched_emails (email_context from Phase 1)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from nameparser import HumanName
from nameparser.config import CONSTANTS as _NP_CONSTANTS
from rapidfuzz import fuzz

from mcpbrain import config, orgs

for _t in ("ps", "pastor", "rev", "reverend", "bishop", "elder", "deacon"):
    _NP_CONSTANTS.titles.add(_t)

log = logging.getLogger(__name__)


# ── Individual checks ─────────────────────────────────────────────────────────

def check_missing_org(conn) -> list[dict]:
    """Entities with no org tag — violates org tagging rules."""
    rows = conn.execute("""
        SELECT id, name, type, email_count
        FROM entities
        WHERE (org IS NULL OR org = '')
          AND type != 'topic'
          AND email_count > 0
        ORDER BY email_count DESC
        LIMIT 50
    """).fetchall()
    return [dict(r) for r in rows]


def check_orphan_entities(conn) -> list[dict]:
    """Entities with no email appearances and no relationships."""
    rows = conn.execute("""
        SELECT e.id, e.name, e.type, e.org
        FROM entities e
        WHERE e.email_count = 0
          AND e.type != 'meeting'
          AND NOT EXISTS (
              SELECT 1 FROM entity_relations
              WHERE entity_a = e.id OR entity_b = e.id
          )
        ORDER BY e.name
        LIMIT 50
    """).fetchall()
    return [dict(r) for r in rows]


def check_ownerless_actions(conn) -> list[dict]:
    """Action items with no clear owner.

    Adapted from Nexus check_ownerless_actions (decisions + message_id join)
    to mcpbrain's unified actions table, joining email_context via thread_id.
    """
    rows = conn.execute("""
        SELECT a.id, a.text, a.org, a.deadline, ec.subject, ec.date_iso
        FROM actions a
        LEFT JOIN email_context ec ON ec.thread_id = a.thread_id
        WHERE a.source = 'email'
          AND (a.owner IS NULL OR a.owner = '' OR a.owner = 'unclear')
        ORDER BY ec.date_iso DESC
        LIMIT 30
    """).fetchall()
    return [dict(r) for r in rows]


def check_possible_duplicates(conn) -> list[dict]:
    """Find person entity pairs that may be the same person.

    Uses nameparser to block by first name, then rapidfuzz token_set_ratio
    to score similarity. Returns pairs scoring >= 75, capped at 50.
    The entity_communities join is included — Task 1 populates it.
    """
    rows = conn.execute("""
        SELECT e.id, e.name, e.type, e.org, e.email_addr, e.email_count,
               ec.community_id
        FROM entities e
        LEFT JOIN entity_communities ec ON ec.entity_id = e.id AND ec.level = 0
        WHERE e.type = 'person' AND e.email_count > 0
    """).fetchall()

    entities = []
    for r in rows:
        parsed = HumanName(r["name"] or "")
        first = (parsed.first or r["name"] or "").lower().strip()
        entities.append({
            "id": r["id"],
            "name": r["name"],
            "org": r["org"] or "",
            "email_addr": r["email_addr"] or "",
            "email_count": r["email_count"],
            "community": r["community_id"],
            "first": first,
            "parsed": parsed,
        })

    blocks: dict[str, list[dict]] = {}
    for e in entities:
        blocks.setdefault(e["first"], []).append(e)

    candidates = []
    for block in blocks.values():
        if len(block) < 2:
            continue
        for i, a in enumerate(block):
            for b in block[i + 1:]:
                name_a = a["name"] or ""
                name_b = b["name"] or ""
                score = fuzz.token_set_ratio(name_a.lower(), name_b.lower())

                has_last_a = bool(a["parsed"].last)
                has_last_b = bool(b["parsed"].last)
                domain_a = a["email_addr"].split("@")[-1] if "@" in a["email_addr"] else ""
                domain_b = b["email_addr"].split("@")[-1] if "@" in b["email_addr"] else ""

                # Gate 1: both have last names — last names must overlap
                if has_last_a and has_last_b:
                    la = a["parsed"].last.lower()
                    lb = b["parsed"].last.lower()
                    is_abbrev = (len(la) <= 3 and lb.startswith(la)) or (len(lb) <= 3 and la.startswith(lb))
                    last_score = fuzz.token_set_ratio(la, lb)
                    if last_score < 65 and not is_abbrev:
                        continue
                    if is_abbrev and last_score < 65:
                        score += 8

                # Gate 2: one is first-name only — require corroborating signal
                if has_last_a != has_last_b:
                    same_org = a["org"] and b["org"] and a["org"] == b["org"]
                    same_domain = domain_a and domain_b and domain_a == domain_b
                    same_community = (
                        a["community"] is not None
                        and a["community"] == b["community"]
                    )
                    if not (same_org or same_domain or same_community):
                        continue

                if a["org"] and b["org"]:
                    if a["org"] == b["org"]:
                        score += 10
                    else:
                        score -= 20

                if domain_a and domain_b and domain_a == domain_b:
                    score += 15

                reasons = []
                if score >= 95:
                    reasons.append("near-exact match")
                elif has_last_a != has_last_b:
                    reasons.append("first name only — same org/domain/community")
                else:
                    reasons.append(f"fuzzy {fuzz.token_set_ratio(name_a.lower(), name_b.lower())}%")
                if a["org"] and b["org"] and a["org"] == b["org"]:
                    reasons.append("same org")
                elif a["org"] and b["org"]:
                    reasons.append("different org")

                if score >= 75:
                    pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                    a_first = a if a["id"] == pair[0] else b
                    b_first = b if b["id"] == pair[1] else a
                    ambiguous = has_last_a != has_last_b
                    candidates.append({
                        "id_a": pair[0],
                        "name_a": a_first["name"],
                        "type_a": "person",
                        "count_a": a_first["email_count"],
                        "org_a": a_first["org"],
                        "id_b": pair[1],
                        "name_b": b_first["name"],
                        "type_b": "person",
                        "count_b": b_first["email_count"],
                        "org_b": b_first["org"],
                        "score": score,
                        "ambiguous_first_name": ambiguous,
                        "reason": ", ".join(reasons),
                    })

    # Additional pass: same email address = almost certain duplicate
    email_blocks: dict[str, list[dict]] = {}
    for e in entities:
        addr = e["email_addr"].lower().strip()
        if addr:
            email_blocks.setdefault(addr, []).append(e)

    seen_pairs = {(c["id_a"], c["id_b"]) for c in candidates}
    for addr, group in email_blocks.items():
        if len(group) < 2:
            continue
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                pair = (min(a["id"], b["id"]), max(a["id"], b["id"]))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                a_first = a if a["id"] == pair[0] else b
                b_first = b if b["id"] == pair[1] else a
                candidates.append({
                    "id_a": pair[0],
                    "name_a": a_first["name"],
                    "type_a": "person",
                    "count_a": a_first["email_count"],
                    "org_a": a_first["org"],
                    "id_b": pair[1],
                    "name_b": b_first["name"],
                    "type_b": "person",
                    "count_b": b_first["email_count"],
                    "org_b": b_first["org"],
                    "score": 100,
                    "ambiguous_first_name": False,
                    "reason": f"same email: {addr}",
                })

    best: dict[tuple, dict] = {}
    for c in candidates:
        key = (c["id_a"], c["id_b"])
        if key not in best or c["score"] > best[key]["score"]:
            best[key] = c

    return sorted(best.values(), key=lambda x: -x["score"])[:50]


def check_community_singletons(conn) -> list[dict]:
    """Communities with only one member — likely noise or needs merging."""
    rows = conn.execute("""
        SELECT cs.community_id, cs.level, cs.title, cs.member_count
        FROM community_summaries cs
        WHERE cs.member_count <= 1
        ORDER BY cs.level, cs.community_id
    """).fetchall()
    return [dict(r) for r in rows]


def check_ambiguous_org(conn) -> list[dict]:
    """Entities tagged 'external' whose email domain maps to a known org."""
    known_domains = dict(orgs.taxonomy_from_config().domain_map)
    results = []
    rows = conn.execute("""
        SELECT id, name, type, org, email_addr, email_count
        FROM entities
        WHERE org = 'external'
          AND email_addr != ''
          AND email_count >= 2
    """).fetchall()
    for row in rows:
        addr = (row["email_addr"] or "").lower()
        for domain, mapped_org in known_domains.items():
            if domain in addr:
                results.append({**dict(row), "should_be": mapped_org})
                break
    return results


def check_duplicate_orgs(conn) -> list[dict]:
    """Detect org field values that are likely variants of a canonical org."""
    CANONICAL = set(orgs.taxonomy_from_config().valid_orgs)

    rows = conn.execute("""
        SELECT org, COUNT(*) as cnt
        FROM entities
        WHERE org != ''
        GROUP BY org
        ORDER BY cnt DESC
    """).fetchall()

    results = []
    for row in rows:
        org_val = row["org"]
        cnt = row["cnt"]
        if org_val in CANONICAL:
            continue
        best_match = ""
        best_score = 0
        for canon in CANONICAL:
            if canon in ("external", "unknown"):
                continue
            score = fuzz.token_set_ratio(org_val.lower(), canon.lower())
            if score > best_score:
                best_score = score
                best_match = canon
        if best_score >= 60:
            results.append({
                "variant_org": org_val,
                "canonical_org": best_match,
                "score": best_score,
                "entity_count": cnt,
            })

    return sorted(results, key=lambda x: -x["entity_count"])


def check_threads_without_summary(conn) -> list[dict]:
    """High-volume threads with no summary."""
    rows = conn.execute("""
        SELECT thread_id, subject, org, email_count, last_updated
        FROM thread_context
        WHERE email_count >= 5
          AND (summary IS NULL OR summary = '')
        ORDER BY email_count DESC
        LIMIT 20
    """).fetchall()
    return [dict(r) for r in rows]


def check_unenriched_emails(conn) -> list[dict]:
    """Emails with no org tag — gaps in the enrichment pipeline."""
    rows = conn.execute("""
        SELECT COUNT(*) as cnt, MIN(date_iso) as oldest, MAX(date_iso) as newest
        FROM email_context
        WHERE (org IS NULL OR org = '')
          AND date_iso != ''
    """).fetchone()
    if rows and rows["cnt"] > 0:
        return [dict(rows)]
    return []


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats(conn) -> dict:
    """Build summary stats for the report header.

    Adapted from Nexus get_stats:
    - total_decisions replaced with total_actions (actions table)
    - entities_with_summary removed (no summary column in mcpbrain entities)
    """
    stats = {}
    stats["total_entities"] = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    stats["total_emails"] = conn.execute("SELECT COUNT(*) FROM email_context").fetchone()[0]
    stats["total_actions"] = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    stats["total_relations"] = conn.execute("SELECT COUNT(*) FROM entity_relations").fetchone()[0]
    stats["total_communities"] = conn.execute(
        "SELECT COUNT(*) FROM community_summaries WHERE level = 0"
    ).fetchone()[0]
    by_type = conn.execute(
        "SELECT type, COUNT(*) as cnt FROM entities GROUP BY type ORDER BY cnt DESC"
    ).fetchall()
    stats["by_type"] = {r["type"]: r["cnt"] for r in by_type}
    by_org = conn.execute(
        "SELECT org, COUNT(*) as cnt FROM entities WHERE org != '' GROUP BY org ORDER BY cnt DESC"
    ).fetchall()
    stats["by_org"] = {r["org"]: r["cnt"] for r in by_org}
    return stats


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(conn) -> str:
    """Build the full markdown lint report.

    Adapted from Nexus build_report with:
    - Dropped unsynthesised/stale_summaries sections (no summary column)
    - total_decisions -> total_actions
    - entities_with_summary line removed
    """
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"# Knowledge Graph Lint — {today}\n"]

    stats = get_stats(conn)
    lines.append("## Stats\n")
    lines.append(f"- Entities: {stats['total_entities']}")
    lines.append(f"- Emails indexed: {stats['total_emails']}")
    lines.append(f"- Actions: {stats['total_actions']}")
    lines.append(f"- Relationships: {stats['total_relations']}")
    lines.append(f"- Communities: {stats['total_communities']}")
    if stats["by_type"]:
        type_str = ", ".join(f"{t}: {c}" for t, c in stats["by_type"].items())
        lines.append(f"- By type: {type_str}")
    if stats["by_org"]:
        org_str = ", ".join(f"{o}: {c}" for o, c in stats["by_org"].items())
        lines.append(f"- By org: {org_str}")
    lines.append("")

    findings_count = 0

    def section(title, rows, formatter):
        nonlocal findings_count
        if not rows:
            lines.append(f"## {title} — OK\n")
            return
        findings_count += len(rows)
        lines.append(f"## {title} — {len(rows)} issues\n")
        lines.extend(formatter(rows))
        lines.append("")

    section(
        "Missing org tag",
        check_missing_org(conn),
        lambda rows: [
            f"- `{r['id']}` ({r['type']}, {r['email_count']} emails)"
            for r in rows
        ],
    )

    section(
        "Ambiguous org (tagged external, domain suggests known org)",
        check_ambiguous_org(conn),
        lambda rows: [
            f"- `{r['id']}` — {r['email_addr']} — currently `{r['org']}`, should be `{r['should_be']}`"
            for r in rows
        ],
    )

    section(
        "Orphan entities (no emails, no relationships)",
        check_orphan_entities(conn),
        lambda rows: [
            f"- `{r['id']}` ({r['type']}, {r['org']})"
            for r in rows
        ],
    )

    section(
        "Ownerless actions",
        check_ownerless_actions(conn),
        lambda rows: [
            f"- [{r['date_iso']}] {r['org']} — {r['text'][:100]}{'...' if len(r['text'] or '') > 100 else ''}"
            for r in rows
        ],
    )

    section(
        "Community singletons",
        check_community_singletons(conn),
        lambda rows: [
            f"- Community {r['community_id']} (level {r['level']}): {r['title'] or 'untitled'}"
            for r in rows
        ],
    )

    section(
        "Threads without summary (>=5 emails)",
        check_threads_without_summary(conn),
        lambda rows: [
            f"- [{r['org']}] {r['subject']} ({r['email_count']} emails)"
            for r in rows
        ],
    )

    unenriched = check_unenriched_emails(conn)
    if unenriched:
        r = unenriched[0]
        findings_count += 1
        lines.append(f"## Unenriched emails (no org tag) — {r['cnt']} emails\n")
        lines.append(f"- Range: {r['oldest']} to {r['newest']}\n")
    else:
        lines.append("## Unenriched emails — OK\n")

    section(
        "Possible duplicate entities",
        check_possible_duplicates(conn),
        lambda rows: [
            f"- `{r['id_a']}` ({r['name_a']}, {r['org_a']}) vs "
            f"`{r['id_b']}` ({r['name_b']}, {r['org_b']}) — score {r['score']}: {r['reason']}"
            for r in rows
        ],
    )

    section(
        "Duplicate org variants",
        check_duplicate_orgs(conn),
        lambda rows: [
            f"- `{r['variant_org']}` ({r['entity_count']} entities) — "
            f"likely `{r['canonical_org']}` (score {r['score']})"
            for r in rows
        ],
    )

    lines.insert(
        1,
        f"\n**{findings_count} total findings.**\n"
        if findings_count
        else "\n**No findings. Graph looks clean.**\n",
    )

    return "\n".join(lines)


# ── findings sink ─────────────────────────────────────────────────────────────

def run(store, *, now: str, log_dir=None) -> dict:
    """Run all lint checks, write a markdown report, and record proactive findings.

    Writes one proactive_findings row per finding with
    finding_type='lint:<check_name>', ref_id=<entity id or str(row key)>,
    severity='info'. Resolves prior lint findings no longer present via
    store.resolve_findings_not_in().

    Returns {"findings": int, "report_path": str}.
    """
    with store._connect() as db:
        report = build_report(db)

    if log_dir is None:
        log_dir = config.app_dir() / "logs"
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    date_str = now[:10]  # YYYY-MM-DD
    report_path = Path(log_dir) / f"lint_{date_str}.md"
    report_path.write_text(report)

    findings_count = 0
    live_lint_ref_ids: dict[str, list[str]] = {}  # check_name -> [ref_id, ...]

    # Checks that produce entity-level rows (id field exists)
    entity_checks = [
        ("missing_org", check_missing_org),
        ("orphan_entity", check_orphan_entities),
        ("ambiguous_org", check_ambiguous_org),
        ("ownerless_action", check_ownerless_actions),
    ]

    with store._connect() as db:
        for check_name, check_fn in entity_checks:
            rows = check_fn(db)
            live_lint_ref_ids[check_name] = []
            for r in rows:
                ref_id = str(r.get("id", ""))
                if not ref_id:
                    continue
                finding_type = f"lint:{check_name}"
                name_or_text = r.get("name") or r.get("text", "")
                store.record_finding(
                    finding_type,
                    ref_id,
                    org=r.get("org", "") or "",
                    summary=f"{check_name}: {(name_or_text or '')[:80]}",
                    detail=str(r),
                    severity="info",
                    detected_at=now,
                )
                live_lint_ref_ids[check_name].append(ref_id)
                findings_count += 1

    # Resolve stale findings for each entity-level check
    for check_name in [cn for cn, _ in entity_checks]:
        finding_type = f"lint:{check_name}"
        store.resolve_findings_not_in(finding_type, live_lint_ref_ids[check_name], now)

    # check_possible_duplicates
    with store._connect() as db:
        rows = check_possible_duplicates(db)
    live_refs: list[str] = []
    for r in rows:
        ref_id = f"{r['id_a']}|{r['id_b']}"
        store.record_finding(
            "lint:possible_duplicate",
            ref_id,
            org="",
            summary=f"Possible duplicate: {r['name_a']} / {r['name_b']}",
            detail=str(r),
            severity="info",
            detected_at=now,
        )
        live_refs.append(ref_id)
        findings_count += 1
    store.resolve_findings_not_in("lint:possible_duplicate", live_refs, now)

    # check_duplicate_orgs
    with store._connect() as db:
        rows = check_duplicate_orgs(db)
    live_refs = []
    for r in rows:
        ref_id = r["variant_org"]
        store.record_finding(
            "lint:duplicate_org",
            ref_id,
            org="",
            summary=f"Org variant '{r['variant_org']}' → canonical '{r['canonical_org']}'",
            detail=str(r),
            severity="info",
            detected_at=now,
        )
        live_refs.append(ref_id)
        findings_count += 1
    store.resolve_findings_not_in("lint:duplicate_org", live_refs, now)

    # check_community_singletons
    with store._connect() as db:
        rows = check_community_singletons(db)
    live_refs = []
    for r in rows:
        ref_id = str(r["community_id"])
        store.record_finding(
            "lint:community_singleton",
            ref_id,
            org="",
            summary=f"Singleton community {r['community_id']}",
            detail=str(r),
            severity="info",
            detected_at=now,
        )
        live_refs.append(ref_id)
        findings_count += 1
    store.resolve_findings_not_in("lint:community_singleton", live_refs, now)

    # check_threads_without_summary
    with store._connect() as db:
        rows = check_threads_without_summary(db)
    live_refs = []
    for r in rows:
        ref_id = r["thread_id"]
        store.record_finding(
            "lint:thread_no_summary",
            ref_id,
            org=r.get("org", "") or "",
            summary=f"Thread {r['thread_id'][:40]} has no summary ({r['email_count']} emails)",
            detail=str(r),
            severity="info",
            detected_at=now,
        )
        live_refs.append(ref_id)
        findings_count += 1
    store.resolve_findings_not_in("lint:thread_no_summary", live_refs, now)

    # check_unenriched_emails
    with store._connect() as db:
        rows = check_unenriched_emails(db)
    live_refs = []
    for r in rows:
        ref_id = "unenriched_batch"
        store.record_finding(
            "lint:unenriched_emails",
            ref_id,
            org="",
            summary=f"{r['cnt']} unenriched emails ({r['oldest']} to {r['newest']})",
            detail=str(r),
            severity="info",
            detected_at=now,
        )
        live_refs.append(ref_id)
        findings_count += 1
    store.resolve_findings_not_in("lint:unenriched_emails", live_refs, now)

    log.info("lint complete: %d findings, report at %s", findings_count, report_path)
    return {"findings": findings_count, "report_path": str(report_path)}
