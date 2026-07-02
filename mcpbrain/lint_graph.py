"""Knowledge graph lint — daily structural integrity checks.

Port of src/lint_graph.py (Nexus) adapted for mcpbrain's store.

Connection pattern: each check receives an open sqlite3.Connection,
opened via ``with store._connect() as db:``.

DROPPED checks (tables/columns not present in mcpbrain):
- check_unsynthesised  — mcpbrain entities has no summary / summary_updated columns
- check_stale_summaries — same reason
- check_possible_duplicates (redundant — deletion scheduled Task A5)
- check_community_singletons (redundant — deletion scheduled Task A5)
- check_threads_without_summary (redundant — deletion scheduled Task A5)

KEPT checks (all tables exist from Phase 1 / Phase 3 Task 0):
- check_missing_org
- check_orphan_entities
- check_ambiguous_org
- check_ownerless_actions (adapted to unified actions table)
- check_duplicate_orgs
- check_unenriched_emails (email_context from Phase 1)
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from mcpbrain import config, orgs

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
    """Entities with no email appearances and no relationships.

    entity_suppressions is optional: it only exists once the AI-adjudicator
    suppress feature has run (or Store.init() has created it), so older/test
    stores may not have it. Join + filter on it only when present — otherwise
    the whole query would error rather than degrade gracefully like today.
    """
    has_supp = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='entity_suppressions'"
    ).fetchone() is not None
    supp_join = ("LEFT JOIN entity_suppressions s ON s.entity_id = e.id"
                 if has_supp else "")
    supp_filter = "AND s.entity_id IS NULL" if has_supp else ""

    rows = conn.execute(f"""
        SELECT e.id, e.name, e.type, e.org
        FROM entities e
        {supp_join}
        WHERE e.email_count = 0
          AND e.type != 'meeting'
          {supp_filter}
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
        email_domain = addr.split("@")[-1] if "@" in addr else addr
        for domain, mapped_org in known_domains.items():
            if email_domain == domain or email_domain.endswith("." + domain):
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


    unenriched = check_unenriched_emails(conn)
    if unenriched:
        r = unenriched[0]
        findings_count += 1
        lines.append(f"## Unenriched emails (no org tag) — {r['cnt']} emails\n")
        lines.append(f"- Range: {r['oldest']} to {r['newest']}\n")
    else:
        lines.append("## Unenriched emails — OK\n")

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
